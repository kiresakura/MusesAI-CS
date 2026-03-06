"""
繆思精工客服系統 — 廣播任務系統

功能：
1. 建立定時廣播任務（指定標籤 + 訊息內容 + 排程時間）
2. 背景排程器每分鐘檢查 pending 任務
3. 執行時根據標籤查用戶、排除 24h 內已收過的、逐一發送
4. 每發一條間隔 1 秒（遵守 API rate limit）
"""

import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta

import requests as http_requests

import user_tags
from error_handler import DB_PATH

logger = logging.getLogger(__name__)

META_PAGE_ACCESS_TOKEN = os.environ.get("META_PAGE_ACCESS_TOKEN", "")


# ============================================================
# SQLite
# ============================================================

def _get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS broadcast_tasks (
            task_id         TEXT PRIMARY KEY,
            name            TEXT NOT NULL,
            target_tags     TEXT NOT NULL,
            match_mode      TEXT NOT NULL DEFAULT 'any',
            message_content TEXT NOT NULL,
            attachment_url  TEXT,
            scheduled_at    TEXT NOT NULL,
            status          TEXT NOT NULL DEFAULT 'pending',
            created_by      TEXT,
            created_at      TEXT NOT NULL,
            sent_count      INTEGER DEFAULT 0,
            fail_count      INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS broadcast_log (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            sent_at TEXT NOT NULL,
            success INTEGER NOT NULL DEFAULT 1
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_bcast_log_user ON broadcast_log(user_id, sent_at)"
    )
    conn.commit()
    return conn


# ============================================================
# CRUD
# ============================================================

def _row_to_dict(r) -> dict:
    return {
        "task_id": r[0], "name": r[1], "target_tags": json.loads(r[2]),
        "match_mode": r[3], "message_content": r[4], "attachment_url": r[5],
        "scheduled_at": r[6], "status": r[7], "created_by": r[8],
        "created_at": r[9], "sent_count": r[10], "fail_count": r[11],
    }


_SELECT_COLS = (
    "task_id, name, target_tags, match_mode, message_content, "
    "attachment_url, scheduled_at, status, created_by, created_at, "
    "sent_count, fail_count"
)


def create_task(
    name: str,
    target_tags: list[str],
    match_mode: str,
    message_content: str,
    scheduled_at: str,
    created_by: str | None = None,
    attachment_url: str | None = None,
) -> str:
    task_id = uuid.uuid4().hex[:8]
    now = datetime.now().isoformat()

    conn = _get_db()
    conn.execute(
        f"INSERT INTO broadcast_tasks ({_SELECT_COLS}) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, 0, 0)",
        (task_id, name, json.dumps(target_tags, ensure_ascii=False),
         match_mode, message_content, attachment_url,
         scheduled_at, created_by, now),
    )
    conn.commit()
    conn.close()

    logger.info(f"broadcast: created task {task_id} '{name}' at {scheduled_at}")
    return task_id


def list_tasks(limit: int = 50) -> list[dict]:
    conn = _get_db()
    rows = conn.execute(
        f"SELECT {_SELECT_COLS} FROM broadcast_tasks ORDER BY created_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [_row_to_dict(r) for r in rows]


def cancel_task(task_id: str) -> bool:
    conn = _get_db()
    cur = conn.execute(
        "UPDATE broadcast_tasks SET status='cancelled' WHERE task_id=? AND status='pending'",
        (task_id,),
    )
    conn.commit()
    conn.close()
    return cur.rowcount > 0


def preview_targets(tags: list[str], match_mode: str) -> dict:
    all_users = user_tags.get_users_by_tags(tags, match_mode)
    excluded = _get_recent_recipients()
    targets = [u for u in all_users if u not in excluded]
    return {
        "total_matched": len(all_users),
        "excluded": len(excluded),
        "will_send": len(targets),
        "user_ids": targets[:20],
    }


# ============================================================
# 執行
# ============================================================

def _get_recent_recipients() -> set[str]:
    cutoff = (datetime.now() - timedelta(hours=24)).isoformat()
    conn = _get_db()
    rows = conn.execute(
        "SELECT DISTINCT user_id FROM broadcast_log WHERE sent_at > ?", (cutoff,)
    ).fetchall()
    conn.close()
    return {r[0] for r in rows}


def _send_message(user_id: str, text: str) -> bool:
    token = META_PAGE_ACCESS_TOKEN or os.environ.get("META_PAGE_ACCESS_TOKEN", "")
    if not token:
        logger.warning(f"broadcast: no PAGE_ACCESS_TOKEN, skip {user_id}")
        return False

    try:
        resp = http_requests.post(
            "https://graph.facebook.com/v21.0/me/messages",
            params={"access_token": token},
            json={"recipient": {"id": user_id}, "message": {"text": text[:2000]}},
            timeout=10,
        )
        ok = resp.status_code == 200
        if not ok:
            logger.error(f"broadcast: send failed {user_id} status={resp.status_code}")
        return ok
    except Exception as e:
        logger.error(f"broadcast: send error {user_id}: {e}")
        return False


def _execute_task(task: dict):
    task_id = task["task_id"]
    logger.info(f"broadcast: executing {task_id} '{task['name']}'")

    conn = _get_db()
    conn.execute("UPDATE broadcast_tasks SET status='running' WHERE task_id=?", (task_id,))
    conn.commit()
    conn.close()

    targets = user_tags.get_users_by_tags(task["target_tags"], task["match_mode"])
    excluded = _get_recent_recipients()
    targets = [u for u in targets if u not in excluded]

    logger.info(f"broadcast: {task_id} sending to {len(targets)} users")

    sent = 0
    failed = 0

    for uid in targets:
        # Check cancellation
        conn = _get_db()
        row = conn.execute(
            "SELECT status FROM broadcast_tasks WHERE task_id=?", (task_id,)
        ).fetchone()
        conn.close()
        if row and row[0] == "cancelled":
            logger.info(f"broadcast: {task_id} cancelled mid-run")
            break

        ok = _send_message(uid, task["message_content"])

        conn = _get_db()
        conn.execute(
            "INSERT INTO broadcast_log (task_id, user_id, sent_at, success) VALUES (?,?,?,?)",
            (task_id, uid, datetime.now().isoformat(), 1 if ok else 0),
        )
        conn.commit()
        conn.close()

        if ok:
            sent += 1
        else:
            failed += 1

        time.sleep(1)

    conn = _get_db()
    conn.execute(
        "UPDATE broadcast_tasks SET status='completed', sent_count=?, fail_count=? "
        "WHERE task_id=? AND status='running'",
        (sent, failed, task_id),
    )
    conn.commit()
    conn.close()

    logger.info(f"broadcast: {task_id} done sent={sent} failed={failed}")


# ============================================================
# 排程器
# ============================================================

def check_and_run_pending():
    now = datetime.now().isoformat()
    conn = _get_db()
    rows = conn.execute(
        f"SELECT {_SELECT_COLS} FROM broadcast_tasks "
        "WHERE status='pending' AND scheduled_at <= ?",
        (now,),
    ).fetchall()
    conn.close()

    for r in rows:
        task = _row_to_dict(r)
        threading.Thread(
            target=_execute_task, args=(task,), daemon=True,
            name=f"bcast-{task['task_id']}",
        ).start()


_scheduler_thread = None


def start_broadcast_scheduler(interval_seconds: int = 60):
    global _scheduler_thread

    def _loop():
        while True:
            try:
                check_and_run_pending()
            except Exception as e:
                logger.error(f"broadcast scheduler error: {e}")
            time.sleep(interval_seconds)

    _scheduler_thread = threading.Thread(target=_loop, daemon=True, name="broadcast-sched")
    _scheduler_thread.start()
    logger.info(f"broadcast scheduler started (every {interval_seconds}s)")
