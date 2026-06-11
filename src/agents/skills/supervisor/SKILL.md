---
name: supervisor
description: Hub-and-spoke 編排者；依進度選 research/plan/validate/book/finish，並套用確定性 guardrails 保證流程收斂。
---

# Supervisor Skill

## 角色

多 Agent 旅遊規劃的 **orchestrator**。觀察 `TravelPlanGraphState`，選出「最該做的下一步」。

## 可用動作

| 動作 | 前置條件 | 用途 |
|------|----------|------|
| `research` | 永遠可選 | 補強景點/美食/部落格 |
| `plan` | 有 research | 產生或重排行程 |
| `validate` | 有 daily_plans | 計算駕車/大眾運輸時間 |
| `book` | 已 `_validated` | 機票/飯店/租車比價 |
| `finish` | 已 `_booked` 或 retry 用盡 | 進入 `format_output` |

## 決策原則

1. 缺研究 → `research`；有研究無行程 → `plan`
2. 有行程未驗證 → `validate`；已驗證未比價 → `book`
3. 僅超預算且尚未 `reconcile_to_budget` → 優先 `book`
4. 駕車/地理/空白天 hard 違規且 `retry_count < max_retries` → `plan`
5. 都合格 → `finish`

## Guardrails（覆寫 LLM 決策）

- **Finish veto**：已驗證但未 `_booked` 時，不得 `finish` → 改 `book`
- **Retry 用盡**：`retry_count >= max_retries` 且 LLM 選 `plan` → `converge_after_retry_exhausted`（validate → book → finish）
- **Stall 保護**：`_stall_count >= 2` 且 retry 用盡 → 不再 replan，走收斂路徑
- **預算 hard only**：若僅剩 budget hard 且可 reconcile → 先 `book`

## 收斂優先（converge_after_retry_exhausted）

重試用盡後**不直接結束**：

```
未 _validated → validate
未 _booked    → book
否則          → finish
```

最終 `status` 可為 `needs_attention`（仍有 hard 違規），但必須有 `price_summary` 與完整 markdown。

## 後備策略（baseline_action）

LLM 決策失敗或非法時，依進度確定性推進，保證最終到達 `finish`。

## 輸出格式

`NextActionOutput`：`next_action` + `reasoning`（中文，引用具體缺口）。

## 參考實作

- `src/agents/supervisor.py` — `decide_next_action`, `apply_guardrails`, `legal_actions`
- `src/core/graph.py` — `supervisor_node`, `route_supervisor`