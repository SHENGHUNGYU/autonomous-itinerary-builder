"""
Pydantic models for the Kyushu Travel Planner Agent.

This file will grow during Phase 1 (full TravelPlanState etc.).
For Phase 2 we focus on Research-related models.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field


# =============================================================================
# Research Models (Phase 2 focus)
# =============================================================================

class Attraction(BaseModel):
    """A single point of interest / scenic spot / activity."""
    name: str = Field(..., description="景點或活動名稱（繁體中文優先）")
    type: Literal["shrine", "castle", "nature", "onsen", "food", "museum", "scenic", "island", "town", "other"] = "other"
    location: str = Field(..., description="所在城市或區域，例如 '太宰府'、'阿蘇'")
    estimated_time_minutes: int = Field(90, ge=30, le=600)  # 上限放寬至 10 小時，容納整日行程景點
    est_cost_twd: int = Field(0, ge=0)
    notes: str = Field("", description="為什麼推薦、特色、適合季節等")


class Meal(BaseModel):
    """Recommended meal (breakfast / lunch / dinner)."""
    meal_type: Literal["breakfast", "lunch", "dinner"]
    name: str = Field(..., description="餐廳或料理名稱")
    location: str
    cuisine: str = Field("local", description="e.g. 博多拉麵、馬肉、和牛、地獄蒸")
    est_cost_twd: int = Field(600, ge=0, le=5000)  # 0 allowed for included hotel breakfast
    notes: str = ""


class BlogSource(BaseModel):
    """A blog post used as grounding to reduce hallucination."""
    title: str
    url: str
    published: str | None = None
    key_takeaways: str = Field("", description="從這篇文章獲得的重要洞見或建議")


class RegionCluster(BaseModel):
    """研究或規劃階段識別的子區域（任何目的地通用）。"""
    name: str = Field(..., description="聚類名稱，通常為城市/區域標籤")
    areas: list[str] = Field(default_factory=list, description="包含的區域標籤")
    hub_city: str = Field("", description="建議過夜基地城市")
    suggested_nights: int = Field(1, ge=1, description="建議停留晚數")


class ResearchBundle(BaseModel):
    """
    Output of the Researcher Agent / tools.
    This is the main structure that will be fed to Planner.
    """
    query_context: str = Field(..., description="原始使用者查詢或解析後的關鍵字")
    attractions: list[Attraction] = Field(default_factory=list, description="推薦景點/活動（至少 8-12 個）")
    meals: list[Meal] = Field(default_factory=list, description="推薦餐廳/料理（至少 12-15 個）")
    blog_sources: list[BlogSource] = Field(default_factory=list, description="用來 grounding 的部落格文章，至少 3 篇")
    region_coverage: list[str] = Field(default_factory=list, description="涵蓋的主要區域，例如 ['福岡', '熊本', '由布院', '阿蘇']")
    region_clusters: list[RegionCluster] = Field(
        default_factory=list,
        description="依 location 動態聚類的子區域（多區目的地時供 Planner 分區排版）",
    )
    route_hint: str = Field(
        "",
        description="建議路線提示（跨區用大眾運輸、分區過夜等），任何目的地通用",
    )
    notes: str = Field("", description="研究過程中的任何特別觀察或限制")


# =============================================================================
# Future / Phase 1 models (stubs for now)
# =============================================================================

class UserQuery(BaseModel):
    """Parsed user request."""
    raw_input: str
    destination: str = "九州"  # 旅遊目的地（地區/城市/自由文字）；出發地另由 DEFAULT_ORIGIN_AIRPORT 控制
    days: int = 5
    budget_twd: int = 40000
    start_date: date | None = None
    preferences: list[str] = Field(default_factory=list)  # food, onsen, nature, history, etc.
    travel_mode: Literal["self_drive", "public", "mixed"] = "self_drive"
    adults: int = 2
    # None = 無每日駕車時間上限（預設）；有值時 RouteValidator 才把它當硬約束檢查。
    # 通常由使用者結束後的自然語言回饋解析後設定（見 src/agents/feedback.py）。
    max_daily_drive_minutes: int | None = None
    # 最近一輪使用者自然語言回饋；供 Planner / Supervisor prompt 參考。
    user_feedback: str = ""
    # 使用者要求餐飲多樣化（由 feedback 解析）；規劃時禁止重複同一餐廳。
    prefer_meal_variety: bool = False
    # 區域停留要求（由 feedback 解析），例如熊本 2 天並住宿當地。
    region_stays: list[dict[str, object]] = Field(default_factory=list)


class DriveSegment(BaseModel):
    from_location: str
    to_location: str
    minutes: int
    km: float
    notes: str = ""
    polyline: str | None = None          # Google encoded polyline (for map rendering)
    mode: Literal["drive", "transit", "flight"] = Field(
        "drive",
        description="移動方式：drive 計入駕車上限；transit/flight 為跨區大眾運輸或航班",
    )
    # decoded_points: list[tuple[float, float]] | None = None  # 可選，未來解碼後直接用


class DayPlan(BaseModel):
    day: int
    date: str
    drive_total_minutes: int
    transit_total_minutes: int = Field(0, description="當日大眾運輸/跨區移動分鐘數（不計入駕車上限）")
    drive_segments: list[DriveSegment] = Field(default_factory=list)
    meals: dict[str, Meal] = Field(default_factory=dict)  # breakfast, lunch, dinner
    attractions: list[Attraction] = Field(default_factory=list)
    hotel: dict | None = None
    notes: str = ""


# Placeholder for the full state (will be expanded in Phase 1)
class TravelPlanState(BaseModel):
    """Main state object passed through the LangGraph."""
    user_query: str
    parsed: UserQuery | None = None
    research: ResearchBundle | None = None
    daily_plans: list[DayPlan] = Field(default_factory=list)
    price_summary: dict = Field(default_factory=dict)
    validation_errors: list[str] = Field(default_factory=list)
    retry_count: int = 0
    final_itinerary_md: str | None = None
    sources: list[BlogSource] = Field(default_factory=list)
    # needs_attention：行程已產出但仍有 hard 違規（誠實標示，不假裝 success）
    status: Literal["running", "success", "needs_attention", "failed"] = "running"
    # 統一 constraint checker 的結構化違規清單（category/severity/message/data）
    violations: list[dict] = Field(default_factory=list)
    trace: list[dict] = Field(default_factory=list)

    # Map data (Phase 3+)
    daily_polylines: dict[int, list[str]] = Field(default_factory=dict)  # day -> list of polylines
    # 行程地圖：每天一個主要地點標記（day -> {name, location, lat, lng}），與交通方式無關
    daily_markers: dict[int, dict] = Field(default_factory=dict)
