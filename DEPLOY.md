# 繆思精工客服系統 — Mac Mini 部署指南

本文件說明如何在 Mac Mini 上部署繆思精工客服系統，使其開機自動啟動、掛掉自動重啟，並透過 Cloudflare Tunnel 對外提供 HTTPS 服務。

---

## 目錄

1. [前置需求](#1-前置需求)
2. [安裝步驟](#2-安裝步驟)
3. [啟動服務](#3-啟動服務)
4. [設定 launchd 開機自啟](#4-設定-launchd-開機自啟)
5. [Cloudflare Tunnel 設定](#5-cloudflare-tunnel-設定)
6. [健康檢查 cron job](#6-健康檢查-cron-job)
7. [驗證部署](#7-驗證部署)
8. [日誌與除錯](#8-日誌與除錯)
9. [常見問題排解](#9-常見問題排解)

---

## 1. 前置需求

| 項目 | 最低版本 | 檢查指令 |
|------|---------|---------|
| macOS | 13 Ventura+ | `sw_vers` |
| Python | 3.10+ | `python3 --version` |
| pip | - | `python3 -m pip --version` |
| Homebrew | - | `brew --version` |

### 安裝 Homebrew（如尚未安裝）

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

### 安裝 Python（如尚未安裝）

```bash
brew install python@3.13
```

---

## 2. 安裝步驟

### 2.1 下載專案

```bash
cd ~/Developer
git clone https://github.com/YOUR_ORG/Muses-AI-CS.git
cd Muses-AI-CS
```

### 2.2 安裝 Python 套件

```bash
python3 -m pip install -r requirements.txt
```

### 2.3 設定環境變數

```bash
cp .env.example .env
```

編輯 `.env`，填入必要的 API Key：

```
EMBEDDING_API_KEY=sk-or-v1-your-key
LLM_API_KEY=sk-or-v1-your-key
META_VERIFY_TOKEN=muses_crafts_2026
META_PAGE_ACCESS_TOKEN=your-page-token
META_APP_SECRET=your-app-secret
PORT=8080
```

### 2.4 確認知識庫向量檔案存在

```bash
ls -la knowledge-vectors.json
```

如果不存在，需要先執行向量化：

```bash
python3 embed_knowledge.py
```

### 2.5 建立日誌目錄

```bash
mkdir -p ~/Library/Logs/muses-chatbot
```

### 2.6 設定腳本執行權限

```bash
chmod +x start_local.sh health_check.sh
```

---

## 3. 啟動服務

### 手動啟動（前台測試）

```bash
./start_local.sh
```

啟動後會看到：

```
========================================
  繆思精工客服系統 — 啟動中
========================================

[OK] 載入 .env 環境變數
[OK] Python 3.13
[OK] 所有套件已就緒
[OK] 必要檔案存在
[OK] 日誌目錄：~/Library/Logs/muses-chatbot

啟動 gunicorn on 0.0.0.0:8080
```

### 快速驗證

```bash
curl http://localhost:8080/health | python3 -m json.tool
```

預期回傳：

```json
{
    "status": "ok",
    "version": "1.0.0",
    "uptime": "0d 0h 0m 5s",
    "entries": 42,
    "sqlite_ok": true,
    "last_successful_reply": null
}
```

---

## 4. 設定 launchd 開機自啟

### 4.1 複製 plist 到 LaunchAgents

```bash
cp com.muses.chatbot.plist ~/Library/LaunchAgents/
```

### 4.2 載入服務

```bash
launchctl load ~/Library/LaunchAgents/com.muses.chatbot.plist
```

### 4.3 確認服務已啟動

```bash
launchctl list | grep muses
```

應看到類似輸出（第一欄是 PID，0 表示正常退出碼）：

```
12345   0   com.muses.chatbot
```

### 4.4 常用 launchctl 指令

```bash
# 停止服務
launchctl unload ~/Library/LaunchAgents/com.muses.chatbot.plist

# 重新啟動
launchctl unload ~/Library/LaunchAgents/com.muses.chatbot.plist
launchctl load ~/Library/LaunchAgents/com.muses.chatbot.plist

# 查看服務狀態（macOS 13+）
launchctl print gui/$(id -u)/com.muses.chatbot
```

> **注意**：launchd 服務需要用戶登入才會運行（LaunchAgents）。
> 如果 Mac Mini 設為自動登入，開機後會自動啟動。
> 設定自動登入：系統設定 → 使用者與群組 → 登入選項 → 自動登入。

---

## 5. Cloudflare Tunnel 設定

Cloudflare Tunnel 可以在不開放任何入站 port 的情況下，讓外部透過自訂域名存取本地服務。

### 5.1 安裝 cloudflared

```bash
brew install cloudflared
```

### 5.2 登入 Cloudflare

```bash
cloudflared tunnel login
```

瀏覽器會開啟 Cloudflare 登入頁面，選擇你管理的域名進行授權。

### 5.3 建立 Tunnel

```bash
cloudflared tunnel create muses-chatbot
```

記下回傳的 **Tunnel ID**（格式：`xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`）。

### 5.4 建立設定檔

建立 `~/.cloudflared/config.yml`：

```yaml
tunnel: <YOUR_TUNNEL_ID>
credentials-file: /Users/zhongliyuanshiqi/.cloudflared/<YOUR_TUNNEL_ID>.json

ingress:
  - hostname: chatbot.yourdomain.com
    service: http://localhost:8080
  - service: http_status:404
```

將 `chatbot.yourdomain.com` 替換為你要使用的子域名。

### 5.5 設定 DNS

```bash
cloudflared tunnel route dns muses-chatbot chatbot.yourdomain.com
```

這會在 Cloudflare DNS 自動建立一筆 CNAME 記錄。

### 5.6 測試 Tunnel

```bash
cloudflared tunnel run muses-chatbot
```

然後從外部瀏覽器存取 `https://chatbot.yourdomain.com/health`。

### 5.7 設定 cloudflared 為 launchd 服務

```bash
cloudflared service install
```

這會自動建立 `~/Library/LaunchAgents/com.cloudflare.cloudflared.plist`。

如果需要手動控制：

```bash
# 啟動
launchctl load ~/Library/LaunchAgents/com.cloudflare.cloudflared.plist

# 停止
launchctl unload ~/Library/LaunchAgents/com.cloudflare.cloudflared.plist

# 確認狀態
launchctl list | grep cloudflare
```

### 5.8 設定 Messenger Webhook URL

在 Meta Developer 後台：

1. 進入你的 App → Messenger → Settings
2. Webhook URL 填入：`https://chatbot.yourdomain.com/webhook`
3. Verify Token 填入你在 `.env` 設定的 `META_VERIFY_TOKEN`
4. 訂閱事件：`messages`, `messaging_postbacks`

---

## 6. 健康檢查 cron job

每 5 分鐘檢查一次服務狀態，異常時發送 macOS 通知。

### 設定 crontab

```bash
crontab -e
```

加入以下行：

```cron
*/5 * * * * /Users/zhongliyuanshiqi/Developer/Muses-AI-CS/health_check.sh
```

### 查看健康檢查日誌

```bash
tail -f ~/Library/Logs/muses-chatbot/health_check.log
```

---

## 7. 驗證部署

完成所有設定後，依序執行以下檢查：

```bash
# 1. 確認 chatbot 服務運行中
launchctl list | grep muses

# 2. 健康檢查
curl -s http://localhost:8080/health | python3 -m json.tool

# 3. 測試對話
curl -s -X POST http://localhost:8080/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "你好", "user_id": "test"}' | python3 -m json.tool

# 4. 確認 Cloudflare Tunnel 運行中
launchctl list | grep cloudflare

# 5. 外部存取測試（從另一台設備或手機）
curl -s https://chatbot.yourdomain.com/health

# 6. 確認日誌正常寫入
ls -la ~/Library/Logs/muses-chatbot/
```

---

## 8. 日誌與除錯

### 日誌檔案位置

| 檔案 | 內容 |
|------|------|
| `~/Library/Logs/muses-chatbot/chatbot.log` | 應用程式主日誌（RotatingFileHandler, 10MB x 5） |
| `~/Library/Logs/muses-chatbot/stdout.log` | launchd stdout 輸出 |
| `~/Library/Logs/muses-chatbot/stderr.log` | launchd stderr 輸出 |
| `~/Library/Logs/muses-chatbot/health_check.log` | 健康檢查記錄 |

### 即時查看日誌

```bash
# 應用程式日誌
tail -f ~/Library/Logs/muses-chatbot/chatbot.log

# launchd 輸出
tail -f ~/Library/Logs/muses-chatbot/stdout.log

# 全部日誌
tail -f ~/Library/Logs/muses-chatbot/*.log
```

### 日誌輪替

應用程式日誌（chatbot.log）自動輪替：
- 每個檔案最大 10MB
- 保留最近 5 個：`chatbot.log`, `chatbot.log.1`, ..., `chatbot.log.5`
- 由 Python `RotatingFileHandler` 管理，無需額外設定

launchd 的 stdout/stderr 日誌不會自動輪替，可加入 cron job 定期清理：

```bash
# 每週日凌晨清空 launchd 日誌
0 0 * * 0 : > ~/Library/Logs/muses-chatbot/stdout.log && : > ~/Library/Logs/muses-chatbot/stderr.log
```

---

## 9. 常見問題排解

### Q: launchctl load 報錯 "service already loaded"

```bash
launchctl unload ~/Library/LaunchAgents/com.muses.chatbot.plist
launchctl load ~/Library/LaunchAgents/com.muses.chatbot.plist
```

### Q: gunicorn 找不到（command not found）

確認 PATH 包含 pip 安裝路徑：

```bash
which gunicorn
```

如果裝在 Homebrew Python 下，編輯 `com.muses.chatbot.plist` 的 PATH 環境變數，加入實際路徑：

```bash
python3 -c "import site; print(site.getusersitepackages())"
```

### Q: 服務啟動後馬上停止（循環重啟）

檢查 stderr 日誌：

```bash
cat ~/Library/Logs/muses-chatbot/stderr.log
```

常見原因：
- `.env` 檔案不存在
- Python 套件未安裝
- `knowledge-vectors.json` 不存在
- port 8080 被占用（`lsof -i :8080`）

### Q: Cloudflare Tunnel 連不上

```bash
# 確認 tunnel 狀態
cloudflared tunnel info muses-chatbot

# 手動執行看錯誤訊息
cloudflared tunnel run muses-chatbot

# 確認 DNS 設定
dig chatbot.yourdomain.com
```

### Q: Messenger Webhook 驗證失敗

1. 確認 `META_VERIFY_TOKEN` 在 `.env` 和 Meta Developer 後台一致
2. 確認 Cloudflare Tunnel 正常運行
3. 測試：`curl "https://chatbot.yourdomain.com/webhook?hub.mode=subscribe&hub.verify_token=muses_crafts_2026&hub.challenge=test123"`

### Q: 記憶體使用過高

gunicorn 預設 2 workers，每個約佔 200-400MB（含知識庫向量）。如果 Mac Mini 記憶體不足，可改為 1 worker：

編輯 `start_local.sh`，將 `--workers 2` 改為 `--workers 1`。

### Q: Mac Mini 休眠導致服務中斷

系統設定 → 電池 / 能源節約器：
- 勾選「防止電腦自動休眠」
- 勾選「當顯示器關閉時，防止自動進入睡眠」

或用指令：

```bash
sudo pmset -a disablesleep 1
sudo pmset -a sleep 0
```
