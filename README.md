# MusesAI-CS

**繆思精工 AI 客服系統** — 基於 RAG 架構的 Messenger 智慧客服機器人

為石材裝潢公司「繆思精工」打造的 AI 客服後端，整合知識庫檢索與大型語言模型，能自動回覆產品規格、花色、報價、施工服務等常見問題。

## 系統架構

```
用戶訊息（Messenger / API）
  │
  ▼
意圖辨識（keyword + LLM zero-shot）
  │
  ├─ greeting   → 制式問候回覆
  ├─ transfer   → 轉人工客服
  ├─ pricing / spec / catalog / service / store
  │     └─ RAG 搜尋知識庫 → LLM 生成回覆
  └─ other      → fallback 引導
```

## 技術棧

| 組件 | 技術 |
|---|---|
| LLM | Google Gemini 3.1 Pro（via OpenRouter） |
| Embedding | OpenAI text-embedding-3-large（via OpenRouter） |
| 向量搜尋 | NumPy 餘弦相似度（JSON 本地向量庫） |
| Web 框架 | Flask |
| 資料儲存 | SQLite（對話歷史 + 錯誤日誌） |
| 部署 | Docker / Fly.io |
| 對外接口 | REST API + Meta Messenger Webhook |

## 專案結構

```
MusesAI-CS/
├── rag_config.py          # 集中設定檔（API、prompt、參數）
├── embed_knowledge.py     # 知識庫向量化腳本
├── rag_search.py          # RAG 搜尋引擎（embedding + 向量檢索 + LLM）
├── intent_classifier.py   # 意圖辨識（keyword 快取 + LLM 分類）
├── chatbot_service.py     # 客服主服務（對話管理 + 流程編排）
├── error_handler.py       # 錯誤處理與 fallback
├── web_server.py          # Flask HTTP 伺服器 + Messenger Webhook
├── knowledge-vectors.json # 預計算的向量資料庫
├── requirements.txt       # Python 依賴
├── Dockerfile             # Docker 映像設定
├── fly.toml               # Fly.io 部署設定
├── .env.example           # 環境變數範本
└── .gitignore
```

## 快速開始

### 前置需求

- Python 3.12+
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

必要環境變數：

| 變數 | 說明 |
|---|---|
| `EMBEDDING_API_KEY` | OpenRouter Embedding API 金鑰 |
| `LLM_API_KEY` | OpenRouter LLM API 金鑰 |

選填環境變數：

| 變數 | 說明 | 預設值 |
|---|---|---|
| `META_VERIFY_TOKEN` | Messenger Webhook 驗證 token | `muses_crafts_2026` |
| `META_PAGE_ACCESS_TOKEN` | Facebook Page Access Token | （空） |
| `META_APP_SECRET` | Facebook App Secret | （空） |
| `PORT` | 伺服器埠號 | `8080` |

### 準備知識庫

如需重新向量化知識庫：

```bash
# 將知識庫 CSV 放在專案目錄，然後執行：
python embed_knowledge.py
```

### 啟動伺服器

```bash
# 開發模式
python web_server.py

# Production（推薦）
gunicorn -w 2 -b 0.0.0.0:8080 web_server:app
```

### CLI 測試

```bash
# 互動式對話測試
python chatbot_service.py

# 自動測試各意圖路徑
python chatbot_service.py --auto
```

## API 端點

### `GET /health`

健康檢查。

```json
{"status": "ok", "version": "1.0.0", "entries": 108}
```

### `POST /chat`

客服對話 API。

**Request:**
```json
{"message": "繆思岩一坪多少錢？", "user_id": "user_123"}
```

**Response:**
```json
{
  "reply": "繆思岩每坪參考價格約...",
  "intent": "pricing",
  "confidence": 0.92,
  "source": "rag"
}
```

### `GET /webhook` & `POST /webhook`

Meta Messenger Webhook 端點（驗證 + 訊息接收）。

## Docker 部署

```bash
docker build -t muses-chatbot .
docker run -p 8080:8080 --env-file .env muses-chatbot
```

## Fly.io 部署

```bash
fly launch
fly secrets set EMBEDDING_API_KEY=your-key LLM_API_KEY=your-key
fly deploy
```

## 意圖類別

| 意圖 | 說明 | 處理方式 |
|---|---|---|
| `greeting` | 打招呼、問候 | 制式回覆（不呼叫 API） |
| `transfer` | 轉人工、投訴 | 制式回覆 + 聯絡資訊 |
| `pricing` | 詢價、報價 | RAG + LLM |
| `spec` | 規格、材質、特性 | RAG + LLM |
| `catalog` | 花色、色系、產品目錄 | RAG + LLM |
| `service` | 施工、熱彎、配送 | RAG + LLM |
| `store` | 商城、購買 | RAG + LLM |
| `other` | 無法歸類 | Fallback 引導 |

## 授權條款

本專案採用 [CC BY-NC 4.0](./LICENSE) 授權。

- 允許：學習、研究、個人使用、修改、分享
- 禁止：商業用途

詳見 [LICENSE](./LICENSE) 及 [PRIVACY_POLICY.md](./PRIVACY_POLICY.md)。
