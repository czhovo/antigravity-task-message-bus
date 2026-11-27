"""Lifecycle hook handler for task-message-bus plugin.

Handles SessionStart, PreInvocation, PostInvocation, and Stop events.
Reads camelCase JSON payload from stdin and writes JSON response to stdout.
"""

from __future__ import annotations

import json
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import bus_core

NON_CONTINUABLE_STOP_REASONS = {
    "USER_CANCELED",
    "MAX_INVOCATIONS",
    "MAX_FORCED_INVOCATIONS",
    "MAX_TOKEN_BUDGET_EXCEEDED",
}


def _build_inject_response(text: str, *, force_continue: bool = False) -> dict:
    resp: dict = {
        "injectSteps": [
            {
                "systemMessage": {
                    "systemMessage": text,
                }
            }
        ]
    }
    if force_continue:
        resp["terminationBehavior"] = "force_continue"
    return resp


def handle_payload(payload: dict) -> dict:
    conv_id = str(payload.get("conversationId") or "").strip()
    if not conv_id:
        return {}

    event = str(payload.get("hookEventName") or "").strip()
    transcript_path = str(payload.get("transcriptPath") or "")
    artifact_dir = str(payload.get("artifactDirectoryPath") or "")
    source = bus_core.detect_source(transcript_path, artifact_dir)
    cwd = str(payload.get("cwd") or "")
    workspace_paths = payload.get("workspacePaths")
    if not isinstance(workspace_paths, list):
        workspace_paths = []
    model_name = str(payload.get("modelName") or "")
    last_user_input = str(payload.get("lastUserInput") or "")

    if event == "Stop":
        term_reason = str(payload.get("terminationReason") or "").strip()
        if term_reason not in NON_CONTINUABLE_STOP_REASONS:
            # First ensure task exists before pulling fanout/direct messages
            bus_core.upsert_task_heartbeat(
                conv_id,
                source=source,
                status="running",
                cwd=cwd,
                workspace_paths=workspace_paths,
                model_name=model_name,
                last_user_input=last_user_input,
                last_event=event,
            )
            pending = bus_core.pull_pending_messages(
                conv_id, via_event="Stop", mark_delivered=True
            )
            if pending:
                text = bus_core.format_injected_messages(pending, via_event="Stop")
                return {
                    "decision": "continue",
                    "reason": text,
                }

        stop_status = "waiting_bg" if payload.get("fullyIdle") is False else "idle"
        bus_core.upsert_task_heartbeat(
            conv_id,
            source=source,
            status=stop_status,
            cwd=cwd,
            workspace_paths=workspace_paths,
            model_name=model_name,
            last_user_input=last_user_input,
            last_event=event,
        )
        return {}

    # SessionStart / PreInvocation / PostInvocation
    bus_core.upsert_task_heartbeat(
        conv_id,
        source=source,
        status="running",
        cwd=cwd,
        workspace_paths=workspace_paths,
        model_name=model_name,
        last_user_input=last_user_input,
        last_event=event or "unknown",
    )

    pending = bus_core.pull_pending_messages(
        conv_id, via_event=event or "Hook", mark_delivered=True
    )
    if not pending:
        return {}

    text = bus_core.format_injected_messages(pending, via_event=event or "Hook")
    if event == "PostInvocation":
        return _build_inject_response(text, force_continue=True)
    return _build_inject_response(text, force_continue=False)


def main() -> None:
    output: dict = {}
    try:
        try:
            sys.stdin.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
        raw = sys.stdin.read().strip()
        payload = json.loads(raw) if raw else {}
        if isinstance(payload, dict):
            output = handle_payload(payload)
    except Exception as exc:
        bus_core.log_error("hook_handler.main", exc)
        output = {}

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.stdout.write(json.dumps(output, ensure_ascii=True) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
