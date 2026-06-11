"""
LangGraph State definition for the Travel Planner.

For Phase 1 PoC we use a TypedDict as the primary graph state (recommended by LangGraph).
Rich domain models (ResearchBundle, DayPlan, etc.) live inside the Pydantic objects stored in this state.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, TypedDict

from pydantic import BaseModel

from src.core.models import (
    BlogSource,
    DayPlan,
    ResearchBundle,
    TravelPlanState as TravelPlanStatePydantic,
    UserQuery,
)


# -----------------------------------------------------------------------------
# LangGraph State (TypedDict) - used by StateGraph
# -----------------------------------------------------------------------------

def _merge_lists(left: list[Any], right: list[Any]) -> list[Any]:
    """Reducer for appending to trace / validation_errors etc."""
    if not left:
        return right or []
    if not right:
        return left
    return left + right


class TravelPlanGraphState(TypedDict, total=False):
    """
    State passed between LangGraph nodes during Phase 1 PoC.
    """
    # Input
    user_query: str
    parsed: UserQuery | None

    # Research (Phase 2 already implemented)
    research: ResearchBundle | None

    # Planning output
    daily_plans: list[DayPlan]
    price_summary: dict[str, Any]

    # 飯店比價結果與選定飯店（在 generate_draft 階段先查好，供 Planner 採用、Booker 重用）
    hotel_options: list[dict[str, Any]]
    selected_hotel: dict[str, Any] | None

    # Validation & Control
    validation_errors: list[str]          # We want to replace on each validation, not accumulate
    violations: list[dict[str, Any]]      # 統一 constraint checker 的結構化違規（category/severity/message/data）
    retry_count: int
    max_retries: int
    _validation_feedback: str             # 給 Planner 的駕車回饋（重試用）
    supervisor_decision: str              # Supervisor 決策結果
    supervisor_reasoning: str             # Supervisor 決策理由

    # Final outputs
    final_itinerary_md: str | None
    sources: list[BlogSource]

    # Map / Route data (Phase 3+)
    daily_polylines: dict[int, list[str]]   # day_number -> list of encoded polylines for that day
    daily_markers: dict[int, dict]          # day_number -> {name, location, lat, lng}（行程地圖標記）

    # Runtime
    status: Literal["running", "success", "failed"]
    trace: Annotated[list[dict[str, Any]], _merge_lists]

    # Best-of-N：跨重試保留「違規最少」的版本，最終輸出最佳版而非最後一版
    _best: dict[str, Any] | None
    _best_score: list[int]

    # Autonomous router progress flags
    next_action: str                      # research | plan | validate | book | finish
    _validated: bool                      # 本輪 daily_plans 是否已跑過 RouteValidator
    _booked: bool                         # 本輪 daily_plans 是否已跑過 Booker
    _book_reconciled: bool                # Booker 是否已嘗試降住宿修復預算
    _supervisor_steps: int                # Supervisor 迴圈步數（防無限 loop）
    _max_supervisor_steps: int
    _stall_count: int                     # 連續 plan 後 violation 分數未改善次數
    _plan_violation_score: list[int]      # 上次 plan 後的 (hard 數, 預算超出額)

    # Internal / debug
    _raw_fixture: dict[str, Any] | None


# Helper to convert our rich Pydantic model to the graph state shape
def pydantic_state_to_graph(state: TravelPlanStatePydantic) -> TravelPlanGraphState:
    return {
        "user_query": state.user_query,
        "parsed": state.parsed,
        "research": state.research,
        "daily_plans": state.daily_plans,
        "price_summary": state.price_summary,
        "validation_errors": state.validation_errors,
        "retry_count": state.retry_count,
        "final_itinerary_md": state.final_itinerary_md,
        "sources": state.sources,
        "status": state.status,
        "trace": state.trace,
        "daily_polylines": state.daily_polylines,
        "daily_markers": state.daily_markers,
    }


def graph_state_to_pydantic(state: TravelPlanGraphState) -> TravelPlanStatePydantic:
    return TravelPlanStatePydantic(
        user_query=state.get("user_query", ""),
        parsed=state.get("parsed"),
        research=state.get("research"),
        daily_plans=state.get("daily_plans", []),
        price_summary=state.get("price_summary", {}),
        validation_errors=state.get("validation_errors", []),
        violations=state.get("violations", []),
        retry_count=state.get("retry_count", 0),
        final_itinerary_md=state.get("final_itinerary_md"),
        sources=state.get("sources", []),
        status=state.get("status", "running"),
        trace=state.get("trace", []),
        daily_polylines=state.get("daily_polylines", {}),
        daily_markers=state.get("daily_markers", {}),
    )
