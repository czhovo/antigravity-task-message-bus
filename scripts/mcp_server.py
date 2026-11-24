"""Zero-dependency stdio MCP Server for task-message-bus plugin."""

from __future__ import annotations

import json
import os
import sys
from typing import Any

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import bus_core

SERVER_INFO = {
    "name": "task-message-bus",
    "version": "1.0.0",
}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "list_tasks",
        "description": (
            "列出所有已知的独立 Antigravity 任务（由生命周期钩子自动登记），"
            "包含 conversationId、别名 alias、来源（app/cli/ide）、运行状态（running/idle）、工作目录、订阅频道与任务摘要。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "onlyRunning": {
                    "type": "boolean",
                    "description": "为 true 时仅返回当前处于 running 状态的任务，默认 false。",
                },
                "source": {
                    "type": "string",
                    "enum": ["app", "cli", "ide", "unknown"],
                    "description": "按任务来源筛选（可选）。",
                },
                "limit": {
                    "type": "integer",
                    "description": "最多返回的任务数量（默认 30，最大 200）。",
                },
            },
        },
    },
    {
        "name": "register_task",
        "description": (
            "为当前或指定独立任务注册易记别名（alias）或管理订阅频道（subscriptions），"
            "方便其他独立任务按别名或频道定向投递消息。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "conversationId": {
                    "type": "string",
                    "description": "当前（或目标）任务的完整 Conversation ID。",
                },
                "alias": {
                    "type": "string",
                    "description": "为该任务设置的唯一易记别名（例如 backend、reviewer、worker-a）。",
                },
                "subscriptions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "覆盖设置该任务订阅的频道列表（可选）。",
                },
                "addSubscriptions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "增量追加订阅的频道列表（可选）。",
                },
                "removeSubscriptions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "取消订阅的频道列表（可选）。",
                },
            },
            "required": ["conversationId"],
        },
    },
    {
        "name": "send_task_message",
        "description": (
            "向另一个独立任务（支持 conversationId、前 6+ 位前缀或 alias）、频道或全部活跃任务主动投递消息。"
            "运行中的目标任务会通过钩子自动实时收到；若目标是已空闲的 CLI（agy）会话且 autoWake=true，发送方会自动拉起 agy --conversation 主动唤醒它。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "fromConversationId": {
                    "type": "string",
                    "description": "发件方当前任务的 Conversation ID。",
                },
                "target": {
                    "type": "string",
                    "description": "目标任务的 conversationId / 别名（targetType='task' 时），或频道名（targetType='channel' 时）；broadcast 时可留空或填 '*'。",
                },
                "body": {
                    "type": "string",
                    "description": "要投递的消息正文内容。",
                },
                "targetType": {
                    "type": "string",
                    "enum": ["task", "channel", "broadcast"],
                    "description": "投递范围：task（点对点，默认）、channel（频道组播）、broadcast（全员广播）。",
                },
                "subject": {
                    "type": "string",
                    "description": "可选的消息主题摘要。",
                },
                "priority": {
                    "type": "string",
                    "enum": ["normal", "urgent"],
                    "description": "消息优先级：normal（默认）或 urgent（置顶优先投递）。",
                },
                "autoWake": {
                    "type": "boolean",
                    "description": "当目标为已空闲的 CLI 会话时是否由发送方自动唤醒（默认 true）。",
                },
                "metadata": {
                    "type": "object",
                    "description": "可选的结构化附加字段。",
                },
            },
            "required": ["fromConversationId", "body"],
        },
    },
    {
        "name": "check_messages",
        "description": (
            "手动立即拉取当前任务的未读消息（通常钩子会自动注入，此工具用于需要显式检查或预览未读队列的场景）。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "conversationId": {
                    "type": "string",
                    "description": "当前任务的 Conversation ID。",
                },
                "markDelivered": {
                    "type": "boolean",
                    "description": "拉取后是否标记为已读（默认 true）。",
                },
                "limit": {
                    "type": "integer",
                    "description": "最多拉取条数（默认 20）。",
                },
            },
            "required": ["conversationId"],
        },
    },
    {
        "name": "get_message_history",
        "description": (
            "查询近期收发的消息记录及其投递状态（pending/delivered、经由哪个钩子投递、是否触发了 CLI 主动唤醒）。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "conversationId": {
                    "type": "string",
                    "description": "可选：按特定任务筛选相关收发消息；省略时返回全局最近消息。",
                },
                "limit": {
                    "type": "integer",
                    "description": "返回条数上限（默认 20，最大 200）。",
                },
            },
        },
    },
    {
        "name": "wake_task",
        "description": (
            "对已有未读消息的空闲独立任务（CLI 会话或 Antigravity App 桌面会话）补发一次主动唤醒，不修改目标任务的模型、推理强度或审核设置。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "目标任务的 conversationId、前 6+ 位前缀或别名。",
                },
                "fromConversationId": {
                    "type": "string",
                    "description": "发起唤醒的当前任务 Conversation ID（可选）。",
                },
                "subject": {
                    "type": "string",
                    "description": "可选的主题说明。",
                },
                "force": {
                    "type": "boolean",
                    "description": "为 true 时忽略唤醒冷却期（默认 false）。",
                },
            },
            "required": ["target"],
        },
    },
]


def _execute_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    if name == "list_tasks":
        tasks = bus_core.list_tasks(
            only_running=bool(args.get("onlyRunning", False)),
            source=args.get("source"),
            limit=int(args.get("limit", 30) or 30),
        )
        return {"count": len(tasks), "tasks": tasks}

    if name == "register_task":
        conv_id = str(args.get("conversationId") or "").strip()
        task = bus_core.register_task(
            conv_id,
            alias=args.get("alias"),
            subscriptions=args.get("subscriptions"),
            add_subscriptions=args.get("addSubscriptions"),
            remove_subscriptions=args.get("removeSubscriptions"),
        )
        return {"ok": True, "task": task}

    if name == "send_task_message":
        from_id = str(args.get("fromConversationId") or "").strip()
        target_type = str(args.get("targetType") or "task").strip().lower()
        target = str(args.get("target") or ("*" if target_type == "broadcast" else "")).strip()
        body = str(args.get("body") or "")
        return bus_core.send_message(
            from_conversation_id=from_id,
            target=target,
            body=body,
            target_type=target_type,
            subject=str(args.get("subject") or ""),
            priority=str(args.get("priority") or "normal"),
            metadata=args.get("metadata") if isinstance(args.get("metadata"), dict) else None,
            auto_wake=bool(args.get("autoWake", True)),
        )

    if name == "check_messages":
        conv_id = str(args.get("conversationId") or "").strip()
        msgs = bus_core.pull_pending_messages(
            conv_id,
            via_event="manual_mcp",
            mark_delivered=bool(args.get("markDelivered", True)),
            limit=int(args.get("limit", 20) or 20),
        )
        return {"count": len(msgs), "messages": msgs}

    if name == "get_message_history":
        conv_id = args.get("conversationId")
        if conv_id is not None:
            conv_id = str(conv_id).strip() or None
        hist = bus_core.get_message_history(
            conversation_id=conv_id,
            limit=int(args.get("limit", 20) or 20),
        )
        return {"count": len(hist), "messages": hist}

    if name == "wake_task":
        return bus_core.wake_task(
            target=str(args.get("target") or "").strip(),
            from_conversation_id=str(args.get("fromConversationId") or "manual").strip(),
            subject=str(args.get("subject") or ""),
            force=bool(args.get("force", False)),
        )

    raise ValueError(f"Unknown tool: {name}")


def _read_message(stdin_buf) -> tuple[dict[str, Any] | None, str]:
    """Read one JSON-RPC message. Supports both NDJSON lines and Content-Length framing."""
    while True:
        line = stdin_buf.readline()
        if not line:
            return None, "ndjson"
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(b"{"):
            return json.loads(stripped.decode("utf-8")), "ndjson"
        # Header-framed (Content-Length)
        headers: dict[str, str] = {}
        cur = line
        while cur and cur.strip():
            text = cur.decode("ascii", errors="ignore")
            if ":" in text:
                k, v = text.split(":", 1)
                headers[k.strip().lower()] = v.strip()
            cur = stdin_buf.readline()
        length = int(headers.get("content-length", "0"))
        if length <= 0:
            continue
        payload = stdin_buf.read(length)
        if not payload:
            return None, "framed"
        return json.loads(payload.decode("utf-8")), "framed"


def _write_message(stdout_buf, msg: dict[str, Any], mode: str) -> None:
    encoded = json.dumps(msg, ensure_ascii=False).encode("utf-8")
    if mode == "framed":
        header = f"Content-Length: {len(encoded)}\r\n\r\n".encode("ascii")
        stdout_buf.write(header + encoded)
    else:
        stdout_buf.write(encoded + b"\n")
    stdout_buf.flush()


def handle_request(req: dict[str, Any]) -> dict[str, Any] | None:
    req_id = req.get("id")
    method = req.get("method", "")
    params = req.get("params") or {}

    # Notifications have no id and expect no response
    if req_id is None and method.startswith("notifications/"):
        return None

    if method == "initialize":
        client_ver = params.get("protocolVersion") or "2024-11-05"
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": client_ver,
                "capabilities": {"tools": {}},
                "serverInfo": SERVER_INFO,
            },
        }

    if method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}}

    if method == "tools/call":
        tool_name = str(params.get("name") or "")
        tool_args = params.get("arguments") or {}
        try:
            data = _execute_tool(tool_name, tool_args if isinstance(tool_args, dict) else {})
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(data, ensure_ascii=False, indent=2),
                        }
                    ],
                    "isError": False,
                },
            }
        except Exception as exc:
            bus_core.log_error(f"mcp.tools/call.{tool_name}", exc)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                                ensure_ascii=False,
                            ),
                        }
                    ],
                    "isError": True,
                },
            }

    if req_id is None:
        return None

    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def main() -> None:
    stdin_buf = sys.stdin.buffer
    stdout_buf = sys.stdout.buffer
    while True:
        try:
            req, mode = _read_message(stdin_buf)
            if req is None:
                break
            resp = handle_request(req)
            if resp is not None:
                _write_message(stdout_buf, resp, mode)
        except Exception as exc:
            bus_core.log_error("mcp_server.loop", exc)
            break


if __name__ == "__main__":
    main()
