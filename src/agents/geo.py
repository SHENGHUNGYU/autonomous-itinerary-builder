"""目的地無關的地理正規化與區域聚類（適用任何日本/海外行程）。"""

from __future__ import annotations

import re
from collections import Counter

from src.core.models import DayPlan, RegionCluster

# 常見行政區尾碼（日本／中文地址）
_AREA_TAIL = re.compile(
    r"([^\s,，、/]{1,12}[市町村区縣郡]|"
    r"[^\s,，、/]{2,20}(?:市|町|村|区|縣|郡))"
)
# 僅去除區域級前綴（勿 strip「太宰府」等子地名，避免「太宰府市」→「市」）
_STRIP_PREFIX = re.compile(
    r"^(日本|台灣|臺灣|中国|中國|韓國|泰国|泰國|"
    r"北海道|九州|關西|关东|關東|冲绳|沖繩)"
)
_CJK = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")
_STATION_SUFFIX = re.compile(r"(車站|駅|站)$")
_FACILITY_TOKENS = frozenset({
    "PLAZA", "AMU", "MALL", "STATION", "ST", "CENTER", "CENTRE",
    "BUILDING", "BLDG", "BDG", "CITY", "PARK",
})
_MUNICIPAL_SUFFIX = re.compile(r"(市|区|區|町|村|郡|縣)$")


def _has_cjk(text: str) -> bool:
    return bool(_CJK.search(text))


def _is_facility_token(token: str) -> bool:
    t = (token or "").strip()
    if not t or _has_cjk(t):
        return False
    return t.upper() in _FACILITY_TOKENS or t.isupper() and len(t) <= 8


def _is_municipal_label(area: str) -> bool:
    return bool(_MUNICIPAL_SUFFIX.search((area or "").strip()))


def normalize_area(location: str, destination: str = "") -> str:
    """把任意 location 字串正規化成可比對的『區域標籤』。"""
    loc = (location or "").strip()
    if not loc:
        return (destination or "").strip() or "unknown"
    loc = _STRIP_PREFIX.sub("", loc).strip() or loc
    # 都道府縣+市（大阪府大阪市），但不含地名本身含「府」者（太宰府市）
    if "府" in loc:
        _pref, rest = loc.split("府", 1)
        if rest and len(rest) >= 2:
            loc = rest.strip()
    m = _AREA_TAIL.search(loc)
    if m:
        return m.group(1).strip()
    parts = [p.strip() for p in re.split(r"[\s,，、/]+", loc) if p.strip()]
    for part in parts:
        m = _AREA_TAIL.search(part)
        if m:
            return m.group(1).strip()
    for part in reversed(parts):
        if _is_facility_token(part):
            continue
        if _has_cjk(part):
            cleaned = _STATION_SUFFIX.sub("", part).strip() or part
            return cleaned[:24]
    for part in parts:
        if _has_cjk(part):
            return part[:24]
    return (parts[0][:24] if parts else loc[:24])


def collapse_metro_visit_areas(areas: set[str], base: str, destination: str = "") -> set[str]:
    """同日景點區域：市/區級與商圈短名（博多、天神）併入同一都市基地，避免假跨區。"""
    if len(areas) < 2:
        return areas
    municipal = {a for a in areas if _is_municipal_label(a)}
    if not municipal:
        return areas
    primary = base if base in municipal else next(iter(municipal))
    collapsed: set[str] = set()
    for area in areas:
        if _is_municipal_label(area):
            collapsed.add(area)
        elif same_area(area, primary, destination):
            collapsed.add(primary)
        elif not _is_municipal_label(area) and len(area) <= 8:
            collapsed.add(primary)
        else:
            collapsed.add(area)
    return collapsed


def same_area(a: str, b: str, destination: str = "") -> bool:
    """兩地點是否視為同一區域（子字串或正規化後相等）。"""
    na, nb = normalize_area(a, destination), normalize_area(b, destination)
    if not na or not nb:
        return False
    if na == nb:
        return True
    return na in nb or nb in na


def collect_areas_from_locations(locations: list[str], destination: str = "") -> list[str]:
    """去重保序的區域列表。"""
    seen: set[str] = set()
    out: list[str] = []
    for loc in locations:
        area = normalize_area(loc, destination)
        if area and area not in seen:
            seen.add(area)
            out.append(area)
    return out


def _cluster_hub(area: str) -> str:
    """合併同圈層子區（例：福岡・太宰府 → 福岡）。"""
    if "・" in area:
        return area.split("・", 1)[0].strip() or area
    return area


def build_region_clusters(
    locations: list[str],
    destination: str,
    trip_days: int,
) -> list[RegionCluster]:
    """依景點/餐飲 location 動態聚類，不硬編碼任何目的地。"""
    raw_areas = collect_areas_from_locations(locations, destination)
    seen_hubs: set[str] = set()
    areas: list[str] = []
    for area in raw_areas:
        hub = _cluster_hub(area)
        if hub not in seen_hubs:
            seen_hubs.add(hub)
            areas.append(hub)
    if not areas:
        areas = [destination] if destination else ["unknown"]
    if len(areas) == 1:
        return [
            RegionCluster(
                name=areas[0],
                areas=[areas[0]],
                hub_city=areas[0],
                suggested_nights=max(1, trip_days),
            )
        ]

    # 多區域：每個 area 一個 cluster；建議晚數依出現次數與天數分配
    counts = Counter(normalize_area(loc, destination) for loc in locations if loc)
    total = sum(counts.values()) or 1
    clusters: list[RegionCluster] = []
    for area in areas:
        weight = counts.get(area, 1) / total
        nights = max(1, round(trip_days * weight))
        clusters.append(
            RegionCluster(
                name=area,
                areas=[area],
                hub_city=area,
                suggested_nights=min(nights, trip_days),
            )
        )
    return clusters[: max(6, trip_days)]


def build_route_hint(clusters: list[RegionCluster], destination: str, trip_days: int) -> str:
    """產生給 Planner 的通用路由提示。"""
    if len(clusters) <= 1:
        return f"{destination or clusters[0].hub_city} 單一區域，可連續住宿同一基地。"
    names = " → ".join(c.hub_city for c in clusters[:5])
    return (
        f"研究涵蓋 {len(clusters)} 個子區域（{names}）。"
        f"{trip_days} 天行程應分區安排：每區至少過夜 1 晚或就近住宿；"
        "跨區移動優先大眾運輸（鐵路/巴士），區內再以自駕或短程交通。"
    )


def day_attraction_areas(dp: DayPlan, destination: str = "") -> set[str]:
    """僅景點所在區域（地理檢查用，不含餐飲）。"""
    areas: set[str] = set()
    for a in dp.attractions:
        if a.location:
            areas.add(normalize_area(a.location, destination))
    return {x for x in areas if x and x != "unknown"}


def day_visit_areas(dp: DayPlan, destination: str = "") -> set[str]:
    areas = set(day_attraction_areas(dp, destination))
    for meal in dp.meals.values():
        if meal.location:
            areas.add(normalize_area(meal.location, destination))
    return areas


def overnight_area(dp: DayPlan, destination: str = "") -> str:
    hotel = dp.hotel or {}
    loc = (hotel.get("location") or hotel.get("name") or "").strip()
    if loc and not same_area(loc, destination, destination) and len(loc) > 2:
        return normalize_area(loc, destination)
    if dp.attractions:
        return normalize_area(dp.attractions[-1].location, destination)
    return normalize_area(destination, destination)


def unique_overnight_areas(daily_plans: list[DayPlan], destination: str = "") -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for dp in daily_plans:
        area = overnight_area(dp, destination)
        if area and area not in seen:
            seen.add(area)
            out.append(area)
    return out