"""
Feedback Agent - 解析使用者結束後的自然語言回饋

職責：
- 把使用者跑完行程後給的自然語言回饋（例：「Day2 開車太久，希望每天不超過 90 分鐘」）
  解析成結構化調整參數，主要是每日駕車時間上限 max_daily_drive_minutes。
- 解析後由 graph.run_planner_refine 套用到 UserQuery，再只重跑 Planner→Validate→Supervisor。

設計：優先用 LLM（call_structured）理解語意；LLM 不可達 / mock 時，
用 regex fallback 從文字抓出「N 分鐘」或「N 小時」當上限，確保離線可用。
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

from src.agents.skills_loader import compose_system_prompt
from src.core.models import UserQuery
from src.services.llm import call_structured


class RegionStayRequest(BaseModel):
    """使用者要求某區域停留天數並當地住宿。"""
    region: str = Field(..., description="區域名稱，例如 熊本、福岡")
    nights: int = Field(..., ge=1, le=7, description="在該區域過夜/停留天數")


class FeedbackAdjustment(BaseModel):
    """從使用者回饋解析出的可套用調整。"""
    max_daily_drive_minutes: int | None = Field(
        None,
        description="使用者希望的每日駕車時間上限（分鐘）；若回饋未提及則為 null",
    )
    require_meal_diversity: bool = Field(
        False,
        description="使用者抱怨餐飲重複或要求多樣化時為 true",
    )
    region_stays: list[RegionStayRequest] = Field(
        default_factory=list,
        description="例如「熊本改為 2 天一夜」→ region=熊本, nights=2",
    )
    notes: str = Field("", description="其他無法量化、但應轉達給 Planner 的回饋重點")


_SYSTEM = (
    "你是一位旅遊行程助理，負責把使用者對既有行程的自然語言回饋，"
    "轉成結構化調整參數。只擷取使用者明確表達的需求，不要臆測。"
)

# 數字 + 單位（小時/分鐘）。容許全形空白與「個」。
_HOUR_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*(?:個)?\s*(?:小時|hr|hour|h)", re.IGNORECASE)
_MIN_RE = re.compile(r"([0-9]+)\s*(?:分鐘|分|min|minute|mins)", re.IGNORECASE)
_MEAL_REPEAT_RE = re.compile(
    r"重複|重复|換餐|换餐|別再|别再|不要一直|不要總是|不要总是|多樣|多样|換一家|换一家",
    re.IGNORECASE,
)
_REGION_STAY_RE = re.compile(
    r"([\u4e00-\u9fffA-Za-z・]+?)(?:改為|改成|改為|安排|停留|至少)(?:約)?(\d+)\s*天",
    re.IGNORECASE,
)
_LOCAL_STAY_RE = re.compile(r"住在當地|當地住宿|住當地")


def _regex_extract_minutes(feedback: str) -> int | None:
    """從回饋文字抓出駕車上限（分鐘）。優先「N 分鐘」，再「N 小時」。"""
    if not feedback:
        return None
    m = _MIN_RE.search(feedback)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            pass
    h = _HOUR_RE.search(feedback)
    if h:
        try:
            return int(round(float(h.group(1)) * 60))
        except ValueError:
            pass
    return None


def _detect_meal_diversity(feedback: str) -> bool:
    if not feedback:
        return False
    if _MEAL_REPEAT_RE.search(feedback):
        return True
    return any(k in feedback for k in ("美食一直", "餐廳一直", "餐厅一直", "老是同一", "都是同一"))


def _regex_extract_region_stays(feedback: str) -> list[RegionStayRequest]:
    out: list[RegionStayRequest] = []
    for m in _REGION_STAY_RE.finditer(feedback or ""):
        region = m.group(1).strip().rstrip("改為").rstrip("改成")
        try:
            nights = int(m.group(2))
        except ValueError:
            continue
        if region and nights >= 1:
            out.append(RegionStayRequest(region=region, nights=nights))
    return out


def summarize_prior_plans(daily_plans: list) -> str:
    """把上一版行程壓成簡短文字，供微調 Planner 參考。"""
    if not daily_plans:
        return ""
    lines: list[str] = ["【上一版行程摘要（請依使用者回饋調整，勿整段重複貼上）】"]
    for dp in daily_plans:
        attrs = "、".join(getattr(a, "name", str(a)) for a in (dp.attractions or [])[:4])
        hotel = dp.hotel or {}
        hname = hotel.get("name", "") if isinstance(hotel, dict) else ""
        hloc = hotel.get("location", "") if isinstance(hotel, dict) else ""
        lines.append(
            f"Day {dp.day}: 景點={attrs or '—'}｜住宿={hname}({hloc})｜"
            f"駕車 {getattr(dp, 'drive_total_minutes', 0)} 分"
        )
    return "\n".join(lines)


def build_refine_planner_feedback(
    user_query: UserQuery,
    adjustment: FeedbackAdjustment | None = None,
    prior_plans: list | None = None,
) -> str:
    """組微調專用 Planner 回饋（直線流程，不經 Supervisor 再推理）。"""
    parts: list[str] = []
    if prior_plans:
        summary = summarize_prior_plans(prior_plans)
        if summary:
            parts.append(summary)
    if user_query.user_feedback:
        parts.append(f"【使用者回饋】{user_query.user_feedback.strip()}")
    adj = adjustment
    if adj and adj.region_stays:
        for rs in adj.region_stays:
            local = "並住宿在當地（hotel.location 填該城市）" if _LOCAL_STAY_RE.search(
                user_query.user_feedback or ""
            ) else "並在該區住宿"
            parts.append(
                f"【硬性要求】{rs.region} 區域安排 {rs.nights} 天{local}；"
                f"相關景點與餐飲集中於該區，勿一日來回其他縣市。"
            )
    elif user_query.region_stays:
        for rs in user_query.region_stays:
            parts.append(
                f"【硬性要求】{rs.get('region', '')} 區域安排 {rs.get('nights', 1)} 天並住宿當地。"
            )
    if user_query.prefer_meal_variety:
        parts.append(
            "【硬性要求】全程不得重複同一餐廳名稱；同一日三餐也不得同名。"
            "每餐從研究池選不同店家，優先尚未用過的餐廳。"
        )
    if user_query.max_daily_drive_minutes is not None:
        parts.append(
            f"【硬性要求】每日自駕段（mode=drive）合計不得超過 "
            f"{user_query.max_daily_drive_minutes} 分鐘。"
        )
    return "\n".join(parts)


def parse_user_feedback(feedback: str, user_query: UserQuery | None = None) -> FeedbackAdjustment:
    """把自然語言回饋解析成 FeedbackAdjustment。

    LLM 解析失敗或無結果時，退回 regex 抽取每日駕車上限。
    """
    feedback = (feedback or "").strip()
    if not feedback:
        return FeedbackAdjustment(max_daily_drive_minutes=None, notes="")

    prompt = (
        "請從以下使用者回饋中擷取結構化調整參數。\n"
        "1. 每日駕車時間上限（分鐘）：\n"
        "   - 『每天不超過 90 分鐘』→ max_daily_drive_minutes=90\n"
        "   - 『每天最多開 2 小時』→ max_daily_drive_minutes=120\n"
        "   - 未提及駕車 → null\n"
        "2. 餐飲多樣化：若抱怨美食/餐廳重複、要求換店、要多樣化 → require_meal_diversity=true\n"
        "3. 區域停留：如「熊本改為 2 天一夜，住在當地」→ region_stays=[{region:'熊本', nights:2}]\n"
        "4. 其他要求（景點、預算、步調等）放進 notes。\n\n"
        f"【使用者回饋】\n{feedback}\n"
    )

    result = call_structured(
        compose_system_prompt("feedback", _SYSTEM), prompt, FeedbackAdjustment, temperature=0.0,
    )

    if result is None:
        return FeedbackAdjustment(
            max_daily_drive_minutes=_regex_extract_minutes(feedback),
            require_meal_diversity=_detect_meal_diversity(feedback),
            region_stays=_regex_extract_region_stays(feedback),
            notes=feedback,
        )

    if result.max_daily_drive_minutes is None:
        result.max_daily_drive_minutes = _regex_extract_minutes(feedback)
    if not result.require_meal_diversity:
        result.require_meal_diversity = _detect_meal_diversity(feedback)
    if not result.region_stays:
        result.region_stays = _regex_extract_region_stays(feedback)
    if not result.notes:
        result.notes = feedback
    return result
