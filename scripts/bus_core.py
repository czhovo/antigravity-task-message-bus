"""Core SQLite WAL message bus and task registry for task-message-bus plugin."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import time
import uuid
from datetime import datetime, timezone
from typing import Any

DEFAULT_TTL_SECONDS = 7 * 24 * 3600
RUNNING_FRESH_SECONDS = 120
WAKE_COOLDOWN_SECONDS = 10


def get_data_dir() -> str:
    env_dir = os.environ.get("TASK_BUS_DATA_DIR")
    if env_dir:
        return os.path.abspath(env_dir)
    return os.path.join(os.path.expanduser("~"), ".gemini", "antigravity", "task_bus")


def get_db_path() -> str:
    data_dir = get_data_dir()
    os.makedirs(data_dir, exist_ok=True)
    return os.path.join(data_dir, "bus.sqlite3")


def get_app_wake_queue_dir() -> str:
    d = os.path.join(get_data_dir(), "app_wake_queue")
    os.makedirs(d, exist_ok=True)
    return d


def get_app_wake_results_dir() -> str:
    d = os.path.join(get_data_dir(), "app_wake_results")
    os.makedirs(d, exist_ok=True)
    return d


def infer_unregistered_source(conversation_id: str) -> str:
    cid = (conversation_id or "").strip()
    if not cid or any(ch in cid for ch in ("/", "\\", "..")):
        return "unknown"
    home = os.path.expanduser("~")
    app_base = os.environ.get("TASK_BUS_APP_ROOT") or os.path.join(home, ".gemini", "antigravity")
    if (
        os.path.isdir(os.path.join(app_base, "brain", cid))
        or os.path.isfile(os.path.join(app_base, "conversations", f"{cid}.pb"))
        or os.path.isfile(os.path.join(app_base, "conversations", f"{cid}.db"))
    ):
        return "app"
    cli_base = os.environ.get("TASK_BUS_CLI_ROOT") or os.path.join(home, ".gemini", "antigravity-cli")
    if (
        os.path.isdir(os.path.join(cli_base, "brain", cid))
        or os.path.isfile(os.path.join(cli_base, "conversations", f"{cid}.pb"))
        or os.path.isfile(os.path.join(cli_base, "conversations", f"{cid}.db"))
    ):
        return "cli"
    return "unknown"


def _get_session_base_dir(source: str) -> str | None:
    src = (source or "").strip().lower()
    home = os.path.expanduser("~")
    if src == "app":
        return os.environ.get("TASK_BUS_APP_ROOT") or os.path.join(home, ".gemini", "antigravity")
    if src == "cli":
        return os.environ.get("TASK_BUS_CLI_ROOT") or os.path.join(home, ".gemini", "antigravity-cli")
    return None


def inspect_target_runtime_state(conversation_id: str, source: str) -> dict[str, Any]:
    """Inspect target session's transcript.jsonl and .db-wal to avoid redundant wake-ups
    when the target is actively running or has pending background tasks / subagents / timers.
    """
    cid = (conversation_id or "").strip()
    base = _get_session_base_dir(source)
    if not cid or not base or any(ch in cid for ch in ("/", "\\", "..")):
        return {"state": "unknown", "shouldWake": True, "reasons": []}

    tp = os.path.join(base, "brain", cid, ".system_generated", "logs", "transcript.jsonl")
    wal = os.path.join(base, "conversations", f"{cid}.db-wal")
    if not os.path.isfile(tp):
        return {"state": "unknown", "shouldWake": True, "reasons": []}

    now = time.time()
    try:
        st = os.stat(tp)
        tp_age = max(0.0, now - st.st_mtime)
        wal_age = max(0.0, now - os.path.getmtime(wal)) if os.path.isfile(wal) else 999999.0
        min_age = min(tp_age, wal_age)

        read_size = min(st.st_size, 131072)
        with open(tp, "rb") as f:
            if st.st_size > read_size:
                f.seek(st.st_size - read_size)
            raw = f.read(read_size).decode("utf-8", errors="replace")

        steps: list[dict[str, Any]] = []
        for ln in raw.splitlines():
            s = ln.strip()
            if not s or not s.startswith("{"):
                continue
            try:
                steps.append(json.loads(s))
            except Exception:
                continue

        if not steps:
            return {"state": "truly_idle", "shouldWake": True, "reasons": []}

        last_closed_idx = -1
        last_trigger_idx = -1
        last_restart_idx = -1
        for i, step in enumerate(steps):
            t = step.get("type")
            st_str = str(step.get("status") or "")
            content = str(step.get("content") or "")
            if t == "USER_INPUT":
                last_trigger_idx = i
            elif t == "PLANNER_RESPONSE":
                tcs = step.get("tool_calls") or []
                tc_names = {tc.get("name") for tc in tcs if isinstance(tc, dict)}
                if tcs and "swarm_wait" not in tc_names:
                    last_trigger_idx = i
                elif st_str in ("DONE", "ERROR", "CANCELED"):
                    last_closed_idx = i
            elif t == "ERROR_MESSAGE":
                last_closed_idx = i
            elif t == "SYSTEM_MESSAGE" and "stopped due to server restart" in content:
                last_restart_idx = i
                prev_t = steps[i - 1].get("type") if i > 0 else None
                if prev_t != "USER_INPUT":
                    last_closed_idx = i

        active_steps: list[Any] = []
        for i, step in enumerate(steps):
            if i <= last_restart_idx:
                continue
            if str(step.get("status") or "") not in ("RUNNING", "PENDING", "WAITING", "IN_PROGRESS"):
                continue
            has_later_sys = any(
                steps[j].get("type") == "SYSTEM_MESSAGE"
                for j in range(i + 1, len(steps))
            )
            if i > last_closed_idx or not has_later_sys:
                active_steps.append(step.get("step_index", i))

        reasons: list[str] = []
        if active_steps and min_age < 7200:
            reasons.append(f"active_steps={active_steps}")
        if last_trigger_idx > last_closed_idx and min_age < 1800:
            reasons.append(f"unclosed_turn(trigger={last_trigger_idx},closed={last_closed_idx})")
        if wal_age < 5.0:
            reasons.append(f"wal_recent({wal_age:.1f}s)")

        if active_steps and min_age < 7200:
            return {"state": "waiting_bg", "shouldWake": False, "reasons": reasons}
        if (last_trigger_idx > last_closed_idx and min_age < 1800) or wal_age < 5.0:
            return {"state": "running", "shouldWake": False, "reasons": reasons}
        return {"state": "truly_idle", "shouldWake": True, "reasons": []}
    except Exception as exc:
        log_error("inspect_target_runtime_state", exc)
        return {"state": "unknown", "shouldWake": True, "reasons": []}


def _cancel_pending_app_wakes(conversation_id: str, *, via_event: str) -> int:
    """Remove any unconsumed app_wake_queue files for a target whose messages were already delivered by hooks."""
    cid_lower = (conversation_id or "").strip().lower()
    if not cid_lower:
        return 0
    queue_dir = os.path.join(get_data_dir(), "app_wake_queue")
    if not os.path.isdir(queue_dir):
        return 0
    results_dir = get_app_wake_results_dir()
    cancelled = 0
    try:
        for fname in os.listdir(queue_dir):
            if not fname.endswith(".json") or fname.startswith("."):
                continue
            fpath = os.path.join(queue_dir, fname)
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    payload = json.load(f)
            except Exception:
                continue
            target_id = str(payload.get("targetConversationId") or "").strip().lower()
            if target_id != cid_lower:
                continue
            wake_id = str(payload.get("wakeId") or "")
            try:
                os.remove(fpath)
                cancelled += 1
            except FileNotFoundError:
                continue
            except Exception:
                continue
            if wake_id:
                try:
                    res = {
                        "wakeId": wake_id,
                        "finishedAt": utc_now_iso(),
                        "ok": True,
                        "reason": "cancelled_already_delivered_by_hook",
                        "deliveredViaEvent": via_event,
                        "targetConversationId": conversation_id,
                    }
                    tmp_r = os.path.join(results_dir, f".{wake_id}.tmp")
                    fin_r = os.path.join(results_dir, f"{wake_id}.json")
                    with open(tmp_r, "w", encoding="utf-8") as rf:
                        json.dump(res, rf, ensure_ascii=False, indent=2)
                    os.replace(tmp_r, fin_r)
                except Exception:
                    pass
    except Exception as exc:
        log_error("cancel_pending_app_wakes", exc)
    return cancelled


def get_error_log_path() -> str:
    data_dir = get_data_dir()
    os.makedirs(data_dir, exist_ok=True)
    return os.path.join(data_dir, "bus_errors.log")


def log_error(context: str, exc: BaseException) -> None:
    try:
        ts = datetime.now(timezone.utc).isoformat()
        with open(get_error_log_path(), "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {context}: {type(exc).__name__}: {exc}\n")
    except Exception:
        pass


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def detect_source(transcript_path: str = "", artifact_dir: str = "") -> str:
    combined = f"{transcript_path} {artifact_dir}".replace("\\", "/").lower()
    if "/antigravity-cli/" in combined:
        return "cli"
    if "/antigravity-ide/" in combined:
        return "ide"
    if "/antigravity/" in combined:
        return "app"
    return "unknown"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(get_db_path(), timeout=5.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    _init_schema(conn)
    return conn


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS tasks (
            conversation_id TEXT PRIMARY KEY,
            alias TEXT,
            source TEXT NOT NULL DEFAULT 'unknown',
            status TEXT NOT NULL DEFAULT 'running',
            cwd TEXT DEFAULT '',
            workspace_paths TEXT DEFAULT '[]',
            model_name TEXT DEFAULT '',
            last_user_input TEXT DEFAULT '',
            subscriptions TEXT DEFAULT '[]',
            last_event TEXT DEFAULT '',
            registered_at REAL NOT NULL,
            last_seen_at REAL NOT NULL,
            last_wake_at REAL DEFAULT 0,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_tasks_alias ON tasks(alias COLLATE NOCASE);
        CREATE INDEX IF NOT EXISTS idx_tasks_last_seen ON tasks(last_seen_at DESC);

        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            msg_uuid TEXT UNIQUE NOT NULL,
            from_conversation_id TEXT NOT NULL,
            from_alias TEXT DEFAULT '',
            target_type TEXT NOT NULL,
            target_value TEXT NOT NULL,
            priority TEXT NOT NULL DEFAULT 'normal',
            subject TEXT DEFAULT '',
            body TEXT NOT NULL,
            metadata TEXT DEFAULT '{}',
            created_at REAL NOT NULL,
            created_at_iso TEXT NOT NULL,
            expires_at REAL NOT NULL,
            wake_requested INTEGER NOT NULL DEFAULT 1,
            wake_result TEXT DEFAULT ''
        );

        CREATE INDEX IF NOT EXISTS idx_messages_target ON messages(target_type, target_value, created_at);
        CREATE INDEX IF NOT EXISTS idx_messages_created ON messages(created_at DESC);

        CREATE TABLE IF NOT EXISTS deliveries (
            msg_id INTEGER NOT NULL,
            recipient_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            delivered_at REAL,
            delivered_via_event TEXT DEFAULT '',
            PRIMARY KEY (msg_id, recipient_id)
        );

        CREATE INDEX IF NOT EXISTS idx_deliveries_recipient_status
            ON deliveries(recipient_id, status);
        """
    )


def _normalize_list(values: Any) -> list[str]:
    if not values:
        return []
    if isinstance(values, str):
        try:
            parsed = json.loads(values)
            if isinstance(parsed, list):
                values = parsed
            else:
                values = [values]
        except Exception:
            values = [v.strip() for v in values.split(",") if v.strip()]
    result: list[str] = []
    seen: set[str] = set()
    for item in values:
        s = str(item).strip()
        if s and s not in seen:
            seen.add(s)
            result.append(s)
    return result


def upsert_task_heartbeat(
    conversation_id: str,
    *,
    source: str | None = None,
    status: str = "running",
    cwd: str | None = None,
    workspace_paths: list[str] | None = None,
    model_name: str | None = None,
    last_user_input: str | None = None,
    last_event: str | None = None,
) -> dict[str, Any]:
    if not conversation_id:
        return {}
    now = time.time()
    now_iso = utc_now_iso()
    summary_input = (last_user_input or "").strip()[:240]

    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM tasks WHERE conversation_id = ?", (conversation_id,)
        ).fetchone()
        if row is None:
            conn.execute(
                """
                INSERT INTO tasks (
                    conversation_id, alias, source, status, cwd, workspace_paths,
                    model_name, last_user_input, subscriptions, last_event,
                    registered_at, last_seen_at, last_wake_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    conversation_id,
                    None,
                    source or "unknown",
                    status,
                    cwd or "",
                    json.dumps(workspace_paths or [], ensure_ascii=False),
                    model_name or "",
                    summary_input,
                    "[]",
                    last_event or "",
                    now,
                    now,
                    0.0,
                    now_iso,
                ),
            )
        else:
            new_source = source if (source and source != "unknown") else row["source"]
            new_cwd = cwd if cwd else row["cwd"]
            new_ws = (
                json.dumps(workspace_paths, ensure_ascii=False)
                if workspace_paths
                else row["workspace_paths"]
            )
            new_model = model_name if model_name else row["model_name"]
            new_input = summary_input if summary_input else row["last_user_input"]
            new_event = last_event if last_event else row["last_event"]
            conn.execute(
                """
                UPDATE tasks
                SET source = ?, status = ?, cwd = ?, workspace_paths = ?,
                    model_name = ?, last_user_input = ?, last_event = ?,
                    last_seen_at = ?, updated_at = ?
                WHERE conversation_id = ?
                """,
                (
                    new_source,
                    status,
                    new_cwd,
                    new_ws,
                    new_model,
                    new_input,
                    new_event,
                    now,
                    now_iso,
                    conversation_id,
                ),
            )
        updated = conn.execute(
            "SELECT * FROM tasks WHERE conversation_id = ?", (conversation_id,)
        ).fetchone()
        conn.execute("COMMIT")
        return _task_row_to_dict(updated) if updated else {}


def register_task(
    conversation_id: str,
    *,
    alias: str | None = None,
    subscriptions: list[str] | None = None,
    add_subscriptions: list[str] | None = None,
    remove_subscriptions: list[str] | None = None,
    source: str | None = None,
    cwd: str | None = None,
) -> dict[str, Any]:
    if not conversation_id:
        raise ValueError("conversation_id is required")
    now = time.time()
    now_iso = utc_now_iso()

    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM tasks WHERE conversation_id = ?", (conversation_id,)
        ).fetchone()

        if alias is not None:
            cleaned_alias = alias.strip() or None
            if cleaned_alias:
                # Reassign alias uniquely if another task previously held it
                conn.execute(
                    "UPDATE tasks SET alias = NULL WHERE alias COLLATE NOCASE = ? AND conversation_id != ?",
                    (cleaned_alias, conversation_id),
                )
        else:
            cleaned_alias = row["alias"] if row else None

        existing_subs = _normalize_list(row["subscriptions"] if row else [])
        if subscriptions is not None:
            new_subs = _normalize_list(subscriptions)
        else:
            new_subs = list(existing_subs)
            for ch in _normalize_list(add_subscriptions):
                if ch not in new_subs:
                    new_subs.append(ch)
            rm_set = set(_normalize_list(remove_subscriptions))
            if rm_set:
                new_subs = [c for c in new_subs if c not in rm_set]

        if row is None:
            conn.execute(
                """
                INSERT INTO tasks (
                    conversation_id, alias, source, status, cwd, workspace_paths,
                    model_name, last_user_input, subscriptions, last_event,
                    registered_at, last_seen_at, last_wake_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    conversation_id,
                    cleaned_alias,
                    source or "unknown",
                    "running",
                    cwd or "",
                    "[]",
                    "",
                    "",
                    json.dumps(new_subs, ensure_ascii=False),
                    "register_task",
                    now,
                    now,
                    0.0,
                    now_iso,
                ),
            )
        else:
            conn.execute(
                """
                UPDATE tasks
                SET alias = ?, subscriptions = ?, source = ?, cwd = ?,
                    last_seen_at = ?, updated_at = ?
                WHERE conversation_id = ?
                """,
                (
                    cleaned_alias,
                    json.dumps(new_subs, ensure_ascii=False),
                    source if (source and source != "unknown") else row["source"],
                    cwd if cwd else row["cwd"],
                    now,
                    now_iso,
                    conversation_id,
                ),
            )
        updated = conn.execute(
            "SELECT * FROM tasks WHERE conversation_id = ?", (conversation_id,)
        ).fetchone()
        conn.execute("COMMIT")
        return _task_row_to_dict(updated) if updated else {}


def _task_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    now = time.time()
    age = max(0.0, now - float(row["last_seen_at"] or 0.0))
    effective_status = row["status"]
    if effective_status == "running" and age > RUNNING_FRESH_SECONDS:
        effective_status = "idle"
    return {
        "conversationId": row["conversation_id"],
        "alias": row["alias"] or "",
        "source": row["source"],
        "status": effective_status,
        "rawStatus": row["status"],
        "cwd": row["cwd"] or "",
        "workspacePaths": _normalize_list(row["workspace_paths"]),
        "modelName": row["model_name"] or "",
        "lastUserInput": row["last_user_input"] or "",
        "subscriptions": _normalize_list(row["subscriptions"]),
        "lastEvent": row["last_event"] or "",
        "lastSeenSecondsAgo": round(age, 1),
        "updatedAt": row["updated_at"],
    }


def list_tasks(
    *,
    only_running: bool = False,
    source: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM tasks ORDER BY last_seen_at DESC LIMIT ?", (max(1, min(limit, 200)),)
        ).fetchall()
    result = [_task_row_to_dict(r) for r in rows]
    if only_running:
        result = [t for t in result if t["status"] == "running"]
    if source:
        s = source.strip().lower()
        result = [t for t in result if t["source"].lower() == s]
    return result


def resolve_task(conn: sqlite3.Connection, target: str) -> sqlite3.Row | None:
    target = target.strip()
    if not target:
        return None
    # 1. Exact conversation_id match
    row = conn.execute(
        "SELECT * FROM tasks WHERE conversation_id = ?", (target,)
    ).fetchone()
    if row:
        return row
    # 2. Exact alias match (case-insensitive)
    row = conn.execute(
        "SELECT * FROM tasks WHERE alias COLLATE NOCASE = ? ORDER BY last_seen_at DESC LIMIT 1",
        (target,),
    ).fetchone()
    if row:
        return row
    # 3. Unique prefix match on conversation_id (at least 6 chars)
    if len(target) >= 6:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE conversation_id LIKE ? ORDER BY last_seen_at DESC LIMIT 2",
            (target + "%",),
        ).fetchall()
        if len(rows) == 1:
            return rows[0]
    return None


def _find_agy_executable() -> str | None:
    custom = os.environ.get("TASK_BUS_AGY_BIN")
    if custom and os.path.isfile(custom):
        return custom
    found = shutil.which("agy")
    if found:
        return found
    candidate = os.path.join(
        os.path.expanduser("~"), "AppData", "Local", "agy", "bin", "agy.exe"
    )
    if os.path.isfile(candidate):
        return candidate
    return None


def _enqueue_app_wake(
    target_conv_id: str,
    *,
    message_id: int | None,
    from_label: str,
    subject: str,
    wait_seconds: float = 3.5,
) -> dict[str, Any]:
    queue_dir = get_app_wake_queue_dir()
    results_dir = get_app_wake_results_dir()
    now_ms = int(time.time() * 1000)
    wake_id = f"{now_ms}_{uuid.uuid4().hex[:8]}"
    wake_text = (
        f"[task-message-bus 唤醒] 收到来自独立任务 {from_label} 的新消息"
        + (f"（主题：{subject}）" if subject else "")
        + "，请读取本次上下文中由钩子自动注入的消息内容并处理。"
    )
    payload = {
        "wakeId": wake_id,
        "targetConversationId": target_conv_id,
        "messageId": message_id,
        "fromLabel": from_label,
        "subject": subject,
        "wakeText": wake_text,
        "createdAt": utc_now_iso(),
        "createdAtMs": now_ms,
    }
    tmp_path = os.path.join(queue_dir, f".{wake_id}.tmp")
    final_path = os.path.join(queue_dir, f"{wake_id}.json")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, final_path)

    result_path = os.path.join(results_dir, f"{wake_id}.json")
    deadline = time.time() + max(0.0, float(wait_seconds or 0.0))
    while time.time() < deadline:
        if os.path.isfile(result_path):
            try:
                with open(result_path, "r", encoding="utf-8") as f:
                    res = json.load(f)
                ok = bool(res.get("ok"))
                return {
                    "attempted": True,
                    "woken": ok,
                    "mode": "app_woken_actively" if ok else "app_wake_failed",
                    "wakeId": wake_id,
                    "bridgeResult": res,
                }
            except Exception as exc:
                log_error("read_app_wake_result", exc)
                break
        time.sleep(0.2)

    return {
        "attempted": True,
        "woken": False,
        "mode": "app_wake_queued",
        "wakeId": wake_id,
        "note": "已写入 App 前端唤醒队列，等待 Antigravity App 前端桥接消费并自动打开目标会话提交唤醒（不修改目标模型/推理强度/审核设置）。",
    }


def _wake_cli_task(
    target_conv_id: str,
    *,
    from_label: str,
    subject: str,
    cwd: str = "",
) -> dict[str, Any]:
    agy_bin = _find_agy_executable()
    if not agy_bin:
        return {"woken": False, "reason": "agy_cli_not_found"}

    wake_prompt = (
        f"[task-message-bus 主动唤醒] 你收到来自独立任务 {from_label} 的新消息"
        + (f"（主题：{subject}）" if subject else "")
        + "。请读取并处理本次上下文中由 task-message-bus 钩子自动注入的消息内容。"
    )
    cmd = [agy_bin, "--conversation", target_conv_id, "-p", wake_prompt]

    creationflags = 0
    if os.name == "nt":
        creationflags = (
            getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        )

    work_dir = cwd if (cwd and os.path.isdir(cwd)) else None
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=work_dir,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=creationflags,
        )
        return {"woken": True, "pid": proc.pid, "command": "agy --conversation <id> -p ..."}
    except Exception as exc:
        log_error("wake_cli_task", exc)
        return {"woken": False, "reason": f"spawn_failed: {exc}"}


def send_message(
    *,
    from_conversation_id: str,
    target: str,
    body: str,
    target_type: str = "task",
    subject: str = "",
    priority: str = "normal",
    metadata: dict[str, Any] | None = None,
    auto_wake: bool = True,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> dict[str, Any]:
    body = (body or "").strip()
    if not body:
        raise ValueError("body is required")
    target_type = (target_type or "task").strip().lower()
    if target_type not in ("task", "channel", "broadcast"):
        raise ValueError("target_type must be one of: task, channel, broadcast")
    priority = (priority or "normal").strip().lower()
    if priority not in ("normal", "urgent"):
        priority = "normal"

    now = time.time()
    now_iso = utc_now_iso()
    expires_at = now + max(60, int(ttl_seconds or DEFAULT_TTL_SECONDS))
    msg_uuid = str(uuid.uuid4())

    wake_action: dict[str, Any] = {"attempted": False, "mode": "queued"}
    resolved_target_id = target.strip()
    target_row_dict: dict[str, Any] | None = None

    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")

        sender_row = conn.execute(
            "SELECT alias FROM tasks WHERE conversation_id = ?",
            (from_conversation_id,),
        ).fetchone()
        from_alias = (sender_row["alias"] or "") if sender_row else ""

        target_row: sqlite3.Row | None = None
        if target_type == "task":
            if not resolved_target_id:
                raise ValueError("target is required when target_type='task'")
            target_row = resolve_task(conn, resolved_target_id)
            if target_row is None:
                inferred_src = infer_unregistered_source(resolved_target_id)
                if inferred_src in ("app", "cli"):
                    conn.execute(
                        """
                        INSERT INTO tasks (
                            conversation_id, alias, source, status, cwd, workspace_paths,
                            model_name, last_user_input, subscriptions, last_event,
                            registered_at, last_seen_at, last_wake_at, updated_at
                        ) VALUES (?, NULL, ?, 'idle', '', '[]', '', '', '[]', 'inferred', ?, 0, 0, ?)
                        """,
                        (resolved_target_id, inferred_src, now, now_iso),
                    )
                    target_row = conn.execute(
                        "SELECT * FROM tasks WHERE conversation_id = ?",
                        (resolved_target_id,),
                    ).fetchone()
            if target_row is not None:
                resolved_target_id = target_row["conversation_id"]
                target_row_dict = _task_row_to_dict(target_row)
            target_value = resolved_target_id
        elif target_type == "channel":
            if not resolved_target_id:
                raise ValueError("target (channel name) is required when target_type='channel'")
            target_value = resolved_target_id
        else:
            target_value = "*"

        cur = conn.execute(
            """
            INSERT INTO messages (
                msg_uuid, from_conversation_id, from_alias, target_type, target_value,
                priority, subject, body, metadata, created_at, created_at_iso,
                expires_at, wake_requested, wake_result
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                msg_uuid,
                from_conversation_id or "external",
                from_alias,
                target_type,
                target_value,
                priority,
                (subject or "").strip(),
                body,
                json.dumps(metadata or {}, ensure_ascii=False),
                now,
                now_iso,
                expires_at,
                1 if auto_wake else 0,
                "",
            ),
        )
        msg_id = int(cur.lastrowid)

        if target_type == "task":
            conn.execute(
                """
                INSERT OR IGNORE INTO deliveries (msg_id, recipient_id, status)
                VALUES (?, ?, 'pending')
                """,
                (msg_id, target_value),
            )

        should_wake_cli = False
        should_wake_app = False
        wake_cwd = ""
        if target_type == "task":
            if target_row is None:
                wake_action = {
                    "attempted": False,
                    "mode": "queued_unregistered_target",
                    "note": "目标任务尚未在注册表或本地会话目录中发现；消息已按指定 conversationId 入队，目标活动时会自动收到。",
                }
            else:
                eff_status = target_row_dict["status"]
                src = target_row_dict["source"]
                runtime_check = inspect_target_runtime_state(target_value, src) if src in ("app", "cli") else {"state": "unknown", "shouldWake": True, "reasons": []}
                if eff_status == "running" or runtime_check["state"] == "running":
                    wake_action = {
                        "attempted": False,
                        "mode": "hook_live_running",
                        "runtimeCheck": runtime_check,
                        "note": "目标任务正在运行中，无需发送唤醒消息，将在下一次 PreInvocation / PostInvocation / Stop 钩子自动实时注入。",
                    }
                elif runtime_check["state"] == "waiting_bg" or (
                    eff_status == "waiting_bg" and runtime_check["state"] != "truly_idle"
                ):
                    wake_action = {
                        "attempted": False,
                        "mode": "hook_waiting_background",
                        "runtimeCheck": runtime_check,
                        "note": "目标任务存在持续运行的后台任务/外部任务/子代理/定时等待，跳过唤醒以免阻塞对方消息队列；消息已入队，将在下次钩子触发时自动注入。",
                    }
                elif src == "cli" and auto_wake:
                    last_wake_at = float(target_row["last_wake_at"] or 0.0)
                    if now - last_wake_at < WAKE_COOLDOWN_SECONDS:
                        wake_action = {
                            "attempted": False,
                            "mode": "cli_wake_cooldown",
                            "note": "目标 CLI 任务刚刚被唤醒（冷却期内），消息已入队并将在本次唤醒中一并投递。",
                        }
                    else:
                        should_wake_cli = True
                        wake_cwd = target_row["cwd"] or ""
                        conn.execute(
                            "UPDATE tasks SET last_wake_at = ? WHERE conversation_id = ?",
                            (now, target_value),
                        )
                elif src == "app" and auto_wake:
                    last_wake_at = float(target_row["last_wake_at"] or 0.0)
                    if now - last_wake_at < WAKE_COOLDOWN_SECONDS:
                        wake_action = {
                            "attempted": False,
                            "mode": "app_wake_cooldown",
                            "note": "目标 App 会话刚刚触发过前端唤醒（冷却期内），消息已入队并将在本次唤醒中一并投递。",
                        }
                    else:
                        should_wake_app = True
                        conn.execute(
                            "UPDATE tasks SET last_wake_at = ? WHERE conversation_id = ?",
                            (now, target_value),
                        )
                elif src == "app":
                    wake_action = {
                        "attempted": False,
                        "mode": "queued_app_idle",
                        "note": "目标为已空闲的 Antigravity App 会话（autoWake=false）；消息已持久化入队。",
                    }
                else:
                    wake_action = {
                        "attempted": False,
                        "mode": "queued_target_idle",
                        "note": f"目标任务当前空闲（source={src}），消息已入队待其下次活动时自动投递。",
                    }
        else:
            wake_action = {
                "attempted": False,
                "mode": f"queued_{target_type}",
                "note": "广播/频道消息已写入总线，所有匹配的任务将在下一次钩子触发时自动收到。",
            }

        conn.execute("COMMIT")

    from_label = f"{from_alias} ({from_conversation_id[:8]})" if from_alias else (from_conversation_id[:8] or "external")
    if should_wake_cli:
        wake_res = _wake_cli_task(
            target_value,
            from_label=from_label,
            subject=(subject or "").strip(),
            cwd=wake_cwd,
        )
        wake_action = {
            "attempted": True,
            "mode": "cli_woken_actively" if wake_res.get("woken") else "cli_wake_failed",
            **wake_res,
        }
        with _connect() as conn:
            conn.execute(
                "UPDATE messages SET wake_result = ? WHERE id = ?",
                (json.dumps(wake_action, ensure_ascii=False), msg_id),
            )
    elif should_wake_app:
        wait_sec = float(os.environ.get("TASK_BUS_WAKE_WAIT_SECONDS", "4.0"))
        wake_action = _enqueue_app_wake(
            target_value,
            message_id=msg_id,
            from_label=from_label,
            subject=(subject or "").strip(),
            wait_seconds=wait_sec,
        )
        with _connect() as conn:
            conn.execute(
                "UPDATE messages SET wake_result = ? WHERE id = ?",
                (json.dumps(wake_action, ensure_ascii=False), msg_id),
            )
    else:
        with _connect() as conn:
            conn.execute(
                "UPDATE messages SET wake_result = ? WHERE id = ?",
                (json.dumps(wake_action, ensure_ascii=False), msg_id),
            )

    return {
        "ok": True,
        "messageId": msg_id,
        "messageUuid": msg_uuid,
        "targetType": target_type,
        "target": target_value,
        "targetInfo": target_row_dict,
        "delivery": wake_action,
        "createdAt": now_iso,
    }


def pull_pending_messages(
    conversation_id: str,
    *,
    via_event: str = "manual",
    mark_delivered: bool = True,
    limit: int = 20,
) -> list[dict[str, Any]]:
    if not conversation_id:
        return []
    now = time.time()
    collected: list[dict[str, Any]] = []

    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")

        task_row = conn.execute(
            "SELECT subscriptions, registered_at FROM tasks WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        subscriptions = _normalize_list(task_row["subscriptions"]) if task_row else []
        # Broadcast/channel messages created within 5 minutes before task registration are still visible
        since_ts = float(task_row["registered_at"]) - 300.0 if task_row else (now - 3600.0)

        # 1. Direct task messages
        direct_rows = conn.execute(
            """
            SELECT m.*
            FROM messages m
            JOIN deliveries d ON d.msg_id = m.id
            WHERE d.recipient_id = ?
              AND d.status = 'pending'
              AND m.expires_at > ?
            ORDER BY CASE WHEN m.priority = 'urgent' THEN 0 ELSE 1 END, m.id ASC
            LIMIT ?
            """,
            (conversation_id, now, limit),
        ).fetchall()

        seen_ids: set[int] = set()
        for r in direct_rows:
            seen_ids.add(int(r["id"]))
            collected.append(_msg_row_to_dict(r))

        # 2. Broadcast & subscribed channel messages not yet delivered to this recipient
        remaining = max(0, limit - len(collected))
        if remaining > 0:
            params: list[Any] = [conversation_id, now, since_ts, conversation_id]
            channel_clause = ""
            if subscriptions:
                placeholders = ",".join("?" for _ in subscriptions)
                channel_clause = f" OR (m.target_type = 'channel' AND m.target_value IN ({placeholders}))"
                params.extend(subscriptions)
            params.append(remaining)

            fanout_rows = conn.execute(
                f"""
                SELECT m.*
                FROM messages m
                WHERE m.from_conversation_id != ?
                  AND m.expires_at > ?
                  AND m.created_at >= ?
                  AND NOT EXISTS (
                      SELECT 1 FROM deliveries d
                      WHERE d.msg_id = m.id AND d.recipient_id = ?
                  )
                  AND (m.target_type = 'broadcast'{channel_clause})
                ORDER BY CASE WHEN m.priority = 'urgent' THEN 0 ELSE 1 END, m.id ASC
                LIMIT ?
                """,
                params,
            ).fetchall()

            for r in fanout_rows:
                mid = int(r["id"])
                if mid not in seen_ids:
                    seen_ids.add(mid)
                    collected.append(_msg_row_to_dict(r))

        if mark_delivered and collected:
            for item in collected:
                mid = item["id"]
                conn.execute(
                    """
                    INSERT INTO deliveries (msg_id, recipient_id, status, delivered_at, delivered_via_event)
                    VALUES (?, ?, 'delivered', ?, ?)
                    ON CONFLICT(msg_id, recipient_id) DO UPDATE SET
                        status = 'delivered',
                        delivered_at = excluded.delivered_at,
                        delivered_via_event = excluded.delivered_via_event
                    """,
                    (mid, conversation_id, now, via_event),
                )

        conn.execute("COMMIT")

    if mark_delivered and collected:
        _cancel_pending_app_wakes(conversation_id, via_event=via_event)

    return collected


def _msg_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    meta: Any = {}
    try:
        meta = json.loads(row["metadata"] or "{}")
    except Exception:
        meta = {}
    return {
        "id": int(row["id"]),
        "messageUuid": row["msg_uuid"],
        "fromConversationId": row["from_conversation_id"],
        "fromAlias": row["from_alias"] or "",
        "targetType": row["target_type"],
        "targetValue": row["target_value"],
        "priority": row["priority"],
        "subject": row["subject"] or "",
        "body": row["body"],
        "metadata": meta,
        "createdAt": row["created_at_iso"],
    }


def get_message_history(
    *,
    conversation_id: str | None = None,
    limit: int = 30,
) -> list[dict[str, Any]]:
    lim = max(1, min(int(limit or 30), 200))
    with _connect() as conn:
        if conversation_id:
            rows = conn.execute(
                """
                SELECT m.*
                FROM messages m
                LEFT JOIN deliveries d ON d.msg_id = m.id AND d.recipient_id = ?
                WHERE m.from_conversation_id = ?
                   OR (m.target_type = 'task' AND m.target_value = ?)
                   OR d.recipient_id = ?
                GROUP BY m.id
                ORDER BY m.id DESC
                LIMIT ?
                """,
                (conversation_id, conversation_id, conversation_id, conversation_id, lim),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM messages ORDER BY id DESC LIMIT ?", (lim,)
            ).fetchall()

        results: list[dict[str, Any]] = []
        for r in rows:
            item = _msg_row_to_dict(r)
            d_rows = conn.execute(
                "SELECT recipient_id, status, delivered_at, delivered_via_event FROM deliveries WHERE msg_id = ?",
                (r["id"],),
            ).fetchall()
            item["deliveries"] = [
                {
                    "recipientId": dr["recipient_id"],
                    "status": dr["status"],
                    "deliveredViaEvent": dr["delivered_via_event"] or "",
                }
                for dr in d_rows
            ]
            if r["wake_result"]:
                try:
                    item["wakeResult"] = json.loads(r["wake_result"])
                except Exception:
                    item["wakeResult"] = r["wake_result"]
            results.append(item)
        return results


def format_injected_messages(messages: list[dict[str, Any]], via_event: str) -> str:
    if not messages:
        return ""
    lines = [
        f"[task-message-bus] 你收到了 {len(messages)} 条来自其他独立任务的主动投递消息（触发时机：{via_event}）："
    ]
    for idx, m in enumerate(messages, 1):
        sender = m["fromConversationId"]
        if m.get("fromAlias"):
            sender = f"{m['fromAlias']} ({sender})"
        scope = (
            "点对点"
            if m["targetType"] == "task"
            else (f"频道 #{m['targetValue']}" if m["targetType"] == "channel" else "全员广播")
        )
        prio = " [紧急]" if m.get("priority") == "urgent" else ""
        subj = f" | 主题：{m['subject']}" if m.get("subject") else ""
        lines.append(
            f"\n--- 消息 #{idx} (id={m['id']}, {scope}{prio}{subj}) ---\n"
            f"发件任务: {sender}\n"
            f"发送时间: {m['createdAt']}\n"
            f"内容:\n{m['body']}"
        )
    lines.append(
        "\n请结合当前任务上下文及时处理上述消息；如需回复，可使用 task-message-bus 的 send_task_message 工具（目标填写发件任务的 conversationId 或别名）。"
    )
    return "\n".join(lines)


def wake_task(
    *,
    target: str,
    from_conversation_id: str = "manual",
    subject: str = "",
    force: bool = False,
    wait_seconds: float | None = None,
) -> dict[str, Any]:
    target = (target or "").strip()
    if not target:
        raise ValueError("target is required")
    now = time.time()
    now_iso = utc_now_iso()

    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        sender_row = conn.execute(
            "SELECT alias FROM tasks WHERE conversation_id = ?",
            (from_conversation_id,),
        ).fetchone()
        from_alias = (sender_row["alias"] or "") if sender_row else ""

        target_row = resolve_task(conn, target)
        if target_row is None:
            inferred_src = infer_unregistered_source(target)
            if inferred_src in ("app", "cli"):
                conn.execute(
                    """
                    INSERT INTO tasks (
                        conversation_id, alias, source, status, cwd, workspace_paths,
                        model_name, last_user_input, subscriptions, last_event,
                        registered_at, last_seen_at, last_wake_at, updated_at
                    ) VALUES (?, NULL, ?, 'idle', '', '[]', '', '', '[]', 'inferred', ?, 0, 0, ?)
                    """,
                    (target, inferred_src, now, now_iso),
                )
                target_row = conn.execute(
                    "SELECT * FROM tasks WHERE conversation_id = ?", (target,)
                ).fetchone()

        if target_row is None:
            conn.execute("COMMIT")
            return {"ok": False, "mode": "target_not_found", "target": target}

        target_id = target_row["conversation_id"]
        target_dict = _task_row_to_dict(target_row)
        src = target_dict["source"]
        eff_status = target_dict["status"]
        runtime_check = inspect_target_runtime_state(target_id, src) if (src in ("app", "cli") and not force) else {"state": "unknown", "shouldWake": True, "reasons": []}

        if not force and (eff_status == "running" or runtime_check["state"] == "running"):
            conn.execute("COMMIT")
            return {
                "ok": True,
                "target": target_id,
                "delivery": {
                    "attempted": False,
                    "mode": "hook_live_running",
                    "runtimeCheck": runtime_check,
                    "note": "目标任务正在运行中，无需重复唤醒。",
                },
            }

        if not force and (
            runtime_check["state"] == "waiting_bg"
            or (eff_status == "waiting_bg" and runtime_check["state"] != "truly_idle")
        ):
            conn.execute("COMMIT")
            return {
                "ok": True,
                "target": target_id,
                "delivery": {
                    "attempted": False,
                    "mode": "hook_waiting_background",
                    "runtimeCheck": runtime_check,
                    "note": "目标任务存在持续运行的后台任务/外部任务/子代理/定时等待，跳过唤醒以免阻塞对方消息队列（如需强制唤醒请传 force=true）。",
                },
            }

        last_wake_at = float(target_row["last_wake_at"] or 0.0)
        if not force and (now - last_wake_at < WAKE_COOLDOWN_SECONDS):
            conn.execute("COMMIT")
            return {
                "ok": True,
                "target": target_id,
                "delivery": {
                    "attempted": False,
                    "mode": f"{src}_wake_cooldown",
                    "note": "目标任务处于唤醒冷却期内。",
                },
            }

        pending_row = conn.execute(
            """
            SELECT m.id, m.subject
            FROM messages m
            JOIN deliveries d ON d.msg_id = m.id
            WHERE d.recipient_id = ? AND d.status = 'pending' AND m.expires_at > ?
            ORDER BY m.id DESC LIMIT 1
            """,
            (target_id, now),
        ).fetchone()
        latest_msg_id = int(pending_row["id"]) if pending_row else None
        eff_subject = (subject or "").strip() or (
            str(pending_row["subject"] or "") if pending_row else ""
        )
        wake_cwd = target_row["cwd"] or ""
        conn.execute(
            "UPDATE tasks SET last_wake_at = ? WHERE conversation_id = ?",
            (now, target_id),
        )
        conn.execute("COMMIT")

    from_label = f"{from_alias} ({from_conversation_id[:8]})" if from_alias else (from_conversation_id[:8] or "manual")
    if src == "cli":
        res = _wake_cli_task(target_id, from_label=from_label, subject=eff_subject, cwd=wake_cwd)
        delivery = {
            "attempted": True,
            "mode": "cli_woken_actively" if res.get("woken") else "cli_wake_failed",
            **res,
        }
    elif src == "app":
        wsec = float(
            os.environ.get("TASK_BUS_WAKE_WAIT_SECONDS", "4.0")
            if wait_seconds is None
            else wait_seconds
        )
        delivery = _enqueue_app_wake(
            target_id,
            message_id=latest_msg_id,
            from_label=from_label,
            subject=eff_subject,
            wait_seconds=wsec,
        )
    else:
        delivery = {"attempted": False, "mode": "unsupported_source", "source": src}

    if latest_msg_id is not None:
        with _connect() as conn:
            conn.execute(
                "UPDATE messages SET wake_result = ? WHERE id = ?",
                (json.dumps(delivery, ensure_ascii=False), latest_msg_id),
            )

    return {"ok": True, "target": target_id, "messageId": latest_msg_id, "delivery": delivery}

