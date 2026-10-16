# Sandbox V1（Windows）

Evolyn 的 Shell 与 MCP 子进程默认在沙箱中运行。本页说明 Windows V1 沙箱
实际强制了什么、没有强制什么，以及在什么情况下使用逃生开关。


## 设计取向

V1 采取务实路线：用操作系统现成能力做到「**进程树托管 + 写入保护**」，
其余维度**如实标注为未强制**，并通过工具结果与日志透传，不假装完整隔离。

| 维度 | V1 状态 |
| --- | --- |
| 进程树托管（Job Object） | 强制 |
| 写入保护（低完整性令牌） | 强制（`EVOLYN_SANDBOX_LOW_IL=0` 时关闭） |
| 读隔离 | 不强制 |
| 网络访问 | 不强制 |

未强制项以 `enforcement` 字段随启动规格与 Shell 工具结果（`sandbox.enforcement`）
透传；MCP 子进程启动时会在日志中 `warning` 记录同样的清单。

## 隔离机制

### 1. 低完整性令牌（写入保护）

沙箱启动器（`app/sandbox/launcher.py`，仅标准库）复制当前进程令牌并降为
低完整性（Low IL，`S-1-16-4096`），再以受限令牌创建目标进程：

- 低完整性进程**无法创建/修改/删除未标注为 Low 的对象**——包括用户目录里的
  个人文件、系统目录，以及工作区内被显式保护的文件；
- 普通用户权限即可完成令牌降级；降级失败会 **fail closed**：拒绝执行并输出
  结构化错误，绝不静默回退到宿主机执行。

写入保护依赖完整性标签，标签由 `icacls /setintegritylevel` 在启动前幂等标注：

- 可写根（`workspace`、额外的 `writable_roots`）标注为 Low；
- npm / uv 等运行时缓存目录按需标注，保证 `npx`、`uvx` 类工具能正常工作；
- 沙箱临时目录 `.evolyn/tmp/sandbox` 标注为 Low，并作为 `TEMP`/`TMP`
  注入子进程（低完整性进程无法写普通用户 TEMP）。

受保护路径强制恢复为 Medium，低完整性进程不可写：

- `workspace/.git`
- `workspace/.evolyn`
- `workspace/.env` 与 `backend/.env`

### 2. Job Object 进程树托管

目标进程在挂起状态下创建，先 `AssignProcessToJobObject`、再恢复执行，
消除「先启动后加入」的竞态。Job Object 使用 `KILL_ON_JOB_CLOSE`：

- 调用方（Evolyn 进程）退出、崩溃或被取消时，整棵子进程树随之终止；
- Shell 工具的超时/取消走同一路径，天然整树连杀。

### 3. 环境变量白名单

沙箱子进程只继承最小环境：`PATH` / `PATHEXT` / `SYSTEMROOT` / `WINDIR` /
`COMSPEC` / `USERPROFILE` / `TEMP` / `TMP` / `LOCALAPPDATA` / `APPDATA` /
`NUMBER_OF_PROCESSORS`。API Key 等敏感变量不会泄漏进沙箱进程。

### 4. 文件系统模式

| 模式 | 行为 |
| --- | --- |
| `workspace_write`（默认） | workspace 可写；其余位置低完整性天然只读 |
| `read_only` | 不标注任何可写根 |
| `none` | 同上，仅进程树托管 |
| `host` | 完全关闭沙箱，直接在宿主机执行（逃生口，日志会标注危险模式） |

`.cmd` / `.bat` 目标（例如 `npx.CMD`）会被包装为 `cmd.exe /d /s /c` 后再进入
沙箱，因为 `CreateProcess` 无法直接启动脚本包装器。

## 已知限制

- **无读隔离**：低完整性令牌只限制写入，不阻止读取；沙箱进程原则上仍能读到
  用户可读的文件；
- **无网络强制**：低完整性不限制 socket，域名级网络策略尚未实现；
- **旧文件限制**：写入保护只对「标注之后」创建的文件生效。workspace 中在启用
  沙箱之前就已存在、且未标 Low 的文件，低完整性进程同样无法写入；需要时对
  workspace 重跑一次标注即可（`icacls` 标注是幂等的）；
- **MCP 网络策略**：扩展配置中的网络/域名开关不会在 Windows V1 被强制，
  仅作为策略记录。

## 逃生开关

1. `EVOLYN_SANDBOX_LOW_IL=0`：跳过低完整性令牌，仅保留 Job Object 进程树托管。
   写入保护随之失效，`enforcement` 会追加 `write_protection`；
2. MCP 扩展配置 `filesystem: "host"`：该 Server 完全关闭沙箱，直接在宿主机执行，
   工具结果与日志会标注为非沙箱运行。
