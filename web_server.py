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

from flask import Flask, request, jsonify
import requests as http_requests

import rag_config
import chatbot_service
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
# 主程式
# ============================================================

if __name__ == "__main__":
    preload_knowledge()
    chatbot_service.cleanup_expired_history()
    logger.info(f"🚀 繆思精工客服系統啟動 — port {PORT}")
    app.run(host="0.0.0.0", port=PORT, debug=False)
