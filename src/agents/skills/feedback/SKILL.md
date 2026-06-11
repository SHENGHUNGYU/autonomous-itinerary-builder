---
name: feedback
description: 解析使用者結束後的自然語言回饋，轉成 max_daily_drive_minutes 與 Planner 可讀的 notes。
---

# Feedback Skill

## 目標

把 `user_feedback` 字串解析為 `FeedbackAdjustment`：

- `max_daily_drive_minutes`：使用者明確要求的每日駕車上限（分鐘）
- `notes`：其他無法量化但須轉達 Planner 的需求

## 解析規則

| 使用者說法 | 輸出 |
|------------|------|
| 每天不超過 90 分鐘 | `max_daily_drive_minutes=90` |
| 每天最多開 2 小時 | `max_daily_drive_minutes=120` |
| 未提及駕車時間 | `null` |
| 景點/預算/步調等 | 放入 `notes` |

## Fallback

LLM 不可用時用 regex：

- `(\d+) 分鐘` → 分鐘數
- `(\d+(?:\.\d+)?) 小時` → × 60

LLM 有回應但未抓到上限時，再用 regex 補一次。

## 下游

`graph.run_planner_refine` 將調整寫入 `UserQuery`，重跑 Planner → Validate → Supervisor。

## 參考實作

- `src/agents/feedback.py` — `parse_user_feedback`