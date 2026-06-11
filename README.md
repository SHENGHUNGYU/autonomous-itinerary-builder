# 自主式旅遊行程排定 Agent

> 使用者輸入目的地、天數、預算、偏好與交通方式，由 **LangGraph 原生多代理系統**自動研究、規劃、驗證駕車時間並比價，產出完整行程表、預算明細與每日路線地圖。

採用 **LangGraph（狀態機 + 條件 Loop）+ 多代理（Researcher / Planner / RouteValidator / Booker / Supervisor）**，LLM 透過**自架 vLLM（OpenAI 相容 API）**做結構化輸出與工具編排。完整規格與架構圖見 [Spec.md](./Spec.md)。

## 架構一覽

```
parse_input → research → plan → validate_route → booker → supervisor
                                                              ├─ proceed_to_output → format_output → END
                                                              ├─ retry_planner  → plan（含回饋）
                                                              ├─ retry_research → research
                                                              └─ fail           → format_output
```

- **Researcher**：ReAct agent，先用 **Serper** 關鍵字搜尋撈部落格/社群 URL，再用 **Firecrawl** 解析這些頁面擷取景點與美食做 grounding。
- **Planner**：vLLM `guided_json` 單次結構化生成每日行程。
- **RouteValidator**：以 **Google Maps Routes API** 確定性計算每日駕車時間並產生 polyline。**預設不設硬上限**（僅計算/顯示）；當使用者透過回饋設定 `max_daily_drive_minutes` 時才當硬約束檢查。
- **Booker**：ReAct agent，編排 **機票（fast-flights）/ 飯店（SerpAPI google_hotels）/ 租車（mock 1500 元/天）** 比價，並用 Serper 估算大眾運輸/門票費用做預算檢查（不預訂）。
- **Supervisor**：LLM 決策放行或重新規劃，retry 上限 3 次。
- **結束後微調**：跑完後可用自然語言回饋（例「希望每天開車不超過 90 分鐘」），由 `Feedback Agent`（LLM + regex fallback）解析成參數，呼叫 `run_planner_refine` **沿用既有 research/booker、只重跑 Planner→Validate→Supervisor**；前端以 `st.session_state` 保存上一輪 state 與回饋歷史。

### 工具分工

| 工具 | 用途 |
|---|---|
| **Serper** | 一般關鍵字搜尋：撈部落格/社群 URL 給 Firecrawl 解析；估算大眾運輸/景點門票費用 |
| **SerpAPI** | 僅用於 `google_hotels` 飯店比價 |
| **Google Maps Routes API** | 每日真實駕車時間 + 距離 + polyline |
| **fast-flights** | 機票比價（免 API key） |
| **Firecrawl** | 解析 Serper 撈到的 URL，結構化擷取景點/美食 |
| **租車** | 固定 1500 元/天 mock 估價 |

## 快速開始

```bash
# 1. 安裝依賴（uv）
uv sync

# 2. 設定環境變數
cp .env.example .env
#   - 接自架 vLLM：設定 OPENAI_BASE_URL / OPENAI_MODEL（缺 /v1 會自動補上）
#   - MOCK_TOOLS=1：景點/飯店/路線用離線模擬資料（仍呼叫 vLLM 做規劃）

# 3. 端到端 demo（命令列）
make mock-demo

# 4. 互動式 UI（推薦）
make ui

# 5. 離線測試（不需 vLLM）
uv run python -m pytest tests/ -v
```

UI 可自訂目的地（九州/大阪/東京/沖繩或任意輸入）、日期、天數、預算、偏好與交通方式，並即時顯示思考軌跡、行程表、預算明細、飯店比價與每日駕車路線地圖。

## 設定（.env）

| 變數 | 說明 |
|---|---|
| `OPENAI_BASE_URL` | vLLM OpenAI 相容端點，例 `http://host:8000/v1`（缺 `/v1` 會自動補） |
| `OPENAI_MODEL` | vLLM 載入的模型名（建議支援 tool-calling，如 Qwen3） |
| `OPENAI_API_KEY` | vLLM 不驗，填任意值即可 |
| `LLM_MAX_TOKENS` | 單次回應上限（預設 4096） |
| `REQUEST_TIMEOUT_SECONDS` | LLM 逾時；完整行程生成約 40–60 秒，勿設太短 |
| `MOCK_TOOLS` | `1` 時外部工具走離線模擬資料 |
| `SERPER_API_KEY` | 一般搜尋（撈 URL + 費用估算） |
| `SERPAPI_KEY` | 僅飯店比價（google_hotels） |
| `GOOGLE_MAPS_API_KEY` | Routes API 駕車時間 + polyline |
| `FIRECRAWL_API_KEY` | 解析搜尋到的 URL 擷取景點/美食（缺則 fallback mock） |
| `DEFAULT_ORIGIN_AIRPORT` | 機票出發機場（預設 `TPE`） |
| `LANGSMITH_*` | 可選的 tracing |

> 機票走 `fast-flights`（免 key）；租車為固定 1500 元/天 mock；大眾運輸/門票由 Serper 估算。

> **Qwen3 注意**：本系統預設關閉 thinking 模式（`enable_thinking=False`）。thinking 會在回答前生成大量推理 token，對結構化輸出既慢又容易撞 token 上限導致 JSON 被截斷。

## 重要檔案

- `Spec.md` — 需求規格 + 系統/代理/工作流程架構圖
- `src/core/graph.py` — LangGraph 多代理狀態機
- `src/agents/*.py` — Researcher / Planner / RouteValidator / Booker / Supervisor / Feedback（回饋解析）
- `src/core/graph.py` 的 `run_planner_refine` / `build_refine_graph` — 結束後依回饋微調（跳過 research/booker）
- `src/services/llm.py` — vLLM 連線 + `call_structured` 結構化輸出
- `src/tools/{maps,web_research,flights}.py` — LangChain `@tool`（Routes / Serper+SerpAPI+Firecrawl / fast-flights，含 mock 模式）
- `ui/streamlit_app.py` — 互動介面 + folium 地圖
- `tests/test_graph_no_llm.py` — 離線回歸測試（守住 retry 有界終止）
- `Makefile` — `make mock-demo` / `make ui` / `make test`

---

