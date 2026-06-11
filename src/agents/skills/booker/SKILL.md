---
name: booker
description: 機票/飯店/租車比價與預算彙總；選最佳性價比住宿並支援 reconcile_to_budget 降級。
---

# Booker Skill

## 目標

產出 `PriceSummary`：各項費用、比價選項、`within_budget` 旗標。不執行真實預訂。

## 查詢項目

| 項目 | 工具 | 備註 |
|------|------|------|
| 機票 | `search_flights` | 去回程；取最便宜 `price_twd` |
| 住宿 | `search_hotels` | 每晚預算 = 總預算 45% / 晚數，下限 3000 TWD |
| 租車 | `search_car_rental` | 僅 `self_drive` 或有 drive 日的 mixed |
| 大眾運輸 | `search_price_twd` | transit 天數 × 成人數 × 一日券估價 |

## 租車/大眾天數推導

`_travel_days_from_plan`：從每日 `drive_segments.mode` 統計 drive 日與 transit 日；無資料時 fallback 平分。

## 住宿選擇（select_best_hotel）

1. 優先每晚價 ≤ `budget_per_night` 者
2. 其中評分最高（同分取較便宜）
3. 全部超預算 → 取最便宜一間
4. 轉成 `DayPlan.hotel` 格式（`hotel_to_dayplan`）

## 總價公式

```
grand_total = flight + accommodation + car + transport + meals_estimate + attractions_tickets
```

- `meals_estimate` / `attractions_tickets` 從 `daily_plans` 加總
- `within_budget = grand_total <= budget_twd`

## 預算修復（reconcile_to_budget）

超預算時：

1. 換 `hotel_options` 中最便宜住宿
2. 就地更新所有 `daily_plans[].hotel`
3. 重算 `grand_total`；仍超則保留 `within_budget=False`

## ReAct 模式（非 mock）

可編排 `flight_search_tool`, `hotel_search_tool`, `carrental_search_tool`, `serper_price_tool`；
結構化總價仍以工具函式結果為準。

## 參考實作

- `src/agents/booker.py` — `run_booker_crew`, `reconcile_to_budget`
- `src/core/graph.py` — `booker` node, `_resolve_hotel_selection`