---
name: route_validator
description: 確定性計算每日駕車/大眾運輸分鐘數；對照 mode-aware 上限產出 violations 與改善建議。
---

# Route Validator Skill

## 目標

更新每個 `DayPlan` 的實際移動時間，並判斷是否違反駕車上限。此節點**不使用 LLM**。

## 流程

1. 遍歷 `daily_plans` 的 `drive_segments`
2. 依段落 `mode` 與 `user_query.travel_mode` 選 Maps API 模式：
   - `self_drive` → API `self_drive`
   - `public` → API `public`
   - `mixed` → 段落 `transit`/`flight` 用 `public`，其餘 `self_drive`
3. 呼叫 `compute_driving_route(from, to, travel_mode)`
4. 寫回 `seg.minutes`、`seg.polyline`（若有）
5. 累加 `drive_total_minutes`（僅 drive 段）與 `transit_total_minutes`

## 違規判定

使用 `check_drive(daily_plans, user_query)`：

- 僅計 `mode=drive` 段落（mixed 下 transit 不計入駕車上限）
- 上限來自 `drive_cap_for(user_query)`
- 超過 → hard violation，訊息含 Day 編號與分鐘數

## 改善建議模板

- 減少該日景點數或換較近住宿
- `mixed`：跨區改標 `mode=transit`
- `self_drive`：可建議部分改大眾運輸

## 輸出

`ValidationResult`：`is_valid`, `violations[]`, `suggestions[]`, `max_daily_drive_minutes`

## Maps 失敗處理

Routes API 404/失敗時 `maps.py` 退回估算；驗證仍繼續，結果標記於 trace。

## 參考實作

- `src/agents/route_validator.py` — `run_route_validator_crew`
- `src/agents/constraints.py` — `check_drive`, `drive_cap_for`
- `src/tools/maps.py` — `compute_driving_route`