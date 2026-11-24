# Optional: Antigravity App Idle Session Wake Bridge

默认情况下，`task-message-bus` 插件可以：
- 对**运行中的 App / CLI / IDE 会话**通过生命周期钩子实时主动投递消息；
- 对**已空闲的 CLI（`agy`）会话**直接由发送方拉起 `agy --conversation <id> -p ...` 主动唤醒。

对于**已空闲的 Antigravity App 桌面会话**，`bus_core.py` 会将唤醒请求原子写入：

```text
~/.gemini/antigravity/task_bus/app_wake_queue/<wakeId>.json
```

并等待前端桥接将执行结果回写至：

```text
~/.gemini/antigravity/task_bus/app_wake_results/<wakeId>.json
```

## 队列文件格式

### 请求（`app_wake_queue/<wakeId>.json`）

```json
{
  "wakeId": "1795520885294_288e371e",
  "targetConversationId": "<target-uuid>",
  "messageId": 1,
  "fromLabel": "sender (a1b2c3d4)",
  "subject": "协作主题",
  "wakeText": "[task-message-bus 唤醒] 收到来自独立任务 ... 的新消息，请读取本次上下文中由钩子自动注入的消息内容并处理。",
  "createdAt": "2026-11-24T11:48:05.294000+00:00",
  "createdAtMs": 1795520885294
}
```

### 结果（`app_wake_results/<wakeId>.json`）

```json
{
  "wakeId": "1795520885294_288e371e",
  "finishedAt": "2026-11-24T11:48:45.979Z",
  "ok": true,
  "reason": "submitted_via_app_ui",
  "targetConversationId": "<target-uuid>",
  "prevConversationId": "<prev-uuid>",
  "returnedToPrev": true
}
```

## 桥接实现要点

如果你维护了本地 Antigravity Electron 前端扩展（如 preload + ipcMain 注入），可以按以下方式接入：

1. **主进程 IPC**：
   - `ag-task-bus:claim-wake`：原子认领 `app_wake_queue/` 中最早的未过期 `.json` 文件，通过内存 `inFlight` Map 避免多窗口重复认领，超时自动释放。
   - `ag-task-bus:finish-wake`：接收渲染进程执行结果；若返回 `retryLater: true`（例如检测到用户输入框有未发送草稿），延后几秒重试（最多 3 次）；否则写入 `app_wake_results/<wakeId>.json` 并删除队列文件。
2. **渲染进程消费器**：
   - 冷启动等待侧边栏会话列表渲染就绪后再开始轮询。
   - 保护用户草稿：若当前窗口或目标会话输入框内存在未发送文本，直接延迟重试，绝不覆盖用户输入。
   - 切入目标会话后二次校验当前会话 ID，再向原生输入框提交 `wakeText`（提交时对 `execCommand('insertText')` 后等待 `80ms` 再判空，避免 Lexical 富文本编辑器重复插入）。
   - 提交确认后自动切回原会话。
   - 全程不触碰目标会话的模型选择、推理强度与审核设置，100% 沿用其原有配置；提交触发新一轮执行后，目标会话的 `PreInvocation` 钩子自动把完整 `[task-message-bus]` 消息注入上下文。
