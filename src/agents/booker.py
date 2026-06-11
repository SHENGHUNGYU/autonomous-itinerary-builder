"""
Booker Agent - 住宿 / 租車比價與預算檢查

職責：
- 用 hotel_search_tool / carrental_search_tool / flight_search_tool 查詢比價（不執行預訂）
- 估算總花費（機票 + 住宿 + 租車 + 門票 + 餐飲）並做預算檢查
- 回傳比價選項清單 + 總價摘要

這是系統中最具代表性的「多工具 ReAct agent」：真實模式下由 LLM 自主編排
機票、飯店與租車三種工具的查詢，再做性價比與預算權衡。結構化總價則由工具結果可靠計算。
"""

from __future__ import annotations

import os

from pydantic import BaseModel, Field

from src.agents.skills_loader import compose_system_prompt, load_agent_skill
from src.core.models import DayPlan, UserQuery
from src.services.llm import get_chat_llm
from src.tools.flights import flight_search_tool, search_flights
from src.tools.mocks import is_mock_mode
from src.tools.web_research import (
    carrental_search_tool,
    hotel_search_tool,
    search_car_rental,
    search_hotels,
    search_price_twd,
    serper_price_tool,
)


def _travel_days_from_plan(daily_plans: list[DayPlan], user_query: UserQuery) -> tuple[int, int]:
    """從行程段落推導租車/大眾運輸天數（任何目的地通用）。"""
    mode = user_query.travel_mode
    days = user_query.days
    if mode == "self_drive":
        return days, 0
    if mode == "public":
        return 0, days

    drive_days = 0
    transit_days = 0
    for dp in daily_plans:
        segs = dp.drive_segments or []
        if any(getattr(s, "mode", "drive") == "drive" for s in segs):
            drive_days += 1
        if any(getattr(s, "mode", "drive") in ("transit", "flight") for s in segs):
            transit_days += 1
    if drive_days == 0 and transit_days == 0:
        return (days + 1) // 2, days // 2
    return max(drive_days, 0), max(transit_days, 0)


def budget_per_night_twd(user_query: UserQuery) -> int:
    """每晚住宿預算 = 總預算的 45% 平攤到晚數，下限 3000。"""
    nights = max(user_query.days - 1, 1)
    return max(int(user_query.budget_twd * 0.45 / nights), 3000)


def hotel_to_dayplan(h: dict, destination: str = "") -> dict:
    """把飯店比價選項（search_hotels 的格式）轉成 DayPlan.hotel 用的 dict。"""
    return {
        "name": h.get("name") or f"{destination}精選飯店",
        "location": (h.get("location") or destination or "").strip(),
        "est_cost_twd": int(h.get("price_per_night_twd") or h.get("est_cost_twd") or 7000),
        "rating": h.get("rating"),
        "link": h.get("link", ""),
    }


def select_best_hotel(
    hotel_options: list[dict],
    budget_per_night: int,
    destination: str = "",
) -> dict | None:
    """從比價選項挑「最佳性價比」飯店，回傳 DayPlan.hotel 格式。

    規則：優先取每晚價 ≤ 預算者，其中挑評分最高（同分取較便宜）；
    若全部超過預算，則退而取最便宜的一間。
    """
    if not hotel_options:
        return None
    within = [h for h in hotel_options if (h.get("price_per_night_twd") or 0) <= budget_per_night]
    if within:
        # 預算內：挑評分最高（同分取較便宜）
        best = max(within, key=lambda h: ((h.get("rating") or 0), -(h.get("price_per_night_twd") or 0)))
    else:
        # 全部超過預算：退而取最便宜的一間
        best = min(hotel_options, key=lambda h: (h.get("price_per_night_twd") or 0))
    return hotel_to_dayplan(best, destination)


class PriceSummary(BaseModel):
    flight_total: int = Field(0)
    accommodation_total: int = Field(0)
    car_rental_estimate: int = Field(0)
    transport_total: int = Field(0)
    attractions_tickets: int = Field(0)
    meals_estimate: int = Field(0)
    grand_total: int = Field(0)
    within_budget: bool = Field(True)
    flight_options: list[dict] = Field(default_factory=list, description="機票比價選項")
    hotel_options: list[dict] = Field(default_factory=list, description="飯店比價選項")
    car_options: list[dict] = Field(default_factory=list, description="租車比價選項")
    notes: str = ""


def run_booker_crew(
    daily_plans: list[DayPlan],
    user_query: UserQuery,
    llm=None,
    hotel_options: list[dict] | None = None,
) -> PriceSummary:
    """執行比價與預算檢查，回傳 PriceSummary（含比價選項）。

    hotel_options：若上游（generate_draft）已查好飯店，直接沿用以保持與每日住宿一致，
    避免重複查詢、也避免比價表與行程內住宿對不上。
    """
    destination = user_query.destination
    nights = max(user_query.days - 1, 1)
    budget_per_night = budget_per_night_twd(user_query)

    # 機票：去回程（依 start_date 推算回程 = 出發 + days）
    depart = str(user_query.start_date) if user_query.start_date else "2026-06-28"
    from datetime import date, timedelta

    base_date = user_query.start_date or date(2026, 6, 28)
    return_date = str(base_date + timedelta(days=max(user_query.days - 1, 1)))
    flight_options = search_flights(
        destination, depart_date=depart, return_date=return_date, adults=user_query.adults
    )

    if hotel_options is None:
        hotel_options = search_hotels(destination, budget_per_night_twd=budget_per_night)

    days = user_query.days
    car_days, transit_days = _travel_days_from_plan(daily_plans, user_query)

    car_options = search_car_rental(destination, days=car_days) if car_days > 0 else []

    transport_total = 0
    if transit_days > 0:
        per_day_fare = search_price_twd(
            f"{destination} 大眾運輸 一日券 票價 TWD", fallback=400
        )
        transport_total = per_day_fare * transit_days * user_query.adults

    # 結構化總價已由下方可靠計算；此 ReAct agent 僅供 LangSmith 觀察「多工具比價」軌跡，
    # 其輸出不影響結果。預設關閉——弱模型常對 flight_search_tool 餵幻覺的過去日期（如 2024-06-01）
    # 造成 SerpAPI 400 + 浪費額度；需展示時設 BOOKER_REACT_OBSERVE=1。
    if not is_mock_mode() and os.getenv("BOOKER_REACT_OBSERVE", "0") == "1":
        try:
            from langgraph.prebuilt import create_react_agent

            system = compose_system_prompt(
                "booker",
                "你是旅遊比價與預算管理專家。請用工具查詢機票、飯店與租車，"
                "在預算內挑出性價比最佳的組合並說明理由。",
            )
            agent = create_react_agent(
                llm or get_chat_llm(temperature=0.2),
                [
                    flight_search_tool,
                    hotel_search_tool,
                    carrental_search_tool,
                    serper_price_tool,
                    load_agent_skill,
                ],
            )
            agent.invoke(
                {
                    "messages": [
                        ("system", system),
                        (
                            "user",
                            f"目的地 {destination}，{user_query.days} 天，{user_query.adults} 人，總預算 "
                            f"{user_query.budget_twd} 元台幣，交通方式 {user_query.travel_mode}。",
                        ),
                    ]
                },
                config={"recursion_limit": 8},
            )
        except Exception as e:
            print(f"[Booker] ReAct agent 執行備註（不影響結果）：{e}")

    # 結構化總價：住宿以「選定的飯店」計（與每日行程顯示的住宿一致），機票取最便宜。
    flight_total = min((f["price_twd"] for f in flight_options if f.get("price_twd")), default=0)
    selected_hotel = select_best_hotel(hotel_options, budget_per_night, destination)
    nightly_rate = (
        selected_hotel["est_cost_twd"] if selected_hotel
        else min((h["price_per_night_twd"] for h in hotel_options), default=7000)
    )
    accommodation_total = nightly_rate * nights
    car_total = min((c["price_total_twd"] for c in car_options), default=0)

    meals_estimate = sum(sum(m.est_cost_twd for m in dp.meals.values()) for dp in daily_plans)
    attractions_tickets = sum(sum(a.est_cost_twd for a in dp.attractions) for dp in daily_plans)

    grand = (
        flight_total + accommodation_total + car_total
        + transport_total + meals_estimate + attractions_tickets
    )

    return PriceSummary(
        flight_total=flight_total,
        accommodation_total=accommodation_total,
        car_rental_estimate=car_total,
        transport_total=transport_total,
        attractions_tickets=attractions_tickets,
        meals_estimate=meals_estimate,
        grand_total=grand,
        within_budget=grand <= user_query.budget_twd,
        flight_options=flight_options,
        hotel_options=hotel_options,
        car_options=car_options,
        notes=(
            f"以最划算選項估算：機票 {flight_total} + 住宿 {nights} 晚 × {nightly_rate} "
            f"+ 租車 {car_total} + 大眾運輸 {transport_total} + 餐飲 {meals_estimate} "
            f"+ 門票 {attractions_tickets}。"
            + ("" if grand <= user_query.budget_twd else " ⚠ 超出預算，建議調整。")
        ),
    )


def reconcile_to_budget(
    summary: PriceSummary,
    daily_plans: list[DayPlan],
    user_query: UserQuery,
) -> PriceSummary:
    """確定性預算修復：若超預算，換成最便宜的住宿並重算（就地更新 daily_plans 的 hotel）。

    這是「保證盡力收斂」的安全護欄——換最便宜飯店是不破壞行程結構的最大省錢槓桿。
    換完仍超出就誠實保留 within_budget=False（交給上層標 needs_attention）。
    """
    if summary.within_budget or not summary.hotel_options:
        return summary

    nights = max(user_query.days - 1, 1)
    cheapest = min(summary.hotel_options, key=lambda h: h.get("price_per_night_twd") or 1_000_000_000)
    new_hotel = hotel_to_dayplan(cheapest, user_query.destination)
    new_acc = new_hotel["est_cost_twd"] * nights
    if new_acc >= summary.accommodation_total:
        return summary  # 已是最便宜，無可降空間

    new_grand = summary.grand_total - summary.accommodation_total + new_acc
    for dp in daily_plans:
        dp.hotel = dict(new_hotel)

    summary.accommodation_total = new_acc
    summary.grand_total = new_grand
    summary.within_budget = new_grand <= user_query.budget_twd
    summary.notes += (
        f" 為符合預算已自動改用最便宜住宿（{new_hotel['name']}，每晚 {new_hotel['est_cost_twd']}）。"
        + ("" if summary.within_budget else " 仍超出預算，建議調整天數或偏好。")
    )
    return summary
