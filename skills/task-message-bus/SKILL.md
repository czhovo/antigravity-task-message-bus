---
name: task-message-bus
description: Use when sending proactive messages between independent Antigravity tasks/conversations, registering task aliases or channels, listing known sessions, or auditing cross-task delivery and CLI/App wake-up status.
---

# Task Message Bus

`task-message-bus` 通过 Antigravity 生命周期钩子（`SessionStart` / `PreInvocation` / `PostInvocation` / `Stop`）与共享 SQLite WAL 消息总线，实现完全独立的任务（跨会话、跨窗口、跨 App 与 CLI）之间的主动消息投递。

## 工作机制

1. **自动登记**：所有启用本插件的独立任务在生命周期钩子触发时，会自动将自身 `conversationId`、来源（`app` / `cli` / `ide`）、运行状态（`running` / `idle`）、工作目录与最近输入登记到共享总线（`~/.gemini/antigravity/task_bus/bus.sqlite3`）。
2. **运行中任务实时主动投递**：
   - `SessionStart` / `PreInvocation`：模型调用前自动拉取未读消息并通过 `injectSteps` 注入上下文。
   - `PostInvocation`：工具执行完毕后自动拉取未读消息，并以 `terminationBehavior: "force_continue"` 确保立即处理。
   - `Stop`：任务准备结束时若恰好收到新消息，钩子返回 `decision: "continue"` 拦截停止并把消息作为指令注入。
3. **空闲目标主动唤醒策略**：
   - **目标为 CLI（`agy`）独立会话且空闲**：发件方调用 `send_task_message`（默认 `autoWake: true`）时，会自动后台拉起 `agy --conversation <target_id> -p ...` 主动唤醒目标会话，唤醒后的 `PreInvocation` 钩子立即投递消息（内置 10 秒唤醒冷却防抖）。
   - **目标为 Antigravity App 桌面会话且空闲**：发件方会自动写入 `~/.gemini/antigravity/task_bus/app_wake_queue/` 唤醒队列；若安装了可选的桌面端前端唤醒桥接，桥接会自动切入目标会话、在原生输入框提交简短唤醒语并自动切回原会话（不修改目标任务的任何模型、推理强度或审核设置），随后目标会话的 `PreInvocation` 钩子第一时间将完整消息注入上下文。
   - **补发唤醒**：若目标已有未读消息但未唤醒，可直接调用 `wake_task` 工具（无需重复发消息正文）。

## MCP 工具（推荐优先使用）

- `list_tasks`：查看已知的独立任务（`conversationId`、`alias`、`source`、`status`、`subscriptions`、`lastUserInput`）。
- `register_task`：为当前任务设置唯一别名（`alias`）或增删订阅频道（`subscriptions` / `addSubscriptions` / `removeSubscriptions`）。
- `send_task_message`：投递消息。
  - `targetType: "task"`：点对点投递，`target` 可填完整 `conversationId`、前 6+ 位前缀或 `alias`。
  - `targetType: "channel"`：向订阅了 `target` 频道的所有独立任务组播。
  - `targetType: "broadcast"`：向全部其他活跃任务广播。
  - `priority`: `"normal"` 或 `"urgent"`（置顶优先投递）。
- `check_messages`：手动立即拉取未读消息（日常无需调用，钩子会自动注入）。
- `get_message_history`：查看近期收发记录、投递状态及唤醒结果。
- `wake_task`：对已有未读消息的空闲任务补发一次主动唤醒。

## 命令行调试入口（可选）

```bash
python ~/.gemini/config/plugins/task-message-bus/scripts/bus_cli.py list
python ~/.gemini/config/plugins/task-message-bus/scripts/bus_cli.py register --id <cid> --alias backend --subscribe deploy
python ~/.gemini/config/plugins/task-message-bus/scripts/bus_cli.py send --from <cid_a> --to backend --subject "联调" --body "接口已就绪"
python ~/.gemini/config/plugins/task-message-bus/scripts/bus_cli.py wake --to <cid_b> --from <cid_a>
python ~/.gemini/config/plugins/task-message-bus/scripts/bus_cli.py history --limit 10
```
