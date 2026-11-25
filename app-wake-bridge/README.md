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

本目录提供完整的脱敏参考实现：
- [`main-ipc.js`](./main-ipc.js)：Electron 主进程 IPC 认领与回写模块（`ag-task-bus:claim-wake` / `ag-task-bus:finish-wake`）。
- [`renderer-bridge.js`](./renderer-bridge.js)：渲染进程唤醒消费器（支持后台隐藏 pane 静默提交优先与降级瞬切）。

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
  "silentNoSwitch": true,
  "draftPreserved": false,
  "reason": "submitted_via_hidden_pane",
  "targetConversationId": "<target-uuid>",
  "prevConversationId": "<prev-uuid>",
  "returnedToPrev": true
}
```

## 核心设计要点

1. **主进程原子认领与长草稿退避（`main-ipc.js`）**：
   - 内存 `inFlight` Map 防止多窗口重复消费同一条唤醒任务（25 秒超时自动释放）。
   - 对 `*has_draft*` 类临时退避，在队列 10 分钟 TTL 内允许最多 120 次 × 5 秒重试，避免因短暂草稿阻挡导致唤醒任务提前丢弃。
2. **后台隐藏 DOM pane 静默提交优先（`renderer-bridge.js`）**：
   - Phase 1 优先查找已挂载在后台隐藏 pane 中的目标会话编辑器（通过 `data-cascade-id` / `data-conversation-id` 或 React Fiber 精确校验 UUID），直接通过 `beforeinput` / `paste` 静默提交，**零界面切换**。
   - Phase 2 当目标未挂载或隐藏态无法提交时，自动降级为瞬切目标会话提交并切回原会话（切回等待压缩至 `150ms`）。
3. **草稿无损保护与 Lexical 三段式清空校验**：
   - 仅在用户过去 `1.5s` 内有真实敲击键盘或输入法活动时短暂避让，不再因当前窗口的静止草稿拦截唤醒。
   - `getEditorText()` 剥离零宽字符 `[\u200B-\u200D\uFEFF]`，防止 Lexical 空编辑器被误判为有草稿。
   - 若目标会话输入框已有未发送草稿，采用 **暂存草稿 → 三段式清空（`selectAll` + `delete` / `Range` + `beforeinput` / `Ctrl+A` + `Backspace`）→ 严格校验为空 → 提交唤醒 → 恢复原草稿**；若清空未成功，直接中止提交并延后重试，绝不将唤醒文本拼接到用户草稿后一并发送。
   - 全程不触碰目标会话的模型选择、推理强度与审核设置。
