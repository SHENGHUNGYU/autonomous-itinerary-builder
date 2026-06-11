"""產生完整 Markdown 行程計劃書（含每日花費與交通細節）。

Skill 文件：``src/agents/skills/output_formatter/SKILL.md``（輸出格式契約與參考）。
"""

from __future__ import annotations

from src.core.models import DayPlan, ResearchBundle, UserQuery

_MEAL_LABELS = {"breakfast": "早餐", "lunch": "午餐", "dinner": "晚餐"}
_MODE_LABELS = {"drive": "自駕", "transit": "大眾運輸", "flight": "航班"}


def _transport_summary_line(dp: DayPlan) -> str:
    """依非零的駕車/大眾運輸分鐘數組出 mode-aware 的當日移動總計。"""
    parts: list[str] = []
    if dp.drive_total_minutes:
        parts.append(f"自駕總計：**{dp.drive_total_minutes}** 分鐘")
    if dp.transit_total_minutes:
        parts.append(f"大眾運輸總計：**{dp.transit_total_minutes}** 分鐘")
    if not parts:
        return "市區步行/短程移動"
    return "｜".join(parts)


def _day_costs(dp: DayPlan) -> tuple[int, int, int]:
    tickets = sum(a.est_cost_twd for a in dp.attractions)
    meals = sum(m.est_cost_twd for m in dp.meals.values())
    hotel = int((dp.hotel or {}).get("est_cost_twd", 0) or 0)
    return tickets, meals, hotel


def build_itinerary_md(
    daily_plans: list[DayPlan],
    price_summary: dict | None,
    parsed: UserQuery | None,
    violations: list | None = None,
    research: ResearchBundle | None = None,
) -> str:
    destination = parsed.destination if parsed else "目的地"
    days = parsed.days if parsed else len(daily_plans)
    mode_label = {"self_drive": "自駕", "mixed": "自駕 + 大眾運輸", "public": "大眾運輸"}.get(
        parsed.travel_mode if parsed else "self_drive", "自駕"
    )
    price = price_summary or {}
    budget_estimated = not price

    lines = [f"# {destination} {days} 天{mode_label}行程計劃書\n"]

    if budget_estimated:
        lines.append("> 以下花費含每日粗估；總預算表若為空則尚未完成比價。\n")

    grand_tickets = grand_meals = grand_hotels = 0

    for dp in daily_plans:
        lines.append(f"## Day {dp.day}（{dp.date}）")
        if dp.notes:
            lines.append(f"> {dp.notes}\n")

        lines.append("### 交通")
        lines.append(f"- {_transport_summary_line(dp)}")
        if dp.drive_segments:
            for seg in dp.drive_segments:
                mode = _MODE_LABELS.get(getattr(seg, "mode", "drive") or "drive", seg.mode)
                km_part = f", {seg.km:.0f}km" if seg.km else ""
                lines.append(
                    f"  - {seg.from_location} → {seg.to_location}（{mode}, {seg.minutes}分{km_part}）"
                )
        else:
            lines.append("  - （無移動段落）")
        lines.append("")

        if dp.attractions:
            lines.append("### 景點")
            lines.append("| 景點 | 地點 | 停留 | 門票(TWD) | 備註 |")
            lines.append("|---|---|---|---|---|")
            for attr in dp.attractions:
                notes = (attr.notes or "").replace("|", "/")[:60]
                lines.append(
                    f"| {attr.name} | {attr.location} | {attr.estimated_time_minutes}分 "
                    f"| {attr.est_cost_twd:,} | {notes} |"
                )
            lines.append("")
        else:
            lines.append("### 景點\n- （本日未安排景點）\n")

        lines.append("### 餐飲")
        lines.append("| 時段 | 餐廳 | 地點 | 料理 | 預估(TWD) |")
        lines.append("|---|---|---|---|---|")
        for mtype in ("breakfast", "lunch", "dinner"):
            meal = dp.meals.get(mtype)
            if meal:
                label = _MEAL_LABELS.get(mtype, mtype)
                cuisine = (meal.cuisine or "").replace("|", "/")
                lines.append(
                    f"| {label} | {meal.name} | {meal.location} | {cuisine} | {meal.est_cost_twd:,} |"
                )
            else:
                label = _MEAL_LABELS.get(mtype, mtype)
                lines.append(f"| {label} | — | — | — | 0 |")
        lines.append("")

        if dp.hotel:
            h = dp.hotel
            rating = h.get("rating", "-")
            link = h.get("link", "")
            nightly = int(h.get("est_cost_twd", 0) or 0)
            link_md = f"[訂房]({link})" if link else "—"
            lines.append("### 住宿")
            lines.append(
                f"- **{h.get('name', '未定')}**（{h.get('location', '')}）"
                f"｜每晚 **{nightly:,}** TWD｜評分 {rating}｜{link_md}\n"
            )

        tickets, meals, hotel = _day_costs(dp)
        day_total = tickets + meals + hotel
        grand_tickets += tickets
        grand_meals += meals
        grand_hotels += hotel
        lines.append("### 當日預估花費")
        lines.append(f"- 門票小計：**{tickets:,}** 元")
        lines.append(f"- 餐飲小計：**{meals:,}** 元")
        lines.append(f"- 住宿：**{hotel:,}** 元")
        lines.append(f"- **當日合計：{day_total:,} 元**\n")
        lines.append("---\n")

    if price:
        budget = price.get("budget", 0)
        total = price.get("estimated_total_twd", 0)
        ok = "✅ 在預算內" if price.get("within_budget") else "⚠️ 超出預算"
        lines.append("## 總預算明細")
        lines.append(f"- 機票：{price.get('flight_total', 0):,} 元")
        lines.append(f"- 住宿：{price.get('accommodation_total', 0):,} 元")
        lines.append(f"- 租車：{price.get('car_rental_estimate', 0):,} 元")
        if price.get("transport_total"):
            lines.append(f"- 大眾運輸：{price.get('transport_total', 0):,} 元")
        lines.append(f"- 餐飲：{price.get('meals_estimate', 0):,} 元")
        lines.append(f"- 門票：{price.get('attractions_tickets', 0):,} 元")
        lines.append(f"- **總計：{total:,} 元 / 預算 {budget:,} 元（{ok}）**\n")

        flights = price.get("flight_options", [])
        if flights:
            lines.append("## 機票比價")
            lines.append("| 航空 | 出發 | 抵達 | 轉機 | 總價(TWD) | 連結 |")
            lines.append("|---|---|---|---|---|---|")
            for f in flights:
                lines.append(
                    f"| {f.get('airline', '')} | {f.get('departure', '')} | {f.get('arrival', '')} | "
                    f"{f.get('stops', 0)} | {f.get('price_twd', 0):,} | [查看]({f.get('link', '')}) |"
                )
            lines.append("")

        hotels = price.get("hotel_options", [])
        if hotels:
            lines.append("## 飯店比價")
            lines.append("| 飯店 | 每晚(TWD) | 評分 | 連結 |")
            lines.append("|---|---|---|---|")
            for h in hotels[:15]:
                lines.append(
                    f"| {h.get('name', '')} | {h.get('price_per_night_twd', 0):,} | "
                    f"{h.get('rating', '-')} | [訂房]({h.get('link', '')}) |"
                )
            lines.append("")
    else:
        lines.append("## 行程粗估（尚未比價）")
        lines.append(f"- 門票合計（依每日計劃）：**{grand_tickets:,}** 元")
        lines.append(f"- 餐飲合計：**{grand_meals:,}** 元")
        lines.append(f"- 住宿合計：**{grand_hotels:,}** 元")
        lines.append(
            f"- **粗估合計：{grand_tickets + grand_meals + grand_hotels:,} 元**"
            f"（不含機票、租車；完成 book 後會更新）\n"
        )

    violations = violations or []
    hard = [v for v in violations if v.get("severity") == "hard"]
    if hard:
        lines.append("## ⚠️ 仍待改善（已達重試上限或需人工微調）")
        for v in hard:
            lines.append(f"- {v.get('message', '')}")
        lines.append("")

    soft = [v for v in violations if v.get("severity") == "soft"]
    if soft:
        lines.append("## 備註提醒（軟性，不影響行程輸出）")
        for v in soft:
            lines.append(f"- {v.get('message', '')}")
        lines.append("")

    if research and research.blog_sources:
        lines.append("## 參考來源（部落格 grounding）")
        for src in research.blog_sources:
            lines.append(f"- [{src.title}]({src.url})")

    return "\n".join(lines)