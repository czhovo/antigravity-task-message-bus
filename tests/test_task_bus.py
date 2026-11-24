import json
import os
import subprocess
import sys
import tempfile
import threading
import time

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOK_SCRIPT = os.path.join(REPO_DIR, "scripts", "hook_handler.py")
MCP_SCRIPT = os.path.join(REPO_DIR, "scripts", "mcp_server.py")


def run_hook(payload: dict, env: dict) -> dict:
    proc = subprocess.run(
        [sys.executable, HOOK_SCRIPT],
        input=json.dumps(payload, ensure_ascii=False),
        text=True,
        encoding="utf-8",
        capture_output=True,
        env=env,
        timeout=10,
    )
    assert proc.returncode == 0, f"hook failed: {proc.stderr}"
    return json.loads(proc.stdout.strip())


def main() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        wake_log = os.path.join(tmpdir, "wake_called.txt")
        if os.name == "nt":
            fake_agy = os.path.join(tmpdir, "fake_agy.cmd")
            with open(fake_agy, "w", encoding="utf-8") as f:
                f.write(f'@echo off\necho %* >> "{wake_log}"\n')
        else:
            fake_agy = os.path.join(tmpdir, "fake_agy.sh")
            with open(fake_agy, "w", encoding="utf-8") as f:
                f.write(f'#!/usr/bin/env sh\necho "$@" >> "{wake_log}"\n')
            os.chmod(fake_agy, 0o755)

        env = os.environ.copy()
        env["TASK_BUS_DATA_DIR"] = tmpdir
        env["TASK_BUS_AGY_BIN"] = fake_agy
        env["TASK_BUS_WAKE_WAIT_SECONDS"] = "2.0"

        # 1. Register two independent tasks via SessionStart hook
        out_a = run_hook(
            {
                "conversationId": "aaaaaaaa-1111-2222-3333-444444444444",
                "hookEventName": "SessionStart",
                "transcriptPath": "/tmp/mock/.gemini/antigravity/brain/aaa/transcript.jsonl",
                "cwd": "/tmp/repo/frontend",
                "lastUserInput": "开发前端页面",
            },
            env,
        )
        assert out_a == {}, f"unexpected SessionStart output: {out_a}"

        out_b = run_hook(
            {
                "conversationId": "bbbbbbbb-1111-2222-3333-444444444444",
                "hookEventName": "SessionStart",
                "transcriptPath": "/tmp/mock/.gemini/antigravity-cli/brain/bbb/transcript.jsonl",
                "cwd": "/tmp/repo/backend",
                "lastUserInput": "维护后端 API",
            },
            env,
        )
        assert out_b == {}, f"unexpected SessionStart output: {out_b}"

        # 2. Test MCP stdio server: initialize, tools/list, register_task, send_task_message
        mcp = subprocess.Popen(
            [sys.executable, MCP_SCRIPT],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )

        def rpc(obj: dict, expect_reply: bool = True):
            mcp.stdin.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
            mcp.stdin.flush()
            if not expect_reply:
                return None
            line = mcp.stdout.readline().decode("utf-8")
            assert line, "EOF from MCP server"
            return json.loads(line)

        r_init = rpc(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2024-11-05"},
            }
        )
        assert r_init["result"]["serverInfo"]["name"] == "task-message-bus"
        rpc({"jsonrpc": "2.0", "method": "notifications/initialized"}, expect_reply=False)

        r_tools = rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tool_names = [t["name"] for t in r_tools["result"]["tools"]]
        assert set(tool_names) == {
            "list_tasks",
            "register_task",
            "send_task_message",
            "check_messages",
            "get_message_history",
            "wake_task",
        }

        # Register alias 'backend' + channel 'deploy' for task B
        r_reg = rpc(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "register_task",
                    "arguments": {
                        "conversationId": "bbbbbbbb-1111-2222-3333-444444444444",
                        "alias": "backend",
                        "subscriptions": ["deploy"],
                    },
                },
            }
        )
        assert r_reg["result"]["isError"] is False

        # Send direct message from A to alias 'backend' (currently running)
        r_send1 = rpc(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "send_task_message",
                    "arguments": {
                        "fromConversationId": "aaaaaaaa-1111-2222-3333-444444444444",
                        "target": "backend",
                        "subject": "接口变更",
                        "body": "请增加 /api/v2/status 字段",
                        "priority": "urgent",
                    },
                },
            }
        )
        send1_data = json.loads(r_send1["result"]["content"][0]["text"])
        assert send1_data["delivery"]["mode"] == "hook_live_running", send1_data

        # 3. Task B PreInvocation receives message once
        pre_b = run_hook(
            {
                "conversationId": "bbbbbbbb-1111-2222-3333-444444444444",
                "hookEventName": "PreInvocation",
                "transcriptPath": "/tmp/mock/.gemini/antigravity-cli/brain/bbb/transcript.jsonl",
            },
            env,
        )
        assert "injectSteps" in pre_b
        injected_text = pre_b["injectSteps"][0]["systemMessage"]["systemMessage"]
        assert "/api/v2/status" in injected_text and "[紧急]" in injected_text

        # Second PreInvocation returns empty {}
        pre_b2 = run_hook(
            {
                "conversationId": "bbbbbbbb-1111-2222-3333-444444444444",
                "hookEventName": "PreInvocation",
                "transcriptPath": "/tmp/mock/.gemini/antigravity-cli/brain/bbb/transcript.jsonl",
            },
            env,
        )
        assert pre_b2 == {}

        # 4. Channel message delivered via PostInvocation with force_continue
        rpc(
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {
                    "name": "send_task_message",
                    "arguments": {
                        "fromConversationId": "aaaaaaaa-1111-2222-3333-444444444444",
                        "targetType": "channel",
                        "target": "deploy",
                        "body": "准备发布 v1.2",
                    },
                },
            }
        )
        post_b = run_hook(
            {
                "conversationId": "bbbbbbbb-1111-2222-3333-444444444444",
                "hookEventName": "PostInvocation",
                "transcriptPath": "/tmp/mock/.gemini/antigravity-cli/brain/bbb/transcript.jsonl",
            },
            env,
        )
        assert post_b.get("terminationBehavior") == "force_continue"
        assert "准备发布 v1.2" in post_b["injectSteps"][0]["systemMessage"]["systemMessage"]

        # 5. Stop hook intercepts when message arrives, then allows idle when empty
        rpc(
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/call",
                "params": {
                    "name": "send_task_message",
                    "arguments": {
                        "fromConversationId": "aaaaaaaa-1111-2222-3333-444444444444",
                        "target": "backend",
                        "body": "等一下，补充一个字段校验",
                    },
                },
            }
        )
        stop_b1 = run_hook(
            {
                "conversationId": "bbbbbbbb-1111-2222-3333-444444444444",
                "hookEventName": "Stop",
                "terminationReason": "NO_TOOL_CALL",
                "transcriptPath": "/tmp/mock/.gemini/antigravity-cli/brain/bbb/transcript.jsonl",
            },
            env,
        )
        assert stop_b1.get("decision") == "continue"
        assert "补充一个字段校验" in stop_b1.get("reason", "")

        stop_b2 = run_hook(
            {
                "conversationId": "bbbbbbbb-1111-2222-3333-444444444444",
                "hookEventName": "Stop",
                "terminationReason": "NO_TOOL_CALL",
                "transcriptPath": "/tmp/mock/.gemini/antigravity-cli/brain/bbb/transcript.jsonl",
            },
            env,
        )
        assert stop_b2 == {}

        stop_a = run_hook(
            {
                "conversationId": "aaaaaaaa-1111-2222-3333-444444444444",
                "hookEventName": "Stop",
                "terminationReason": "NO_TOOL_CALL",
                "transcriptPath": "/tmp/mock/.gemini/antigravity/brain/aaa/transcript.jsonl",
            },
            env,
        )
        assert stop_a == {}

        # 6. Sending to idle CLI task B triggers active wake-up
        r_wake = rpc(
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {
                    "name": "send_task_message",
                    "arguments": {
                        "fromConversationId": "aaaaaaaa-1111-2222-3333-444444444444",
                        "target": "backend",
                        "subject": "紧急唤醒",
                        "body": "空闲 CLI 会话唤醒测试",
                    },
                },
            }
        )
        wake_data = json.loads(r_wake["result"]["content"][0]["text"])
        assert wake_data["delivery"]["mode"] == "cli_woken_actively", wake_data
        assert wake_data["delivery"]["woken"] is True

        for _ in range(20):
            if os.path.isfile(wake_log):
                break
            time.sleep(0.1)
        assert os.path.isfile(wake_log), "fake_agy was not invoked!"
        with open(wake_log, "r", encoding="utf-8", errors="replace") as f:
            wake_args = f.read()
        assert "--conversation bbbbbbbb-1111-2222-3333-444444444444" in wake_args, wake_args

        r_wake2 = rpc(
            {
                "jsonrpc": "2.0",
                "id": 8,
                "method": "tools/call",
                "params": {
                    "name": "send_task_message",
                    "arguments": {
                        "fromConversationId": "aaaaaaaa-1111-2222-3333-444444444444",
                        "target": "backend",
                        "body": "冷却期内第二条消息",
                    },
                },
            }
        )
        wake2_data = json.loads(r_wake2["result"]["content"][0]["text"])
        assert wake2_data["delivery"]["mode"] == "cli_wake_cooldown", wake2_data

        # 7. Sending to idle App task A dispatches to app_wake_queue & receives active bridge result
        def simulate_app_bridge():
            qdir = os.path.join(tmpdir, "app_wake_queue")
            rdir = os.path.join(tmpdir, "app_wake_results")
            for _ in range(40):
                if os.path.isdir(qdir):
                    files = [f for f in os.listdir(qdir) if f.endswith(".json") and not f.startswith(".")]
                    if files:
                        fpath = os.path.join(qdir, files[0])
                        with open(fpath, "r", encoding="utf-8") as f:
                            p = json.load(f)
                        os.makedirs(rdir, exist_ok=True)
                        with open(os.path.join(rdir, f"{p['wakeId']}.json"), "w", encoding="utf-8") as rf:
                            json.dump({"ok": True, "reason": "submitted_via_app_ui"}, rf)
                        os.remove(fpath)
                        return
                time.sleep(0.05)

        t_bridge = threading.Thread(target=simulate_app_bridge, daemon=True)
        t_bridge.start()

        r_app_idle = rpc(
            {
                "jsonrpc": "2.0",
                "id": 9,
                "method": "tools/call",
                "params": {
                    "name": "send_task_message",
                    "arguments": {
                        "fromConversationId": "bbbbbbbb-1111-2222-3333-444444444444",
                        "target": "aaaaaaaa-1111-2222-3333-444444444444",
                        "body": "给空闲 App 任务的消息",
                    },
                },
            }
        )
        t_bridge.join(timeout=3)
        app_idle_data = json.loads(r_app_idle["result"]["content"][0]["text"])
        assert app_idle_data["delivery"]["mode"] == "app_woken_actively", app_idle_data
        assert app_idle_data["delivery"]["woken"] is True

        # 8. History check
        r_hist = rpc(
            {
                "jsonrpc": "2.0",
                "id": 10,
                "method": "tools/call",
                "params": {"name": "get_message_history", "arguments": {"limit": 10}},
            }
        )
        hist_data = json.loads(r_hist["result"]["content"][0]["text"])
        assert hist_data["count"] == 6, hist_data

        mcp.stdin.close()
        mcp.wait(timeout=5)
        print("ALL_TESTS_PASSED")


if __name__ == "__main__":
    main()
