# Antigravity Task Message Bus

基于 **Antigravity 生命周期钩子（`hooks.json`）** 与 **零外部依赖本地 MCP Server** 的跨独立任务主动消息投递插件。

让完全独立的 Antigravity 会话（跨窗口、跨工作区、跨 Antigravity App 与 `agy` CLI）之间能够像团队成员一样主动发消息、订阅频道、全员广播，并在目标任务空闲时由发送方主动唤醒。

## 核心特性

- **零轮询自动收信（Lifecycle Hooks）**：
  - `SessionStart` / `PreInvocation`：模型调用前自动拉取未读消息并通过 `injectSteps` 注入上下文。
  - `PostInvocation`：工具调用完成后若有新消息到达，自动注入并返回 `terminationBehavior: "force_continue"`。
  - `Stop`：任务准备结束瞬间若恰好收到新消息，返回 `decision: "continue"` 拦截停止并立即交给模型处理。
- **零依赖原生 MCP 工具（Python 3.10+ 标准库）**：
  - 绕开终端审批弹窗与沙箱写路径限制，直接提供 `list_tasks`、`register_task`、`send_task_message`、`check_messages`、`get_message_history`、`wake_task` 六个 MCP 工具。
- **并发安全存储（SQLite WAL）**：
  - 共享总线存储在 `~/.gemini/antigravity/task_bus/bus.sqlite3`（可通过环境变量 `TASK_BUS_DATA_DIR` 覆盖），支持多进程原子事务与防重复投递。
- **多种路由模式**：
  - 点对点（支持完整 `conversationId`、前 6+ 位前缀、或自定义 `alias` 别名）
  - 频道组播（`targetType: "channel"`）
  - 全员广播（`targetType: "broadcast"`）
  - 优先级支持（`normal` / `urgent` 置顶）
- **空闲目标主动唤醒**：
  - **CLI（`agy`）独立会话**：发件方自动后台拉起 `agy --conversation <id> -p ...` 唤醒目标会话（含 10 秒冷却防抖）。
  - **Antigravity App 桌面会话**：发件方写入 `~/.gemini/antigravity/task_bus/app_wake_queue/` 原子队列；配合可选的 [App 前端唤醒桥接](./app-wake-bridge/README.md) 可自动切入目标会话提交唤醒并切回原会话，**完全不修改目标任务的模型、推理强度或审核设置**。

## 目录结构

```text
antigravity-task-message-bus/
├── plugin.json                  # Antigravity 插件清单
├── hooks.json                   # SessionStart / PreInvocation / PostInvocation / Stop 钩子
├── mcp_config.json              # MCP Server 配置模板
├── install.ps1                  # Windows 一键安装脚本
├── install.sh                   # macOS / Linux 一键安装脚本
├── rules/
│   └── AGENTS.md                # 全局规则（自动识别 [task-message-bus] 注入消息）
├── skills/
│   └── task-message-bus/
│       └── SKILL.md             # 技能使用说明
├── scripts/
│   ├── bus_core.py              # SQLite WAL 消息总线核心与唤醒调度
│   ├── hook_handler.py          # 生命周期钩子处理器
│   ├── mcp_server.py            # 零依赖 stdio MCP Server
│   └── bus_cli.py               # 命令行调试与脚本调用入口
├── app-wake-bridge/
│   └── README.md                # 可选：空闲 Antigravity App 会话主动唤醒桥接说明
└── tests/
    └── test_task_bus.py         # 端到端自动化测试
```

## 快速安装

### Windows (PowerShell)

```powershell
git clone https://github.com/czhovo/antigravity-task-message-bus.git
cd antigravity-task-message-bus
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

### macOS / Linux

```bash
git clone https://github.com/czhovo/antigravity-task-message-bus.git
cd antigravity-task-message-bus
bash ./install.sh
```

安装脚本会将插件复制到 `~/.gemini/config/plugins/task-message-bus/` 并自动生成匹配当前环境的 `mcp_config.json`。Antigravity 会在下一轮对话或新会话中自动加载该插件。

## 运行测试

```bash
python tests/test_task_bus.py
```

测试使用独立临时目录（`TASK_BUS_DATA_DIR`）隔离运行，覆盖：
- `SessionStart` / `PreInvocation` / `PostInvocation` / `Stop` 拦截与放行
- 别名路由、频道组播与防重复投递
- MCP stdio 协议握手与工具调用
- 空闲 CLI 会话主动唤醒与冷却防抖
- 空闲 App 会话唤醒队列与桥接回传

## License

MIT
