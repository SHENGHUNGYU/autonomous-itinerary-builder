---
name: planner
description: 依 ResearchBundle 與 UserQuery 產生多日結構化行程；遵守駕車上限、地理聚類、三餐與住宿連續性。
---

# Planner Skill

## 目標

輸出 `ItineraryOutput`：`daily_plans[]` + `overall_notes` + `estimated_total_cost_twd`。

## 輸入優先順序

1. `UserQuery`（天數、預算、交通方式、偏好、使用者回饋）
2. `ResearchBundle` 景點/餐飲/部落格 grounding（禁止脫離來源臆測）
3. `validation_feedback`（重試時必須逐條修正）
4. `preferred_hotel`（單區域時可參考；多區域須每日就近換宿）

## 每日必填結構

每個 `DayPlan` 須包含：

- `attractions`：2–3 個，來自研究池
- `meals`：僅 `breakfast` / `lunch` / `dinner` 三個 key
- `hotel`：name + location（具體城市）+ est_cost_twd
- `drive_segments`：依移動順序串接（住宿→景點→…→住宿）
- `notes`：1–2 句當日亮點

## 交通方式規則

| travel_mode | 規則 |
|-------------|------|
| `self_drive` | 長短程皆可 `mode=drive`；每日 drive 合計 ≤ `drive_cap_for()` |
| `mixed` | 同聚類內 `drive`；跨聚類 `transit`；禁止遠距一日來回 |
| `public` | 長程段落標 `transit` |

`drive_cap_for` 預設：self_drive 240、mixed 180、public 90（分鐘）；與使用者 `max_daily_drive_minutes` 取較嚴者。

## 餐飲選擇

- 每餐從研究池選與**當日過夜區域**相同或鄰近的餐廳
- 禁止拉麵/屋台當早餐
- 規劃後由 graph 執行 `_sanitize_day_meals` 補齊缺餐

## 住宿連續性

- 同一城市連續日：同一間飯店（name + location 一致）
- 換城市才換飯店；`hotel.location` 勿填「九州」「北海道」等大地區名
- 多 `region_clusters` 時每個過夜城市選就近住宿

## drive_segments 填寫

- `from_location` / `to_location`：可填景點/飯店地名
- `mode`：`drive` | `transit` | `flight`
- `minutes` / `km` 可填 0（RouteValidator 會重算）

## 輸出紀律

- `temperature=0`，guided JSON 一次輸出
- 用字精簡，避免超出 context 導致 JSON 截斷
- 總花費盡量 ≤ `budget_twd`（不含機票）

## 參考實作

- `src/agents/planner.py` — `run_planner_crew`（全行程相容/後備）、`plan_single_day`（新：單日細部規劃，2026-06-10 改善主力）
- `src/agents/constraints.py` — `build_planner_feedback`, `check_geography`
- `src/agents/geo.py` — `overnight_area`, `region_clusters`, `same_area`

## Per-day 模式（推薦用於多區域行程）

- 上層（graph.generate_draft）先用 research.region_clusters 建 day → target_area skeleton。
- 每一天呼叫 `plan_single_day(research, user_query, day, target_area, prev_location=前日結束點, preferred_hotel=該區候選)`。
- Prompt 極小、聚焦本地 research slice + 連續性規則，輸出單一 DayPlan。
- 組裝後仍跑原有 `_normalize_hotels` / `_sanitize_all_day_meals` / `_diversify_attractions` / `_assign_hotels_by_overnight_areas` 等後處理。
- 好處：本地 hotel/餐廳/景點選擇正確率大幅提升，避免 7.json 類的「全九州同住鹿兒島 + 每天1景點 + 同餐重複」問題。