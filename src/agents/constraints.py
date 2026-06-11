"""
統一的確定性 Constraint Checker（單一品質門檻）

把原本分散在 RouteValidator（駕車）與 Supervisor（LLM 主觀判斷）的品質判斷，
收斂成一處確定性檢查，回傳結構化的 violations。供：
  - RouteValidator：取「駕車」子集即時顯示
  - Supervisor：取完整集合做決策（並轉成給 Planner 的結構化 adjustments）
  - graph.format_output：依「是否有 hard violation」決定誠實的最終 status

設計原則：這些都是可確定性計算的規則，不交給 LLM 猜。LLM 只負責產草稿與邊界決策。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.agents.geo import (
    collapse_metro_visit_areas,
    day_attraction_areas,
    normalize_area,
    overnight_area,
    same_area,
)
from src.core.models import DayPlan, UserQuery

# 每日駕車時間的合理上限（分鐘），依交通方式而定。
# 不再依賴使用者是否手動設定——永遠有一個合理預設，避免「9 小時自駕卻判 PASS」。
DEFAULT_DRIVE_CAP_MINUTES = {
    "self_drive": 240,  # 自駕，單日最多約 4 小時車程
    "mixed": 180,       # 自駕 + 大眾運輸
    "public": 90,       # 純大眾運輸，單日移動更短
}
_FALLBACK_DRIVE_CAP = 240

# 每日景點數的合理範圍
MIN_ATTRACTIONS_PER_DAY = 2


@dataclass
class Violation:
    """單一違規項目。"""
    category: str   # budget | drive | empty_day | sparse_day | mode_consistency
    severity: str   # hard（必須修）| soft（建議修）
    message: str    # 給人看的中文說明
    data: dict = field(default_factory=dict)  # 結構化資料（金額、天、分鐘…），供回饋與修復用


def drive_cap_for(user_query: UserQuery | None) -> int:
    """回傳本次套用的每日駕車上限：mode 預設與使用者設定取較嚴者。"""
    if user_query is None:
        return _FALLBACK_DRIVE_CAP
    base = DEFAULT_DRIVE_CAP_MINUTES.get(user_query.travel_mode, _FALLBACK_DRIVE_CAP)
    user_cap = user_query.max_daily_drive_minutes
    return min(base, user_cap) if user_cap is not None else base


def check_drive(daily_plans: list[DayPlan], user_query: UserQuery | None) -> list[Violation]:
    """駕車時間違規（mode-aware；僅計 mode=drive 的段落）。

    純大眾運輸（public）不套駕車硬上限——移動時間由地鐵/巴士決定，
    對 public 行程套用駕車式上限只會產生無謂的誤判違規。
    """
    if user_query and user_query.travel_mode == "public":
        return []
    cap = drive_cap_for(user_query)
    out: list[Violation] = []
    for dp in daily_plans:
        drive_min = dp.drive_total_minutes
        if drive_min == 0 and dp.drive_segments:
            drive_min = sum(
                s.minutes for s in dp.drive_segments if getattr(s, "mode", "drive") == "drive"
            )
        if drive_min > cap:
            out.append(Violation(
                category="drive",
                severity="hard",
                message=f"Day {dp.day} 駕車 {drive_min} 分鐘，超過上限 {cap} 分鐘",
                data={"day": dp.day, "minutes": drive_min, "cap": cap},
            ))
    return out


def check_geography(daily_plans: list[DayPlan], user_query: UserQuery | None) -> list[Violation]:
    """地理合理性：單日跨多區卻從遠距基地一日來回（任何目的地通用）。"""
    if not user_query or not daily_plans:
        return []
    dest = user_query.destination
    out: list[Violation] = []
    for dp in daily_plans:
        visits = day_attraction_areas(dp, dest)
        if len(visits) < 2:
            continue
        base = overnight_area(dp, dest)
        visits = collapse_metro_visit_areas(visits, base, dest)
        if len(visits) < 2:
            continue
        far = [a for a in visits if not same_area(a, base, dest)]
        if len(far) >= 1:
            out.append(Violation(
                category="geography",
                severity="soft",
                message=(
                    f"Day {dp.day} 從基地「{base}」安排 {len(visits)} 個不同區域景點"
                    f"（{', '.join(sorted(visits)[:4])}），應分區過夜或跨區改用大眾運輸"
                ),
                data={"day": dp.day, "base": base, "areas": sorted(visits)},
            ))
    return out


def check_itinerary_shape(daily_plans: list[DayPlan]) -> list[Violation]:
    """每日行程結構：空白天（hard）、景點過少（soft）。"""
    out: list[Violation] = []
    for dp in daily_plans:
        n = len(dp.attractions)
        if n == 0:
            out.append(Violation(
                category="empty_day",
                severity="hard",
                message=f"Day {dp.day} 沒有安排任何景點（空白天）",
                data={"day": dp.day},
            ))
        elif n < MIN_ATTRACTIONS_PER_DAY:
            out.append(Violation(
                category="sparse_day",
                severity="soft",
                message=f"Day {dp.day} 只有 {n} 個景點，建議 {MIN_ATTRACTIONS_PER_DAY}-3 個",
                data={"day": dp.day, "count": n},
            ))
    return out


def check_budget(price_summary: dict | None, user_query: UserQuery | None) -> list[Violation]:
    """預算違規（hard）。"""
    if not price_summary:
        return []
    budget = price_summary.get("budget") or (user_query.budget_twd if user_query else 0)
    total = price_summary.get("estimated_total_twd", 0)
    if budget and total > budget:
        return [Violation(
            category="budget",
            severity="soft",
            message=f"總花費 {total:,} 超出預算 {budget:,}（超 {total - budget:,} 元）",
            data={"total": total, "budget": budget, "over": total - budget},
        )]
    return []


def check_mode_consistency(
    price_summary: dict | None, user_query: UserQuery | None
) -> list[Violation]:
    """交通方式一致性：public 卻有租車費用（soft）。"""
    if not price_summary or user_query is None:
        return []
    out: list[Violation] = []
    car = price_summary.get("car_rental_estimate", 0)
    if user_query.travel_mode == "public" and car > 0:
        out.append(Violation(
            category="mode_consistency",
            severity="soft",
            message=f"交通方式為大眾運輸，卻估了 {car:,} 元租車費用",
            data={"car_rental_estimate": car},
        ))
    return out


def check_constraints(
    daily_plans: list[DayPlan],
    price_summary: dict | None,
    user_query: UserQuery | None,
) -> list[Violation]:
    """完整檢查：駕車 + 行程結構 + 預算 + 交通一致性。"""
    return (
        check_drive(daily_plans, user_query)
        + check_geography(daily_plans, user_query)
        + check_itinerary_shape(daily_plans)
        + check_budget(price_summary, user_query)
        + check_mode_consistency(price_summary, user_query)
    )


def has_hard(violations: list[Violation]) -> bool:
    return any(v.severity == "hard" for v in violations)


def summarize(violations: list[Violation]) -> str:
    """把 violations 整理成多行中文摘要（供 prompt / log）。"""
    if not violations:
        return "（無違規，行程符合所有約束）"
    return "\n".join(f"- [{v.severity}] {v.message}" for v in violations)


def to_dicts(violations: list[Violation]) -> list[dict]:
    """序列化成 dict（存進 graph state / 給 UI）。"""
    return [
        {"category": v.category, "severity": v.severity, "message": v.message, "data": v.data}
        for v in violations
    ]


def build_planner_feedback(
    violations: list[Violation], user_query: UserQuery | None
) -> str:
    """把 violations 轉成給 Planner 的『可執行硬性指令』（取代過去的自由文字回饋）。

    這是讓重試迴圈收斂的關鍵：弱模型需要明確、結構化、祈使句的規則，而非含糊建議。
    """
    if not violations:
        return ""
    cap = drive_cap_for(user_query)
    lines: list[str] = ["上一版行程有以下問題，這次規劃【必須】全部修正："]

    drive_days = [v.data["day"] for v in violations if v.category == "drive"]
    if drive_days:
        lines.append(
            f"- 駕車過長（Day {drive_days}）：每日總駕車時間【必須 ≤ {cap} 分鐘】。"
            "請把遠距景點移到鄰近同區的日子，或把該區的住宿換到該區域附近，"
            "不要從同一個基地對遠方景點做一日來回。"
        )
    budget_v = next((v for v in violations if v.category == "budget"), None)
    if budget_v:
        lines.append(
            f"- 超出預算：總花費需壓到 {budget_v.data['budget']:,} 元以內"
            f"（目前超出約 {budget_v.data['over']:,} 元）。請選較便宜的住宿、減少付費景點或縮短租車天數。"
        )
    empty_days = [v.data["day"] for v in violations if v.category == "empty_day"]
    sparse_days = [v.data["day"] for v in violations if v.category == "sparse_day"]
    if empty_days or sparse_days:
        affected = sorted(set(empty_days + sparse_days))
        lines.append(
            f"- 行程不均（Day {affected}）：每天【至少安排 2 個景點】，"
            "不可有空白天，把景點平均分配到各天。"
        )
    if any(v.category == "mode_consistency" for v in violations):
        lines.append(
            "- 交通方式不一致：請依使用者選的交通方式安排（大眾運輸為主就不要規劃長程自駕）。"
        )
    geo_violations = [v for v in violations if v.category == "geography" and v.severity == "hard"]
    if geo_violations:
        geo_days = [v.data["day"] for v in geo_violations]
        detail_parts = []
        for v in geo_violations:
            areas = v.data.get("areas") or []
            base = v.data.get("base", "")
            if areas:
                detail_parts.append(f"Day {v.data.get('day')}: 基地「{base}」vs 景點區「{', '.join(areas[:4])}」")
        detail = "；".join(detail_parts) if detail_parts else ""
        lines.append(
            f"- 地理不合理（Day {geo_days}）：多區景點不可從同一遠距基地一日來回。"
            f"{' 具體：' + detail if detail else ''} "
            "請改為在該區過夜，或把跨區移動的 drive_segments 標為 mode=transit。"
        )
    if user_query and user_query.travel_mode == "mixed":
        lines.append(
            "- mixed 模式：同一區域內用 mode=drive；跨區域用 mode=transit（JR/巴士），"
            "且每日自駕段合計必須 ≤ 上限。"
        )
    return "\n".join(lines)
