"""Mock demo trace 步驟強化：注入動態 Supervisor 決策與各 Agent 摘要。"""

from __future__ import annotations

import copy
from typing import Any

from src.core.models import DayPlan, ResearchBundle, UserQuery
from src.demo.feedback_intent import wants_day4_transit

_MODE_LABELS = {
    "self_drive": "自駕",
    "mixed": "自駕 + 大眾運輸",
    "public": "大眾運輸",
}

_ACTION_WORKER = {
    "research": "Researcher Agent",
    "plan": "Planner Agent",
    "validate": "RouteValidator Agent",
    "book": "Booker Agent",
    "finish": "Output Formatter Agent",
}


def enrich_trace_step(
    step: dict[str, Any],
    *,
    query: UserQuery,
    price: dict[str, Any] | None = None,
    research: ResearchBundle | None = None,
    daily_plans: list[DayPlan] | None = None,
) -> dict[str, Any]:
    """依當前查詢與比價結果，強化 mock trace 的 reasoning / summary。"""
    s = copy.deepcopy(step)
    price = price or {}
    name = s.get("step", "")
    dest = query.destination
    days = query.days
    budget = query.budget_twd
    mode = _MODE_LABELS.get(query.travel_mode, query.travel_mode)
    prefs = "、".join(query.preferences) if query.preferences else "一般"
    feedback = query.user_feedback or ""
    is_refine = bool(step.get("is_retry") or feedback)
    day4_transit = wants_day4_transit(feedback)
    day4_plan = next((dp for dp in (daily_plans or []) if dp.day == 4), None)

    if name == "parse_input":
        s["summary"] = (
            f"已解析結構化需求：{dest} {days} 天、預算 {budget:,} TWD、"
            f"交通 {mode}、偏好 {prefs}。"
        )

    elif name == "research":
        n_attr = len(research.attractions) if research else s.get("attractions", 0)
        n_meal = len(research.meals) if research else s.get("meals", 0)
        n_blog = len(research.blog_sources) if research else 0
        s["attractions"] = n_attr
        s["meals"] = n_meal
        s["status"] = f"ok ({n_blog} 篇部落格 grounding)"
        blogs = (research.blog_sources if research else [])[:3]
        s["summary"] = (
            f"Serper 搜尋旅遊攻略 → 擷取 {n_attr} 個景點、{n_meal} 家餐廳；"
            f"部落格 grounding {n_blog} 篇。"
        )
        if blogs:
            s["blog_titles"] = [b.title for b in blogs]

    elif name == "generate_draft":
        retry = s.get("is_retry")
        if retry and day4_transit:
            s["summary"] = (
                f"依回饋「{feedback}」局部重排：保留 D1–D3 與 D5，"
                "將 D4 改為箱根周遊券＋浪漫特快等大眾運輸串接，移除自駕路段。"
            )
            s["day_areas"] = ["淺草・晴空塔", "澀谷・原宿", "上野・築地", "箱根大眾運輸", "皇居・銀座"]
        else:
            s["summary"] = (
                f"依研究聚類產出 {s.get('days_generated', days)} 天行程草稿"
                + ("（依使用者回饋重排）" if retry else "")
                + f"；交通模式 {mode}，新宿為住宿基地。"
            )
            s["day_areas"] = ["淺草・晴空塔", "澀谷・原宿", "上野・築地", "箱根自駕", "皇居・銀座"]

    elif name == "validate_route":
        if is_refine and day4_transit and day4_plan:
            transit_min = day4_plan.transit_total_minutes or 97
            s["daily_drive_minutes"] = {"1": 0, "2": 0, "3": 0, "4": 0, "5": 0}
            s["feedback"] = (
                "重驗 D4 大眾運輸動線：新宿→箱根湯本（浪漫特快）→"
                f"蘆之湖遊船→箱根神社，全日移動約 {transit_min} 分鐘，無自駕路段。"
            )
        else:
            ddm = s.get("daily_drive_minutes") or {}
            if daily_plans and is_refine:
                ddm = {str(dp.day): dp.drive_total_minutes for dp in daily_plans}
                s["daily_drive_minutes"] = ddm
            drive_parts = [f"D{d} 自駕 {m} 分" for d, m in ddm.items() if int(m) > 0]
            transit_note = "其餘日程以地鐵為主，未超過駕車上限。"
            s["feedback"] = (
                "路線驗證通過。"
                + (" " + " · ".join(drive_parts) + "。" if drive_parts else " ")
                + transit_note
            )
        s["summary"] = s["feedback"]

    elif name == "booker":
        grand = s.get("grand_total") or price.get("estimated_total_twd", 0)
        flight = price.get("flight_total", 0)
        acc = price.get("accommodation_total", 0)
        hotels = price.get("hotel_options") or []
        hotel_name = hotels[0]["name"] if hotels else "精選飯店"
        s["grand_total"] = grand
        s["hotel_options"] = len(hotels) or s.get("hotel_options", 0)
        within = price.get("within_budget", True)
        s["status"] = "ok" if within else "over_budget"
        s["summary"] = (
            f"機票最低 {flight:,} · 住宿 {acc:,}（{hotel_name}）· "
            f"預估總計 {grand:,} TWD"
            + ("，在預算內。" if within else f"，超出預算 {grand - budget:,} TWD。")
        )

    elif name == "format_output":
        s["summary"] = "彙整每日行程、比價表與參考來源，產出 Markdown 計劃書。"

    elif name == "refine_gate":
        if is_refine and day4_transit:
            s["summary"] = "局部重排完成：D4 已改為大眾運輸版箱根一日遊，報告已更新。"
        else:
            s["summary"] = (
                "已套用使用者回饋；駕車與餐飲約束符合，無需二次重規劃，直接輸出。"
                if not s.get("retry_refine")
                else "仍有效違規，觸發第二輪 Planner 重試。"
            )

    elif name == "supervisor":
        action = s.get("next_action", "")
        worker = _ACTION_WORKER.get(action, action)
        n_blog = len(research.blog_sources) if research else 0
        grand = price.get("estimated_total_twd", 0)
        within = price.get("within_budget", True)

        if action == "research":
            s["reasoning"] = (
                f"【狀態評估】行程尚未建立，research 為空。\n"
                f"【決策】啟動 {worker}，以 {dest} {days} 天、偏好「{prefs}」"
                f"搜尋景點/美食並建立 {n_blog or '≥3'} 篇部落格 grounding，降低幻覺風險。\n"
                f"【護欄】無違規；retry 0/3。"
            )
        elif action == "plan":
            s["reasoning"] = (
                f"【狀態評估】研究完成（景點/餐飲池已足），尚無 daily_plans。\n"
                f"【決策】派 {worker} 以 {mode} 模式排版 {days} 天 4 夜："
                f"都心地鐵為主、第 4 天箱根改自駕。\n"
                f"【約束】每日需含三餐與 ≥1 景點；住宿連住新宿。\n"
                f"【護欄】無 hard 違規；retry 0/3。"
            )
        elif action == "validate":
            s["reasoning"] = (
                f"【狀態評估】Planner 已產出 {days} 天草稿，尚未驗證移動時間。\n"
                f"【決策】交由 {worker} 計算每日自駕/大眾運輸分鐘數，"
                f"確認第 4 天箱根自駕段落合理。\n"
                f"【護欄】無違規；retry 0/3。"
            )
        elif action == "book":
            s["reasoning"] = (
                f"【狀態評估】路線驗證 PASS；第 4 天自駕 163 分，其餘日程地鐵為主。\n"
                f"【決策】啟動 {worker} 比價機票（TPE↔NRT）與新宿住宿，"
                f"併入租車/門票/餐飲估算總預算。\n"
                f"【護欄】無 hard 違規；retry 0/3。"
            )
        elif action == "finish":
            budget_note = (
                f"總計 {grand:,} TWD 在預算 {budget:,} 內"
                if within
                else f"總計 {grand:,} TWD 略超預算 {budget:,}（誠實標示於計劃書）"
            )
            s["reasoning"] = (
                f"【狀態評估】Booker 完成；{budget_note}。\n"
                f"【決策】無需重試，交由 {worker} 輸出最佳版本計劃書"
                f"（含比價表與 {n_blog} 篇參考來源）。\n"
                f"【護欄】0 hard 違規；流程收斂。"
            )
        s["dispatch_to"] = worker

    return s