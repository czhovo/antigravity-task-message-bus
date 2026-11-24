# Task Message Bus Rules

- When you see a system message prefixed with `[task-message-bus]`, it was proactively delivered from another independent Antigravity task via lifecycle hooks. Treat it as an incoming cross-task message, act on it in context, and reply via `task-message-bus` (`send_task_message`) if a response is expected.
- When the user asks to message another independent conversation/task, list active tasks, set a task alias, subscribe to a channel, or inspect cross-task delivery status, use the `task-message-bus` MCP server tools (`list_tasks`, `register_task`, `send_task_message`, `check_messages`, `get_message_history`) and pass your current `Conversation ID` as `fromConversationId` / `conversationId`.
- Do not poll `check_messages` in a loop; lifecycle hooks (`PreInvocation`, `PostInvocation`, `Stop`) deliver incoming messages automatically while a task is running.
