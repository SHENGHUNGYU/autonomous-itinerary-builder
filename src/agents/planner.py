"""
Planner Agent - 行程規劃

職責：
- 吃 ResearchBundle + UserQuery + 驗證回饋（若重試）
- 產生符合天數、預算、偏好、每日駕車時間限制的詳細每日行程
- 以結構化輸出（vLLM guided_json）回傳 ItineraryOutput

說明：行程生成是「單次結構化生成」最適合的工作，故用 call_structured 而非 ReAct 迴圈。

Per-day mode (2026-06-10 improvement): plan_single_day produces ONE focused DayPlan using a tiny
area-specific research slice + continuity from prev day's end location. This replaces the previous
one-shot all-days call for multi-region trips to avoid sparse days, wrong global hotels, and meal
repeats (see 7.json failure mode).
"""

from __future__ import annotations

import os

from pydantic import BaseModel, Field

from src.agents.constraints import drive_cap_for
from src.agents.skills_loader import compose_system_prompt
from src.core.models import DayPlan, ResearchBundle, UserQuery
from src.services.llm import call_structured


class ItineraryOutput(BaseModel):
    """Planner 預期的結構化輸出。"""
    daily_plans: list[DayPlan] = Field(..., description="詳細的每日行程規劃")
    overall_notes: str = Field("", description="整體行程的規劃說明、調整原因、注意事項")
    estimated_total_cost_twd: int = Field(0, description="粗估總花費（不含機票）")


_SYSTEM = (
    "你是一位擁有 15 年經驗的日本旅遊行程規劃師，自駕與大眾運輸行程都同樣擅長。"
    "你會依使用者選定的交通方式安排移動，平衡景點、美食、住宿與移動時間，"
    "並遵守使用者的預算、偏好與每日移動時間限制。"
    "你的輸出會被 Supervisor（總監）審核。"
)


def run_planner_crew(
    research: ResearchBundle,
    user_query: UserQuery,
    validation_feedback: str = "",
    llm=None,
    preferred_hotel: dict | None = None,
    prior_daily_plans: list | None = None,
    *,
    compact: bool = False,
) -> ItineraryOutput:
    """執行 Planner，回傳結構化 ItineraryOutput。

    preferred_hotel：上游已從比價結果選定的飯店（DayPlan.hotel 格式）。
    若提供，要求 Planner 直接採用此飯店作為住宿，使行程與比價表一致。
    """
    cap = drive_cap_for(user_query)
    drive_rule = (
        f"每日『自駕段』（mode=drive）合計必須 ≤ {cap} 分鐘（依交通方式 {user_query.travel_mode} 的系統上限）。"
        "跨區移動請用 mode=transit，不計入此上限。"
    )

    # 併入使用者自然語言回饋（與既有驗證回饋一起餵給模型）。
    feedback_parts = [p for p in (validation_feedback, user_query.user_feedback) if p]
    if user_query.region_stays:
        for rs in user_query.region_stays:
            region = rs.get("region", "")
            nights = rs.get("nights", 1)
            feedback_parts.append(
                f"【硬性要求】{region} 區域安排 {nights} 天並住宿當地（hotel.location 填該城市）。"
            )
    feedback_text = "\n".join(feedback_parts) if feedback_parts else "（無，這是首次規劃）"
    prior_block = ""
    if prior_daily_plans and not compact:
        from src.agents.feedback import summarize_prior_plans
        prior_block = summarize_prior_plans(prior_daily_plans) + "\n\n"

    multi_region = len(research.region_clusters) > 1 or len(research.region_coverage) > 2
    if preferred_hotel and not multi_region:
        hotel_rule = (
            f"單一區域行程：可參考已選飯店 name=「{preferred_hotel.get('name', '')}」、"
            f"location=「{preferred_hotel.get('location', '')}」。"
            "同城連續日共用同一間，location 填具體城市（勿只填大地區名）。"
        )
    else:
        hotel_rule = (
            "多區域行程：每個過夜城市選就近住宿，換城市必換飯店；"
            "hotel.location 必須是具體城市/區名（例如「函館市」「札幌市」），"
            "不可只填「北海道」「九州」等大地區名。"
        )
    route_block = ""
    if research.route_hint:
        route_block = f"\n### 路線提示\n{research.route_hint}\n"
    if research.region_clusters:
        cluster_lines = "\n".join(
            f"- {c.name}（建議 {c.suggested_nights} 晚，基地 {c.hub_city}）"
            for c in research.region_clusters[:8]
        )
        route_block += f"\n### 子區域聚類\n{cluster_lines}\n"
    mode_block = ""
    if user_query.travel_mode == "mixed":
        mode_block = (
            "\n【mixed 交通規則】同一子區域內移動：drive_segments.mode=drive；"
            "跨子區域（不同城市/聚類）移動：drive_segments.mode=transit。"
            "禁止從單一基地對多個遠距城市做一日來回自駕。\n"
        )
    elif user_query.travel_mode == "public":
        mode_block = (
            "\n【大眾運輸】全程以地鐵/鐵路/巴士＋步行為主，不安排自駕；"
            "移動段落（含市區地鐵）一律標 mode=transit，from/to 盡量用『車站或景點名 區名』。\n"
        )

    # 精簡序列化研究資料，避免完整 JSON 塞爆小 context 模型（如 8192 的 Qwen3-14B-AWQ）。
    attractions_text = "\n".join(
        f"- {a.name}｜{a.type}｜{a.location}｜{a.estimated_time_minutes}分｜{a.est_cost_twd}元"
        for a in research.attractions[: (8 if compact else 12)]
    ) or "（無）"
    meals_text = "\n".join(
        f"- {m.meal_type}｜{m.name}｜{m.location}｜{m.cuisine}"
        for m in research.meals[: (10 if compact else 15)]
    ) or "（無）"
    variety_rule = ""
    if user_query.prefer_meal_variety:
        variety_rule = "   使用者要求餐飲多樣化：全程禁止重複餐廳名稱，每餐選不同店家。\n"
    # 部落格以 markdown 整理：標題加連結 + 完整重點，作為行程安排的 grounding 依據。
    blog_blocks = []
    for i, s in enumerate(research.blog_sources, 1):
        block = f"{i}. **[{s.title}]({s.url})**"
        if s.published:
            block += f"（{s.published}）"
        if s.key_takeaways:
            block += f"\n   - 重點：{s.key_takeaways.strip()[:120]}"
        blog_blocks.append(block)
    blogs_text = "\n".join(blog_blocks) or "（無）"

    prompt = (
        "請根據以下資訊，規劃一個完整、合理且符合所有約束的多日行程。\n\n"
        f"【使用者需求】\n"
        f"目的地={user_query.destination} 天數={user_query.days} 預算={user_query.budget_twd} "
        f"交通={user_query.travel_mode} 偏好={','.join(user_query.preferences)}\n\n"
        "## 研究資料（已 grounding）\n\n"
        f"### 推薦景點\n{attractions_text}\n\n"
        f"### 推薦餐飲\n{meals_text}\n\n"
        "### 參考部落格\n"
        "（以下是上述景點/美食的來源，請依其重點安排行程，避免脫離來源臆測）\n"
        f"{blogs_text}\n"
        f"{route_block}"
        f"{mode_block}\n"
        f"{prior_block}"
        f"【使用者回饋 / 前一次驗證回饋（若有，必須據此調整）】\n{feedback_text}\n\n"
        "規劃規則：\n"
        f"1. 共 {user_query.days} 天，{drive_rule}\n"
        f"2. 總花費盡量控制在預算 {user_query.budget_twd} 元台幣內。\n"
        "3. 優先使用研究資料中的景點與餐飲，並符合使用者偏好。\n"
        "   每餐從研究池選與當日過夜區域相同或鄰近的餐廳；禁止拉麵/屋台當早餐。\n"
        "   美食請用研究池中的『實際店家/料理名稱』，不可用英文佔位字（如 Local Food、Area）。\n"
        f"{variety_rule}"
        "3b. 【重要】每天的景點不可與其他天重複；請把研究池中不同的景點平均分配到各天，"
        "讓 5 天看到不同地方，避免同一個地標出現多次。\n"
        f"4. 每天需包含 breakfast/lunch/dinner、2-3 個景點、住宿。{hotel_rule}\n"
        "5. drive_segments 必填：依移動順序串接（住宿→景點→…→住宿），"
        "每段填 from_location、to_location、mode（drive/transit/flight）。"
        "minutes/km 可填 0，系統會重算。\n"
        "6. 住宿連續性：停留在『同一座城市』的連續日子請使用『同一間飯店』"
        "（hotel.name 與 location 都保持一致），只有當行程移動到不同城市時才更換飯店，"
        "避免沒換城市卻每天換旅館。\n"
        "7. 每天的 notes 寫 1-2 句精簡的當日重點即可（主題或亮點），不需冗長。\n"
        "8. 請直接輸出 JSON，務必完整閉合；用字精簡，避免冗長描述以免超出長度。\n"
    )

    # temperature=0：結構化輸出用貪婪解碼，大幅降低弱模型在 guided JSON 下重複/跑不停的機率。
    planner_max_tokens = int(os.getenv("PLANNER_MAX_TOKENS", "4096"))
    result = call_structured(
        compose_system_prompt("planner", _SYSTEM),
        prompt,
        ItineraryOutput,
        temperature=0.0,
        max_tokens=planner_max_tokens,
    )
    if result is None and not compact:
        return run_planner_crew(
            research,
            user_query,
            validation_feedback=validation_feedback,
            llm=llm,
            preferred_hotel=preferred_hotel,
            prior_daily_plans=prior_daily_plans,
            compact=True,
        )
    if result is None:
        raise RuntimeError("Planner 結構化輸出失敗")
    return result


# =============================================================================
# Per-day focused planner (new for quality on multi-region / 5d+ trips)
# Replaces the previous one-shot full ItineraryOutput for the main path in generate_draft.
# =============================================================================

def plan_single_day(
    research: ResearchBundle,
    user_query: UserQuery,
    day: int,
    target_area: str,
    prev_location: str | None = None,
    validation_feedback: str = "",
    llm=None,
    preferred_hotel: dict | None = None,
) -> DayPlan:
    """產生單日詳細行程（結構化輸出）。

    與 run_planner_crew 的關鍵差異：
    - Prompt 只聚焦「這一天」+ 目標過夜區域，研究資料大幅縮減。
    - 明確帶入 prev_location 作為 drive_segments 起點，確保跨日連續。
    - 強制 hotel.location 與 target_area 一致、2-3 景點、餐飲不重複。
    - 輸出直接是 DayPlan（上層組裝成 list 後再跑既有 post-process）。
    """
    cap = drive_cap_for(user_query)
    drive_rule = (
        f"當日自駕段（mode=drive）合計必須 ≤ {cap} 分鐘。"
        "跨區移動標 mode=transit。"
    )

    feedback_parts = [p for p in (validation_feedback, user_query.user_feedback) if p]
    feedback_text = "\n".join(feedback_parts) if feedback_parts else "（首次規劃此日）"

    # 僅保留與 target_area 相關或通用的少量 grounding（模型仍會被指令限制只挑本地）。
    # 精簡到 6 景點 / 8 餐 避免 token 浪費，重點靠 instruction + location match。
    attractions_text = "\n".join(
        f"- {a.name}｜{a.location}｜{a.estimated_time_minutes}分｜{a.est_cost_twd}元"
        for a in research.attractions[:6]
    ) or "（無）"
    meals_text = "\n".join(
        f"- {m.meal_type}｜{m.name}｜{m.location}｜{m.cuisine}"
        for m in research.meals[:8]
    ) or "（無）"

    prev_line = f"前一天結束位置（本日第一個 drive_segment from 必須由此開始）：{prev_location}" if prev_location else "無前一天資訊（首日從機場/市區住宿開始）。"
    hotel_hint = ""
    if preferred_hotel:
        hotel_hint = (
            f"優先採用此區域飯店：name=\"{preferred_hotel.get('name','')}\" "
            f"location=\"{preferred_hotel.get('location', target_area)}\" "
            f"est_cost_twd≈{preferred_hotel.get('est_cost_twd', 0)}。"
        )

    prompt = (
        f"請只規劃第 {day} 天單日詳細行程，目標過夜區域/城市為「{target_area}」。\n\n"
        f"【使用者需求】目的地={user_query.destination} 總天數={user_query.days} "
        f"預算={user_query.budget_twd} 交通={user_query.travel_mode} 偏好={','.join(user_query.preferences)}\n\n"
        f"### 今日專用研究資料（僅供參考，務必優先選 location 與 {target_area} 相同或鄰近者）\n"
        f"推薦景點：\n{attractions_text}\n\n"
        f"推薦餐飲：\n{meals_text}\n\n"
        f"【前一天連續性】{prev_line}\n"
        f"{hotel_hint}\n"
        f"【回饋 / 必須修正】\n{feedback_text}\n\n"
        "單日規劃規則（嚴格遵守）：\n"
        f"1. {drive_rule}\n"
        "2. 安排 2-3 個不同景點（來自研究池，location 匹配今日 target_area）。\n"
        "3. 必有 breakfast/lunch/dinner 三餐，選研究池中「不同店家」、與 target_area 鄰近；"
        "   禁止同一餐名在同一天重複；禁止拉麵/屋台當早餐。\n"
        f"4. 住宿：hotel.location 必須是「{target_area}」或具體城市名（不可填「九州」等大地區）。"
        "   若提供 preferred_hotel 就直接採用。\n"
        "5. drive_segments：從 prev_location（若有）開始，串接 住宿/景點 順序，"
        "   每段填 from/to/mode/minutes/km（minutes 可 0，系統會重算）。\n"
        "6. notes 只寫 1 句當日亮點。\n"
        "7. 直接輸出單一 DayPlan JSON（不要包 ItineraryOutput），務必完整閉合。\n"
    )

    planner_max_tokens = int(os.getenv("PLANNER_MAX_TOKENS", "2048"))  # per-day 更小上限即可
    result = call_structured(
        compose_system_prompt("planner", _SYSTEM),
        prompt,
        DayPlan,  # 直接要單日結構
        temperature=0.0,
        max_tokens=planner_max_tokens,
    )
    if result is None:
        # 極簡後備：避免整天崩潰，上層仍會 sanitize / 補段
        return DayPlan(
            day=day,
            date=f"Day {day}",
            drive_total_minutes=60,
            drive_segments=[],
            meals={},
            attractions=research.attractions[:2] if research and research.attractions else [],
            hotel=preferred_hotel or {"name": f"{target_area} 住宿", "location": target_area, "est_cost_twd": 4000},
            notes=f"第 {day} 天（{target_area} 後備）。",
        )
    # 確保 day 正確（模型偶爾會寫錯）
    if getattr(result, "day", None) != day:
        result = result.model_copy(update={"day": day})
    if not getattr(result, "date", None):
        result = result.model_copy(update={"date": f"Day {day}"})
    return result
