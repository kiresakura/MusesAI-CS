#!/usr/bin/env bash
# ============================================================
# 繆思精工客服系統 — 健康檢查 cron job
#
# 用法：每 5 分鐘執行一次
#   crontab -e
#   */5 * * * * /Users/zhongliyuanshiqi/Developer/Muses-AI-CS/health_check.sh
# ============================================================

HEALTH_URL="http://localhost:8080/health"
LOG_FILE="$HOME/Library/Logs/muses-chatbot/health_check.log"

mkdir -p "$(dirname "$LOG_FILE")"

TIMESTAMP=$(date "+%Y-%m-%d %H:%M:%S")

# 發送健康檢查請求（5 秒超時）
RESPONSE=$(curl -s --max-time 5 "$HEALTH_URL" 2>/dev/null)
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "$HEALTH_URL" 2>/dev/null)

if [ "$HTTP_CODE" = "200" ]; then
    STATUS=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status','unknown'))" 2>/dev/null || echo "parse_error")
    if [ "$STATUS" = "ok" ]; then
        echo "$TIMESTAMP [OK] status=$STATUS" >> "$LOG_FILE"
    else
        echo "$TIMESTAMP [WARN] status=$STATUS (degraded)" >> "$LOG_FILE"
        # macOS 通知（degraded 狀態）
        osascript -e "display notification \"服務狀態: $STATUS\" with title \"繆思客服系統警告\"" 2>/dev/null || true
    fi
else
    echo "$TIMESTAMP [ERROR] HTTP $HTTP_CODE — 服務無回應" >> "$LOG_FILE"
    # macOS 通知（服務掛掉）
    osascript -e 'display notification "健康檢查失敗，服務可能已停止" with title "繆思客服系統錯誤"' 2>/dev/null || true
fi

# 保留最近 2000 行日誌
tail -n 2000 "$LOG_FILE" > "$LOG_FILE.tmp" && mv "$LOG_FILE.tmp" "$LOG_FILE"
