<div align="center">

# Evolyn

**Build agents that remember, continue, and learn.**

一个面向长期工作、在本地持续运行的 AI Agent Harness（Windows）。

[核心能力](#what-evolyn-can-do) · [架构](#how-it-fits-together) · [快速开始](#quick-start) · [开发检查](#development-checks)

</div>

Evolyn 是一个面向长期工作的本地 AI Agent Harness。它不只完成当前对话，还会管理
长上下文、跟踪复杂任务、恢复中断 Run、使用本地与 MCP 工具，并从真实完成的工作中
逐步形成可复用的记忆与 Skill。

当前项目由 Python Harness 与 CLI 组成，可选运行无头 Host（WS /rpc）；模型层通过
统一 Adapter 接入 OpenAI、Qwen、DeepSeek 与 Anthropic。Computer Runtime 为纯 Python
的 Windows 实现（UI Automation + SendInput + Pillow 截屏）。

> 当前阶段为本地开发版本，接口、数据格式和交互仍可能调整。

## What Evolyn Can Do

- **Multi-Provider Models** — 统一适配 OpenAI、Qwen、DeepSeek 和 Anthropic API。
- **Tool System** — 本地文件、Shell、网页搜索、时间等工具共享注册、超时、权限和审计边界；Shell 默认在 workspace 沙箱中执行。
- **MCP Extensions** — 通过 `backend\.evolyn\mcp.json` 接入外部 stdio MCP Server；第三方进程使用白名单环境，并默认在沙箱内以工作区可写模式运行。
- **Memory** — Core Memory 常驻，Ordinary Memory 每 Run 自动混合召回（FTS5 + 向量 + RRF）候选，模型按需读取正文，并在 Run 后反思更新；Markdown 是唯一权威存储，搜索索引可重建。
- **Task / Plan Mode** — 一个整体目标对应一个 Task，使用 Steps 跟踪复杂工作的真实进度。
- **Skill & Skill Learning** — 按需激活 Skill，并从多个 Completed Task 的 Trace 中提炼候选经验。
- **Context Management** — 每轮整理工具结果，超过预算后滚动摘要，同时保留当前目标和关键状态。
- **Run / Recovery** — 持久化 Run 生命周期，通过 Checkpoint 从中断边界创建恢复 Run。
- **Trace & Usage** — 记录模型、工具、审批、压缩和 Post-Run 事件，并拆分缓存与可计费用量。
- **Automation** — 支持 once、interval 和 cron，将定时输入送入正常 Conversation/Run 链路；触发人工审批时在终端等待应答。
- **Approval** — 高风险工具在 CLI 中弹出终端审批，可继续或拒绝执行；非交互场景可通过权限规则预置放行。
- **Computer Runtime (Windows)** — 纯 Python 提供结构化观察、目标验证和受控桌面操作；V1 仅主屏截屏。
- **Artifacts** — 将 Run 生成的文件或链接作为可追踪交付物发布。

## How It Fits Together

```text
CLI / Automation
       ↓
ConversationService
       ↓
   RunManager
       ↓
  AgentRuntime
   ↙       ↘
Context          ToolRegistry → Permission → Executor → Hooks
  ↓                                      ↓
Model Adapter                     Local / MCP / Computer
                                      ↓
                          Sandbox Supervisor (Shell / MCP)
       ↓
Trace · Checkpoint · Usage · Artifact

Post-Run
  ├─ Memory Reflection / Maintenance
  └─ Task-backed Skill Learning
```

CLI 直接复用与 Host 相同的业务栈：

```text
python -m app
  ↓
ConversationService / RunManager / AgentRuntime
```

可选的无头 Host：

```text
WS /rpc client
  ↓
WS /rpc (JSON-RPC)
  ↓
Evolyn Host (python -m app.server)
```

`GET /health`、Computer screenshot 和 Artifact content 只承担本地 transport；正常业务
通过 `WS /rpc` 完成。Host 默认只接受 loopback 客户端。

## Core Concepts

| 概念 | 职责 |
| --- | --- |
| Conversation | 保存用户与 Agent 的完整原始消息历史 |
| Context | 为当前模型请求整理预算、工具结果和滚动摘要 |
| Task | 记录当前长期目标、Steps 和进度 |
| Memory | 保存跨会话仍值得知道的事实、偏好和决定 |
| Skill | 保存以后处理同类任务时可复用的方法 |
| Run | 表示一次 Agent 执行的完整生命周期 |
| Trace | 记录一次 Run 实际发生了什么 |
| Checkpoint | 保存中断后的可恢复边界 |
| Artifact | 表示一次 Run 可交付的文件或链接 |
| Automation | 描述未来何时向 Conversation 投递新输入 |

## Quick Start

### Requirements

- Windows 10/11
- Python 3.12+

### 1. Clone and install

python -m venv backend\.venv
backend\.venv\Scripts\python -m pip install -r backend\requirements.txt
copy backend\.env.example backend\.env
```

首次使用可以通过 CLI 设置主模型。API Key 优先保存到 Windows 凭据管理器（目标
`Evolyn/ModelApiKey/<provider>`），模型名称与 API 地址等非敏感配置保存在本地
`.evolyn` 目录；也可以继续使用 `backend\.env` 配置。真实 API Key 不要提交到 Git。

### 2. Start with CLI

```powershell
cd backend
.venv\Scripts\python -m app --setup
.venv\Scripts\python -m app
```

一次性消息与恢复会话：

```powershell
.venv\Scripts\python -m app --message "总结 workspace 里的文件"
.venv\Scripts\python -m app --conversation <会话 ID 前缀>
```

CLI 可用于快速验证模型、工具、会话恢复、Memory、MCP、Run 和 Trace。

### 3. Optional: run the Host

终端中启动无头 Host（默认仅监听 loopback）：

```powershell
cd backend
.venv\Scripts\python -m app.server
```

## Extensions

### MCP

在 `backend\.evolyn\mcp.json` 中配置外部 stdio MCP Server（Host 也提供 `mcp.*` RPC 供程序化
管理）。MCP 工具注册后仍会经过 Evolyn 的权限、执行、Hook 和 Trace 链路；MCP 子进程
默认只能读写 workspace，Host 环境变量使用白名单传递。可在扩展配置中关闭网络或收紧
文件权限，显式选择宿主机执行会显示为危险模式。

沙箱在 Windows 上提供 Job Object 进程树托管 + 低完整性令牌写保护（部分隔离）：读隔离
与网络拒绝无法强制，会如实标注「未强制」；平台无法落实请求策略时会拒绝启动对应进程，
不会静默回退到宿主机。逃生开关与完整边界见 [Sandbox V1](docs/sandbox.md)。

### Skills

Skill 可以由用户导入，也可以由 Skill Learning 从多个已完成 Task 的真实 Trace 中生成
Candidate。学习产生的 Candidate 不会绕过人工确认直接成为正式 Skill。

## Development Checks

Backend（PowerShell）：

```powershell
cd backend
.venv\Scripts\python -m pip install -r requirements.txt -r requirements-dev.txt
.venv\Scripts\python -m pytest
.venv\Scripts\python -m ruff check .
.venv\Scripts\python -m compileall -q app tests
```

GitHub Actions 在 `windows-latest` 上运行同一套 Backend 基线（`PYTHONUTF8=1`）。

## Repository Layout

```text
evolyn/
├── backend/                         Python Harness、CLI 与测试
│   ├── app/
│   │   ├── agent/                   AgentRuntime 与事件
│   │   ├── context/                 上下文构建与压缩
│   │   ├── tools/                   本地工具、权限、Executor 与 Hooks
│   │   ├── sandbox/                 Windows 沙箱（低完整性 + Job Object）
│   │   ├── memory/                  Core / Ordinary Memory
│   │   ├── task/                    Task 与 Plan Mode
│   │   ├── skills/                  Skill Store 与运行时激活
│   │   ├── skill_learning/          Trace-backed Skill Learning
│   │   ├── run/                     Run 生命周期与 Recovery
│   │   ├── checkpoint/              恢复边界
│   │   ├── automation/              Automation 领域模型
│   │   ├── scheduler/               定时调度
│   │   ├── computer/                Windows Computer Runtime（UI Automation）
│   │   ├── mcp/                     MCP Client
│   │   └── server/                  可选 Host 与 WS /rpc
│   ├── scripts/                     本地维护与演示脚本
│   └── tests/                       离线测试、E2E 与 Eval
├── docs/                            Sandbox 边界说明
└── workspace/                       Agent 被允许操作的本地工作区（运行时创建）
```

## Current Boundaries

- Evolyn 目前以本地单用户环境为目标，不是公网多租户服务；
- 仅支持 Windows 10/11 桌面会话；Computer Runtime V1 仅主屏截屏，无法操作以管理员权限运行的应用，`open_app` 不支持 UWP 应用；
- 沙箱提供部分隔离：进程树托管与写入保护已强制，读隔离与网络访问不强制，详见 [Sandbox V1](docs/sandbox.md)；
- MCP 当前主要接入 stdio Server；
- Skill Learning 生成 Candidate，不自动绕过人工确认；
- CLI 下 Automation 触发人工审批时会在终端等待应答，非交互场景需要预置权限规则；
