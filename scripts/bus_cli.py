"""Command-line interface for task-message-bus plugin (debugging & scriptable access)."""

from __future__ import annotations

import argparse
import json
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import bus_core


def _print_json(data: object) -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    print(json.dumps(data, ensure_ascii=False, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser(description="Antigravity task-message-bus CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="List known tasks")
    p_list.add_argument("--running", action="store_true", help="Only running tasks")
    p_list.add_argument("--source", choices=["app", "cli", "ide", "unknown"])
    p_list.add_argument("--limit", type=int, default=30)

    p_reg = sub.add_parser("register", help="Register alias or channel subscriptions")
    p_reg.add_argument("--id", required=True, help="Conversation ID")
    p_reg.add_argument("--alias", help="Unique alias")
    p_reg.add_argument("--subscribe", nargs="*", help="Channels to add")
    p_reg.add_argument("--unsubscribe", nargs="*", help="Channels to remove")

    p_send = sub.add_parser("send", help="Send message to a task, channel, or broadcast")
    p_send.add_argument("--from", dest="from_id", required=True, help="Sender conversation ID")
    p_send.add_argument("--to", default="", help="Target conversation ID, alias, or channel")
    p_send.add_argument(
        "--type",
        dest="target_type",
        choices=["task", "channel", "broadcast"],
        default="task",
    )
    p_send.add_argument("--subject", default="")
    p_send.add_argument("--body", required=True)
    p_send.add_argument("--priority", choices=["normal", "urgent"], default="normal")
    p_send.add_argument("--no-wake", action="store_true", help="Disable auto-waking idle CLI targets")

    p_check = sub.add_parser("check", help="Check pending messages for a task")
    p_check.add_argument("--id", required=True, help="Conversation ID")
    p_check.add_argument("--peek", action="store_true", help="Do not mark as delivered")

    p_hist = sub.add_parser("history", help="Show recent message history")
    p_hist.add_argument("--id", help="Filter by conversation ID")
    p_hist.add_argument("--limit", type=int, default=20)

    p_wake = sub.add_parser("wake", help="Actively wake an idle CLI or App task")
    p_wake.add_argument("--to", required=True, help="Target conversation ID or alias")
    p_wake.add_argument("--from", dest="from_id", default="manual", help="Sender conversation ID")
    p_wake.add_argument("--subject", default="")
    p_wake.add_argument("--force", action="store_true")
    p_wake.add_argument("--wait", type=float, default=6.0)

    args = parser.parse_args()

    if args.cmd == "list":
        _print_json(
            bus_core.list_tasks(
                only_running=args.running, source=args.source, limit=args.limit
            )
        )
    elif args.cmd == "register":
        _print_json(
            bus_core.register_task(
                args.id,
                alias=args.alias,
                add_subscriptions=args.subscribe,
                remove_subscriptions=args.unsubscribe,
            )
        )
    elif args.cmd == "send":
        _print_json(
            bus_core.send_message(
                from_conversation_id=args.from_id,
                target=args.to or ("*" if args.target_type == "broadcast" else ""),
                body=args.body,
                target_type=args.target_type,
                subject=args.subject,
                priority=args.priority,
                auto_wake=not args.no_wake,
            )
        )
    elif args.cmd == "check":
        _print_json(
            bus_core.pull_pending_messages(
                args.id, via_event="manual_cli", mark_delivered=not args.peek
            )
        )
    elif args.cmd == "history":
        _print_json(bus_core.get_message_history(conversation_id=args.id, limit=args.limit))
    elif args.cmd == "wake":
        _print_json(
            bus_core.wake_task(
                target=args.to,
                from_conversation_id=args.from_id,
                subject=args.subject,
                force=args.force,
                wait_seconds=args.wait,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
