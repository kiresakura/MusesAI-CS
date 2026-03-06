"""
繆思精工客服系統 — Web Server

Flask HTTP 伺服器，提供以下 endpoints：
- GET  /health   → 健康檢查（含運行時間、SQLite 狀態、最後成功回覆時間）
- POST /chat     → 客服對話 API
- GET  /webhook  → Meta Messenger 驗證
- POST /webhook  → Meta Messenger 訊息處理
"""

import hashlib
import hmac
import json
import logging
import logging.handlers
import os
import sqlite3
import sys
import time

from datetime import datetime, timedelta
from flask import Flask, request, jsonify, send_file
import requests as http_requests

import rag_config
import chatbot_service
import chat_mode
import user_tags
import broadcast
from error_handler import DB_PATH

# ============================================================
# Flask App 初始化
# ============================================================

app = Flask(__name__)

# ============================================================
# Logging（RotatingFileHandler + stdout）
# ============================================================

_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
_LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"
_LOG_DIR = os.path.expanduser("~/Library/Logs/muses-chatbot")
_LOG_FILE = os.path.join(_LOG_DIR, "chatbot.log")

os.makedirs(_LOG_DIR, exist_ok=True)

_root_logger = logging.getLogger()
_root_logger.setLevel(logging.INFO)

# 檔案日誌：10MB per file, 保留最近 5 個
_file_handler = logging.handlers.RotatingFileHandler(
    _LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8",
)
_file_handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_LOG_DATEFMT))
_root_logger.addHandler(_file_handler)

# stdout 日誌（方便即時除錯）
_stream_handler = logging.StreamHandler(sys.stdout)
_stream_handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_LOG_DATEFMT))
_root_logger.addHandler(_stream_handler)

logger = logging.getLogger(__name__)

# 環境變數
META_VERIFY_TOKEN = os.environ.get("META_VERIFY_TOKEN", "muses_crafts_2026")
META_PAGE_ACCESS_TOKEN = os.environ.get("META_PAGE_ACCESS_TOKEN", "")
META_APP_SECRET = os.environ.get("META_APP_SECRET", "")
PORT = int(os.environ.get("PORT", 8080))

# 運行狀態追蹤
_entries_count = 0
_start_time = time.time()
_last_successful_reply = None


# ============================================================
# 啟動預載
# ============================================================

def preload_knowledge():
    """啟動時預載 knowledge-vectors.json 到記憶體。"""
    global _entries_count
    logger.info("📂 預載知識庫...")

    try:
        from rag_search import load_vector_db
        vector_db = load_vector_db(rag_config.VECTORS_OUTPUT_PATH)
        _entries_count = len(vector_db)

        # 透過 chatbot_service 的延遲載入機制，觸發載入
        chatbot_service._ensure_rag_loaded()
        logger.info(f"✅ 知識庫載入完成：{_entries_count} 筆")
    except Exception as e:
        logger.error(f"❌ 知識庫載入失敗：{e}")
        _entries_count = 0


# ============================================================
# Request Logging Middleware
# ============================================================

@app.before_request
def log_request():
    """記錄每個請求。"""
    logger.info(f"→ {request.method} {request.path} [{request.remote_addr}]")


@app.after_request
def log_response(response):
    """記錄回應狀態碼。"""
    logger.info(f"← {response.status_code} {request.method} {request.path}")
    return response


# ============================================================
# Endpoints
# ============================================================

@app.route("/health", methods=["GET"])
def health():
    """
    健康檢查端點。

    回傳：服務狀態、運行時間、知識庫筆數、SQLite 連線狀態、最後成功回覆時間。
    """
    uptime_seconds = time.time() - _start_time
    days, remainder = divmod(int(uptime_seconds), 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    uptime_str = f"{days}d {hours}h {minutes}m {seconds}s"

    # SQLite 連線測試
    sqlite_ok = False
    try:
        conn = sqlite3.connect(DB_PATH, timeout=3)
        conn.execute("SELECT 1")
        conn.close()
        sqlite_ok = True
    except Exception:
        pass

    status = "ok" if (sqlite_ok and _entries_count > 0) else "degraded"

    return jsonify({
        "status": status,
        "version": "1.0.0",
        "uptime": uptime_str,
        "uptime_seconds": round(uptime_seconds),
        "entries": _entries_count,
        "sqlite_ok": sqlite_ok,
        "last_successful_reply": _last_successful_reply,
    })


@app.route("/chat", methods=["POST"])
def chat():
    """
    客服對話 API。

    Request:  {"message": "...", "user_id": "..."}
    Response: {"reply": "...", "intent": "...", "references": [...]}
    """
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Invalid JSON"}), 400

    message = data.get("message", "").strip()
    user_id = data.get("user_id", "anonymous")

    if not message:
        return jsonify({"error": "message is required"}), 400

    logger.info(f"💬 [chat] user={user_id} message={message[:80]}")

    # ── 人工介入檢查 ──
    if chat_mode.is_manual(user_id):
        chatbot_service._add_to_history(user_id, "user", message)
        mode_info = chat_mode.get_mode(user_id)
        return jsonify({
            "reply": None,
            "intent": "manual_hold",
            "confidence": 1.0,
            "source": "manual_hold",
            "attachments": [],
            "follow_up_messages": [],
            "manual_mode": mode_info,
        })

    # channel 可由呼叫端指定（預設 universal）
    channel = data.get("channel", "universal")

    try:
        global _last_successful_reply
        result = chatbot_service.process_message(user_id, message, channel=channel)
        _last_successful_reply = time.strftime("%Y-%m-%d %H:%M:%S")
        return jsonify({
            "reply": result["reply"],
            "intent": result["intent"],
            "confidence": result.get("confidence", 0.0),
            "source": result.get("source", "unknown"),
            "attachments": result.get("attachments", []),
            "follow_up_messages": result.get("follow_up_messages", []),
            "references": [],
        })
    except Exception as e:
        logger.error(f"❌ [chat] 處理失敗：{e}")
        return jsonify({
            "reply": "系統暫時忙碌中，請稍後再試 🙏",
            "intent": "error",
            "references": [],
        }), 500


# ============================================================
# Meta Messenger Webhook
# ============================================================

@app.route("/webhook", methods=["GET"])
def webhook_verify():
    """
    Meta Messenger Webhook 驗證。

    Meta 會發送 GET 請求驗證 webhook URL，
    需要比對 hub.verify_token 並回傳 hub.challenge。
    """
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == META_VERIFY_TOKEN:
        logger.info("✅ Webhook 驗證成功")
        return challenge, 200
    else:
        logger.warning(f"❌ Webhook 驗證失敗：mode={mode}, token={token}")
        return "Forbidden", 403


@app.route("/webhook", methods=["POST"])
def webhook_receive():
    """
    Meta Messenger Webhook 接收訊息。

    流程：
    1. 驗證 X-Hub-Signature-256（如果有 APP_SECRET）
    2. 解析 messaging events
    3. 呼叫 chatbot_service 處理
    4. 用 Meta Send API 回覆
    """
    # ── 驗證簽名 ──
    if META_APP_SECRET:
        signature = request.headers.get("X-Hub-Signature-256", "")
        if not _verify_signature(request.data, signature):
            logger.warning("❌ Webhook 簽名驗證失敗")
            return "Invalid signature", 403

    # ── 解析 payload ──
    data = request.get_json(silent=True)
    if not data:
        return "OK", 200

    if data.get("object") != "page":
        return "OK", 200

    # ── 處理每個 messaging event ──
    for entry in data.get("entry", []):
        for event in entry.get("messaging", []):
            sender_id = event.get("sender", {}).get("id")
            message_obj = event.get("message", {})
            message_text = message_obj.get("text")

            # 跳過非文字訊息（圖片、貼圖等）
            if not sender_id or not message_text:
                continue

            logger.info(f"📩 [webhook] sender={sender_id} message={message_text[:80]}")

            # ── 人工介入檢查：manual 模式只記錄不回覆 ──
            if chat_mode.is_manual(sender_id):
                logger.info(f"⏸️ [webhook] sender={sender_id} is in manual mode, skipping AI reply")
                chatbot_service._add_to_history(sender_id, "user", message_text)
                continue

            try:
                global _last_successful_reply
                # 呼叫客服系統處理（Messenger 來源固定為 fb）
                result = chatbot_service.process_message(
                    sender_id, message_text, channel="fb"
                )
                _last_successful_reply = time.strftime("%Y-%m-%d %H:%M:%S")
                # 主回覆
                _send_messenger_reply(sender_id, result["reply"])

                # 身分確認後的 must-send cascade（SR-001、SR-040、SR-022 等）
                for follow_up in result.get("follow_up_messages", []):
                    _send_messenger_reply(sender_id, follow_up["content"])

            except Exception as e:
                logger.error(f"❌ [webhook] 處理失敗：{e}")
                _send_messenger_reply(
                    sender_id,
                    "系統暫時忙碌中，請稍後再試 🙏"
                )

    return "OK", 200


# ============================================================
# Meta Messenger 工具函數
# ============================================================

def _verify_signature(payload: bytes, signature: str) -> bool:
    """驗證 Meta Webhook 的 X-Hub-Signature-256。"""
    if not signature.startswith("sha256="):
        return False

    expected = hmac.new(
        META_APP_SECRET.encode("utf-8"),
        payload,
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(f"sha256={expected}", signature)


def _send_messenger_reply(recipient_id: str, text: str):
    """
    透過 Meta Send API 回覆訊息。

    POST https://graph.facebook.com/v21.0/me/messages
    """
    if not META_PAGE_ACCESS_TOKEN:
        logger.warning("⚠️  META_PAGE_ACCESS_TOKEN 未設定，無法回覆 Messenger")
        return

    url = "https://graph.facebook.com/v21.0/me/messages"
    headers = {"Content-Type": "application/json"}
    params = {"access_token": META_PAGE_ACCESS_TOKEN}

    # Messenger 訊息長度限制 2000 字元，超過就截斷
    if len(text) > 2000:
        text = text[:1997] + "..."

    payload = {
        "recipient": {"id": recipient_id},
        "message": {"text": text},
    }

    try:
        response = http_requests.post(
            url,
            headers=headers,
            params=params,
            json=payload,
            timeout=10,
        )

        if response.status_code != 200:
            logger.error(f"❌ Send API 錯誤 {response.status_code}: {response.text[:200]}")
        else:
            logger.info(f"✅ 已回覆 {recipient_id}")

    except Exception as e:
        logger.error(f"❌ Send API 請求失敗：{e}")


# ============================================================
# 管理後台
# ============================================================

@app.route("/")
def admin_page():
    """管理後台首頁。"""
    return send_file(os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html"))


# ============================================================
# 對話管理 API
# ============================================================

@app.route("/api/conversations/recent", methods=["GET"])
def api_recent_conversations():
    """最近 24 小時有對話的用戶列表。"""
    try:
        cutoff = (datetime.now() - timedelta(hours=24)).isoformat()
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute("""
            SELECT ch.user_id, ch.content, ch.role, ch.timestamp
            FROM conversation_history ch
            INNER JOIN (
                SELECT user_id, MAX(id) as max_id
                FROM conversation_history
                WHERE timestamp > ?
                GROUP BY user_id
            ) latest ON ch.id = latest.max_id
            ORDER BY ch.timestamp DESC
        """, (cutoff,)).fetchall()
        conn.close()

        users = []
        for row in rows:
            uid, content, role, ts = row
            mode_info = chat_mode.get_mode(uid)
            users.append({
                "user_id": uid,
                "last_message": content[:50],
                "last_role": role,
                "last_time": ts,
                "mode": mode_info["mode"],
                "switched_by": mode_info["switched_by"],
                "auto_revert_at": mode_info["auto_revert_at"],
                "remaining_seconds": mode_info["remaining_seconds"],
            })
        return jsonify({"users": users})
    except Exception as e:
        logger.error(f"[api/conversations/recent] error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/conversations/<user_id>/history", methods=["GET"])
def api_conversation_history(user_id):
    """取得用戶完整對話歷史。"""
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute(
            "SELECT role, content, timestamp FROM conversation_history "
            "WHERE user_id = ? ORDER BY id ASC",
            (user_id,),
        ).fetchall()
        conn.close()

        messages = [
            {"role": r[0], "content": r[1], "timestamp": r[2]}
            for r in rows
        ]
        mode_info = chat_mode.get_mode(user_id)
        return jsonify({"user_id": user_id, "messages": messages, "mode": mode_info})
    except Exception as e:
        logger.error(f"[api/conversations/history] error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/conversations/updates", methods=["GET"])
def api_conversation_updates():
    """增量更新：回傳 since 之後的新訊息 + 所有 manual 用戶的模式狀態。"""
    since = request.args.get("since", "")
    if not since:
        return jsonify({"error": "since parameter is required"}), 400

    try:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute(
            "SELECT user_id, role, content, timestamp FROM conversation_history "
            "WHERE timestamp > ? ORDER BY id ASC",
            (since,),
        ).fetchall()
        conn.close()

        messages = [
            {"user_id": r[0], "role": r[1], "content": r[2], "timestamp": r[3]}
            for r in rows
        ]

        # 回傳有新訊息的用戶 + 所有 manual 用戶的模式狀態
        affected = set(m["user_id"] for m in messages)
        manual_users = chat_mode.list_manual_users()
        for mu in manual_users:
            affected.add(mu["user_id"])

        modes = {}
        for uid in affected:
            modes[uid] = chat_mode.get_mode(uid)

        return jsonify({
            "messages": messages,
            "modes": modes,
            "server_time": datetime.now().isoformat(),
        })
    except Exception as e:
        logger.error(f"[api/conversations/updates] error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/conversations/<user_id>/send", methods=["POST"])
def api_send_message(user_id):
    """手動發送訊息給用戶（透過 Messenger Send API）並記錄到對話歷史。"""
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Invalid JSON"}), 400

    message = data.get("message", "").strip()
    if not message:
        return jsonify({"error": "message is required"}), 400

    _send_messenger_reply(user_id, message)
    chatbot_service._add_to_history(user_id, "assistant", message)
    ts = datetime.now().isoformat()

    return jsonify({"success": True, "message": message, "timestamp": ts})


# ============================================================
# 人工介入 API
# ============================================================

@app.route("/api/chat-mode", methods=["POST"])
def api_set_chat_mode():
    """
    切換用戶聊天模式。

    Request:  {"user_id": "xxx", "mode": "manual", "operator": "小王"}
    Response: 切換後的狀態
    """
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Invalid JSON"}), 400

    user_id = data.get("user_id", "").strip()
    mode = data.get("mode", "").strip()
    operator = data.get("operator", "").strip() or None

    if not user_id:
        return jsonify({"error": "user_id is required"}), 400
    if mode not in ("auto", "manual"):
        return jsonify({"error": "mode must be 'auto' or 'manual'"}), 400

    try:
        result = chat_mode.set_mode(user_id, mode, operator)
        return jsonify(result)
    except Exception as e:
        logger.error(f"[api/chat-mode] error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/chat-mode/<user_id>", methods=["GET"])
def api_get_chat_mode(user_id):
    """查詢用戶當前聊天模式。"""
    return jsonify(chat_mode.get_mode(user_id))


@app.route("/api/chat-mode/list", methods=["GET"])
def api_list_manual_users():
    """列出所有目前處於 manual 模式的用戶。"""
    users = chat_mode.list_manual_users()
    return jsonify({"count": len(users), "users": users})


@app.route("/api/chat-mode/extend", methods=["POST"])
def api_extend_manual():
    """
    延長手動模式時間。

    Request:  {"user_id": "xxx", "minutes": 30}
    Response: 更新後的狀態（含新的 auto_revert_at）
    """
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Invalid JSON"}), 400

    user_id = data.get("user_id", "").strip()
    minutes = data.get("minutes")

    if not user_id:
        return jsonify({"error": "user_id is required"}), 400
    if not isinstance(minutes, (int, float)) or minutes <= 0:
        return jsonify({"error": "minutes must be a positive number"}), 400

    try:
        result = chat_mode.extend_manual(user_id, int(minutes))
        return jsonify(result)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error(f"[api/chat-mode/extend] error: {e}")
        return jsonify({"error": str(e)}), 500


# ============================================================
# 用戶標籤 API
# ============================================================

@app.route("/api/users/<user_id>/tags", methods=["GET"])
def api_get_user_tags(user_id):
    """查看用戶標籤。"""
    tags = user_tags.get_tags(user_id)
    return jsonify({"user_id": user_id, "tags": tags})


@app.route("/api/users/<user_id>/tags", methods=["POST"])
def api_add_user_tag(user_id):
    """新增標籤。body: {"tag": "tv_wall"}"""
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Invalid JSON"}), 400

    tag = data.get("tag", "").strip()
    if not tag:
        return jsonify({"error": "tag is required"}), 400

    if tag not in user_tags.ALL_VALID_TAGS:
        return jsonify({
            "error": f"Invalid tag: {tag}",
            "valid_tags": user_tags.VALID_TAGS,
        }), 400

    user_tags.add_tag(user_id, tag, tagged_by="manual")
    return jsonify({"success": True, "tags": user_tags.get_tags(user_id)})


@app.route("/api/users/<user_id>/tags/<tag>", methods=["DELETE"])
def api_remove_user_tag(user_id, tag):
    """移除標籤。"""
    removed = user_tags.remove_tag(user_id, tag)
    if not removed:
        return jsonify({"error": "Tag not found"}), 404
    return jsonify({"success": True, "tags": user_tags.get_tags(user_id)})


@app.route("/api/tags/definitions", methods=["GET"])
def api_tag_definitions():
    """回傳所有可用標籤定義（供前端下拉選單用）。"""
    return jsonify({"tags": user_tags.VALID_TAGS, "labels": user_tags.TAG_LABELS})


# ============================================================
# 廣播 API
# ============================================================

@app.route("/api/broadcast", methods=["POST"])
def api_create_broadcast():
    """
    建立廣播任務。

    body: {
        "name": "年前促銷",
        "target_tags": ["designer", "tv_wall"],
        "match_mode": "any",
        "message_content": "新春優惠...",
        "scheduled_at": "2026-03-07T10:00:00",
        "created_by": "小王"
    }
    """
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Invalid JSON"}), 400

    name = data.get("name", "").strip()
    target_tags = data.get("target_tags", [])
    match_mode = data.get("match_mode", "any")
    message_content = data.get("message_content", "").strip()
    scheduled_at = data.get("scheduled_at", "").strip()
    created_by = data.get("created_by", "").strip() or None
    attachment_url = data.get("attachment_url", "").strip() or None

    if not name:
        return jsonify({"error": "name is required"}), 400
    if not target_tags:
        return jsonify({"error": "target_tags is required"}), 400
    if match_mode not in ("any", "all"):
        return jsonify({"error": "match_mode must be 'any' or 'all'"}), 400
    if not message_content:
        return jsonify({"error": "message_content is required"}), 400
    if not scheduled_at:
        return jsonify({"error": "scheduled_at is required"}), 400

    task_id = broadcast.create_task(
        name=name, target_tags=target_tags, match_mode=match_mode,
        message_content=message_content, scheduled_at=scheduled_at,
        created_by=created_by, attachment_url=attachment_url,
    )
    return jsonify({"success": True, "task_id": task_id})


@app.route("/api/broadcast/list", methods=["GET"])
def api_list_broadcasts():
    """任務列表。"""
    tasks = broadcast.list_tasks()
    return jsonify({"tasks": tasks})


@app.route("/api/broadcast/preview", methods=["GET"])
def api_preview_broadcast():
    """預覽目標用戶數。query: tags=tv_wall,designer&match_mode=any"""
    tags_str = request.args.get("tags", "")
    match_mode = request.args.get("match_mode", "any")

    if not tags_str:
        return jsonify({"error": "tags parameter is required"}), 400

    tags = [t.strip() for t in tags_str.split(",") if t.strip()]
    result = broadcast.preview_targets(tags, match_mode)
    return jsonify(result)


@app.route("/api/broadcast/<task_id>", methods=["DELETE"])
def api_cancel_broadcast(task_id):
    """取消 pending 任務。"""
    ok = broadcast.cancel_task(task_id)
    if not ok:
        return jsonify({"error": "Task not found or not pending"}), 404
    return jsonify({"success": True, "task_id": task_id})


# ============================================================
# 主程式
# ============================================================

if __name__ == "__main__":
    preload_knowledge()
    chatbot_service.cleanup_expired_history()
    chat_mode.start_auto_revert_scheduler(interval_seconds=60)
    broadcast.start_broadcast_scheduler(interval_seconds=60)
    logger.info(f"🚀 繆思精工客服系統啟動 — port {PORT}")
    app.run(host="0.0.0.0", port=PORT, debug=False)
