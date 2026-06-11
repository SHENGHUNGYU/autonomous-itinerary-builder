---
name: researcher
description: 蒐集目的地景點、美食與部落格 grounding；Serper 雙查詢 + Firecrawl 結構化擷取，過濾餐飲雜訊。
---

# Researcher Skill

## 目標

為 Planner 產出可信的 `ResearchBundle`：景點池、餐飲池、部落格來源、區域聚類與路線提示。

## 工具鏈（必須依序）

1. **Serper 行程查詢**：`{destination} 自駕 行程 攻略 推薦`
2. **Serper 美食查詢**（獨立）：`{destination} 美食 餐廳 必吃 推薦`
3. **URL 去重**：同站 `.com` / `.cn` 同 path 只 scrape 一次
4. **Firecrawl extract**：對 top-2 部落格 + top-2 美食文做結構化擷取
5. **後處理**：`build_region_clusters` + `build_route_hint`（見 `src/agents/geo.py`）

## 餐飲品質規則

- `meal_type` 僅允許 `breakfast | lunch | dinner | snack`
- 拉麵、屋台、一蘭等不得標為 `breakfast` → 改為 `lunch`
- 餐廳 `location` 須落在目的地相關區域（過濾異地雜訊）
- 同名餐廳去重；TW/CN 同文只保留一筆

## 景點規則

- 優先自駕友善類型：神社、自然、溫泉、小鎮、景觀道路
- `location` 填具體縣市/區（例：福岡市、由布院），勿只填大地區名
- 門票 `est_cost_twd` 合理估算；無資料可填 0

## 輸出契約

回傳 `ResearchBundle` 欄位：

| 欄位 | 說明 |
|------|------|
| `attractions` | 8–15 筆，含 name/type/location/停留/門票 |
| `meals` | 10–15 筆，三餐均衡 |
| `blog_sources` | 每篇含 title/url/key_takeaways |
| `region_clusters` | 子區域 + 建議晚數 + hub_city |
| `route_hint` | 建議環狀/南北向移動順序 |

## Mock 模式

`MOCK_TOOLS=1` 時走 fixture，不呼叫外部 API。ReAct agent 為可觀測性附帶執行，結構化結果以 `run_researcher()` 為準。

## 參考實作

- `src/tools/web_research.py` — `_filter_meals`, `run_researcher`
- `src/agents/researcher.py` — `run_researcher_crew`