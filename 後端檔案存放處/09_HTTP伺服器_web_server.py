"""
繆思精工客服系統 — Web Server

Flask HTTP 伺服器，提供以下 endpoints：
- GET  /health   → 健康檢查
- POST /chat     → 客服對話 API
- GET  /webhook  → Meta Messenger 驗證
- POST /webhook  → Meta Messenger 訊息處理
"""

import hashlib
import hmac
import json
import logging
import os
import sys
import time

from flask import Flask, request, jsonify
import requests as http_requests

import rag_config
import chatbot_service

# ============================================================
# Flask App 初始化
# ============================================================

app = Flask(__name__)

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# 環境變數
META_VERIFY_TOKEN = os.environ.get("META_VERIFY_TOKEN", "muses_crafts_2026")
META_PAGE_ACCESS_TOKEN = os.environ.get("META_PAGE_ACCESS_TOKEN", "")
META_APP_SECRET = os.environ.get("META_APP_SECRET", "")
PORT = int(os.environ.get("PORT", 8080))

# 向量資料庫載入狀態
_entries_count = 0


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
    """健康檢查。"""
    return jsonify({
        "status": "ok",
        "version": "1.0.0",
        "entries": _entries_count,
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

    try:
        result = chatbot_service.process_message(user_id, message)
        return jsonify({
            "reply": result["reply"],
            "intent": result["intent"],
            "confidence": result.get("confidence", 0.0),
            "source": result.get("source", "unknown"),
            "references": [],  # TODO: 未來可加入引用資料
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
                # 呼叫客服系統處理
                result = chatbot_service.process_message(sender_id, message_text)
                reply_text = result["reply"]

                # 用 Meta Send API 回覆
                _send_messenger_reply(sender_id, reply_text)

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
