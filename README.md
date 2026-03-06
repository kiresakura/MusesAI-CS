# MusesAI-CS

**繆思精工 AI 客服系統** — 基於 RAG 架構的 Messenger 智慧客服機器人

為石材裝潢公司「繆思精工」打造的 AI 客服後端，整合意圖辨識、預存語錄、知識庫檢索與大型語言模型，能自動回覆產品規格、花色、報價、施工服務等常見問題，並支援多輪對話、用戶身分識別與狀態追蹤。

## 系統架構

```
用戶訊息（Messenger / REST API）
  │
  ▼
┌─────────────────────────┐
│  意圖辨識（15 類意圖）     │  keyword scoring 優先，fallback LLM zero-shot
│  + 複合意圖 + 身分偵測    │
└────────┬────────────────┘
         │
  ┌──────┴──────────────────────────────────────┐
  │                                              │
  ▼                                              ▼
快速路徑                                    智慧回覆路徑
  │                                              │
  ├─ greeting  → 制式問候                    ┌────┴────┐
  ├─ transfer  → 轉人工客服                  │ 語錄匹配  │  48 條預存語錄
  ├─ identity  → 身分確認 + must-send       │（優先）   │  依身分/渠道過濾
  │               cascade                   └────┬────┘
  └─ 信心度低  → fallback 引導                    │ 無命中
                                                  ▼
                                            ┌──────────┐
                                            │ RAG + LLM │  向量檢索 → LLM 生成
                                            └──────────┘
```

### 對話狀態機

```
new（新用戶）
  │ 偵測到身分
  ▼
identified（身分已知）
  │
  ├─ 產品關鍵字 → inquiring_product（電視牆/熱彎/一體盆）
  ├─ 拜訪意圖   → inquiring_visit
  └─ 報價意圖   → pending_info → 提供資訊後回到 identified
```

## 功能總覽

### 核心客服

- **意圖辨識**：15 類意圖，keyword scoring 優先，LLM fallback
- **預存語錄**：48 條語錄依身分/渠道過濾匹配
- **RAG 問答**：向量檢索 + LLM 生成回覆
- **多輪對話**：用戶狀態機追蹤身分、產品興趣、對話階段

### 人工介入（chat_mode）

- 客服人員可將用戶切換為「手動模式」，AI 暫停回覆
- 手動模式預設 60 分鐘後自動回到 AI 模式
- 支援延長手動時間（+30 分鐘）
- 背景排程器每 60 秒檢查過期的手動模式

### 用戶標籤系統（user_tags）

- 4 大類 15 個標籤：身分、產品興趣、狀態、區域
- AI 自動打標：根據訊息關鍵字自動標記用戶
- 手動打標：管理後台可新增/移除標籤
- 標籤查詢：支援 any（OR）/ all（AND）匹配模式

### 廣播排程（broadcast）

- 建立定時廣播任務：指定標籤 + 訊息內容 + 排程時間
- 預覽目標用戶數（含 24 小時去重排除）
- 背景排程器每 60 秒檢查待發任務
- 逐一發送（1 秒間隔，遵守 API rate limit）
- 支援中途取消

### Web 管理後台

- 純 HTML + CSS + JS 單頁應用（深色主題）
- **對話頁**：用戶列表、聊天氣泡、模式切換、手動回覆、標籤管理
- **廣播頁**：任務列表、新增表單（標籤選擇、匹配模式、排程）、任務詳情
- 5 秒輪詢即時更新

## 技術棧

| 組件 | 技術 |
|------|------|
| LLM | Qwen 3.5 397B-A17B MoE（via OpenRouter） |
| Embedding | OpenAI text-embedding-3-large（via OpenRouter） |
| 向量搜尋 | NumPy 餘弦相似度（JSON 本地向量庫） |
| Web 框架 | Flask + Gunicorn |
| 資料儲存 | SQLite WAL（對話歷史 + 用戶狀態 + 錯誤日誌 + 標籤 + 廣播） |
| 前端 | 純 HTML + vanilla JS + CSS（單一 index.html） |
| 日誌 | RotatingFileHandler（10MB x 5）+ stdout |
| 部署 | macOS launchd / Docker / Fly.io |
| 對外穿透 | Cloudflare Tunnel |
| 對外接口 | REST API + Meta Messenger Webhook |

## 專案結構

```
MusesAI-CS/
├── web_server.py            # Flask HTTP 伺服器 + Messenger Webhook + 管理 API
├── chatbot_service.py       # 客服主服務（對話管理 + 流程編排 + 自動打標）
├── intent_classifier.py     # 意圖辨識（15 類，keyword scoring + LLM fallback）
├── scripted_responses.py    # 預存語錄匹配引擎
├── scripted_responses.json  # 預存語錄資料庫（48 條）
├── user_state.py            # 用戶身分識別 + 對話狀態機 + 追問邏輯
├── chat_mode.py             # 人工介入模式管理（手動/自動切換 + 自動回歸）
├── user_tags.py             # 用戶標籤系統（CRUD + 關鍵字自動打標）
├── broadcast.py             # 廣播任務排程（建立 + 執行 + 去重 + rate limit）
├── rag_config.py            # 集中設定檔（API key、prompt、參數）
├── rag_search.py            # RAG 搜尋引擎（embedding + 向量檢索 + LLM）
├── embed_knowledge.py       # 知識庫向量化腳本
├── error_handler.py         # 錯誤處理與 fallback
├── knowledge-vectors.json   # 預計算的向量資料庫
├── index.html               # Web 管理後台（單頁應用）
│
├── start_local.sh           # 本地啟動腳本（gunicorn + .env 載入 + 前置檢查）
├── com.muses.chatbot.plist  # macOS launchd 服務（開機自啟 + 自動重啟）
├── health_check.sh          # 健康檢查 cron job（異常發 macOS 通知）
├── DEPLOY.md                # Mac Mini 部署完整指南
│
├── Dockerfile               # Docker 映像設定
├── fly.toml                 # Fly.io 部署設定
├── requirements.txt         # Python 依賴
├── .env.example             # 環境變數範本
├── .gitignore
├── LICENSE                  # CC BY-NC 4.0
└── PRIVACY_POLICY.md        # 隱私權政策
```

## 快速開始

### 前置需求

- Python 3.10+
- [OpenRouter](https://openrouter.ai/) API Key

### 安裝

```bash
git clone https://github.com/kiresakura/MusesAI-CS.git
cd MusesAI-CS
pip install -r requirements.txt
```

### 設定環境變數

```bash
cp .env.example .env
# 編輯 .env 填入你的 API Key
```

**必要環境變數：**

| 變數 | 說明 |
|------|------|
| `EMBEDDING_API_KEY` | OpenRouter Embedding API 金鑰 |
| `LLM_API_KEY` | OpenRouter LLM API 金鑰 |

**選填環境變數：**

| 變數 | 說明 | 預設值 |
|------|------|--------|
| `META_VERIFY_TOKEN` | Messenger Webhook 驗證 token | `muses_crafts_2026` |
| `META_PAGE_ACCESS_TOKEN` | Facebook Page Access Token | （空） |
| `META_APP_SECRET` | Facebook App Secret | （空） |
| `PORT` | 伺服器埠號 | `8080` |

### 準備知識庫

如需重新向量化知識庫：

```bash
python embed_knowledge.py
```

### 啟動伺服器

```bash
# 本地啟動（推薦，含前置檢查）
chmod +x start_local.sh
./start_local.sh

# 或手動用 gunicorn
gunicorn -w 2 -b 0.0.0.0:8080 web_server:app

# 開發模式
python web_server.py
```

### CLI 測試

```bash
# 互動式對話測試
python chatbot_service.py

# 自動測試各意圖路徑
python chatbot_service.py --auto

# 意圖辨識單元測試
python intent_classifier.py
```

## API 端點

### 客服 API

| 方法 | 路徑 | 說明 |
|------|------|------|
| `GET` | `/health` | 健康檢查 |
| `POST` | `/chat` | 客服對話 |
| `GET/POST` | `/webhook` | Messenger Webhook |

### 管理後台 API

| 方法 | 路徑 | 說明 |
|------|------|------|
| `GET` | `/` | Web 管理後台 |
| `GET` | `/api/conversations/recent` | 最近對話列表 |
| `GET` | `/api/conversations/<user_id>/history` | 對話歷史 |
| `GET` | `/api/conversations/updates` | 增量更新 |
| `POST` | `/api/conversations/<user_id>/send` | 手動發送訊息 |

### 人工介入 API

| 方法 | 路徑 | 說明 |
|------|------|------|
| `POST` | `/api/chat-mode` | 切換手動/自動模式 |
| `GET` | `/api/chat-mode/<user_id>` | 查詢用戶模式 |
| `GET` | `/api/chat-mode/list` | 列出手動模式用戶 |
| `POST` | `/api/chat-mode/extend` | 延長手動時間 |

### 標籤 API

| 方法 | 路徑 | 說明 |
|------|------|------|
| `GET` | `/api/tags/definitions` | 取得標籤定義 |
| `GET` | `/api/users/<user_id>/tags` | 查詢用戶標籤 |
| `POST` | `/api/users/<user_id>/tags` | 新增標籤 |
| `DELETE` | `/api/users/<user_id>/tags/<tag>` | 移除標籤 |

### 廣播 API

| 方法 | 路徑 | 說明 |
|------|------|------|
| `POST` | `/api/broadcast` | 建立廣播任務 |
| `GET` | `/api/broadcast/list` | 列出廣播任務 |
| `GET` | `/api/broadcast/preview` | 預覽目標用戶數 |
| `DELETE` | `/api/broadcast/<task_id>` | 取消廣播任務 |

## 意圖辨識系統

支援 15 類意圖，關鍵字快速匹配優先，無命中時才呼叫 LLM（節省 API 費用）。

| 意圖 | 說明 | 處理方式 |
|------|------|---------|
| `greeting` | 打招呼、問候 | 制式回覆 |
| `transfer` | 轉人工、投訴 | 制式回覆 + 聯絡資訊 |
| `identity` | 自報身分（設計師/屋主/廠商） | 身分語錄 + must-send cascade |
| `pricing` | 詢價、報價 | 語錄 / RAG + LLM |
| `spec` | 規格、尺寸、特性 | 語錄 / RAG + LLM |
| `catalog` | 花色、型錄、產品目錄 | 語錄 / RAG + LLM |
| `service` | 施工、安裝、配送 | 語錄 / RAG + LLM |
| `store` | 商城、購買 | 語錄 / RAG + LLM |
| `visit` | 拜訪倉庫、看實品 | 語錄 / RAG + LLM |
| `hot_bend` | 熱彎加工 | 語錄 / RAG + LLM |
| `basin` | 一體盆 | 語錄 / RAG + LLM |
| `tv_wall` | 電視牆 | 語錄 / RAG + LLM |
| `material` | 材質成分、製程 | 語錄 / RAG + LLM |
| `promotion` | 優惠、折扣、活動 | 語錄 / RAG + LLM |
| `other` | 無法歸類 | Fallback 引導 |

**複合意圖支援：** 同一訊息可觸發多個意圖，主意圖依優先級排序，次要意圖列入 `sub_intents`。

**身分偵測：** 訊息中提及身分關鍵字時，`identity_hint` 回傳 `designer` / `manufacturer` / `owner`。

## 部署

### Mac Mini 長期運行（推薦）

使用 macOS launchd 服務 + Cloudflare Tunnel：

```bash
# 安裝為系統服務（開機自啟 + 掛掉自動重啟）
cp com.muses.chatbot.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.muses.chatbot.plist
```

完整部署步驟（含 Cloudflare Tunnel、健康檢查 cron、日誌管理）請參閱 **[DEPLOY.md](./DEPLOY.md)**。

### Docker

```bash
docker build -t muses-chatbot .
docker run -p 8080:8080 --env-file .env muses-chatbot
```

### Fly.io

```bash
fly launch
fly secrets set EMBEDDING_API_KEY=your-key LLM_API_KEY=your-key
fly deploy
```

## 授權條款

本專案採用 [CC BY-NC 4.0](./LICENSE) 授權。

- 允許：學習、研究、個人使用、修改、分享
- 禁止：商業用途

詳見 [LICENSE](./LICENSE) 及 [PRIVACY_POLICY.md](./PRIVACY_POLICY.md)。
