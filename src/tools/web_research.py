"""
Web Research Tools for Phase 2 (Researcher Agent).

Responsibilities:
- Firecrawl v4 (or current unified client) structured extraction for Kyushu attractions & food
- SerpAPI Google search for recent travel blogs (grounding to fight hallucination)
- Full support for MOCK_TOOLS=1 (returns high-quality data from fixture or curated realistic data)

搜尋流程（2026-05-31 更新）：
  1. Serper（google.serper.dev）做關鍵字搜尋，撈出部落格 / 社群文章 URL。
  2. Firecrawl 解析這些 URL，做結構化景點 / 美食擷取。
  3. 擷取結果餵給 Planner。

注意：SerpAPI 已停用（disable），改用 Serper 做搜尋；飯店/租車在無價格引擎下
回退 mock，機票改由 src/tools/flights.py 的 fast-flights 提供。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx
from dotenv import load_dotenv
from langchain_core.tools import tool
from pydantic import BaseModel, Field

from src.core.models import Attraction, BlogSource, Meal, ResearchBundle
from src.tools.mocks import is_mock_mode, load_kyushu_fixture

load_dotenv()

# =============================================================================
# C-1 研究結果快取（依目的地 + 數量，TTL 內直接命中，跳過 Serper + Firecrawl）
# =============================================================================
_CACHE_DIR = Path(os.getenv("RESEARCH_CACHE_DIR", ".cache/research"))
_CACHE_TTL = int(os.getenv("CACHE_TTL_SECONDS", "86400"))  # 預設 1 天


def _cache_path(
    destination: str,
    max_attractions: int,
    max_meals: int,
    source_urls: list[str] | None = None,
) -> Path:
    urls_token = "|".join(sorted(source_urls or []))
    raw = f"{destination}|{max_attractions}|{max_meals}|{urls_token}"
    key = hashlib.md5(raw.encode("utf-8")).hexdigest()
    return _CACHE_DIR / f"{key}.json"


def _load_research_cache(p: Path) -> tuple[list[Attraction], list[Meal]] | None:
    """TTL 內回傳快取的 (attractions, meals)，否則 None。"""
    try:
        if p.exists() and (time.time() - p.stat().st_mtime) < _CACHE_TTL:
            d = json.loads(p.read_text("utf-8"))
            return ([Attraction(**a) for a in d.get("attractions", [])],
                    [Meal(**m) for m in d.get("meals", [])])
    except Exception as e:  # noqa: BLE001
        print(f"[web_research] 讀取研究快取失敗（忽略）：{e}")
    return None


def _save_research_cache(p: Path, attractions: list[Attraction], meals: list[Meal]) -> None:
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(
                {"attractions": [a.model_dump() for a in attractions],
                 "meals": [m.model_dump() for m in meals]},
                ensure_ascii=False,
            ),
            "utf-8",
        )
    except Exception as e:  # noqa: BLE001
        print(f"[web_research] 寫入研究快取失敗（忽略）：{e}")

# -----------------------------------------------------------------------------
# Schema definitions for Firecrawl structured extraction
# -----------------------------------------------------------------------------

class AttractionSchema(BaseModel):
    """Schema used to guide Firecrawl LLM extraction."""
    name: str = Field(..., description="景點名稱，使用繁體中文或日文原名 + 中文翻譯")
    type: str = Field(..., description="類型：shrine, castle, nature, onsen, museum, scenic, island, town 等")
    location: str = Field(..., description="所在縣市或區域，例如 福岡市、熊本、由布院、阿蘇")
    why_recommended: str = Field(..., description="為什麼推薦、特色亮點")
    estimated_visit_time_min: int = Field(90, description="建議停留時間（分鐘）")
    cost_amount: int = Field(0, description="門票/體驗費用的『原文金額』，不要自己換算；免費填 0")
    cost_currency: str = Field("JPY", description="上述金額幣別：JPY(円/¥)、TWD(台幣/NT)、USD；免費或未標填 none")


class FoodSchema(BaseModel):
    """Schema for local food / restaurant recommendations."""
    name: str = Field(..., description="餐廳或料理名稱")
    meal_type: str = Field(..., description="breakfast, lunch, dinner, snack")
    location: str
    cuisine_style: str = Field(..., description="當地特色料理風格，例如拉麵、和牛、海鮮、甜點等")
    why_recommended: str
    cost_amount: int = Field(0, description="單人預估的『原文金額』，不要自己換算")
    cost_currency: str = Field("JPY", description="幣別：JPY(円/¥)、TWD、USD；未標填 none")


class ResearchExtraction(BaseModel):
    """Top-level schema returned by Firecrawl JSON mode."""
    attractions: list[AttractionSchema] = Field(default_factory=list, description="至少 8-10 個推薦景點")
    foods: list[FoodSchema] = Field(default_factory=list, description="至少 12-15 個推薦餐飲選擇")


# -----------------------------------------------------------------------------
# Serper 關鍵字搜尋（google.serper.dev）— 撈 URL + 部落格 grounding
# -----------------------------------------------------------------------------

SERPER_ENDPOINT = "https://google.serper.dev/search"

# 低品質或與行程正文無關的網域（影片、OTA、團購、活動頁）
_BLOCK_DOMAINS = {
    "youtube.com", "youtu.be", "facebook.com", "instagram.com", "tiktok.com",
    "tripadvisor.com", "booking.com", "agoda.com", "kkday.com", "klook.com",
    "liontravel.com", "expedia.com", "hotels.com", "google.com", "google.com.tw",
    "maps.google.com", "play.google.com", "apps.apple.com",
}
_JUNK_SECTION_MARKERS = (
    "延伸閱讀", "相關文章", "你可能會喜歡", "你可能也喜歡", "熱門文章",
    "訂閱", "分享此文", "廣告合作", "贊助商", "留言", "comments",
)


def _parse_days_from_query(query: str, default: int = 5) -> int:
    """從使用者字串抓天數（例「玩 5 天」），供搜尋評分與查詢改寫。"""
    m = re.search(r"(\d+)\s*天", query)
    return int(m.group(1)) if m else default


def _canonical_article_key(url: str) -> str:
    """同篇文章不同語系/網域（.com/.cn）視為同一來源。"""
    try:
        path = urlparse(url).path.rstrip("/").lower()
        path = re.sub(r"\.(html?|php|aspx?)$", "", path)
        return path or url.lower()
    except Exception:  # noqa: BLE001
        return url.lower()


def _dedupe_results_by_path(results: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for r in results:
        link = r.get("link", "")
        key = _canonical_article_key(link)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def _domain_of(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
        return host[4:] if host.startswith("www.") else host
    except Exception:  # noqa: BLE001
        return ""


def _is_blocked_url(url: str) -> bool:
    domain = _domain_of(url)
    if not domain:
        return True
    return any(domain == b or domain.endswith("." + b) for b in _BLOCK_DOMAINS)


def score_search_result(
    result: dict,
    destination: str,
    days: int | None = None,
) -> float:
    """Serper 結果評分：越高越適合當行程 grounding 來源。"""
    link = result.get("link", "")
    if not link or _is_blocked_url(link):
        return -999.0

    text = f"{result.get('title', '')} {result.get('snippet', '')}"
    score = 0.0

    if destination and destination in text:
        score += 2.0
    if days is not None and (f"{days}天" in text or f"{days} 天" in text):
        score += 3.0
    for kw, pts in (("行程", 1.5), ("懶人包", 1.0), ("攻略", 1.0), ("自由行", 1.0), ("自駕", 1.0)):
        if kw in text:
            score += pts
    if link.endswith(".tw") or ".tw/" in link or _domain_of(link).endswith(".tw"):
        score += 0.5

    domain = _domain_of(link)
    if any(x in domain for x in ("blog", "travel", "trip", "tour")):
        score += 0.5

    return score


def rank_search_results(
    results: list[dict],
    destination: str,
    days: int | None = None,
) -> list[dict]:
    """依評分排序，過濾黑名單。"""
    scored = [
        (score_search_result(r, destination, days), r)
        for r in results
    ]
    scored = [(s, r) for s, r in scored if s > -100]
    scored.sort(key=lambda x: x[0], reverse=True)
    return [r for _, r in scored]


def _normalize_dedupe_key(name: str, location: str) -> tuple[str, str]:
    def norm(s: str) -> str:
        s = s.strip().lower()
        s = re.sub(r"[`'\"「」【】\[\]()（）]", "", s)
        return re.sub(r"\s+", "", s)

    return norm(name), norm(location)


def _dedupe_attractions(items: list[Attraction]) -> list[Attraction]:
    """同名同區景點合併，保留較完整的 notes。"""
    merged: dict[tuple[str, str], Attraction] = {}
    for a in items:
        key = _normalize_dedupe_key(a.name, a.location)
        prev = merged.get(key)
        if prev is None or len(a.notes) > len(prev.notes):
            merged[key] = a
    return list(merged.values())


_LUNCH_NOT_BREAKFAST = ("拉麵", "拉面", "ラーメン", "ramen", "一蘭", "屋台", "烏龍", "うどん", "燒鳥", "烧鸟")


def _filter_meals(
    items: list[Meal],
    destination: str,
    region_areas: list[str] | None = None,
) -> list[Meal]:
    """修正 meal_type 語意錯標，並過濾與目的地無關的餐廳。"""
    from src.agents.geo import normalize_area, same_area

    out: list[Meal] = []
    for m in items:
        mtype = m.meal_type
        if mtype == "breakfast" and any(k in m.name for k in _LUNCH_NOT_BREAKFAST):
            mtype = "lunch"
        meal = m.model_copy(update={"meal_type": mtype})
        if region_areas:
            area = normalize_area(meal.location, destination)
            if not any(same_area(area, r, destination) for r in region_areas):
                if destination not in (meal.location or ""):
                    continue
        out.append(meal)
    return out


def _dedupe_meals(items: list[Meal]) -> list[Meal]:
    merged: dict[tuple[str, str, str], Meal] = {}
    for m in items:
        key = (*_normalize_dedupe_key(m.name, m.location), m.meal_type)
        prev = merged.get(key)
        if prev is None or len(m.notes) > len(prev.notes):
            merged[key] = m
    return list(merged.values())


def _clean_markdown(md: str, max_chars: int) -> str:
    """確定性剔除導航/頁尾雜訊，優先保留正文段落。"""
    lines: list[str] = []
    for line in md.splitlines():
        stripped = line.strip()
        if any(m in stripped for m in _JUNK_SECTION_MARKERS):
            break
        if stripped.count("](") >= 2 and len(stripped) < 140:
            continue
        lines.append(line)
    text = "\n".join(lines).strip()
    if not text:
        return md[:max_chars]

    # 窗口夠大時不再「跳到第一個行程關鍵字」硬截——保留正文（含前段脈絡），
    # 只去掉導覽/頁尾雜訊，最後依 max_chars 收尾即可。
    return text[:max_chars]


def _serper_search(query: str, num_results: int = 5) -> list[dict]:
    """
    呼叫 Serper 做 Google 關鍵字搜尋，回傳 organic 結果（title / link / snippet / date）。
    無 key 或失敗時回空 list（呼叫端負責 fallback）。
    """
    api_key = os.getenv("SERPER_API_KEY")
    if not api_key or api_key.startswith("your_"):
        print("[web_research] No valid SERPER_API_KEY → serper search skipped")
        return []
    try:
        resp = httpx.post(
            SERPER_ENDPOINT,
            headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
            json={"q": query, "num": num_results, "gl": "jp", "hl": "zh-tw"},
            timeout=20.0,
        )
        resp.raise_for_status()
        organic = resp.json().get("organic", [])
        return [
            {
                "title": o.get("title", ""),
                "link": o.get("link", ""),
                "snippet": o.get("snippet", ""),
                "date": o.get("date", ""),
            }
            for o in organic[:num_results]
        ]
    except Exception as e:
        print(f"[web_research] Serper search failed: {e}")
        return []


# -----------------------------------------------------------------------------
# Firecrawl Research（解析 Serper 撈到的 URL）
# -----------------------------------------------------------------------------

def _discover_urls(destination: str, max_urls: int = 2, days: int | None = None) -> list[str]:
    """雙查詢：行程攻略 + 美食專文，canonical path 去重後取 top URL。"""
    days_part = f"{days}天 " if days else ""
    queries = [
        f"{destination} {days_part}自由行 行程 景點 推薦",
        f"{destination} 美食 餐廳 必吃 推薦",
    ]
    seen: set[str] = set()
    urls: list[str] = []
    for query in queries:
        results = _serper_search(query, num_results=max(8, max_urls * 3))
        ranked = _dedupe_results_by_path(rank_search_results(results, destination, days))
        for r in ranked:
            link = r.get("link", "")
            key = _canonical_article_key(link)
            if not link or key in seen:
                continue
            seen.add(key)
            urls.append(link)
            if len(urls) >= max_urls:
                return urls
    return urls


_VALID_ATTRACTION_TYPES = {
    "shrine", "castle", "nature", "onsen", "food", "museum",
    "scenic", "island", "town", "other",
}
_VALID_MEAL_TYPES = {"breakfast", "lunch", "dinner"}


# 幣別 → 台幣匯率（與 search_price_twd 的換算一致）。日本攻略價格多為日圓。
_FX_TO_TWD = {"JPY": 0.21, "USD": 32.0, "TWD": 1.0}


def _price_to_twd(amount, currency, *, default_currency: str = "JPY") -> int:
    """把『原文金額 + 幣別』確定性換算成台幣整數。

    這修掉「擷取直接把日圓數字塞進 TWD 欄位」（例：USJ ¥8,600→誤記 8,600 TWD）。
    未標幣別且金額>0 時，依日本攻略慣例以日圓估算。
    """
    try:
        amt = int(amount or 0)
    except (TypeError, ValueError):
        return 0
    if amt <= 0:
        return 0
    cur = (currency or "").strip().upper()
    if cur in ("", "NONE", "N/A", "FREE"):
        cur = default_currency
    rate = _FX_TO_TWD.get(cur, _FX_TO_TWD[default_currency])
    return int(round(amt * rate))


def _safe_attraction(item: dict, destination: str) -> Attraction | None:
    """把單筆 Firecrawl 景點資料轉成 Attraction；超範圍值會 clamp，無法轉換回 None（不拖垮整批）。"""
    try:
        a_type = str(item.get("type", "scenic")).lower().strip()
        if a_type not in _VALID_ATTRACTION_TYPES:
            a_type = "other"
        # clamp 停留時間到 model 允許範圍 [30, 600]，避免 480/360 這類值直接驗證失敗
        minutes = int(item.get("estimated_visit_time_min", 90) or 90)
        minutes = max(30, min(600, minutes))
        # 換算成台幣並 clamp（單一景點門票合理上限），擋住未換算的日圓漏網
        cost = min(6000, _price_to_twd(item.get("cost_amount"), item.get("cost_currency")))
        return Attraction(
            name=item.get("name", "Unknown"),
            type=a_type,
            location=item.get("location", destination),
            estimated_time_minutes=minutes,
            est_cost_twd=cost,
            notes=item.get("why_recommended", ""),
        )
    except Exception as e:  # noqa: BLE001
        print(f"[web_research] 略過一筆無法解析的景點：{e}")
        return None


def _safe_meal(item: dict, destination: str) -> Meal | None:
    """把單筆 Firecrawl 美食資料轉成 Meal；超範圍值會 clamp，無法轉換回 None。"""
    try:
        m_type = str(item.get("meal_type", "lunch")).lower().strip()
        if m_type not in _VALID_MEAL_TYPES:
            m_type = "lunch"  # snack 等非三餐類型併入午餐
        cost = min(5000, _price_to_twd(item.get("cost_amount"), item.get("cost_currency")))
        if cost <= 0:
            cost = 300  # 無價格資訊時的保守單餐台幣估值
        return Meal(
            meal_type=m_type,
            name=item.get("name", "Unknown"),
            location=item.get("location", destination),
            cuisine=item.get("cuisine_style", "local") or "local",
            est_cost_twd=cost,
            notes=item.get("why_recommended", ""),
        )
    except Exception as e:  # noqa: BLE001
        print(f"[web_research] 略過一筆無法解析的美食：{e}")
        return None


def extract_research_data(
    destination: str = "九州",
    max_attractions: int = 10,
    max_meals: int = 15,
    use_real_api: bool | None = None,
    source_urls: list[str] | None = None,
) -> tuple[list[Attraction], list[Meal]]:
    """
    用 Firecrawl 一次擷取「景點 + 美食」，兩者都跟著 destination 走。
    任一筆驗證失敗只略過該筆（不再整批丟棄）；mock 模式 / 無 key / 失敗時，
    景點與美食都退回「與目的地相符」的 mock 資料（九州用策展資料，其餘用通用模板）。

    source_urls：上游（run_researcher）已搜尋到的部落格 URL。提供時直接用這些 URL 做
    Firecrawl 擷取，使「列出的參考部落格」與「景點來源」一致（真正的 grounding）。
    """
    if use_real_api is None:
        use_real_api = not is_mock_mode()

    if not use_real_api:
        return _mock_attractions_dispatch(destination, max_attractions), \
            _mock_meals_dispatch(destination, max_meals)

    max_urls = int(os.getenv("SCRAPE_MAX_URLS", "3"))
    target_urls = (source_urls or [])[:max_urls]

    # C-1：命中快取就直接回傳（快取 key 含來源 URL，確保與當前部落格一致）
    cache_p = _cache_path(destination, max_attractions, max_meals, target_urls)
    cached = _load_research_cache(cache_p)
    if cached is not None:
        print(f"[web_research] 命中研究快取：{destination}")
        return cached

    firecrawl_key = os.getenv("FIRECRAWL_API_KEY")
    if not firecrawl_key or firecrawl_key.startswith("your_"):
        print("[web_research] No valid FIRECRAWL_API_KEY → falling back to mock data")
        return _mock_attractions_dispatch(destination, max_attractions), \
            _mock_meals_dispatch(destination, max_meals)

    try:
        from firecrawl import Firecrawl

        fc = Firecrawl(api_key=firecrawl_key)

        # C-2：優先用上游傳入的部落格 URL；未提供時才自行用 Serper 探索（預設僅 2 篇，省用量）
        if not target_urls:
            target_urls = _discover_urls(destination)
        if not target_urls:
            print(f"[web_research] No URLs discovered for '{destination}' → mock fallback")
            return _mock_attractions_dispatch(destination, max_attractions), \
                _mock_meals_dispatch(destination, max_meals)

        # C-3：用便宜的 scrape 取 markdown，再交給自家 LLM 結構化擷取（取代昂貴的 fc.extract）
        attractions, meals = _scrape_and_extract(
            fc, target_urls, destination, max_attractions, max_meals
        )

        # 不足即補：弱模型擷取量常偏低（甚至只 1 筆），導致行程空泛、景點重複。
        # 低於門檻時用該目的地策展資料補足並去重，確保 Planner 有足夠且具體的素材。
        attractions = _augment_attractions_to_min(attractions, destination, max_attractions)
        meals = _augment_meals_to_min(meals, destination, max_meals)

        # 寫入快取（僅在拿到真實資料時；純 mock 結果不污染快取）
        _save_research_cache(cache_p, attractions, meals)
        return attractions, meals

    except Exception as e:  # noqa: BLE001
        print(f"[web_research] 研究擷取失敗：{e}")
        return _mock_attractions_dispatch(destination, max_attractions), \
            _mock_meals_dispatch(destination, max_meals)


def _extract_from_page_markdown(
    md: str,
    destination: str,
    max_attractions: int,
    max_meals: int,
    source_url: str = "",
) -> tuple[list[Attraction], list[Meal]]:
    """單頁 markdown → 結構化景點/美食（逐頁擷取，避免多頁合併稀釋）。"""
    try:
        from src.services.llm import call_structured

        # 擷取輸出（10 景點+15 美食的中文 JSON）需要足夠 token，否則 JSON 在閉合前被截 →
        # guided_json 失敗 → 只救回零星幾筆。給獨立的 EXTRACT_MAX_TOKENS，不與小任務共用 LLM_MAX_TOKENS。
        res = call_structured(
            system=(
                f"你是旅遊資料擷取助手。會收到『單一篇』旅遊文章內容，請只擷取『確實位於 {destination}』的"
                "推薦景點與在地美食。規則：忽略導覽列、廣告、相關文章、其他地區、訂房連結與留言；"
                "若內容與目的地無關，回傳空列表。盡量以繁體中文輸出。"
                "費用請填『原文金額 cost_amount』與『幣別 cost_currency』，"
                "切勿自行換算；日本文章金額多為日圓(円/¥)→填 JPY，免費填 0 並 cost_currency=none。"
            ),
            prompt=(
                f"目的地：{destination}\n"
                f"來源：{source_url or '（未知）'}\n"
                f"請擷取至多 {max_attractions} 個景點與 {max_meals} 個美食。\n\n"
                f"文章內容：\n{md}"
            ),
            schema=ResearchExtraction,
            max_tokens=int(os.getenv("EXTRACT_MAX_TOKENS", "6000")),
        )
    except Exception as e:  # noqa: BLE001
        print(f"[web_research] 單頁 LLM 擷取失敗：{e}")
        return [], []

    if not res:
        return [], []

    attractions = [
        a for a in (_safe_attraction(i.model_dump(), destination) for i in res.attractions) if a
    ]
    meals = [m for m in (_safe_meal(i.model_dump(), destination) for i in res.foods) if m]
    return attractions[:max_attractions], meals[:max_meals]


def _scrape_and_extract(
    fc,
    urls: list[str],
    destination: str,
    max_attractions: int,
    max_meals: int,
) -> tuple[list[Attraction], list[Meal]]:
    """C-3：逐頁 scrape → 清洗 → 單頁 LLM 擷取 → 合併去重。"""
    # 預設拉高到可容納整篇攻略（64K 窗口下綽綽有餘）；env 可覆寫。
    max_chars_per_page = int(os.getenv("SCRAPE_MAX_CHARS_PER_PAGE", "24000"))
    n_urls = max(1, len(urls))
    per_page_attr = max(4, (max_attractions + n_urls - 1) // n_urls)
    per_page_meal = max(4, (max_meals + n_urls - 1) // n_urls)

    all_attractions: list[Attraction] = []
    all_meals: list[Meal] = []

    for u in urls:
        try:
            doc = fc.scrape(u, formats=["markdown"])
            md = getattr(doc, "markdown", None)
            if md is None and hasattr(doc, "get"):
                md = doc.get("markdown") or (doc.get("data", {}) or {}).get("markdown")
            if not md:
                continue
            cleaned = _clean_markdown(md, max_chars_per_page)
            attrs, meals = _extract_from_page_markdown(
                cleaned, destination, per_page_attr, per_page_meal, source_url=u,
            )
            all_attractions.extend(attrs)
            all_meals.extend(meals)
            print(f"[web_research] 單頁擷取 {u[:60]}… → {len(attrs)} 景點 / {len(meals)} 美食")
        except Exception as e:  # noqa: BLE001
            print(f"[web_research] scrape 失敗 {u}：{e}")

    if not all_attractions and not all_meals:
        return [], []

    attractions = _dedupe_attractions(all_attractions)[:max_attractions]
    region_areas = list({
        a.location for a in attractions if a.location
    })
    meals = _filter_meals(
        _dedupe_meals(all_meals), destination, region_areas or None,
    )[:max_meals]
    return attractions, meals


def extract_attractions_and_food(
    destination: str = "九州",
    max_items: int = 10,
    use_real_api: bool | None = None,
) -> list[Attraction] | list[dict]:
    """向後相容：只回傳景點（內部走 extract_research_data）。"""
    attractions, _ = extract_research_data(
        destination=destination, max_attractions=max_items, use_real_api=use_real_api
    )
    return attractions


def _mock_attractions_dispatch(destination: str, max_items: int = 10) -> list[Attraction]:
    """依目的地選擇 mock 景點：九州有高品質策展資料，其他用通用模板。"""
    if "九州" in destination or "kyushu" in destination.lower():
        return _get_mock_attractions(max_items)
    return _get_mock_attractions_for_destination(destination, max_items)


def _mock_meals_dispatch(destination: str, max_meals: int = 15) -> list[Meal]:
    """依目的地選擇 mock 餐廳：九州用 fixture 策展資料，其他用通用模板（避免九州餐廳亂入其他目的地）。"""
    if "九州" in destination or "kyushu" in destination.lower():
        return _get_mock_meals_kyushu(max_meals)
    return _get_mock_meals_for_destination(destination, max_meals)


# 研究擷取不足時的補底門檻（低於則用策展資料補足）。
_MIN_ATTRACTIONS_AFTER_AUGMENT = 8
_MIN_MEALS_AFTER_AUGMENT = 12


def _augment_attractions_to_min(
    items: list[Attraction], destination: str, max_attractions: int
) -> list[Attraction]:
    """景點不足門檻時，用策展/mock 資料補足並去重（真實擷取優先在前），截到上限。"""
    target_min = min(max_attractions, _MIN_ATTRACTIONS_AFTER_AUGMENT)
    if len(items) >= target_min:
        return items[:max_attractions]
    topped = _dedupe_attractions(
        list(items) + _mock_attractions_dispatch(destination, max_attractions)
    )
    return topped[:max_attractions]


def _augment_meals_to_min(
    items: list[Meal], destination: str, max_meals: int
) -> list[Meal]:
    """美食不足門檻時，用策展/mock 資料補足並去重（真實擷取優先在前），截到上限。"""
    target_min = min(max_meals, _MIN_MEALS_AFTER_AUGMENT)
    if len(items) >= target_min:
        return items[:max_meals]
    topped = _dedupe_meals(
        list(items) + _mock_meals_dispatch(destination, max_meals)
    )
    return topped[:max_meals]


# 向後相容別名（舊呼叫點仍可用）
def extract_kyushu_attractions_and_food(
    region_focus: str = "九州",
    max_items: int = 10,
    use_real_api: bool | None = None,
) -> list[Attraction] | list[dict]:
    return extract_attractions_and_food(region_focus, max_items, use_real_api)


def _get_mock_attractions(max_items: int = 10) -> list[Attraction]:
    """High-quality hardcoded realistic attractions for Kyushu (used in mock mode)."""
    data = [
        Attraction(name="太宰府天滿宮", type="shrine", location="福岡・太宰府", estimated_time_minutes=90, est_cost_twd=0, notes="學問之神，梅花與紅葉極美"),
        Attraction(name="櫻井二見浦 夫婦岩", type="scenic", location="福岡・糸島", estimated_time_minutes=60, est_cost_twd=0, notes="絕佳海景 + 鳥居"),
        Attraction(name="熊本城", type="castle", location="熊本市", estimated_time_minutes=120, est_cost_twd=800, notes="修復後天守閣壯麗"),
        Attraction(name="阿蘇山 草千里", type="nature", location="阿蘇", estimated_time_minutes=90, est_cost_twd=1000, notes="火山景觀 + 草原"),
        Attraction(name="黑川溫泉 鄉", type="onsen", location="熊本・黑川", estimated_time_minutes=150, est_cost_twd=1500, notes="日本最美露天溫泉鄉之一"),
        Attraction(name="由布院 湯之坪街道", type="town", location="由布院", estimated_time_minutes=90, est_cost_twd=0, notes="散步、咖啡、伴手禮小鎮"),
        Attraction(name="別府地獄溫泉", type="onsen", location="別府", estimated_time_minutes=120, est_cost_twd=1200, notes="八大地獄 + 地獄蒸"),
        Attraction(name="能古島", type="island", location="福岡・能古島", estimated_time_minutes=120, est_cost_twd=1200, notes="花海 + 自行車"),
    ]
    return data[:max_items]


# -----------------------------------------------------------------------------
# 熱門目的地策展庫（離線也豐富、location 用具體區名；研究不足時用來補足）
# -----------------------------------------------------------------------------

_CURATED_ATTRACTIONS: dict[str, list[Attraction]] = {
    "東京": [
        Attraction(name="淺草寺・雷門", type="shrine", location="東京・淺草", estimated_time_minutes=90, est_cost_twd=0, notes="東京最古老寺院，雷門大燈籠與仲見世通商店街"),
        Attraction(name="明治神宮", type="shrine", location="東京・原宿", estimated_time_minutes=90, est_cost_twd=0, notes="都心森林神社，被廣大綠地環抱，適合靜心散步"),
        Attraction(name="新宿御苑", type="nature", location="東京・新宿", estimated_time_minutes=120, est_cost_twd=200, notes="日式＋英式庭園，櫻花與紅葉名所，自然景觀首選"),
        Attraction(name="上野恩賜公園", type="nature", location="東京・上野", estimated_time_minutes=90, est_cost_twd=0, notes="博物館群與不忍池，春櫻夏荷的城市綠洲"),
        Attraction(name="高尾山", type="nature", location="東京・八王子", estimated_time_minutes=180, est_cost_twd=480, notes="登山纜車＋健行步道，山頂展望富士山，親近自然"),
        Attraction(name="台場海濱公園", type="scenic", location="東京・台場", estimated_time_minutes=90, est_cost_twd=0, notes="彩虹大橋海景與自由女神像，黃昏夜景極美"),
        Attraction(name="東京晴空塔", type="scenic", location="東京・墨田", estimated_time_minutes=120, est_cost_twd=1200, notes="634 公尺展望台，俯瞰關東平原"),
        Attraction(name="澀谷 SHIBUYA SKY", type="scenic", location="東京・澀谷", estimated_time_minutes=90, est_cost_twd=700, notes="頂樓開放式展望台，俯瞰澀谷十字路口"),
        Attraction(name="皇居外苑・二重橋", type="scenic", location="東京・千代田", estimated_time_minutes=60, est_cost_twd=0, notes="護城河與松林綠地，江戶城遺跡散策"),
        Attraction(name="teamLab Planets", type="museum", location="東京・豐洲", estimated_time_minutes=120, est_cost_twd=1000, notes="沉浸式數位藝術，水景與花景互動展"),
        Attraction(name="築地場外市場", type="food", location="東京・築地", estimated_time_minutes=90, est_cost_twd=0, notes="海鮮丼、玉子燒與在地小吃，美食巡禮"),
        Attraction(name="東京鐵塔", type="scenic", location="東京・港區", estimated_time_minutes=90, est_cost_twd=900, notes="東京地標，芝公園角度經典夜景"),
    ],
    "大阪": [
        Attraction(name="大阪城天守閣", type="castle", location="大阪・中央區", estimated_time_minutes=120, est_cost_twd=200, notes="豐臣秀吉名城，護城河與西之丸庭園"),
        Attraction(name="道頓堀", type="scenic", location="大阪・難波", estimated_time_minutes=90, est_cost_twd=0, notes="固力果跑跑人招牌與運河，美食一級戰區"),
        Attraction(name="新世界・通天閣", type="town", location="大阪・浪速區", estimated_time_minutes=90, est_cost_twd=300, notes="昭和懷舊街區，串炸發源地"),
        Attraction(name="萬博紀念公園", type="nature", location="大阪・吹田", estimated_time_minutes=120, est_cost_twd=0, notes="太陽之塔與大片綠地，自然散步"),
        Attraction(name="黑門市場", type="food", location="大阪・日本橋", estimated_time_minutes=90, est_cost_twd=0, notes="大阪廚房，海鮮與和牛現烤"),
        Attraction(name="箕面瀑布", type="nature", location="大阪・箕面", estimated_time_minutes=150, est_cost_twd=0, notes="森林步道與瀑布，紅葉名所"),
    ],
    "京都": [
        Attraction(name="伏見稻荷大社", type="shrine", location="京都・伏見", estimated_time_minutes=120, est_cost_twd=0, notes="千本鳥居，山林參道健行"),
        Attraction(name="清水寺", type="shrine", location="京都・東山", estimated_time_minutes=120, est_cost_twd=120, notes="清水舞台與二三年坂老街"),
        Attraction(name="嵐山竹林・渡月橋", type="nature", location="京都・嵐山", estimated_time_minutes=120, est_cost_twd=0, notes="竹林小徑與保津川，自然景觀代表"),
        Attraction(name="金閣寺", type="shrine", location="京都・北區", estimated_time_minutes=90, est_cost_twd=120, notes="鏡湖池倒映金閣，世界遺產"),
        Attraction(name="嵐山嵯峨野", type="nature", location="京都・嵐山", estimated_time_minutes=120, est_cost_twd=0, notes="嵯峨野觀光小火車與田園山景"),
        Attraction(name="錦市場", type="food", location="京都・中京", estimated_time_minutes=90, est_cost_twd=0, notes="京都廚房，醃漬物與抹茶甜點"),
    ],
    "沖繩": [
        Attraction(name="美麗海水族館", type="museum", location="沖繩・本部町", estimated_time_minutes=150, est_cost_twd=450, notes="黑潮之海大水槽與鯨鯊"),
        Attraction(name="古宇利島・古宇利大橋", type="island", location="沖繩・今歸仁", estimated_time_minutes=120, est_cost_twd=0, notes="跨海大橋與透明海水，戀之島"),
        Attraction(name="萬座毛", type="scenic", location="沖繩・恩納村", estimated_time_minutes=60, est_cost_twd=100, notes="象鼻岩斷崖海景"),
        Attraction(name="瀨長島", type="island", location="沖繩・豐見城", estimated_time_minutes=120, est_cost_twd=0, notes="海濱白色商店街與夕陽"),
        Attraction(name="知念岬公園", type="nature", location="沖繩・南城", estimated_time_minutes=60, est_cost_twd=0, notes="三面環海岬角，自然展望"),
        Attraction(name="國際通", type="town", location="沖繩・那霸", estimated_time_minutes=120, est_cost_twd=0, notes="那霸主街，伴手禮與沖繩料理"),
    ],
    "北海道": [
        Attraction(name="小樽運河", type="scenic", location="北海道・小樽", estimated_time_minutes=120, est_cost_twd=0, notes="石造倉庫與煤氣燈，運河夜景"),
        Attraction(name="大通公園", type="nature", location="北海道・札幌", estimated_time_minutes=90, est_cost_twd=0, notes="市中心綠帶，四季祭典舞台"),
        Attraction(name="登別地獄谷", type="onsen", location="北海道・登別", estimated_time_minutes=120, est_cost_twd=0, notes="火山噴氣地形與名湯溫泉"),
        Attraction(name="洞爺湖", type="nature", location="北海道・洞爺湖", estimated_time_minutes=120, est_cost_twd=0, notes="火山湖與展望台，自然景觀"),
        Attraction(name="白色戀人公園", type="museum", location="北海道・札幌", estimated_time_minutes=90, est_cost_twd=200, notes="巧克力工廠與英式庭園"),
        Attraction(name="二条市場", type="food", location="北海道・札幌", estimated_time_minutes=90, est_cost_twd=0, notes="海膽鮭魚卵丼，道地海鮮早餐"),
    ],
}


def _match_curated(destination: str, table: dict) -> list | None:
    """以子字串模糊比對策展庫；找不到回 None。"""
    dest = (destination or "").strip()
    if not dest:
        return None
    for key, items in table.items():
        if key in dest or dest in key:
            return items
    return None


def _get_mock_attractions_for_destination(destination: str, max_items: int = 10) -> list[Attraction]:
    """非九州目的地：優先用策展庫（具體區名、含自然景觀），找不到才用通用模板。"""
    curated = _match_curated(destination, _CURATED_ATTRACTIONS)
    if curated:
        return list(curated[:max_items])
    base = [
        Attraction(name=f"{destination} 經典地標", type="scenic", location=destination, estimated_time_minutes=90, est_cost_twd=500, notes="當地必訪景點"),
        Attraction(name=f"{destination} 美食街", type="food", location=destination, estimated_time_minutes=60, est_cost_twd=300, notes="在地人氣小吃"),
        Attraction(name=f"{destination} 自然公園", type="nature", location=destination, estimated_time_minutes=120, est_cost_twd=800, notes="適合散步與拍照"),
    ]
    # 簡單重複補足數量
    while len(base) < max_items:
        base.append(base[-1])
    return base[:max_items]


def _get_mock_meals_kyushu(max_meals: int = 15) -> list[Meal]:
    """九州策展餐廳（從 fixture 的每日 meals 取出）。"""
    fixture = load_kyushu_fixture()
    meals: list[Meal] = []
    for day in fixture.get("daily_plans", []):
        for mtype, meal in day.get("meals", {}).items():
            if isinstance(meal, dict):
                meals.append(
                    Meal(
                        meal_type=mtype if mtype in _VALID_MEAL_TYPES else "lunch",
                        name=meal.get("name", ""),
                        location=meal.get("location", ""),
                        cuisine=meal.get("notes", "local") or "local",
                        est_cost_twd=max(0, min(5000, int(meal.get("est_cost_twd", 600) or 600))),
                    )
                )
    return meals[:max_meals]


_CURATED_MEALS: dict[str, list[Meal]] = {
    "東京": [
        Meal(meal_type="breakfast", name="飯店自助早餐", location="東京", cuisine="日式", est_cost_twd=0),
        Meal(meal_type="breakfast", name="Pelican 炭烤吐司", location="東京・淺草", cuisine="麵包", est_cost_twd=180),
        Meal(meal_type="breakfast", name="築地 丸武 玉子燒", location="東京・築地", cuisine="玉子燒", est_cost_twd=120),
        Meal(meal_type="breakfast", name="上島珈琲 晨間套餐", location="東京・銀座", cuisine="咖啡早餐", est_cost_twd=200),
        Meal(meal_type="lunch", name="一蘭拉麵", location="東京・新宿", cuisine="拉麵", est_cost_twd=450),
        Meal(meal_type="lunch", name="壽司大", location="東京・築地", cuisine="壽司", est_cost_twd=700),
        Meal(meal_type="lunch", name="大黑家天婦羅", location="東京・淺草", cuisine="天婦羅", est_cost_twd=600),
        Meal(meal_type="lunch", name="阿美橫町小吃", location="東京・上野", cuisine="小吃", est_cost_twd=400),
        Meal(meal_type="lunch", name="名代宇奈とと 鰻魚飯", location="東京・澀谷", cuisine="鰻魚飯", est_cost_twd=500),
        Meal(meal_type="dinner", name="思い出橫丁 串燒", location="東京・新宿", cuisine="串燒", est_cost_twd=800),
        Meal(meal_type="dinner", name="月島文字燒", location="東京・月島", cuisine="文字燒", est_cost_twd=700),
        Meal(meal_type="dinner", name="敘敘苑燒肉", location="東京・澀谷", cuisine="燒肉", est_cost_twd=1200),
        Meal(meal_type="dinner", name="淺草今半 壽喜燒", location="東京・淺草", cuisine="壽喜燒", est_cost_twd=1100),
        Meal(meal_type="dinner", name="根室花丸 迴轉壽司", location="東京・銀座", cuisine="迴轉壽司", est_cost_twd=900),
        Meal(meal_type="dinner", name="もつ鍋", location="東京・新宿", cuisine="鍋物", est_cost_twd=850),
    ],
    "大阪": [
        Meal(meal_type="breakfast", name="飯店自助早餐", location="大阪", cuisine="日式", est_cost_twd=0),
        Meal(meal_type="breakfast", name="551 蓬萊 肉包", location="大阪・難波", cuisine="點心", est_cost_twd=150),
        Meal(meal_type="lunch", name="たこ家道頓堀くくる 章魚燒", location="大阪・道頓堀", cuisine="章魚燒", est_cost_twd=350),
        Meal(meal_type="lunch", name="自由軒 名物咖哩", location="大阪・難波", cuisine="咖哩", est_cost_twd=450),
        Meal(meal_type="lunch", name="大阪王將 煎餃", location="大阪・梅田", cuisine="中華", est_cost_twd=400),
        Meal(meal_type="dinner", name="美津の 大阪燒", location="大阪・道頓堀", cuisine="大阪燒", est_cost_twd=600),
        Meal(meal_type="dinner", name="串炸 だるま", location="大阪・新世界", cuisine="串炸", est_cost_twd=700),
        Meal(meal_type="dinner", name="蟹道樂", location="大阪・道頓堀", cuisine="蟹料理", est_cost_twd=1200),
        Meal(meal_type="dinner", name="黑門市場 和牛串", location="大阪・日本橋", cuisine="和牛", est_cost_twd=800),
    ],
    "京都": [
        Meal(meal_type="breakfast", name="飯店和式早餐", location="京都", cuisine="日式", est_cost_twd=0),
        Meal(meal_type="breakfast", name="イノダコーヒ 晨食", location="京都・中京", cuisine="咖啡", est_cost_twd=250),
        Meal(meal_type="lunch", name="錦市場 京漬物與豆乳甜甜圈", location="京都・中京", cuisine="小吃", est_cost_twd=400),
        Meal(meal_type="lunch", name="嵐山 湯豆腐", location="京都・嵐山", cuisine="湯豆腐", est_cost_twd=700),
        Meal(meal_type="lunch", name="抹茶蕎麥麵", location="京都・東山", cuisine="蕎麥麵", est_cost_twd=500),
        Meal(meal_type="dinner", name="町家 おばんざい", location="京都・祇園", cuisine="京料理", est_cost_twd=900),
        Meal(meal_type="dinner", name="先斗町 川床料理", location="京都・先斗町", cuisine="京懷石", est_cost_twd=1200),
        Meal(meal_type="dinner", name="一蘭拉麵 京都店", location="京都・東山", cuisine="拉麵", est_cost_twd=450),
        Meal(meal_type="dinner", name="茶寮都路里 抹茶甜點", location="京都・祇園", cuisine="甜點", est_cost_twd=350),
    ],
    "沖繩": [
        Meal(meal_type="breakfast", name="飯店自助早餐", location="沖繩", cuisine="日式", est_cost_twd=0),
        Meal(meal_type="breakfast", name="沖繩善哉與ぶくぶく茶", location="沖繩・那霸", cuisine="甜點", est_cost_twd=200),
        Meal(meal_type="lunch", name="沖繩麵 そば", location="沖繩・那霸", cuisine="沖繩麵", est_cost_twd=350),
        Meal(meal_type="lunch", name="塔可飯 タコライス", location="沖繩・北谷", cuisine="速食", est_cost_twd=300),
        Meal(meal_type="lunch", name="海葡萄海鮮丼", location="沖繩・本部", cuisine="海鮮", est_cost_twd=600),
        Meal(meal_type="dinner", name="沖繩和牛燒肉", location="沖繩・那霸", cuisine="燒肉", est_cost_twd=1200),
        Meal(meal_type="dinner", name="アグー豬涮涮鍋", location="沖繩・恩納", cuisine="鍋物", est_cost_twd=1000),
        Meal(meal_type="dinner", name="國際通居酒屋", location="沖繩・那霸", cuisine="居酒屋", est_cost_twd=800),
        Meal(meal_type="dinner", name="瀨長島海景晚餐", location="沖繩・豐見城", cuisine="西式", est_cost_twd=700),
    ],
    "北海道": [
        Meal(meal_type="breakfast", name="飯店自助早餐", location="北海道", cuisine="日式", est_cost_twd=0),
        Meal(meal_type="breakfast", name="二条市場 海鮮丼", location="北海道・札幌", cuisine="海鮮", est_cost_twd=300),
        Meal(meal_type="lunch", name="成吉思汗烤羊肉", location="北海道・札幌", cuisine="燒肉", est_cost_twd=700),
        Meal(meal_type="lunch", name="函館鹽味拉麵", location="北海道・函館", cuisine="拉麵", est_cost_twd=450),
        Meal(meal_type="lunch", name="湯咖哩", location="北海道・札幌", cuisine="咖哩", est_cost_twd=500),
        Meal(meal_type="dinner", name="かに本家 螃蟹三吃", location="北海道・札幌", cuisine="蟹料理", est_cost_twd=1200),
        Meal(meal_type="dinner", name="すみれ 味噌拉麵", location="北海道・札幌", cuisine="拉麵", est_cost_twd=500),
        Meal(meal_type="dinner", name="政壽司 小樽壽司", location="北海道・小樽", cuisine="壽司", est_cost_twd=1000),
        Meal(meal_type="dinner", name="北海道牛奶霜淇淋甜點", location="北海道・札幌", cuisine="甜點", est_cost_twd=250),
    ],
}


def _get_mock_meals_for_destination(destination: str, max_meals: int = 15) -> list[Meal]:
    """非九州目的地：優先用策展庫（具體區名、合理成本），找不到才用通用模板。"""
    curated = _match_curated(destination, _CURATED_MEALS)
    if curated:
        return list(curated[:max_meals])
    template = [
        ("breakfast", f"{destination} 飯店早餐", "已含", 0),
        ("lunch", f"{destination} 在地人氣餐館", "當地特色料理", 450),
        ("dinner", f"{destination} 名物晚餐", "在地必吃", 800),
    ]
    meals: list[Meal] = []
    while len(meals) < max_meals:
        mtype, name, cuisine, cost = template[len(meals) % len(template)]
        meals.append(Meal(meal_type=mtype, name=name, location=destination, cuisine=cuisine, est_cost_twd=cost))
    return meals[:max_meals]


# -----------------------------------------------------------------------------
# 部落格 grounding 搜尋（Serper）- 對抗幻覺的主要來源
# -----------------------------------------------------------------------------

def _results_to_blog_sources(results: list[dict]) -> list[BlogSource]:
    return [
        BlogSource(
            title=item.get("title", "Unknown Blog"),
            url=item.get("link", ""),
            published=item.get("date") or item.get("snippet", "")[:80],
            key_takeaways=item.get("snippet", "")[:200],
        )
        for item in results
    ]


def search_blogs_with_urls(
    query: str = "九州 自駕 5 天 2026 推薦 行程",
    num_results: int = 8,
    use_real_api: bool | None = None,
    destination: str = "",
    days: int | None = None,
    max_scrape_urls: int | None = None,
) -> tuple[list[BlogSource], list[str]]:
    """搜尋旅遊部落格，評分後回傳 (blog_sources, urls)。

    只回傳實際會 scrape 的 top-N 篇，讓「列出的部落格」=「景點來源」。
    mock / 無 key / 搜尋失敗時，blog_sources 退回 fixture、urls 為空。
    """
    if use_real_api is None:
        use_real_api = not is_mock_mode()

    if not use_real_api:
        return _get_mock_blog_sources(), []

    scrape_n = max_scrape_urls or int(os.getenv("SCRAPE_MAX_URLS", "3"))
    dest = destination or query.split()[0] if query else ""
    search_q = f"{query} 行程 景點 攻略"
    results = _serper_search(search_q, num_results=num_results)
    if not results:
        return _get_mock_blog_sources(), []

    ranked = _dedupe_results_by_path(rank_search_results(results, dest, days))
    if not ranked:
        return _get_mock_blog_sources(), []

    selected = ranked[:scrape_n]
    blogs = _results_to_blog_sources(selected)
    urls = [item.get("link", "") for item in selected if item.get("link")]
    return blogs, urls


def search_travel_blogs(
    query: str = "九州 自駕 5 天 2026 推薦 行程",
    num_results: int = 5,
    use_real_api: bool | None = None,
) -> list[BlogSource]:
    """用 Serper 搜尋最新旅遊部落格 / 社群文章作為 grounding（只回 blog_sources）。"""
    return search_blogs_with_urls(query, num_results, use_real_api)[0]


# 向後相容別名
def search_travel_blogs_serpapi(
    query: str = "九州 自駕 5 天 2026 推薦 行程",
    num_results: int = 5,
    use_real_api: bool | None = None,
) -> list[BlogSource]:
    return search_travel_blogs(query, num_results, use_real_api)


def _get_mock_blog_sources() -> list[BlogSource]:
    """Return realistic blog sources (aligned with the fixture)."""
    fixture = load_kyushu_fixture()
    sources = fixture.get("blog_sources", [])
    return [
        BlogSource(
            title=s.get("title", ""),
            url=s.get("url", ""),
            published=s.get("published"),
            key_takeaways=s.get("key_takeaways", ""),
        )
        for s in sources
    ]


# -----------------------------------------------------------------------------
# High-level Researcher Function (used by Crew / Graph)
# -----------------------------------------------------------------------------

def run_researcher(
    query: str,
    destination: str = "九州",
    max_attractions: int = 12,
    days: int | None = None,
) -> ResearchBundle:
    """
    Main entry point for the Researcher stage.
    Accepts `destination` (and in the future more UserQuery fields) so research scope adapts
    to whatever the user selected in the UI (九州、大阪、東京、沖繩、自訂...).
    """
    trip_days = days if days is not None else _parse_days_from_query(query)
    print(
        f"[Researcher] Starting research for destination=[{destination}] | "
        f"{trip_days}天 | query: {query[:60]}... (mock={is_mock_mode()})"
    )

    # 先搜尋部落格，評分後只取 top URL；再用同一批 URL 逐頁擷取並去重。
    blogs, blog_urls = search_blogs_with_urls(
        query=f"{destination} {trip_days}天 自由行 {query}",
        num_results=8,
        destination=destination,
        days=trip_days,
    )

    # 通用化：景點與美食都跟著 destination 走（真實 Firecrawl 用 blog_urls / 離線 mock 皆然）
    attractions, meals_raw = extract_research_data(
        destination=destination,
        max_attractions=max_attractions,
        source_urls=blog_urls or None,
    )

    # region_coverage 動態化：從景點/餐廳的 location 取不重複區域，取不到就用 destination
    coverage: list[str] = []
    for item in [*attractions, *meals_raw]:
        loc = (getattr(item, "location", "") or "").strip()
        if loc and loc not in coverage:
            coverage.append(loc)
    region_coverage = coverage[:6] or [destination]

    from src.agents.geo import build_region_clusters, build_route_hint

    all_locs = [getattr(x, "location", "") for x in [*attractions, *meals_raw]]
    clusters = build_region_clusters(all_locs, destination, trip_days)
    route_hint = build_route_hint(clusters, destination, trip_days)

    bundle = ResearchBundle(
        query_context=query,
        attractions=attractions,
        meals=meals_raw[:15],
        blog_sources=blogs,
        region_coverage=region_coverage,
        region_clusters=clusters,
        route_hint=route_hint,
        notes=(
            f"研究完成（destination={destination}, days={trip_days}, "
            f"sources={len(blog_urls)}, clusters={len(clusters)}, mock_tools={is_mock_mode()}）。"
            f"Serper 評分篩選 → 逐頁 Firecrawl 擷取 → 去重（失敗時退回目的地 mock）。"
        ),
    )
    return bundle


# -----------------------------------------------------------------------------
# 飯店比價（SerpAPI google_hotels）— Booker Agent 使用
# -----------------------------------------------------------------------------

def search_hotels(
    destination: str,
    budget_per_night_twd: int = 8000,
    num_results: int = 4,
    use_real_api: bool | None = None,
    check_in_date: str | None = None,
    check_out_date: str | None = None,
) -> list[dict]:
    """
    用 SerpAPI google_hotels 引擎查詢飯店比價（不執行預訂）。
    無 key / 失敗 / mock 模式時退回確定性估價。
    """
    if use_real_api is None:
        use_real_api = not is_mock_mode()

    api_key = os.getenv("SERPAPI_KEY")
    if not use_real_api or not api_key or api_key.startswith("your_"):
        return _get_mock_hotels(destination, budget_per_night_twd, num_results)

    try:
        import serpapi

        client = serpapi.Client(api_key=api_key)
        results = client.search({
            "engine": "google_hotels",
            "q": f"{destination} 飯店",
            "currency": "TWD",
            "hl": "zh-tw",
            "gl": "jp",
            "check_in_date": check_in_date or os.getenv("HOTEL_CHECK_IN", "2026-06-28"),
            "check_out_date": check_out_date or os.getenv("HOTEL_CHECK_OUT", "2026-06-29"),
        })
        data = results.as_dict() if hasattr(results, "as_dict") else results
        props = data.get("properties", []) or []
        hotels: list[dict] = []
        for p in props[:num_results]:
            rate = (p.get("rate_per_night") or {}).get("extracted_lowest") \
                or (p.get("total_rate") or {}).get("extracted_lowest") \
                or budget_per_night_twd
            hotels.append({
                "name": p.get("name", "Unknown"),
                "price_per_night_twd": int(rate),
                "rating": p.get("overall_rating", 0),
                "link": p.get("link", "") or p.get("serpapi_property_details_link", ""),
            })
        return hotels or _get_mock_hotels(destination, budget_per_night_twd, num_results)
    except Exception as e:
        print(f"[web_research] SerpAPI google_hotels failed: {e}")
        return _get_mock_hotels(destination, budget_per_night_twd, num_results)


def _get_mock_hotels(destination: str, budget_per_night_twd: int, num_results: int) -> list[dict]:
    base = max(3000, int(budget_per_night_twd * 0.7))
    options = [
        {"name": f"{destination} 車站前商務酒店", "price_per_night_twd": base, "rating": 4.1, "link": "https://www.booking.com/"},
        {"name": f"{destination} 溫泉旅館（含早餐）", "price_per_night_twd": int(base * 1.4), "rating": 4.5, "link": "https://www.agoda.com/"},
        {"name": f"{destination} 平價民宿", "price_per_night_twd": int(base * 0.75), "rating": 3.9, "link": "https://www.booking.com/"},
        {"name": f"{destination} 市區精品飯店", "price_per_night_twd": int(base * 1.2), "rating": 4.3, "link": "https://www.agoda.com/"},
    ]
    return options[:num_results]


# -----------------------------------------------------------------------------
# 租車比價（mock 固定 1500 元/天）
# -----------------------------------------------------------------------------

CAR_RENTAL_PER_DAY_TWD = 1500


def search_car_rental(
    destination: str,
    days: int = 5,
    num_results: int = 3,
    use_real_api: bool | None = None,
) -> list[dict]:
    """回傳租車比價選項（不執行預訂）。無價格 API，以固定 1500 元/天 mock 估價。"""
    return _get_mock_car_rentals(destination, days, num_results)


def _get_mock_car_rentals(destination: str, days: int, num_results: int) -> list[dict]:
    per_day = CAR_RENTAL_PER_DAY_TWD  # 固定 1500/天
    options = [
        {"vendor": "Times Car Rental", "car_class": "小型車 (Vitz/Fit)", "price_total_twd": per_day * days, "link": "https://www.kkday.com/"},
        {"vendor": "Toyota Rent a Car", "car_class": "標準車 (Corolla)", "price_total_twd": int(per_day * 1.3) * days, "link": "https://www.rentalcars.com/"},
        {"vendor": "Nissan Rent a Car", "car_class": "休旅車 (X-Trail)", "price_total_twd": int(per_day * 1.8) * days, "link": "https://www.kkday.com/"},
    ]
    return options[:num_results]


# -----------------------------------------------------------------------------
# 費用估價（Serper 搜尋）— 大眾運輸 / 景點門票等
# -----------------------------------------------------------------------------

class _PriceEstimate(BaseModel):
    """LLM 從搜尋摘要判讀出的價格估算（用 json_schema 由 vLLM 端強制約束）。"""
    found: bool = Field(..., description="摘要中是否有可信、與查詢項目相符的價格資訊")
    amount_twd: int = Field(0, description="換算成新台幣的整數金額；找不到回 0")
    currency_detected: str = Field("none", description="原文幣別：TWD / JPY / USD / none")
    reasoning: str = Field("", description="一句話說明價格依據（哪段摘要）")


def search_price_twd(query: str, fallback: int = 0, use_real_api: bool | None = None) -> int:
    """
    用 Serper 搜尋取得最新摘要，再交給 LLM 判讀出一個台幣金額（找不到回 fallback）。
    用於估算大眾運輸票價、景點門票等「可搜尋得到」的費用。

    相較舊版正則：LLM 會排除年份/電話等雜訊、判斷數字是否真的是該項目價格，
    並把日圓（円）等外幣換算成台幣。LLM 不可用時回 fallback。
    """
    if use_real_api is None:
        use_real_api = not is_mock_mode()
    if not use_real_api:
        return fallback

    results = _serper_search(query, num_results=5)
    if not results:
        return fallback

    blob = "\n".join(
        f"- {r.get('title','')}｜{r.get('snippet','')}" for r in results
    )

    try:
        from src.services.llm import call_structured

        est = call_structured(
            system=(
                "你是旅遊費用查價助理。會收到一個查詢項目與若干 Google 搜尋摘要，"
                "請判讀該項目最合理的單價。規則："
                "1) 只採用與查詢項目明確相符的價格，忽略年份、日期、電話、評論數、瀏覽數等雜訊；"
                "2) 若摘要是日圓(円/JPY)請以 1 JPY≈0.21 TWD 換算；美元(USD)以 1 USD≈32 TWD 換算；"
                "3) 換算後四捨五入為整數台幣；4) 若沒有可信價格，found=false 且 amount_twd=0。"
            ),
            prompt=f"查詢項目：{query}\n\n搜尋摘要：\n{blob}",
            schema=_PriceEstimate,
        )
        if est and est.found and est.amount_twd > 0:
            return int(est.amount_twd)
    except Exception as e:
        print(f"[web_research] LLM 估價失敗：{e}")

    return fallback


# -----------------------------------------------------------------------------
# LangChain Tools（供 ReAct agent 自主呼叫）
# -----------------------------------------------------------------------------

@tool
def firecrawl_extract_tool(destination: str, max_items: int = 10) -> list[dict]:
    """爬取並擷取指定目的地的推薦景點與活動（含名稱、類型、地點、停留時間、費用）。"""
    attractions = extract_attractions_and_food(destination=destination, max_items=max_items)
    return [a.model_dump() if isinstance(a, Attraction) else a for a in attractions]


@tool
def serper_search_tool(query: str, num_results: int = 5) -> list[dict]:
    """以 Serper 做 Google 關鍵字搜尋，撈出最新旅遊部落格/社群文章，回傳標題、連結、重點摘要。"""
    blogs = search_travel_blogs(query=query, num_results=num_results)
    return [b.model_dump() for b in blogs]


# 向後相容別名
serpapi_blog_search_tool = serper_search_tool


@tool
def serper_price_tool(query: str, fallback: int = 0) -> int:
    """用 Serper 搜尋並估算某項費用的台幣金額（例：景點門票、一日券、大眾運輸票價）。回傳整數，找不到回 fallback。"""
    return search_price_twd(query, fallback=fallback)


@tool
def hotel_search_tool(destination: str, budget_per_night_twd: int = 8000) -> list[dict]:
    """查詢目的地飯店比價選項（名稱、每晚價格、評分、連結）。不執行預訂。"""
    return search_hotels(destination, budget_per_night_twd=budget_per_night_twd)


@tool
def carrental_search_tool(destination: str, days: int = 5) -> list[dict]:
    """查詢目的地租車比價選項（車行、車型、總價、連結）。不執行預訂。"""
    return search_car_rental(destination, days=days)


# 向後相容別名
serpapi_hotel_tool = hotel_search_tool
serpapi_carrental_tool = carrental_search_tool
