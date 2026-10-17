"""只读探索子 Agent 的 A/B 评测（v3 阶段 0）。

结构：

- ``tasks.py``      29 个真实代码探索任务 + 标准答案（core 20 + large 9）
- ``fixtures.py``   夹具仓库（shop-auth-sandbox）确定性生成（107 文件 / 10 万字符）
- ``harness.py``    A（主 Agent 直接探索）/ B（委派子 Agent）两臂装配与运行
- ``metrics.py``    五维指标采集（主上下文峰值 / 累计消耗 / 回传大小 / 成本 / 正确率）
- ``reporting.py``  Go/No-go 联合判定与报告渲染
- ``run_live.py``   显式真实模型跑批入口（有 API 成本）
- ``tests/``        离线自检（不调用真实模型）

用法见 ``README.md``。
"""
