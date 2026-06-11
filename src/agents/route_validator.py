"""
RouteValidator - 駕車時間驗證

職責：
- 用 maps 工具計算每日真實駕車時間
- 對照硬約束（自駕每日 ≤ 120 分鐘）判斷是否違規
- 產生明確的 violations 與可執行的改善建議

設計：駕車時間驗證是「確定性計算」，不該交給 LLM 猜，故此處為純程式判斷，
僅在需要時產生規則式的改善建議。

Skill 文件：``src/agents/skills/route_validator/SKILL.md``（治理與參考，執行時不載入 LLM）。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from src.agents.constraints import check_drive, drive_cap_for
from src.core.models import DayPlan, UserQuery
from src.tools.maps import (
    compute_driving_route,
    estimate_drive_minutes_fallback,
    estimate_transit_minutes_fallback,
)


class ValidationResult(BaseModel):
    is_valid: bool = Field(..., description="整體行程是否通過駕車時間驗證")
    violations: list[str] = Field(default_factory=list, description="違規項目清單")
    suggestions: list[str] = Field(default_factory=list, description="具體的改善建議")
    max_daily_drive_minutes: int | None = Field(None, description="本次套用的每日駕車時間上限")


def _segment_api_mode(seg_mode: str, user_query: UserQuery | None) -> str:
    """把段落 mode 轉成 maps API 的 travel_mode 參數。"""
    if not user_query:
        return "self_drive"
    if user_query.travel_mode == "self_drive":
        return "self_drive"
    if user_query.travel_mode == "public":
        return "public"
    # mixed：依段落標記
    if seg_mode in ("transit", "flight"):
        return "public"
    return "self_drive"


def run_route_validator_crew(
    daily_plans: list[DayPlan],
    user_query: UserQuery,
    llm=None,
) -> ValidationResult:
    """確定性計算每日駕車/大眾運輸分鐘數與 polyline，並用 mode-aware 上限檢查駕車違規。"""
    cap = drive_cap_for(user_query)

    for dp in daily_plans:
        drive_total = 0
        transit_total = 0
        for seg in dp.drive_segments:
            seg_mode = getattr(seg, "mode", "drive") or "drive"
            if seg_mode == "flight":
                continue
            api_mode = _segment_api_mode(seg_mode, user_query)
            result = compute_driving_route(
                seg.from_location, seg.to_location, travel_mode=api_mode,
            )
            minutes = result.duration_minutes
            if minutes <= 0:
                minutes = (
                    estimate_transit_minutes_fallback(seg.from_location, seg.to_location)
                    if api_mode == "public"
                    else estimate_drive_minutes_fallback(seg.from_location, seg.to_location)
                )
            seg.minutes = minutes
            if result.polyline:
                seg.polyline = result.polyline
            # 依「有效交通方式」分桶並對齊 seg.mode，讓 output_formatter / constraints 一致：
            # public（或 mixed 的跨區段）→ 大眾運輸；其餘 → 自駕。
            if api_mode == "public":
                seg.mode = "transit"
                transit_total += minutes
            else:
                drive_total += minutes
        dp.drive_total_minutes = drive_total
        dp.transit_total_minutes = transit_total

    drive_violations = check_drive(daily_plans, user_query)
    violations = [v.message for v in drive_violations]
    suggestions = []
    for v in drive_violations:
        tip = f"建議減少 Day {v.data['day']} 的景點數量，或更換較近的住宿以縮短移動。"
        if user_query and user_query.travel_mode == "mixed":
            tip += " 跨區移動請改標 mode=transit（鐵路/巴士），勿長距離自駕。"
        elif user_query and user_query.travel_mode == "self_drive":
            tip += " 亦可考慮該日部分改用大眾運輸。"
        suggestions.append(tip)

    return ValidationResult(
        is_valid=len(violations) == 0,
        violations=violations,
        suggestions=suggestions,
        max_daily_drive_minutes=cap,
    )