"""
LangGraph 多代理狀態機

流程（autonomous hub-and-spoke）：
    parse → supervisor ⇄ research / plan / validate / book
        └─ finish → format_output → END
    Supervisor 以 decide_next_action + 確定性 guardrails 動態選下一步。

代理：Researcher / Planner / RouteValidator / Booker 為專業代理，Supervisor 為 LLM 編排者。
LLM 透過自架 vLLM（OpenAI 相容）；MOCK_TOOLS=1 時所有外部工具走確定性 mock。
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta
from typing import Any, Literal, Union

from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph

from src.agents.booker import reconcile_to_budget, run_booker_crew
from src.agents.constraints import (
    build_planner_feedback,
    check_constraints,
    has_hard,
    summarize,
    to_dicts,
)
from src.agents.geo import overnight_area, same_area
from src.agents.output_formatter import build_itinerary_md
from src.agents.planner import ItineraryOutput, plan_single_day, run_planner_crew
from src.agents.researcher import run_researcher_crew
from src.agents.route_validator import run_route_validator_crew
from src.agents.supervisor import (
    AgentAction,
    SupervisorDecision,
    decide_next_action,
    run_supervisor,
)
from src.core.models import (
    DayPlan,
    DriveSegment,
    Meal,
    ResearchBundle,
    TravelPlanState as TravelPlanStatePydantic,
    UserQuery,
)
from src.core.state import TravelPlanGraphState, graph_state_to_pydantic


# =============================================================================
# Node Functions
# =============================================================================

def parse_input(state: TravelPlanGraphState) -> dict[str, Any]:
    """優先使用結構化 UserQuery；否則對原始字串做最小解析。"""
    existing_parsed = state.get("parsed")
    query = state.get("user_query", "")

    if existing_parsed and isinstance(existing_parsed, UserQuery):
        parsed = existing_parsed
        status_msg = "ok (structured input)"
    else:
        parsed = UserQuery(
            raw_input=query,
            destination="九州",
            days=5,
            budget_twd=40000,
            start_date=date(2026, 6, 28),
            preferences=["美食", "自然景觀"],
            travel_mode="self_drive",
        )
        status_msg = "ok (fallback parse)"

    trace_step = {
        "step": "parse_input",
        "status": status_msg,
        "destination": parsed.destination,
        "days": parsed.days,
        "budget": parsed.budget_twd,
        "timestamp": datetime.now().isoformat(),
    }
    return {
        "parsed": parsed,
        "user_query": query or parsed.raw_input,
        "status": "running",
        "trace": [trace_step],
    }


def research(state: TravelPlanGraphState) -> dict[str, Any]:
    """Researcher Agent：蒐集景點/美食/部落格 grounding。"""
    parsed: UserQuery | None = state.get("parsed")
    query = state.get("user_query", "")
    destination = parsed.destination if parsed and parsed.destination else "九州"

    print(f"[Node] research → destination={destination}")
    days = parsed.days if parsed else None
    bundle: ResearchBundle = run_researcher_crew(
        query, destination=destination, days=days,
    )

    trace_step = {
        "step": "research",
        "status": f"ok ({len(bundle.blog_sources)} blog sources grounded)",
        "attractions": len(bundle.attractions),
        "meals": len(bundle.meals),
        "timestamp": datetime.now().isoformat(),
    }
    return {
        "research": bundle,
        "sources": bundle.blog_sources,
        "_last_worker": "research",
        "trace": [trace_step],
    }


def _resolve_hotel_selection(
    state: TravelPlanGraphState,
) -> tuple[list[dict], dict | None, dict[str, list[dict]]]:
    """依目的地與研究子區域查詢飯店比價（任何目的地通用）。

    回傳 (合併 hotel_options, 預設 selected_hotel, hotels_by_area)。
    """
    from src.agents.booker import budget_per_night_twd, select_best_hotel
    from src.tools.web_research import search_hotels

    parsed: UserQuery | None = state.get("parsed")
    research: ResearchBundle | None = state.get("research")
    if not parsed:
        return (
            state.get("hotel_options", []) or [],
            state.get("selected_hotel"),
            state.get("hotels_by_area") or {},
        )

    hotels_by_area: dict[str, list[dict]] = dict(state.get("hotels_by_area") or {})
    if not hotels_by_area:
        areas: list[str] = [parsed.destination]
        if research and research.region_clusters:
            areas.extend(c.hub_city for c in research.region_clusters if c.hub_city)
        areas = list(dict.fromkeys(a for a in areas if a))
        budget = budget_per_night_twd(parsed)
        for area in areas:
            if area not in hotels_by_area:
                hotels_by_area[area] = search_hotels(area, budget_per_night_twd=budget)

    hotel_options = state.get("hotel_options") or []
    if not hotel_options:
        seen: set[str] = set()
        for opts in hotels_by_area.values():
            for h in opts:
                key = h.get("name") or ""
                if key and key not in seen:
                    seen.add(key)
                    hotel_options.append(h)

    selected = select_best_hotel(
        hotel_options, budget_per_night_twd(parsed), parsed.destination
    )
    return hotel_options, selected, hotels_by_area


def _assign_hotels_by_overnight_areas(
    daily_plans: list[DayPlan],
    parsed: UserQuery,
    hotels_by_area: dict[str, list[dict]],
    default_hotel: dict | None,
) -> None:
    """依每日過夜區域從對應比價結果選住宿（就地修改）。"""
    from src.agents.booker import budget_per_night_twd, select_best_hotel
    from src.agents.geo import overnight_area, same_area

    budget = budget_per_night_twd(parsed)
    for dp in daily_plans:
        area = overnight_area(dp, parsed.destination)
        candidates: list[dict] = []
        for key, opts in hotels_by_area.items():
            if same_area(key, area, parsed.destination):
                candidates.extend(opts)
        if not candidates:
            candidates = hotels_by_area.get(parsed.destination) or []
        picked = select_best_hotel(candidates, budget, area) or default_hotel
        if picked:
            dp.hotel = dict(picked)


def _fill_missing_hotels(daily_plans: list[DayPlan], default_hotel: dict | None) -> None:
    """只替『沒有住宿』的日子補上預設飯店（就地修改）。

    重點：不再強制把同一間飯店套到每一天——大區域行程（如北海道）需要多基地住宿，
    Planner 會依各過夜城市選不同飯店；此處只在 Planner 沒給住宿時補預設，
    再交給 _normalize_hotels 做「同城連住、換城才換」的連續性處理。
    """
    if not default_hotel:
        return
    for dp in daily_plans:
        if not _hotel_location(dp.hotel) and not (dp.hotel or {}).get("name"):
            dp.hotel = dict(default_hotel)


_VALID_MEAL_SLOTS = frozenset({"breakfast", "lunch", "dinner"})
_LUNCH_NOT_BREAKFAST = ("拉麵", "拉面", "ラーメン", "ramen", "一蘭", "屋台", "烏龍", "うどん")


def _meal_name_key(name: str) -> str:
    return (name or "").strip().lower()


def _pick_meal_from_pool(
    pool: list,
    mtype: str,
    area: str,
    destination: str,
    used_names: set[str],
    day_used: set[str],
    prefer_variety: bool,
    day_index: int,
) -> Meal | None:
    """從研究池選餐；prefer_variety 時避開已用過的餐廳名稱。"""
    candidates = [m for m in pool if m.meal_type == mtype]
    if not candidates:
        candidates = list(pool)

    def _ok(m) -> bool:
        key = _meal_name_key(m.name)
        if prefer_variety and (key in used_names or key in day_used):
            return False
        return same_area(m.location, area, destination) or same_area(m.location, destination, destination)

    local = [m for m in candidates if _ok(m)]
    if not local and prefer_variety:
        local = [
            m for m in candidates
            if _meal_name_key(m.name) not in day_used
            and _meal_name_key(m.name) not in used_names
        ]
    if not local:
        local = candidates
    if not local:
        return None
    # 即使未開啟 prefer_variety，也輕度避免跨日重複（池夠大時挑沒用過的）。
    fresh = [
        m for m in local
        if _meal_name_key(m.name) not in used_names
        and _meal_name_key(m.name) not in day_used
    ]
    pick_pool = fresh or local
    return pick_pool[(day_index + hash(mtype)) % len(pick_pool)]


def _sanitize_day_meals(
    dp: DayPlan,
    research: ResearchBundle | None,
    destination: str,
    *,
    used_names: set[str] | None = None,
    prefer_variety: bool = False,
    day_index: int = 0,
) -> None:
    """僅保留三餐 slot，修正明顯錯標的 meal_type，缺槽從研究池補齊。"""
    used_names = used_names if used_names is not None else set()
    day_used: set[str] = set()
    cleaned: dict[str, Meal] = {}
    for key, meal in dp.meals.items():
        if key not in _VALID_MEAL_SLOTS:
            continue
        mtype = key
        if mtype == "breakfast" and any(k in meal.name for k in _LUNCH_NOT_BREAKFAST):
            mtype = "lunch"
        key_name = _meal_name_key(meal.name)
        if prefer_variety and (key_name in used_names or key_name in day_used):
            continue
        cleaned[mtype] = meal.model_copy(update={"meal_type": mtype})
        day_used.add(key_name)

    area = overnight_area(dp, destination)
    pool = research.meals if research else []
    for mtype in _VALID_MEAL_SLOTS:
        if mtype in cleaned:
            continue
        picked = _pick_meal_from_pool(
            pool, mtype, area, destination, used_names, day_used, prefer_variety, day_index,
        )
        if picked:
            cleaned[mtype] = picked.model_copy(update={"meal_type": mtype})
            day_used.add(_meal_name_key(picked.name))
    dp.meals = cleaned
    for meal in cleaned.values():
        used_names.add(_meal_name_key(meal.name))


def _sanitize_all_day_meals(
    daily_plans: list[DayPlan],
    research: ResearchBundle | None,
    parsed: UserQuery | None,
) -> None:
    prefer = bool(parsed and parsed.prefer_meal_variety)
    used: set[str] = set()
    dest = parsed.destination if parsed else "九州"
    for i, dp in enumerate(daily_plans):
        _sanitize_day_meals(
            dp, research, dest,
            used_names=used, prefer_variety=prefer, day_index=i,
        )


def _attraction_key(name: str) -> str:
    return (name or "").strip().lower()


def _diversify_attractions(
    daily_plans: list[DayPlan], research: ResearchBundle | None
) -> None:
    """確定性去重：避免同一景點被放進每一天（弱模型常見問題）。

    跨日掃描，遇到重複景點時，優先換成研究池中尚未使用的景點；
    若池中無可替換選項，則保留原景點（寧可偶爾重複，也不讓某天變空白）。
    """
    if not research or not research.attractions:
        return
    pool = list(research.attractions)
    used: set[str] = set()
    for dp in daily_plans:
        new_attr: list = []
        for a in dp.attractions:
            key = _attraction_key(a.name)
            if key in used:
                repl = next(
                    (p for p in pool if _attraction_key(p.name) not in used), None
                )
                if repl is not None:
                    new_attr.append(repl)
                    used.add(_attraction_key(repl.name))
                else:
                    new_attr.append(a)  # 無可替換 → 保留（避免空白天）
            else:
                new_attr.append(a)
                used.add(key)
        dp.attractions = new_attr


def _strip_segment_polylines(daily_plans: list[DayPlan]) -> None:
    """驗證後剝離 segment 上的 polyline，避免 state 膨脹觸發 len limit。"""
    for dp in daily_plans:
        for seg in dp.drive_segments:
            seg.polyline = None


def generate_draft(state: TravelPlanGraphState) -> dict[str, Any]:
    """Planner Agent：產生每日行程（結構化 LLM 輸出，失敗則用最小 fallback）。

    規劃前先做飯店比價並選定一間，餵給 Planner 並在產出後強制套用，
    讓每日「住宿」與比價表一致（不再由 LLM 自行編造飯店）。
    """
    parsed: UserQuery | None = state.get("parsed")
    research: ResearchBundle | None = state.get("research")
    validation_feedback = state.get("_validation_feedback", "")
    is_retry = bool(validation_feedback) and state.get("retry_count", 0) > 0

    hotel_options, selected_hotel, hotels_by_area = _resolve_hotel_selection(state)
    hotel_state = {
        "hotel_options": hotel_options,
        "selected_hotel": selected_hotel,
        "hotels_by_area": hotels_by_area,
    }
    # 微調流程沿用首輪 price_summary 時，不重設 booker 完成旗標
    reuse_booker = bool(state.get("price_summary"))

    try:
        if research and parsed:
            print("[Node] generate_draft → Planner Agent (per-day)")
            prior_plans = state.get("_prior_daily_plans") or []
            days = max(1, int(getattr(parsed, "days", 5) or 5))

            # 簡單 skeleton：依 research.region_clusters 順序分配每日目標過夜區
            # （重用既有 cluster 資訊；5 天 6 區時會自然循環/壓縮，後續 post-process 仍會調整）
            clusters = research.region_clusters or []
            if clusters:
                day_areas = [ (c.hub_city or c.name or parsed.destination) for c in clusters[:days] ]
                # 若天數 > clusters，循環補；若少則重複最後
                while len(day_areas) < days:
                    day_areas.append(day_areas[-1] if day_areas else parsed.destination)
            else:
                day_areas = [parsed.destination] * days

            daily_plans: list[DayPlan] = []
            prev_loc: str | None = None
            for i, area in enumerate(day_areas, 1):
                # 取該區 hotel 候選（_resolve_hotel_selection 已準備 hotels_by_area）
                area_cands = hotels_by_area.get(area) or hotels_by_area.get(parsed.destination) or []
                pref_h = area_cands[0] if area_cands else selected_hotel

                dp = plan_single_day(
                    research=research,
                    user_query=parsed,
                    day=i,
                    target_area=area,
                    prev_location=prev_loc,
                    validation_feedback=validation_feedback,
                    preferred_hotel=pref_h,
                )
                daily_plans.append(dp)

                # 更新下一天起點（優先 hotel，其次最後景點）— 重用模組內 _hotel_location
                prev_loc = _hotel_location(dp.hotel) if dp.hotel else None
                if not prev_loc and dp.attractions:
                    prev_loc = dp.attractions[-1].location or None

            # 沿用所有既有確定性後處理（最大相容 + 修補 LLM 小錯誤）
            _assign_hotels_by_overnight_areas(
                daily_plans, parsed, hotels_by_area, selected_hotel,
            )
            _fill_missing_hotels(daily_plans, selected_hotel)
            _normalize_hotels(daily_plans)
            _sanitize_all_day_meals(daily_plans, research, parsed)
            _diversify_attractions(daily_plans, research)

            trace_step = {
                "step": "generate_draft",
                "status": "ok (per-day Planner)",
                "days_generated": len(daily_plans),
                "day_areas": day_areas,
                "selected_hotel": selected_hotel.get("name") if selected_hotel else None,
                "is_retry": is_retry,
                "timestamp": datetime.now().isoformat(),
            }
            return {
                "daily_plans": daily_plans,
                "trace": [trace_step],
                "_validated": False,
                "_booked": state.get("_booked", False) if reuse_booker else False,
                "_book_reconciled": state.get("_book_reconciled", False) if reuse_booker else False,
                "_last_worker": "generate_draft",
                **hotel_state,
            }
    except Exception as e:
        print(f"[Node] generate_draft → per-day Planner 失敗，使用最小 fallback：{e}")

    result = _minimal_fallback_plan(state)
    _fill_missing_hotels(result["daily_plans"], selected_hotel)
    _normalize_hotels(result["daily_plans"])
    result.update(hotel_state)
    result.update({
        "_validated": False,
        "_booked": state.get("_booked", False) if reuse_booker else False,
        "_book_reconciled": state.get("_book_reconciled", False) if reuse_booker else False,
    })
    return result


def _minimal_fallback_plan(state: TravelPlanGraphState) -> dict[str, Any]:
    """最小確定性 fallback：用研究資料拼出可用行程，確保 demo 不會整體崩潰。"""
    parsed: UserQuery | None = state.get("parsed")
    research: ResearchBundle | None = state.get("research")

    days = parsed.days if parsed else 5
    destination = parsed.destination if parsed else "九州"
    start_date = parsed.start_date if parsed and parsed.start_date else date(2026, 6, 28)
    attractions = research.attractions if research else []
    meals = research.meals if research else []

    daily_plans: list[DayPlan] = []
    for i in range(days):
        day_attr = attractions[i * 2 : i * 2 + 2] or attractions[:2]
        day_meals = {}
        for j, mtype in enumerate(["breakfast", "lunch", "dinner"]):
            idx = (i * 3 + j) % max(len(meals), 1)
            if meals:
                m = meals[idx]
                day_meals[mtype] = Meal(
                    meal_type=mtype, name=m.name, location=m.location,
                    cuisine=m.cuisine, est_cost_twd=m.est_cost_twd, notes=m.notes,
                )
            else:
                day_meals[mtype] = Meal(
                    meal_type=mtype, name=f"{destination}當地餐", location=destination,
                    est_cost_twd=250 if mtype == "breakfast" else 450,
                )
        daily_plans.append(DayPlan(
            day=i + 1,
            date=str(start_date + timedelta(days=i)),
            drive_total_minutes=40,
            drive_segments=[DriveSegment(
                from_location=f"{destination}住宿", to_location=f"{destination}景點區",
                minutes=40, km=30.0, notes="市區移動",
            )],
            meals=day_meals,
            attractions=day_attr,
            hotel={"name": f"{destination}精選飯店", "est_cost_twd": 7000},
            notes=f"第 {i + 1} 天（最小 fallback 行程）",
        ))

    trace_step = {
        "step": "generate_draft",
        "status": "ok (minimal fallback)",
        "days_generated": len(daily_plans),
        "timestamp": datetime.now().isoformat(),
    }
    return {"daily_plans": daily_plans, "trace": [trace_step]}


def _hotel_location(hotel: dict | None) -> str:
    """從 hotel dict 取出可用於路由的地點字串。"""
    if not hotel:
        return ""
    return (hotel.get("location") or hotel.get("name") or "").strip()


def _same_city(a: str, b: str) -> bool:
    """粗略判斷兩個地點字串是否同一城市（子字串相互包含即視為同城）。"""
    a, b = a.strip(), b.strip()
    if not a or not b:
        return False
    return a == b or a in b or b in a


def _normalize_hotels(daily_plans: list[DayPlan]) -> None:
    """連續同城市的日子共用同一間住宿；只有換城市才換飯店（就地修改）。

    Planner（LLM）常常即使停留同城市也每天換不同飯店（甚至跨城亂跳）。
    這裡確定性地讓「住宿預設延續前一天，除非當天住宿地點換了城市」。
    """
    for i in range(1, len(daily_plans)):
        prev = daily_plans[i - 1].hotel
        prev_loc = _hotel_location(prev)
        if not prev_loc:
            continue  # 前一天無住宿資訊，無從延續
        cur_loc = _hotel_location(daily_plans[i].hotel)
        # 當天沒住宿資訊，或與前一天同城 → 沿用前一天住宿
        if not cur_loc or _same_city(prev_loc, cur_loc):
            daily_plans[i].hotel = dict(prev)


_SEGMENT_PLACEHOLDER_MARKERS = ("景點區", "精選飯店", "住宿")


def _segments_need_rebuild(dp: DayPlan) -> bool:
    """placeholder 或空路段無法算出真實駕車時間，需依景點/住宿重建。"""
    if not dp.drive_segments:
        return True
    for seg in dp.drive_segments:
        blob = f"{seg.from_location} {seg.to_location}"
        if any(m in blob for m in _SEGMENT_PLACEHOLDER_MARKERS):
            return True
        if (seg.minutes or 0) <= 0 and (seg.km or 0) <= 0:
            return True
    return False


def _ensure_drive_segments(daily_plans: list[DayPlan]) -> None:
    """確定性推導每日 drive_segments（就地修改）。

    Planner（LLM）常常不產生 drive_segments（尤其 mixed/public 模式），或 fallback
    留下「九州住宿→景點區」placeholder，導致駕車時間為 0。這裡在段落缺失或為
    placeholder 時，用當天景點與住宿合成 waypoint 路徑供 RouteValidator 計算。

    跨日連續性：前一天的住宿作為當天的起點。連續相同地點會略過（避免 A→A 的 0 段）。
    """
    prev_hotel = ""
    for dp in daily_plans:
        if _segments_need_rebuild(dp):
            dp.drive_segments = []
        if not dp.drive_segments:
            waypoints: list[str] = []
            if prev_hotel:
                waypoints.append(prev_hotel)
            for a in dp.attractions:
                loc = (a.location or "").strip()
                name = (a.name or "").strip()
                if loc:
                    waypoints.append(f"{name} {loc}".strip() if name else loc)
            hotel_loc = _hotel_location(dp.hotel)
            if hotel_loc:
                waypoints.append(hotel_loc)

            segs: list[DriveSegment] = []
            for frm, to in zip(waypoints, waypoints[1:]):
                if frm != to:  # 略過同點段落
                    segs.append(DriveSegment(from_location=frm, to_location=to, minutes=0, km=0.0))
            dp.drive_segments = segs

        prev_hotel = _hotel_location(dp.hotel) or prev_hotel


def _day_main_location(dp: DayPlan) -> tuple[str, str]:
    """選出當天「主要地點」，回傳 (顯示名, 用於地理編碼的查詢字串)。

    取停留時間最長的景點為主；無景點則用住宿。
    """
    if dp.attractions:
        a = max(dp.attractions, key=lambda x: x.estimated_time_minutes or 0)
        loc = (a.location or "").strip()
        query = (f"{a.name} {loc}".strip()) or a.name
        return a.name, query
    hotel_loc = _hotel_location(dp.hotel)
    return (hotel_loc, hotel_loc) if hotel_loc else ("", "")


def _build_daily_markers(daily_plans: list[DayPlan], destination: str = "") -> dict[int, dict]:
    """為每天產生一個「主要地點」地圖標記（與交通方式無關），用於行程地圖。"""
    from src.tools.maps import geocode_location

    markers: dict[int, dict] = {}
    for dp in daily_plans:
        name, query = _day_main_location(dp)
        if not query:
            continue
        coord = geocode_location(query, region_hint=destination)
        if coord:
            markers[dp.day] = {
                "name": name or query,
                "location": (dp.attractions[0].location if dp.attractions else _hotel_location(dp.hotel)),
                "lat": coord[0],
                "lng": coord[1],
            }
    return markers


def validate_route(state: TravelPlanGraphState) -> dict[str, Any]:
    """RouteValidator：確定性計算每日駕車時間，收集 polyline，並建立行程地圖標記。"""
    daily_plans: list[DayPlan] = state.get("daily_plans", [])
    retry_count = state.get("retry_count", 0)
    parsed: UserQuery | None = state.get("parsed")

    print("[Node] validate_route")
    # Planner 未給 drive_segments 時，先用當天地點序列確定性推導，確保有駕車時間與地圖路線
    _ensure_drive_segments(daily_plans)
    result = run_route_validator_crew(daily_plans=daily_plans, user_query=parsed)

    daily_polylines: dict[int, list[str]] = {}
    for dp in daily_plans:
        daily_polylines[dp.day] = [s.polyline for s in dp.drive_segments if s.polyline]
    _strip_segment_polylines(daily_plans)

    # 行程地圖：每天一個主要地點標記（不分自駕/大眾運輸）
    daily_markers = _build_daily_markers(daily_plans, parsed.destination if parsed else "")

    errors = result.violations
    feedback = "\n".join(result.suggestions) if result.suggestions else ""

    trace_step = {
        "step": "validate_route",
        "status": "PASS" if not errors else f"FAIL ({len(errors)} issues)",
        "feedback": feedback,
        "daily_drive_minutes": {dp.day: dp.drive_total_minutes for dp in daily_plans},
        "timestamp": datetime.now().isoformat(),
    }
    # 駕車只是品質的一環；最終 status 由 format_output 依完整 constraint 檢查誠實決定。
    return {
        "daily_plans": daily_plans,
        "validation_errors": errors,
        "status": "running",
        "_validated": True,
        "_validation_feedback": feedback,
        "daily_polylines": daily_polylines,
        "daily_markers": daily_markers,
        "_last_worker": "validate_route",
        "_stall_count": 0 if errors else state.get("_stall_count", 0),
        "trace": [trace_step],
    }


def booker(state: TravelPlanGraphState) -> dict[str, Any]:
    """Booker Agent：飯店/租車比價 + 預算檢查。"""
    daily_plans: list[DayPlan] = state.get("daily_plans", [])
    parsed: UserQuery | None = state.get("parsed")

    print("[Node] booker → 比價與預算檢查")
    summary = run_booker_crew(
        daily_plans=daily_plans,
        user_query=parsed,
        hotel_options=state.get("hotel_options") or None,
    )
    # 確定性預算修復：超預算時自動換最便宜住宿（就地更新 daily_plans 的 hotel）
    reconciled = False
    if parsed and not summary.within_budget:
        before = summary.grand_total
        summary = reconcile_to_budget(summary, daily_plans, parsed)
        reconciled = summary.grand_total < before or summary.within_budget

    price_summary = {
        "estimated_total_twd": summary.grand_total,
        "flight_total": summary.flight_total,
        "accommodation_total": summary.accommodation_total,
        "car_rental_estimate": summary.car_rental_estimate,
        "transport_total": summary.transport_total,
        "meals_estimate": summary.meals_estimate,
        "attractions_tickets": summary.attractions_tickets,
        "budget": parsed.budget_twd if parsed else 40000,
        "within_budget": summary.within_budget,
        "flight_options": summary.flight_options,
        "hotel_options": summary.hotel_options,
        "car_options": summary.car_options,
        "note": summary.notes,
    }

    trace_step = {
        "step": "booker",
        "status": "ok" if summary.within_budget else "over_budget",
        "grand_total": summary.grand_total,
        "hotel_options": len(summary.hotel_options),
        "timestamp": datetime.now().isoformat(),
    }
    # 回傳 daily_plans：預算修復可能就地換了住宿，需讓 state 反映
    return {
        "price_summary": price_summary,
        "daily_plans": daily_plans,
        "_booked": True,
        "_book_reconciled": reconciled or summary.within_budget,
        "_last_worker": "booker",
        "trace": [trace_step],
    }


def _legacy_supervisor_decision(action: AgentAction) -> str:
    """將自主路由動作對應到舊版 trace / UI 相容的 decision 字串。"""
    if action == AgentAction.PLAN:
        return SupervisorDecision.RETRY_PLANNER.value
    if action == AgentAction.RESEARCH:
        return SupervisorDecision.RETRY_RESEARCH.value
    return SupervisorDecision.PROCEED_TO_OUTPUT.value


def supervisor_node(state: TravelPlanGraphState) -> dict[str, Any]:
    """Supervisor 中樞：LLM 自主選下一步 + guardrails + constraints + best-of-N。"""
    parsed: UserQuery | None = state.get("parsed")
    if not parsed:
        return {
            "next_action": AgentAction.FINISH.value,
            "supervisor_decision": SupervisorDecision.PROCEED_TO_OUTPUT.value,
            "trace": [{
                "step": "supervisor",
                "next_action": AgentAction.FINISH.value,
                "status": "no parsed input",
                "timestamp": datetime.now().isoformat(),
            }],
        }

    research: ResearchBundle | None = state.get("research")
    daily_plans = state.get("daily_plans", [])
    retry_count = state.get("retry_count", 0)
    max_retries = state.get("max_retries", 3)
    max_steps = state.get("_max_supervisor_steps", 25)
    steps = state.get("_supervisor_steps", 0) + 1
    price_summary = state.get("price_summary", {})
    user_feedback = parsed.user_feedback

    violations = check_constraints(daily_plans, price_summary, parsed)
    viol_dicts = to_dicts(violations)
    n_hard = sum(1 for v in violations if v.severity == "hard")
    score = _plan_score(violations)

    # 僅在 booker 跑完（預算已納入）後才納入 best-of-N，避免未比價版本被當最佳
    best_update = (
        _maybe_update_best(state, daily_plans, price_summary, violations)
        if daily_plans and state.get("_validated") and state.get("_booked")
        else {}
    )

    prev_score = state.get("_plan_violation_score")
    stall_count = state.get("_stall_count", 0)
    # 僅在「剛完成重規劃」時累加 stall；research/validate 不應消耗 stall 額度
    if state.get("_last_worker") == "generate_draft" and prev_score is not None:
        if tuple(prev_score) <= tuple(score):
            stall_count += 1
        else:
            stall_count = 0

    router_state = dict(state)
    router_state["violations"] = viol_dicts

    if steps >= max_steps:
        action = AgentAction.FINISH
        reasoning = f"已達 Supervisor 步數上限（{max_steps}），輸出目前最佳結果。"
    elif stall_count >= 2 and retry_count >= max_retries:
        from src.agents.supervisor import converge_after_retry_exhausted

        action = converge_after_retry_exhausted(router_state)
        reasoning = "連續重規劃未改善且已達重試上限，改走驗證與比價後輸出。"
    else:
        routed = decide_next_action(router_state, parsed, retry_count, max_retries)
        action = routed.next_action
        reasoning = routed.reasoning
        if action == AgentAction.PLAN and retry_count >= max_retries:
            from src.agents.supervisor import converge_after_retry_exhausted

            action = converge_after_retry_exhausted(router_state)
            reasoning = "已達重試上限，不再重規劃，改走驗證與比價。"

    decision_value = _legacy_supervisor_decision(action)
    new_retry = retry_count
    if action == AgentAction.PLAN and has_hard(violations) and retry_count < max_retries:
        new_retry = retry_count + 1

    planner_feedback = build_planner_feedback(violations, parsed) if action == AgentAction.PLAN else ""
    feedback = "\n".join(p for p in (user_feedback, planner_feedback) if p).strip()
    if not reasoning or reasoning.startswith("（後備策略）"):
        reasoning = _supervisor_reasoning(
            research, parsed, daily_plans, violations, price_summary,
            new_retry, max_retries, decision_value,
        )

    print(
        f"[Node] supervisor → {action.value}（{len(violations)} 違規 / {n_hard} hard，"
        f"retry={new_retry}，step={steps}）"
    )

    trace_step = {
        "step": "supervisor",
        "next_action": action.value,
        "decision": decision_value,
        "reasoning": reasoning,
        "confidence": 0.9,
        "violations": len(violations),
        "hard_violations": n_hard,
        "retry_count": new_retry,
        "supervisor_step": steps,
        "timestamp": datetime.now().isoformat(),
    }
    out = {
        "next_action": action.value,
        "supervisor_decision": decision_value,
        "supervisor_reasoning": reasoning,
        "retry_count": new_retry,
        "_validation_feedback": feedback,
        "violations": viol_dicts,
        "_supervisor_steps": steps,
        "_stall_count": stall_count,
        "_plan_violation_score": list(score),
        "trace": [trace_step],
    }
    out.update(best_update)
    return out


def _plan_score(violations) -> tuple[int, int]:
    """plan 品質分數：(hard 違規數, 預算超出額)，越小越好。"""
    n_hard = sum(1 for v in violations if v.severity == "hard")
    over = next((v.data.get("over", 0) for v in violations if v.category == "budget"), 0)
    return (n_hard, over)


def _plan_score_from_dicts(violations: list[dict]) -> tuple[int, int]:
    """同 _plan_score，但吃序列化後的 dict（state 內存的是 dict）。"""
    n_hard = sum(1 for v in violations if v.get("severity") == "hard")
    over = next((v["data"].get("over", 0) for v in violations if v.get("category") == "budget"), 0)
    return (n_hard, over)


def _maybe_update_best(state, daily_plans, price_summary, violations) -> dict[str, Any]:
    """若目前版本優於已記錄的最佳版本，更新 best 快照。回傳要併入 state 的欄位。"""
    import copy

    score = _plan_score(violations)
    prev = state.get("_best_score")
    if prev is not None and tuple(prev) <= score:
        return {}
    return {
        "_best_score": list(score),
        "_best": {
            "daily_plans": copy.deepcopy(daily_plans),
            "price_summary": copy.deepcopy(price_summary),
            "violations": to_dicts(violations),
            "daily_polylines": copy.deepcopy(state.get("daily_polylines", {})),
            "daily_markers": copy.deepcopy(state.get("daily_markers", {})),
        },
    }


def _supervisor_reasoning(
    research, parsed, daily_plans, violations, price_summary,
    retry_count, max_retries, decision_value,
) -> str:
    """用 LLM 產生決策說明（純解釋，不影響決策）；失敗時用確定性文字。"""
    try:
        decision = run_supervisor(
            research=research,
            user_query=parsed,
            daily_plans=daily_plans,
            validation_errors=[v.message for v in violations],
            validation_feedback=summarize(violations),
            retry_count=retry_count,
            max_retries=max_retries,
            price_summary=price_summary,
            user_feedback=parsed.user_feedback if parsed else "",
        )
        if decision and decision.reasoning:
            return decision.reasoning
    except Exception as e:  # noqa: BLE001
        print(f"[supervisor] 說明生成失敗（不影響決策）：{e}")
    if decision_value == SupervisorDecision.RETRY_PLANNER.value:
        return f"偵測到 {sum(1 for v in violations if v.severity=='hard')} 項必須修正的問題，請 Planner 依指令重規劃。"
    if violations:
        return "已達重試上限，仍有部分問題未解；輸出目前最佳版本並誠實標示待改善項。"
    return "行程通過所有約束檢查，直接輸出。"


_ACTION_TO_NODE: dict[str, str] = {
    AgentAction.RESEARCH.value: "research",
    AgentAction.PLAN.value: "generate_draft",
    AgentAction.VALIDATE.value: "validate_route",
    AgentAction.BOOK.value: "booker",
    AgentAction.FINISH.value: "format_output",
}


def route_after_supervisor(state: TravelPlanGraphState) -> str:
    """Supervisor 自主路由：依 next_action 派工至 worker 或結束。"""
    if state.get("_supervisor_steps", 0) >= state.get("_max_supervisor_steps", 25):
        return "format_output"

    action = state.get("next_action", AgentAction.FINISH.value)
    return _ACTION_TO_NODE.get(action, "format_output")


def format_output(state: TravelPlanGraphState) -> dict[str, Any]:
    """產生動態 Markdown 行程表（含預算明細 + 飯店比價）。"""
    parsed: UserQuery | None = state.get("parsed")
    research = state.get("research")

    # best-of-N：若目前版本比歷史最佳差，改用最佳版本輸出（避免輸出最後一版的爛行程）
    best = state.get("_best")
    cur_violations = state.get("violations", []) or []
    cur_score = _plan_score_from_dicts(cur_violations)
    restored = {}
    if best is not None and tuple(state.get("_best_score", [99, 0])) < cur_score:
        daily_plans = best["daily_plans"]
        price = best["price_summary"]
        restored = {
            "daily_plans": daily_plans,
            "price_summary": price,
            "violations": best["violations"],
            "daily_polylines": best.get("daily_polylines", {}),
            "daily_markers": best.get("daily_markers", {}),
        }
        print(f"[Node] format_output → 採用 best-of-N 版本（score={state.get('_best_score')} < {cur_score}）")
    else:
        daily_plans = state.get("daily_plans", [])
        price = state.get("price_summary", {})

    out_violations_pre = restored.get("violations", cur_violations)
    md = build_itinerary_md(
        daily_plans, price, parsed, out_violations_pre, research,
    )
    out_violations = out_violations_pre
    hard = [v for v in out_violations if v.get("severity") == "hard"]
    if hard or not price:
        final_status = "needs_attention"
    else:
        final_status = "success"
    trace_step = {
        "step": "format_output",
        "status": "ok" if not hard else f"needs_attention ({len(hard)} hard)",
        "timestamp": datetime.now().isoformat(),
    }

    return {
        "final_itinerary_md": md,
        "daily_plans": daily_plans,
        "price_summary": price,
        "violations": out_violations,
        "trace": [trace_step],
        "status": final_status,
        **{k: v for k, v in restored.items() if k not in ("daily_plans", "price_summary", "violations")},
    }


# =============================================================================
# Graph Builder
# =============================================================================

def build_travel_graph() -> CompiledStateGraph:
    """Autonomous hub-and-spoke：Supervisor 中樞動態派工。"""
    graph = StateGraph(TravelPlanGraphState)

    graph.add_node("parse", parse_input)
    graph.add_node("research", research)
    graph.add_node("generate_draft", generate_draft)
    graph.add_node("validate_route", validate_route)
    graph.add_node("booker", booker)
    graph.add_node("supervisor", supervisor_node)
    graph.add_node("format_output", format_output)

    graph.set_entry_point("parse")
    graph.add_edge("parse", "supervisor")
    for worker in ("research", "generate_draft", "validate_route", "booker"):
        graph.add_edge(worker, "supervisor")

    graph.add_conditional_edges(
        "supervisor",
        route_after_supervisor,
        {
            "format_output": "format_output",
            "generate_draft": "generate_draft",
            "research": "research",
            "validate_route": "validate_route",
            "booker": "booker",
        },
    )
    graph.add_edge("format_output", END)
    return graph.compile()


def refine_gate(state: TravelPlanGraphState) -> dict[str, Any]:
    """微調後備：僅在使用者設了駕車上限且仍違規時，最多再規劃一次。"""
    parsed: UserQuery | None = state.get("parsed")
    retry = state.get("retry_count", 0)
    max_retries = state.get("max_retries", 0)
    errors = state.get("validation_errors", []) or []
    drive_related = any("駕車" in e for e in errors)

    if (
        drive_related
        and parsed
        and parsed.max_daily_drive_minutes is not None
        and retry < max_retries
    ):
        from src.agents.feedback import build_refine_planner_feedback

        feedback = build_refine_planner_feedback(
            parsed, prior_plans=state.get("_prior_daily_plans"),
        )
        if errors:
            feedback = f"{feedback}\n\n【駕車驗證】\n" + "\n".join(errors)
        return {
            "retry_count": retry + 1,
            "_validation_feedback": feedback,
            "_retry_refine": True,
            "trace": [{
                "step": "refine_gate",
                "status": f"retry plan ({retry + 1}/{max_retries})",
                "timestamp": datetime.now().isoformat(),
            }],
        }
    return {
        "_retry_refine": False,
        "trace": [{
            "step": "refine_gate",
            "status": "finish",
            "timestamp": datetime.now().isoformat(),
        }],
    }


def route_after_refine_gate(state: TravelPlanGraphState) -> str:
    return "generate_draft" if state.get("_retry_refine") else "format_output"


def build_refine_graph() -> CompiledStateGraph:
    """微調直線流程：plan → validate → format（跳過 Supervisor / research / booker）。"""
    graph = StateGraph(TravelPlanGraphState)

    graph.add_node("generate_draft", generate_draft)
    graph.add_node("validate_route", validate_route)
    graph.add_node("refine_gate", refine_gate)
    graph.add_node("format_output", format_output)

    graph.set_entry_point("generate_draft")
    graph.add_edge("generate_draft", "validate_route")
    graph.add_edge("validate_route", "refine_gate")
    graph.add_conditional_edges(
        "refine_gate",
        route_after_refine_gate,
        {"generate_draft": "generate_draft", "format_output": "format_output"},
    )
    graph.add_edge("format_output", END)
    return graph.compile()


# =============================================================================
# Public API
# =============================================================================

_graph: CompiledStateGraph | None = None
_refine_graph: CompiledStateGraph | None = None


def get_graph() -> CompiledStateGraph:
    global _graph
    if _graph is None:
        _graph = build_travel_graph()
    return _graph


def get_refine_graph() -> CompiledStateGraph:
    global _refine_graph
    if _refine_graph is None:
        _refine_graph = build_refine_graph()
    return _refine_graph


def _build_initial_state(user_input: Union[str, UserQuery]) -> TravelPlanGraphState:
    """組首輪的 graph 初始狀態（invoke 與 stream 共用）。"""
    if isinstance(user_input, UserQuery):
        query_str = user_input.raw_input
        parsed_input = user_input
    else:
        query_str = user_input
        parsed_input = None

    return {
        "user_query": query_str,
        "parsed": parsed_input,
        "research": None,
        "daily_plans": [],
        "price_summary": {},
        "validation_errors": [],
        "retry_count": 0,
        "max_retries": 3,
        "final_itinerary_md": None,
        "sources": [],
        "status": "running",
        "trace": [],
        "_validated": False,
        "_booked": False,
        "_book_reconciled": False,
        "_supervisor_steps": 0,
        "_max_supervisor_steps": 25,
        "_stall_count": 0,
    }


def run_planner(user_input: Union[str, UserQuery]) -> TravelPlanStatePydantic:
    """主入口：接受原始字串或結構化 UserQuery。"""
    final_state = get_graph().invoke(_build_initial_state(user_input))
    return graph_state_to_pydantic(final_state)


def run_planner_stream(user_input: Union[str, UserQuery]):
    """串流版主入口：每個節點完成就 yield 當前完整狀態（供 UI 即時顯示進度）。"""
    for state in get_graph().stream(_build_initial_state(user_input), stream_mode="values"):
        yield graph_state_to_pydantic(state)


def run_planner_refine(
    prior_state: TravelPlanStatePydantic,
    user_query: UserQuery,
) -> TravelPlanStatePydantic:
    """依使用者回饋微調行程：沿用 prior_state 的 research / booker 結果，
    只重跑 Planner→Validate→Supervisor。

    Args:
        prior_state: 上一輪 run_planner / run_planner_refine 的完整結果。
        user_query:  已套用回饋的 UserQuery（含 max_daily_drive_minutes / user_feedback）。
    """
    final_state = get_refine_graph().invoke(_build_refine_initial_state(prior_state, user_query))
    return graph_state_to_pydantic(final_state)


def _build_refine_initial_state(
    prior_state: TravelPlanStatePydantic,
    user_query: UserQuery,
) -> TravelPlanGraphState:
    """組微調流程的 graph 初始狀態（invoke 與 stream 共用）。"""
    from src.agents.feedback import build_refine_planner_feedback

    # 僅駕車上限類回饋允許一次重試；餐飲/景點/區域停留類回饋直線一次到位
    max_retries = 1 if user_query.max_daily_drive_minutes is not None else 0
    prior_plans = list(prior_state.daily_plans or [])

    return {
        "user_query": user_query.raw_input,
        "parsed": user_query,
        "research": prior_state.research,
        "price_summary": prior_state.price_summary,
        "sources": prior_state.sources,
        "daily_polylines": {},
        "hotel_options": (prior_state.price_summary or {}).get("hotel_options", []),
        "daily_plans": [],
        "validation_errors": [],
        "retry_count": 0,
        "max_retries": max_retries,
        "_validation_feedback": build_refine_planner_feedback(
            user_query, prior_plans=prior_plans,
        ),
        "_prior_daily_plans": prior_plans,
        "final_itinerary_md": None,
        "status": "running",
        "trace": [],
        "_validated": False,
        "_booked": True,
        "_book_reconciled": True,
        "_refine_mode": True,
        "_supervisor_steps": 0,
        "_max_supervisor_steps": 0,
        "_stall_count": 0,
    }


def run_planner_refine_stream(
    prior_state: TravelPlanStatePydantic,
    user_query: UserQuery,
):
    """串流版微調入口：每個節點完成就 yield 當前完整狀態。"""
    initial = _build_refine_initial_state(prior_state, user_query)
    for state in get_refine_graph().stream(initial, stream_mode="values"):
        yield graph_state_to_pydantic(state)


# =============================================================================
# CLI Demo
# =============================================================================

def run_planner_demo(query: str | None = None) -> TravelPlanStatePydantic:
    demo_parsed = UserQuery(
        raw_input=query or "我想在 6 月底去九州玩 5 天，預算 4 萬，想自駕，含美食與景點，幫我比價飯店",
        destination="九州",
        days=5,
        budget_twd=40000,
        start_date=date(2026, 6, 28),
        preferences=["美食", "自然景觀", "溫泉"],
        travel_mode="self_drive",
    )
    return run_planner(demo_parsed)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--query", type=str, default=None)
    args = parser.parse_args()

    state = run_planner_demo(args.query)

    print("\n" + "=" * 60)
    print(f"狀態：{state.status} | 重試次數：{state.retry_count}")
    print(f"景點：{len(state.research.attractions) if state.research else 0} | "
          f"部落格來源：{len(state.sources)} 篇 | "
          f"每日駕車：{[dp.drive_total_minutes for dp in state.daily_plans]}")
    if state.final_itinerary_md:
        print("\n--- Markdown 輸出（前 1000 字）---")
        print(state.final_itinerary_md[:1000] + "...\n")
    print("=" * 60 + "\n")


def run_planner_with_forced_violation(query: str | None = None) -> TravelPlanStatePydantic:
    """Demo：強制一段超長駕車，觀察 retry + 修正 loop。"""
    from src.tools import maps as maps_tool

    original = maps_tool.MOCK_DRIVE_TIMES_MINUTES.copy()
    maps_tool.MOCK_DRIVE_TIMES_MINUTES[("九州住宿", "九州景點區")] = 165
    try:
        state = run_planner(query or UserQuery(
            raw_input="九州自駕 5 天", destination="九州", days=5, budget_twd=40000,
            start_date=date(2026, 6, 28), preferences=["美食"], travel_mode="self_drive",
        ))
        print("Status:", state.status, "| Retries:", state.retry_count)
        print("Drive/day:", [dp.drive_total_minutes for dp in state.daily_plans])
        return state
    finally:
        maps_tool.MOCK_DRIVE_TIMES_MINUTES.clear()
        maps_tool.MOCK_DRIVE_TIMES_MINUTES.update(original)


if __name__ == "__main__":
    main()
