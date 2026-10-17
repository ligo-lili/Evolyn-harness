"""阶段 0 Live 评测：真实模型 A/B 跑批与 Go/No-go 报告。

用法（在 backend 目录）：

    .venv/bin/python -m tests.eval.subagent.run_live --arm both
    .venv/bin/python -m tests.eval.subagent.run_live --arm both \\
        --task T01-locate-validate-session --task T05-login-call-chain
    .venv/bin/python -m tests.eval.subagent.run_live --arm both --category security
    .venv/bin/python -m tests.eval.subagent.run_live --arm both --suite large
    .venv/bin/python -m tests.eval.subagent.run_live --arm both --runs 3 \\
        --provider deepseek --model deepseek-v4-flash \\
        --price-input 0.27 --price-output 1.10

说明：

- 预登记双套件：``--suite core``（20 个基础探索任务，对照面）与
  ``--suite large``（9 个大范围盘点/负向证明/跨模块链任务，委派机制的
  工作区间）分别跑批、分别归档；评分口径与门槛数字不随结果调整。
- A 臂 = 主 Agent 直接探索（基线）；B 臂 = 现行 V1 委派（``subagent_run``，
  deferred 注册，与生产装配一致）。
- ``--runs N``：每条任务每臂运行 N 次，报告按任务折叠（波动大的题集建议 3）。
- 未提供 ``--price-input/--price-output`` 时，成本门槛使用 chargeable token
  口径，报告会显式标注。
- 退出码：0=Go（或 ``--allow-no-go``），1=No-go，2=无匹配任务。
- 真实模型跑批会产生 API 费用；先确认预算再执行。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from app.models.config import ModelSettings
from app.models.registry import ModelAdapterRegistry

from .harness import DELEGATE_ARM, DIRECT_ARM, HarnessConfig, run_task
from .metrics import Prices, RunMetrics, build_run_metrics
from .reporting import (
    GateThresholds,
    compare_arms,
    comparison_to_json,
    render_markdown,
)
from .tasks import SUITES, select_tasks, validate_task_set

_DEFAULT_REPORTS_DIR = Path(__file__).resolve().parent / "reports"

_ARM_CHOICES: dict[str, tuple[str, ...]] = {
    "both": (DIRECT_ARM, DELEGATE_ARM),
    "a": (DIRECT_ARM,),
    "b": (DELEGATE_ARM,),
}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="只读探索子 Agent 阶段 0：A/B Live 评测。",
    )
    parser.add_argument(
        "--arm",
        choices=sorted(_ARM_CHOICES),
        default="both",
        help="运行哪组臂：both（默认 A+B）/ a（仅基线）/ b（仅委派）。",
    )
    parser.add_argument(
        "--task",
        action="append",
        help="只跑指定任务 ID（可多次）。",
    )
    parser.add_argument(
        "--category",
        action="append",
        help="只跑指定分类（可多次）：function_locate / call_chain / … / security。",
    )
    parser.add_argument(
        "--suite",
        action="append",
        choices=SUITES,
        help=(
            "只跑指定套件（可多次）：core=20 个基础探索任务；"
            "large=9 个大范围盘点/负向证明/跨模块链任务。"
        ),
    )
    parser.add_argument(
        "--runs",
        type=int,
        default=1,
        help="每条任务每臂运行次数（默认 1；波动大的题集建议 3）。",
    )
    parser.add_argument("--provider", help="模型提供商（默认使用 .env 默认）。")
    parser.add_argument("--model", help="模型名（默认使用提供商配置）。")
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="运行目录（默认系统临时目录；用于保留现场复现）。",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help=(
            "报告输出路径（默认 tests/eval/subagent/reports/"
            "report_subagent_<时间戳>.md；同时写同名 .json）。"
        ),
    )
    parser.add_argument(
        "--print",
        action="store_true",
        help="同时把报告打印到终端。",
    )
    parser.add_argument(
        "--allow-no-go",
        action="store_true",
        help="No-go 时仍返回退出码 0（探索性运行）。",
    )
    parser.add_argument(
        "--price-input",
        type=float,
        default=None,
        help="输入单价（每百万 token，配合 --price-output 使用）。",
    )
    parser.add_argument(
        "--price-output",
        type=float,
        default=None,
        help="输出单价（每百万 token，配合 --price-input 使用）。",
    )
    parser.add_argument("--currency", default="USD", help="成本货币（默认 USD）。")
    parser.add_argument(
        "--max-steps",
        type=int,
        default=40,
        help="两臂统一的主 Agent 步数上限（默认 40）。",
    )
    parser.add_argument(
        "--max-tool-rounds",
        type=int,
        default=35,
        help="两臂统一的主 Agent 工具轮上限（默认 35；小于步数上限，"
        "保证撞顶前收口）。",
    )
    parser.add_argument(
        "--max-context-reduction",
        type=float,
        default=0.5,
        help="上下文峰值最小降幅阈值（默认 0.5，即 50%%）。",
    )
    parser.add_argument(
        "--max-correctness-drop",
        type=float,
        default=0.05,
        help="正确率允许下降幅度（默认 0.05，即 5pp）。",
    )
    parser.add_argument(
        "--max-cost-ratio",
        type=float,
        default=1.3,
        help="总成本最大比值（默认 1.3）。",
    )
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("--runs must be at least 1")
    if (args.price_input is None) != (args.price_output is None):
        parser.error("--price-input and --price-output must be provided together")
    return args


async def main(args: argparse.Namespace) -> int:
    validate_task_set()
    tasks = select_tasks(
        task_ids=tuple(args.task or ()),
        categories=tuple(args.category or ()),
        suites=tuple(args.suite or ()),
    )
    if not tasks:
        print("没有匹配的任务。", file=sys.stderr)
        return 2

    settings = ModelSettings()
    resolved_provider = args.provider or settings.model_default_provider.value
    try:
        resolved_model = (
            args.model or settings.provider_config(resolved_provider).model
        )
    except ValueError:
        resolved_model = args.model or "未知"

    prices = (
        Prices(
            input_per_million=args.price_input,
            output_per_million=args.price_output,
            currency=args.currency,
        )
        if args.price_input is not None and args.price_output is not None
        else None
    )
    thresholds = GateThresholds(
        min_context_reduction=args.max_context_reduction,
        max_correctness_drop=args.max_correctness_drop,
        max_cost_ratio=args.max_cost_ratio,
    )

    invocation_id = datetime.now(UTC).strftime("%Y%m%d_%H%M%S_%f")
    root = (
        args.root / f"eval-subagent-{invocation_id}"
        if args.root is not None
        else Path(tempfile.mkdtemp(prefix="evolyn-subagent-eval-"))
    )
    root.mkdir(parents=True, exist_ok=True)

    arms: tuple[str, ...] = _ARM_CHOICES[args.arm]

    registry = ModelAdapterRegistry(settings)
    config = HarnessConfig(
        provider=resolved_provider,
        model=resolved_model,
        max_steps=args.max_steps,
        max_tool_rounds=args.max_tool_rounds,
    )

    print(
        f"阶段 0 A/B 评测：{len(tasks)} 个任务 × {args.runs} 次 × "
        f"{len(arms)} 臂（provider={resolved_provider} model={resolved_model}）"
    )
    runs_by_arm: dict[str, list[RunMetrics]] = {arm: [] for arm in arms}
    for arm in arms:
        for task in tasks:
            for run_index in range(1, args.runs + 1):
                run_root = root / arm / task.id / f"run-{run_index}"
                outcome = await run_task(
                    task,
                    arm=arm,
                    root=run_root,
                    config=config,
                    registry=registry,
                )
                metrics = build_run_metrics(
                    task,
                    arm,
                    result=outcome.result,
                    events=outcome.events,
                    duration_s=outcome.duration_s,
                    error=outcome.error,
                    run_index=run_index,
                )
                runs_by_arm[arm].append(metrics)
                marker = "✅" if metrics.answer.passed else "❌"
                peak = (
                    f"{metrics.main_peak_prompt_tokens:,}"
                    if metrics.main_peak_prompt_tokens is not None
                    else "n/a"
                )
                print(
                    f"  [{arm}] {task.id} run#{run_index} {marker} "
                    f"peak={peak} "
                    f"facts={metrics.answer.key_fact_matched}/"
                    f"{metrics.answer.key_fact_total} "
                    f"tokens={metrics.all_chargeable_tokens:,} "
                    f"{metrics.duration_s:.1f}s"
                )

    generated_at = datetime.now(UTC).isoformat(timespec="seconds")
    out_path = args.out or (
        _DEFAULT_REPORTS_DIR
        / f"report_subagent_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}.md"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)

    meta: dict[str, object] = {
        "generated_at": generated_at,
        "provider": resolved_provider,
        "model": resolved_model,
        "task_count": len(tasks),
        "runs_per_task": args.runs,
        "arms": list(arms),
        "run_root": str(root),
        "command": " ".join(sys.argv),
    }

    if len(arms) == 1:
        json_path = out_path.with_suffix(".json")
        payload = {
            "meta": meta,
            "arm": arms[0],
            "runs": [run.as_dict() for run in runs_by_arm[arms[0]]],
        }
        json_path.write_text(
            _dumps(payload),
            encoding="utf-8",
        )
        print("\n单臂模式：跳过 Go/No-go 联合判定。")
        print(f"结果已保存：{json_path}")
        return 0

    comparison = compare_arms(
        tuple(runs_by_arm[arms[0]]),
        tuple(runs_by_arm[arms[1]]),
        thresholds=thresholds,
        prices=prices,
    )
    report = render_markdown(comparison, meta)
    out_path.write_text(report, encoding="utf-8")
    json_path = out_path.with_suffix(".json")
    json_path.write_text(
        _dumps(comparison_to_json(comparison, meta)),
        encoding="utf-8",
    )

    print(f"\n判定：{comparison.verdict.verdict}")
    for check in comparison.verdict.checks:
        marker = "✅" if check.passed else "❌"
        print(f"  {marker} {check.name}：{check.detail}")
    print(f"\n报告已保存：{out_path}")
    print(f"原始数据：{json_path}")
    if args.print:
        print("\n" + report)
    if not comparison.verdict.passed and not args.allow_no_go:
        return 1
    return 0


def _dumps(payload: object) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    sys.exit(asyncio.run(main(_parse_args())))
