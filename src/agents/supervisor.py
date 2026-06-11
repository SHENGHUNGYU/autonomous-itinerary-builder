"""
Supervisor Agent（LLM 決策）

接收驗證後的完整狀態，決定下一步：
- proceed_to_output：行程夠好，產出最終結果
- retry_planner：請 Planner 依回饋調整（最常見）
- retry_research：研究資料不足，重新研究
- fail：超過重試次數或無法解決

這是流程中唯一真正適合用 LLM 推理的決策點，故以結構化輸出（guided_json）實作。
"""

from __future__ import annotations

import os
from enum import Enum

from pydantic import BaseModel, Field

from src.agents.skills_loader import compose_system_prompt
from src.core.models import ResearchBundle, UserQuery
from src.services.llm import call_structured


class SupervisorDecision(str, Enum):
    PROCEED_TO_OUTPUT = "proceed_to_output"
    RETRY_PLANNER = "retry_planner"
    RETRY_RESEARCH = "retry_research"
    FAIL = "fail"


# =============================================================================
# 自主路由（hub-and-spoke）：Supervisor 每一步動態決定下一個 worker
# =============================================================================

class AgentAction(str, Enum):
    """Supervisor 可調度的下一步。"""
    RESEARCH = "research"   # 蒐集/補強景點、美食、部落格
    PLAN = "plan"          # 產生或重排每日行程
    VALIDATE = "validate"  # 計算駕車時間、檢查路線可行性
    BOOK = "book"          # 機票/飯店/租車比價 + 預算檢查
    FINISH = "finish"      # 行程已足夠好，產出最終結果


class NextActionOutput(BaseModel):
    """Supervisor 的自主路由決策（小輸出，弱模型也穩）。"""
    next_action: AgentAction = Field(..., description="下一步要執行哪個 worker")
    reasoning: str = Field("", description="為何選這步（中文，請引用目前狀態的具體缺口）")


_ROUTER_SYSTEM = (
    "你是多 Agent 旅遊規劃系統的總監（orchestrator）。你的工作是觀察目前狀態，"
    "從可用動作中選出『最該做的下一步』，像一個會自己判斷的專案經理。原則：\n"
    "- 缺研究資料 → research；有研究但無行程 → plan；行程未驗證駕車 → validate；\n"
    "  尚未比價/檢查預算 → book；行程已驗證、預算可接受、無明顯問題 → finish。\n"
    "- 驗證有違規或超出預算且還能重試時，回 plan 重排；不要對已合格的行程無謂重試。\n"
    "- 只能從『可用動作』清單中挑一個。盡快讓使用者拿到可用結果。"
)


def _derive_progress(state: dict) -> dict:
    price = state.get("price_summary") or {}
    violations = state.get("violations") or []
    n_hard = sum(1 for v in violations if v.get("severity") == "hard")
    return {
        "has_research": state.get("research") is not None,
        "has_plan": bool(state.get("daily_plans")),
        "validated": bool(state.get("_validated")),
        "booked": bool(state.get("_booked")),
        "has_violations": n_hard > 0 or bool(state.get("validation_errors")),
        "n_hard": n_hard,
        "over_budget": price != {} and not price.get("within_budget", True),
    }


def legal_actions(progress: dict, retry_count: int = 0, max_retries: int = 3) -> list[AgentAction]:
    """依目前進度回傳合法的下一步（避免在沒有前置資料時亂跳）。"""
    actions = [AgentAction.RESEARCH]
    if progress["has_research"]:
        actions.append(AgentAction.PLAN)
    if progress["has_plan"]:
        actions.append(AgentAction.VALIDATE)
        if progress["validated"]:
            actions.append(AgentAction.BOOK)
        if progress["booked"] or retry_count >= max_retries:
            actions.append(AgentAction.FINISH)
    return actions


def only_budget_hard(violations: list) -> bool:
    """是否僅剩預算類 hard 違規（可用 book 修復，不必立刻重 plan）。"""
    hard = [
        v for v in violations
        if (v.get("severity") if isinstance(v, dict) else v.severity) == "hard"
    ]
    if not hard:
        return False
    cats = {
        v.get("category") if isinstance(v, dict) else v.category
        for v in hard
    }
    return cats == {"budget"}


def _has_hard_any(violations: list) -> bool:
    if not violations:
        return False
    if isinstance(violations[0], dict):
        return any(v.get("severity") == "hard" for v in violations)
    return any(v.severity == "hard" for v in violations)


def converge_after_retry_exhausted(state: dict) -> AgentAction:
    """重試用盡後仍走完 validate → book → finish（收斂優先）。"""
    if not state.get("_validated"):
        return AgentAction.VALIDATE
    if not state.get("_booked"):
        return AgentAction.BOOK
    return AgentAction.FINISH


_ACTION_LABELS = {
    AgentAction.RESEARCH: "蒐集研究",
    AgentAction.PLAN: "重排行程",
    AgentAction.VALIDATE: "驗證路線",
    AgentAction.BOOK: "比價預算",
    AgentAction.FINISH: "完成輸出",
}


def _guardrail_reasoning(
    guarded: AgentAction,
    original: AgentAction,
    retry_count: int,
    max_retries: int,
    state: dict,
    violations: list,
) -> str:
    """護欄覆寫 LLM 決策時，產生與實際動作一致的中文理由。"""
    n_hard = sum(
        1 for v in violations
        if (v.get("severity") if isinstance(v, dict) else v.severity) == "hard"
    )
    validated = bool(state.get("_validated"))
    booked = bool(state.get("_booked"))
    stall = state.get("_stall_count", 0)

    original_label = _ACTION_LABELS.get(original, original.value)
    guarded_label = _ACTION_LABELS.get(guarded, guarded.value)

    if original == AgentAction.PLAN and retry_count >= max_retries:
        reason = (
            f"重試已用盡（{retry_count}/{max_retries}），不再重排行程，"
            "依收斂策略推進後續步驟。"
        )
    elif stall >= 2 and retry_count >= max_retries:
        reason = (
            f"連續重規劃未改善且重試已用盡（{retry_count}/{max_retries}），"
            "改走驗證與比價後輸出。"
        )
    elif guarded == AgentAction.BOOK and validated and not booked:
        reason = "行程已驗證駕車，但尚未比價，需先完成預算檢查。"
    elif guarded == AgentAction.FINISH and booked:
        reason = "驗證與比價已完成，輸出目前最佳行程。"
    else:
        reason = "依系統護欄規則調整下一步，確保流程可收斂。"

    status_bits = []
    if n_hard:
        status_bits.append(f"{n_hard} 項 hard 違規")
    if validated:
        status_bits.append("已驗證")
    if booked:
        status_bits.append("已比價")
    status = "、".join(status_bits) if status_bits else "無顯著違規"

    return (
        f"LLM 建議「{original_label}」，但{reason}"
        f"目前狀態：{status}。實際執行：{guarded_label}。"
    )


def apply_guardrails(
    action: AgentAction,
    state: dict,
    violations: list,
    retry_count: int,
    max_retries: int,
) -> AgentAction:
    """覆寫 LLM 路由：finish veto、預算優先 book、stall 保護。"""
    viol_dicts = violations if violations and isinstance(violations[0], dict) else [
        {"category": v.category, "severity": v.severity, "message": v.message, "data": v.data}
        for v in violations
    ]

    if action == AgentAction.PLAN and retry_count >= max_retries:
        return converge_after_retry_exhausted(state)

    # 已比價且無 hard 違規：即使 LLM 想再 plan（多為了 soft 微調，如超預算/地理），
    # 直接收斂輸出，避免只剩軟性問題時仍空轉重排。
    if action == AgentAction.PLAN and state.get("_booked") and not _has_hard_any(viol_dicts):
        return AgentAction.FINISH

    if action == AgentAction.FINISH and _has_hard_any(viol_dicts) and retry_count < max_retries:
        if only_budget_hard(viol_dicts) and state.get("_booked") and not state.get("_book_reconciled"):
            return AgentAction.BOOK
        return AgentAction.PLAN

    if action == AgentAction.BOOK and state.get("_book_reconciled"):
        if _has_hard_any(viol_dicts) and retry_count < max_retries:
            return AgentAction.PLAN
        return AgentAction.FINISH

    # 已驗證但尚未比價：不得直接結束
    if action == AgentAction.FINISH and state.get("_validated") and not state.get("_booked"):
        return AgentAction.BOOK

    # stall + 重試用盡：不再 replan，改走驗證與比價
    if (
        action == AgentAction.PLAN
        and state.get("_stall_count", 0) >= 2
        and retry_count >= max_retries
    ):
        return converge_after_retry_exhausted(state)

    return action


def baseline_action(
    progress: dict,
    retry_count: int,
    max_retries: int,
    state: dict | None = None,
    violations: list | None = None,
) -> AgentAction:
    """確定性後備策略：保證流程一定會推進、最終會收斂到 finish。"""
    state = state or {}
    viol_dicts = violations or state.get("violations") or []

    if not progress["has_research"]:
        return AgentAction.RESEARCH
    if not progress["has_plan"]:
        return AgentAction.PLAN
    if not progress["validated"]:
        return AgentAction.VALIDATE
    if not progress["booked"]:
        return AgentAction.BOOK
    if (
        only_budget_hard(viol_dicts)
        and progress["over_budget"]
        and not state.get("_book_reconciled")
        and retry_count < max_retries
    ):
        return AgentAction.BOOK
    if progress["has_violations"] and retry_count < max_retries:
        return AgentAction.PLAN
    # 超預算為軟性提醒（不再為了預算重排）；已比價即收斂輸出。
    return AgentAction.FINISH


def decide_next_action(
    state: dict,
    user_query: UserQuery,
    retry_count: int,
    max_retries: int,
) -> NextActionOutput:
    """LLM 自主選擇下一步；非法/失敗時退回確定性後備策略。"""
    progress = _derive_progress(state)
    legal = legal_actions(progress, retry_count, max_retries)
    viol_dicts = state.get("violations") or []
    fallback = baseline_action(
        progress, retry_count, max_retries, state=state, violations=viol_dicts,
    )

    research = state.get("research")
    daily_plans = state.get("daily_plans", []) or []
    price = state.get("price_summary") or {}
    research_line = (
        f"景點 {len(research.attractions)}、餐飲 {len(research.meals)}、"
        f"部落格 {len(research.blog_sources)} 篇" if research else "（尚無研究資料）"
    )
    budget_line = (
        f"總花費 {price.get('estimated_total_twd', 0)} / 預算 "
        f"{price.get('budget', user_query.budget_twd)}，在預算內：{price.get('within_budget', '未知')}"
        if price else "（尚未比價）"
    )
    violation_lines = "\n".join(
        f"- [{v.get('severity')}] {v.get('message')}" for v in viol_dicts[:10]
    ) or "無"

    prompt = (
        f"【使用者需求】目的地 {user_query.destination}、{user_query.days} 天、"
        f"預算 {user_query.budget_twd}、交通 {user_query.travel_mode}\n"
        f"【研究】{research_line}\n"
        f"【行程】已產生 {len(daily_plans)} 天"
        + ("（已驗證駕車）" if progress["validated"] else "（未驗證）")
        + f"（hard 違規 {progress['n_hard']} 項）\n"
        f"【約束違規】\n{violation_lines}\n"
        f"【駕車摘要】{state.get('validation_errors') or '無'}\n"
        f"【預算】{budget_line}\n"
        f"【已比價】{progress['booked']}｜【已嘗試降住宿】{state.get('_book_reconciled', False)}\n"
        f"【重試】{retry_count}/{max_retries}\n\n"
        f"可用動作（只能擇一）：{[a.value for a in legal]}\n"
        "提示：僅超預算且尚未降住宿時優先 book；駕車/空白天等問題用 plan；都合格才 finish。"
    )

    router_max = int(os.getenv("SUPERVISOR_MAX_TOKENS", "1024"))
    result = call_structured(
        compose_system_prompt("supervisor", _ROUTER_SYSTEM),
        prompt,
        NextActionOutput,
        temperature=0.0,
        max_tokens=router_max,
    )
    if result is None or result.next_action not in legal:
        return NextActionOutput(
            next_action=fallback,
            reasoning=f"（後備策略）LLM 決策失敗或不合法，依進度選擇 {fallback.value}。",
        )
    guarded = apply_guardrails(
        result.next_action, state, viol_dicts, retry_count, max_retries,
    )
    if guarded != result.next_action:
        return NextActionOutput(
            next_action=guarded,
            reasoning=_guardrail_reasoning(
                guarded, result.next_action, retry_count, max_retries, state, viol_dicts,
            ),
        )
    return result


class SupervisorOutput(BaseModel):
    """Supervisor 的結構化決策。"""
    decision: SupervisorDecision = Field(..., description="系統下一步動作")
    reasoning: str = Field(..., description="決策理由（中文），引用具體問題或驗證回饋")
    confidence: float = Field(0.8, ge=0.0, le=1.0, description="對此決策的信心 0-1")
    suggested_adjustments: list[str] = Field(
        default_factory=list, description="若 retry_planner，給 Planner 的具體建議"
    )


_SYSTEM = (
    "你是多 Agent 旅遊規劃系統的總監。核心原則：\n"
    "1. 優先讓使用者拿到可用結果，不做無謂重試。\n"
    "2. 只有問題嚴重且有明確改善空間時才要求 retry_planner。\n"
    "3. 研究資料明顯不足才 retry_research。\n"
    "4. 重試次數接近上限時要更謹慎；已達上限請選 proceed_to_output 或 fail。\n"
    "你必須給出清晰、可執行的理由。"
)


def run_supervisor(
    research: ResearchBundle | None,
    user_query: UserQuery,
    daily_plans: list,
    validation_errors: list[str],
    validation_feedback: str,
    retry_count: int,
    max_retries: int = 3,
    price_summary: dict | None = None,
    user_feedback: str = "",
    llm=None,
) -> SupervisorOutput:
    """以 LLM 結構化推理決定下一步；失敗時安全預設。"""
    research_summary = ""
    if research:
        research_summary = (
            f"景點 {len(research.attractions)}、餐飲 {len(research.meals)}、"
            f"部落格來源 {len(research.blog_sources)} 篇"
        )

    plans_summary = "\n".join(
        f"Day {dp.day}: {dp.drive_total_minutes} 分鐘駕車, {len(dp.attractions)} 景點, "
        f"住宿: {(dp.hotel or {}).get('name', '無')}"
        for dp in daily_plans
    )

    budget_line = ""
    if price_summary:
        budget_line = (
            f"\n【預算檢查】總花費 {price_summary.get('estimated_total_twd', 0)} / "
            f"預算 {price_summary.get('budget', user_query.budget_twd)}，"
            f"是否在預算內：{price_summary.get('within_budget', True)}"
        )

    feedback_line = ""
    if user_feedback:
        feedback_line = (
            f"\n【使用者回饋（結束後提出，務必納入）】\n{user_feedback}\n"
            "若回饋指出的問題尚未被滿足，應 retry_planner 並把要求放進 suggested_adjustments。"
        )

    prompt = (
        f"【使用者需求】\n{user_query.model_dump_json(indent=2)}\n\n"
        f"【研究成果】\n{research_summary}\n\n"
        f"【目前規劃摘要】\n{plans_summary}\n"
        f"{budget_line}\n"
        f"{feedback_line}\n\n"
        f"【駕車驗證】\n錯誤：{validation_errors}\n回饋：{validation_feedback}\n\n"
        f"【重試狀態】\n目前 {retry_count} / {max_retries} 次\n\n"
        "請決定下一步（proceed_to_output / retry_planner / retry_research / fail）。"
    )

    legacy_max = int(os.getenv("SUPERVISOR_MAX_TOKENS", "1024"))
    result = call_structured(
        compose_system_prompt("supervisor", _SYSTEM),
        prompt,
        SupervisorOutput,
        temperature=0.0,
        max_tokens=legacy_max,
    )
    if result is None:
        # 安全預設：行程已通過驗證或已達上限就放行（避免對合格行程無限重試）；
        # 僅在「仍有違規且未達上限」時才重試。
        if not validation_errors or retry_count >= max_retries:
            return SupervisorOutput(
                decision=SupervisorDecision.PROCEED_TO_OUTPUT,
                reasoning="Supervisor 決策失敗；行程已通過驗證或已達重試上限，直接輸出目前最佳結果。",
                confidence=0.3,
            )
        return SupervisorOutput(
            decision=SupervisorDecision.RETRY_PLANNER,
            reasoning="Supervisor 決策失敗，仍有駕車違規，預設請 Planner 依回饋重試。",
            confidence=0.3,
            suggested_adjustments=["大幅減少違規日子的移動距離"],
        )
    return result
