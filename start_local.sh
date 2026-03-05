#!/usr/bin/env bash
# ============================================================
# 繆思精工客服系統 — 本地啟動腳本
#
# 用法：
#   chmod +x start_local.sh
#   ./start_local.sh
# ============================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# ── 顏色 ──
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  繆思精工客服系統 — 啟動中${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""

# ── 1. 載入 .env ──
if [ -f "$SCRIPT_DIR/.env" ]; then
    echo -e "${GREEN}[OK]${NC} 載入 .env 環境變數"
    set -a
    source "$SCRIPT_DIR/.env"
    set +a
else
    echo -e "${RED}[ERROR]${NC} 找不到 .env 檔案"
    echo "  請複製 .env.example 為 .env 並填入 API Key："
    echo "  cp .env.example .env"
    exit 1
fi

# ── 2. 檢查 Python 版本 ──
PYTHON_CMD=""
for cmd in python3 python; do
    if command -v "$cmd" &>/dev/null; then
        PYTHON_CMD="$cmd"
        break
    fi
done

if [ -z "$PYTHON_CMD" ]; then
    echo -e "${RED}[ERROR]${NC} 找不到 Python，請先安裝 Python 3.10+"
    exit 1
fi

PYTHON_VERSION=$($PYTHON_CMD -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
MAJOR=$($PYTHON_CMD -c "import sys; print(sys.version_info.major)")
MINOR=$($PYTHON_CMD -c "import sys; print(sys.version_info.minor)")

if [ "$MAJOR" -lt 3 ] || { [ "$MAJOR" -eq 3 ] && [ "$MINOR" -lt 10 ]; }; then
    echo -e "${RED}[ERROR]${NC} Python 版本 $PYTHON_VERSION 太舊，需要 3.10+"
    exit 1
fi
echo -e "${GREEN}[OK]${NC} Python $PYTHON_VERSION"

# ── 3. 檢查必要套件 ──
MISSING=0
for pkg in flask gunicorn requests numpy; do
    if ! $PYTHON_CMD -c "import $pkg" 2>/dev/null; then
        echo -e "${RED}[MISSING]${NC} $pkg"
        MISSING=1
    fi
done

if [ "$MISSING" -eq 1 ]; then
    echo ""
    echo -e "${YELLOW}安裝缺少的套件...${NC}"
    $PYTHON_CMD -m pip install -r "$SCRIPT_DIR/requirements.txt"
    echo ""
fi
echo -e "${GREEN}[OK]${NC} 所有套件已就緒"

# ── 4. 檢查必要檔案 ──
for f in web_server.py knowledge-vectors.json; do
    if [ ! -f "$SCRIPT_DIR/$f" ]; then
        echo -e "${RED}[ERROR]${NC} 找不到 $f"
        exit 1
    fi
done
echo -e "${GREEN}[OK]${NC} 必要檔案存在"

# ── 5. 檢查 API Key ──
if [ -z "${LLM_API_KEY:-}" ]; then
    echo -e "${YELLOW}[WARN]${NC} LLM_API_KEY 未設定，RAG 回覆功能將無法使用"
fi

# ── 6. 建立日誌目錄 ──
LOG_DIR="$HOME/Library/Logs/muses-chatbot"
mkdir -p "$LOG_DIR"
echo -e "${GREEN}[OK]${NC} 日誌目錄：$LOG_DIR"

# ── 7. 啟動 gunicorn ──
echo ""
echo -e "${GREEN}啟動 gunicorn on 0.0.0.0:${PORT:-8080}${NC}"
echo "  按 Ctrl+C 停止"
echo ""

exec gunicorn \
    --workers 2 \
    --bind "0.0.0.0:${PORT:-8080}" \
    --access-logfile - \
    --error-logfile - \
    --timeout 120 \
    --preload \
    web_server:app
