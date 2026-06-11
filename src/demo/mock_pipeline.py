"""
Mock agent pipeline — 模擬完整 LangGraph 流程，全程使用東京 5 天 fixture。

每個 agent 步驟之間插入可設定延遲，供 Streamlit demo 展示進度動畫。
"""

from __future__ import annotations

import copy
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from src.agents.output_formatter import build_itinerary_md
from src.demo.feedback_intent import wants_day4_transit
from src.demo.trace_enrichment import enrich_trace_step
from src.core.models import (
    Attraction,
    BlogSource,
    DayPlan,
    DriveSegment,
    Meal,
    RegionCluster,
    ResearchBundle,
    TravelPlanState,
    UserQuery,
)

FIXTURE_PATH = Path(__file__).parent.parent.parent / "tests" / "fixtures" / "tokyo_5day_realistic.json"

DEFAULT_AGENT_DELAYS: dict[str, float] = {
    "parse_input": 0.6,
    "research": 2.0,
    "generate_draft": 2.5,
    "validate_route": 1.8,
    "booker": 1.5,
    "supervisor": 1.2,
    "format_output": 1.0,
    "refine_gate": 0.8,
}


def load_tokyo_fixture() -> dict[str, Any]:
    if not FIXTURE_PATH.exists():
        raise FileNotFoundError(f"Tokyo fixture not found: {FIXTURE_PATH}")
    with open(FIXTURE_PATH, encoding="utf-8") as f:
        return json.load(f)


def _meal_from_dict(mtype: str, raw: dict) -> Meal:
    return Meal(
        meal_type=mtype,
        name=raw.get("name", ""),
        location=raw.get("location", ""),
        cuisine=raw.get("cuisine", "local"),
        est_cost_twd=int(raw.get("est_cost_twd", 0) or 0),
        notes=raw.get("notes", ""),
    )


def _attraction_from_dict(raw: dict) -> Attraction:
    return Attraction(
        name=raw.get("name", ""),
        type=raw.get("type", "other"),
        location=raw.get("location", ""),
        estimated_time_minutes=int(raw.get("estimated_time_minutes", 90) or 90),
        est_cost_twd=int(raw.get("est_cost_twd", 0) or 0),
        notes=raw.get("notes", ""),
    )


def _segment_from_dict(raw: dict) -> DriveSegment:
    return DriveSegment(
        from_location=raw.get("from", raw.get("from_location", "")),
        to_location=raw.get("to", raw.get("to_location", "")),
        minutes=int(raw.get("minutes", 0) or 0),
        km=float(raw.get("km", 0) or 0),
        notes=raw.get("notes", ""),
        mode=raw.get("mode", "drive"),
    )


def _day_plan_from_dict(raw: dict) -> DayPlan:
    meals = {
        mtype: _meal_from_dict(mtype, mraw)
        for mtype, mraw in (raw.get("meals") or {}).items()
    }
    return DayPlan(
        day=int(raw["day"]),
        date=raw.get("date", ""),
        drive_total_minutes=int(raw.get("drive_total_minutes", 0) or 0),
        transit_total_minutes=int(raw.get("transit_total_minutes", 0) or 0),
        drive_segments=[_segment_from_dict(s) for s in raw.get("drive_segments", [])],
        meals=meals,
        attractions=[_attraction_from_dict(a) for a in raw.get("attractions", [])],
        hotel=raw.get("hotel"),
        notes=raw.get("notes", ""),
    )


def _research_from_fixture(fixture: dict[str, Any], query: UserQuery) -> ResearchBundle:
    from src.tools.web_research import _get_mock_attractions_for_destination, _get_mock_meals_for_destination

    rmeta = fixture.get("research", {})
    clusters = [
        RegionCluster(
            name=c.get("name", ""),
            areas=c.get("areas", []),
            hub_city=c.get("hub_city", ""),
            suggested_nights=int(c.get("suggested_nights", 1) or 1),
        )
        for c in rmeta.get("region_clusters", [])
    ]
    attractions = _get_mock_attractions_for_destination(query.destination, 12)
    meals = _get_mock_meals_for_destination(query.destination, 15)
    sources = [
        BlogSource(
            title=s.get("title", ""),
            url=s.get("url", ""),
            published=s.get("published"),
            key_takeaways=s.get("key_takeaways", ""),
        )
        for s in fixture.get("blog_sources", [])
    ]
    return ResearchBundle(
        query_context=query.raw_input,
        attractions=attractions,
        meals=meals,
        blog_sources=sources,
        region_coverage=rmeta.get("region_coverage", []),
        region_clusters=clusters,
        route_hint=rmeta.get("route_hint", ""),
        notes=rmeta.get("notes", ""),
    )


def _price_summary_from_fixture(fixture: dict[str, Any], query: UserQuery) -> dict[str, Any]:
    raw = dict(fixture.get("price_summary", {}))
    raw.setdefault("budget", query.budget_twd)
    return raw


def _daily_markers_from_fixture(fixture: dict[str, Any]) -> dict[int, dict]:
    raw = fixture.get("daily_markers", {})
    return {int(k): dict(v) for k, v in raw.items()}


def _timestamped(step: dict) -> dict:
    out = dict(step)
    out.setdefault("timestamp", datetime.now().isoformat())
    return out


def _apply_day4_transit(dp: DayPlan) -> None:
    """將第 4 天箱根段落改為大眾運輸（局部重規劃 demo）。"""
    dp.drive_total_minutes = 0
    dp.transit_total_minutes = 97
    dp.drive_segments = [
        DriveSegment(
            from_location="新宿格蘭貝爾酒店",
            to_location="箱根神社",
            minutes=48,
            km=0.0,
            mode="transit",
            notes="小田急浪漫特快至箱根湯本，轉登山巴士",
        ),
        DriveSegment(
            from_location="箱根神社",
            to_location="蘆之湖遊船",
            minutes=19,
            km=0.0,
            mode="transit",
            notes="箱根周遊券含巴士至海賊船碼頭",
        ),
        DriveSegment(
            from_location="蘆之湖遊船",
            to_location="新宿格蘭貝爾酒店",
            minutes=48,
            km=0.0,
            mode="transit",
            notes="原路返回新宿（浪漫特快）",
        ),
    ]
    dp.notes = (
        "Day 4 箱根近郊一日遊，依回饋改搭小田急浪漫特快與箱根周遊券，"
        "總大眾運輸約 97 分鐘，取消租車"
    )
    if dp.hotel:
        dp.hotel = dict(dp.hotel)
        dp.hotel["notes"] = "箱根大眾運輸一日來回，仍回新宿住宿"


def _adjust_price_for_day4_transit(price: dict[str, Any]) -> dict[str, Any]:
    """D4 改大眾運輸：移除租車，補鐵路/周遊券差額，重算總計。"""
    out = copy.deepcopy(price)
    car_removed = int(out.get("car_rental_estimate") or 0)
    rail_addon = 1183  # 浪漫特快 + 箱根周遊券 vs 原行程地鐵估算差額
    out["car_rental_estimate"] = 0
    out["transport_total"] = int(out.get("transport_total") or 0) + rail_addon
    out["car_options"] = []
    out["note"] = (
        (out.get("note") or "")
        + "；依回饋 Day 4 改大眾運輸，已移除租車並計入箱根鐵路票券。"
    ).lstrip("；")
    components = (
        int(out.get("flight_total") or 0),
        int(out.get("accommodation_total") or 0),
        int(out.get("car_rental_estimate") or 0),
        int(out.get("transport_total") or 0),
        int(out.get("meals_estimate") or 0),
        int(out.get("attractions_tickets") or 0),
    )
    out["estimated_total_twd"] = sum(components)
    budget = int(out.get("budget") or 0)
    if budget:
        out["within_budget"] = out["estimated_total_twd"] <= budget
    return out


def _apply_refine_adjustments(
    daily_plans: list[DayPlan],
    query: UserQuery,
) -> list[DayPlan]:
    """依回饋微調 mock 行程（駕車上限、餐飲多樣化、D4 大眾運輸）。"""
    plans = copy.deepcopy(daily_plans)
    if wants_day4_transit(query.user_feedback):
        for dp in plans:
            if dp.day == 4:
                _apply_day4_transit(dp)
                break

    if query.max_daily_drive_minutes is not None:
        cap = query.max_daily_drive_minutes
        for dp in plans:
            if dp.drive_total_minutes > cap:
                scale = cap / max(dp.drive_total_minutes, 1)
                dp.drive_total_minutes = cap
                for seg in dp.drive_segments:
                    if getattr(seg, "mode", "drive") == "drive":
                        seg.minutes = max(5, int(seg.minutes * scale))

    if query.prefer_meal_variety:
        alt_lunches = [
            ("阿美橫町小吃", "東京・上野", "小吃", 387),
            ("壽司大", "東京・築地", "壽司", 671),
            ("もつ鍋", "東京・新宿", "鍋物", 843),
            ("一蘭拉麵", "東京・新宿", "拉麵", 463),
            ("箱根 湯葉料理", "箱根", "鄉土料理", 658),
        ]
        for i, dp in enumerate(plans):
            if "lunch" in dp.meals and i < len(alt_lunches):
                name, loc, cuisine, cost = alt_lunches[i]
                dp.meals["lunch"] = Meal(
                    meal_type="lunch", name=name, location=loc,
                    cuisine=cuisine, est_cost_twd=cost,
                    notes="依回饋調整，避免餐廳重複",
                )
    return plans


def _delay_for(step_name: str, delays: dict[str, float]) -> float:
    return delays.get(step_name, delays.get("supervisor", 1.0))


def _yield_steps(
    steps: list[dict],
    base_state: TravelPlanState,
    delays: dict[str, float],
    *,
    phase_data: dict[str, Any] | None = None,
) -> Iterator[TravelPlanState]:
    """依 trace 步驟逐步 yield 狀態（每步前 sleep）。"""
    trace: list[dict] = []
    state = copy.deepcopy(base_state)
    state.trace = []
    state.status = "running"
    state.final_itinerary_md = None

    phase_data = phase_data or {}

    query = phase_data.get("parsed")
    price = phase_data.get("price_summary")
    research = phase_data.get("research")

    for step in steps:
        step_name = step.get("step", "")
        time.sleep(_delay_for(step_name, delays))
        enriched = (
            enrich_trace_step(
                step,
                query=query,
                price=price,
                research=research,
                daily_plans=phase_data.get("daily_plans"),
            )
            if query
            else step
        )
        stamped = _timestamped(enriched)
        trace.append(stamped)
        state.trace = list(trace)

        if step_name == "parse_input":
            state.parsed = phase_data.get("parsed", state.parsed)
        elif step_name == "research":
            state.research = phase_data.get("research", state.research)
            state.sources = state.research.blog_sources if state.research else []
        elif step_name == "generate_draft":
            state.daily_plans = phase_data.get("daily_plans", state.daily_plans)
        elif step_name == "validate_route":
            state.daily_plans = phase_data.get("daily_plans", state.daily_plans)
            state.daily_markers = phase_data.get("daily_markers", state.daily_markers)
        elif step_name == "booker":
            state.price_summary = phase_data.get("price_summary", state.price_summary)
        elif step_name == "format_output":
            state.daily_plans = phase_data.get("daily_plans", state.daily_plans)
            state.price_summary = phase_data.get("price_summary", state.price_summary)
            state.final_itinerary_md = phase_data.get("final_itinerary_md")
            state.daily_markers = phase_data.get("daily_markers", state.daily_markers)
            state.status = phase_data.get("status", "success")

        yield copy.deepcopy(state)


def run_mock_planner_stream(
    user_input: UserQuery,
    *,
    delays: dict[str, float] | None = None,
) -> Iterator[TravelPlanState]:
    """模擬初次規劃：parse → supervisor ⇄ agents → format_output。"""
    delays = delays or DEFAULT_AGENT_DELAYS
    fixture = load_tokyo_fixture()
    query = user_input.model_copy(update={"destination": user_input.destination or "東京"})

    daily_plans = [_day_plan_from_dict(d) for d in fixture.get("daily_plans", [])]
    research = _research_from_fixture(fixture, query)
    price = _price_summary_from_fixture(fixture, query)
    markers = _daily_markers_from_fixture(fixture)
    md = build_itinerary_md(daily_plans, price, query, [], research)

    steps = fixture.get("trace_template", {}).get("initial", [])
    if not steps:
        raise ValueError("Fixture missing trace_template.initial")

    skeleton = TravelPlanState(
        user_query=query.raw_input,
        parsed=query,
        status="running",
    )
    phase_data = {
        "parsed": query,
        "research": research,
        "daily_plans": daily_plans,
        "price_summary": price,
        "daily_markers": markers,
        "final_itinerary_md": md,
        "status": "success",
    }
    yield from _yield_steps(steps, skeleton, delays, phase_data=phase_data)


def run_mock_planner_refine_stream(
    prior_state: TravelPlanState,
    user_query: UserQuery,
    *,
    delays: dict[str, float] | None = None,
) -> Iterator[TravelPlanState]:
    """模擬套用回饋：generate_draft → validate → refine_gate → format_output。"""
    delays = delays or DEFAULT_AGENT_DELAYS
    fixture = load_tokyo_fixture()

    daily_plans = _apply_refine_adjustments(
        prior_state.daily_plans or [_day_plan_from_dict(d) for d in fixture.get("daily_plans", [])],
        user_query,
    )
    research = prior_state.research or _research_from_fixture(fixture, user_query)
    price = dict(prior_state.price_summary or _price_summary_from_fixture(fixture, user_query))
    if wants_day4_transit(user_query.user_feedback):
        price = _adjust_price_for_day4_transit(price)
    markers = prior_state.daily_markers or _daily_markers_from_fixture(fixture)
    md = build_itinerary_md(daily_plans, price, user_query, [], research)

    steps = fixture.get("trace_template", {}).get("refine", [])
    if not steps:
        raise ValueError("Fixture missing trace_template.refine")

    skeleton = TravelPlanState(
        user_query=user_query.raw_input,
        parsed=user_query,
        research=research,
        price_summary=price,
        sources=prior_state.sources,
        status="running",
    )
    phase_data = {
        "parsed": user_query,
        "research": research,
        "daily_plans": daily_plans,
        "price_summary": price,
        "daily_markers": markers,
        "final_itinerary_md": md,
        "status": "success",
    }
    yield from _yield_steps(steps, skeleton, delays, phase_data=phase_data)