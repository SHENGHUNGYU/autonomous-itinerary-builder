"""
Maps & Routes Tools.

Responsibilities:
- 真實模式：用 Google Routes API（computeRoutes）計算真實駕車時間、距離與 polyline，
  支援 TRAFFIC_AWARE 路線。
- Full MOCK_TOOLS support with realistic Kyushu (and generic) data.

這是 RouteValidator Agent 與每日駕車時間硬約束的基礎。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import polyline
import requests
from dotenv import load_dotenv
from langchain_core.tools import tool

from src.tools.mocks import is_mock_mode

load_dotenv()


@dataclass
class RouteResult:
    duration_seconds: int
    distance_meters: int
    duration_minutes: int
    polyline: str | None = None
    status: str = "OK"
    error_message: str | None = None


# =============================================================================
# Mock Data (used when MOCK_TOOLS=1 or no key)
# =============================================================================

# Simple realistic driving times (in minutes) for common Kyushu pairs.
# This allows us to simulate violations for testing the retry loop.
MOCK_DRIVE_TIMES_MINUTES: dict[tuple[str, str], int] = {
    ("Fukuoka", "Dazaifu"): 35,
    ("Fukuoka", "Itoshima"): 50,
    ("Fukuoka", "Kumamoto"): 95,
    ("Kumamoto", "Aso"): 65,
    ("Kumamoto", "Yufuin"): 105,
    ("Yufuin", "Beppu"): 45,
    ("Yufuin", "Fukuoka"): 90,
    ("Aso", "Kumamoto"): 65,
    ("Kumamoto", "Fukuoka"): 95,
    # 東京近郊自駕
    ("新宿", "箱根"): 71,
    ("箱根", "新宿"): 79,
    ("新宿格拉斯麗飯店", "箱根神社"): 71,
    ("蘆之湖遊船", "新宿格拉斯麗飯店"): 79,
    # Add some "bad" routes that can trigger violations for testing
    ("Kumamoto", "Kagoshima"): 180,   # Too long → good for testing retry
    ("Fukuoka", "Kagoshima"): 240,
}


# 常見地點的近似座標（mock 地圖用）。找不到時以名稱 hash 在日本一帶生成穩定座標。
MOCK_PLACE_COORDS: dict[str, tuple[float, float]] = {
    # 九州
    "fukuoka": (33.5902, 130.4017), "福岡": (33.5902, 130.4017),
    "博多": (33.5897, 130.4207), "hakata": (33.5897, 130.4207),
    "dazaifu": (33.5150, 130.5350), "太宰府": (33.5150, 130.5350),
    "itoshima": (33.5570, 130.1960), "糸島": (33.5570, 130.1960),
    "kumamoto": (32.8032, 130.7079), "熊本": (32.8032, 130.7079),
    "aso": (32.8847, 131.1040), "阿蘇": (32.8847, 131.1040),
    "yufuin": (33.2647, 131.3550), "由布院": (33.2647, 131.3550),
    "beppu": (33.2846, 131.4912), "別府": (33.2846, 131.4912),
    "kagoshima": (31.5966, 130.5571), "鹿兒島": (31.5966, 130.5571),
    # 關西
    "osaka": (34.6937, 135.5023), "大阪": (34.6937, 135.5023),
    "梅田": (34.7025, 135.4959), "umeda": (34.7025, 135.4959),
    "難波": (34.6627, 135.5021), "namba": (34.6627, 135.5021),
    "心齋橋": (34.6723, 135.5007), "shinsaibashi": (34.6723, 135.5007),
    "道頓堀": (34.6687, 135.5013), "dotonbori": (34.6687, 135.5013),
    "新世界": (34.6524, 135.5063), "黑門市場": (34.6654, 135.5066),
    "京都": (35.0116, 135.7681), "kyoto": (35.0116, 135.7681),
    "神戶": (34.6901, 135.1955), "kobe": (34.6901, 135.1955),
    "奈良": (34.6851, 135.8048), "nara": (34.6851, 135.8048),
    # 東京都心與近郊
    "東京": (35.6762, 139.6503), "tokyo": (35.6762, 139.6503),
    "淺草": (35.7148, 139.7967), "asakusa": (35.7148, 139.7967),
    "澀谷": (35.6595, 139.7004), "shibuya": (35.6595, 139.7004),
    "原宿": (35.6702, 139.7027), "harajuku": (35.6702, 139.7027),
    "新宿": (35.6938, 139.7034), "shinjuku": (35.6938, 139.7034),
    "上野": (35.7148, 139.7745), "ueno": (35.7148, 139.7745),
    "築地": (35.6654, 139.7707), "tsukiji": (35.6654, 139.7707),
    "豐洲": (35.6498, 139.7920), "toyosu": (35.6498, 139.7920),
    "銀座": (35.6717, 139.7650), "ginza": (35.6717, 139.7650),
    "皇居": (35.6806, 139.7537), "千代田": (35.6806, 139.7537),
    "晴空塔": (35.7101, 139.8107), "skytree": (35.7101, 139.8107),
    "箱根": (35.2324, 139.1069), "hakone": (35.2324, 139.1069),
    "高尾山": (35.6258, 139.2693),
    "沖繩": (26.2124, 127.6809), "那霸": (26.2124, 127.6809), "okinawa": (26.2124, 127.6809),
    "札幌": (43.0618, 141.3545), "sapporo": (43.0618, 141.3545), "北海道": (43.0618, 141.3545),
    "名古屋": (35.1815, 136.9066), "nagoya": (35.1815, 136.9066),
}


def geocode_location(name: str, region_hint: str = "") -> tuple[float, float] | None:
    """把地點名稱轉成 (lat, lng)。

    真實模式用 Google Geocoding API；mock 模式 / 無 key / 失敗時，
    退回近似座標表（找不到則以名稱 hash 生成穩定座標）。供「行程地圖」每日主要地點標記用。
    """
    if not name or not name.strip():
        return None

    if is_mock_mode():
        return _mock_coord_for(name)

    api_key = os.getenv("GOOGLE_MAPS_API_KEY")
    if not api_key or api_key.startswith("your_"):
        return _mock_coord_for(name)

    try:
        resp = requests.get(
            "https://maps.googleapis.com/maps/api/geocode/json",
            params={"address": f"{name} {region_hint}".strip(), "key": api_key, "language": "zh-TW"},
            timeout=10,
        )
        data = resp.json()
        results = data.get("results") or []
        if results:
            loc = results[0]["geometry"]["location"]
            return (loc["lat"], loc["lng"])
        print(f"[maps] geocode 無結果 {name}: status={data.get('status')} → 退回近似座標")
    except Exception as e:
        print(f"[maps] geocode 失敗 {name}: {e} → 退回近似座標")
    return _mock_coord_for(name)


def _mock_coord_for(name: str) -> tuple[float, float]:
    low = (name or "").lower()
    for key, coord in MOCK_PLACE_COORDS.items():
        if key in low:
            return coord
    # 穩定 fallback：九州中心附近的小偏移
    h = abs(hash(name))
    return (32.8 + (h % 100) / 100.0, 130.4 + (h // 100 % 100) / 100.0)


def _mock_polyline(origin: str, destination: str) -> str:
    """用近似座標生成一條真實可解碼的 encoded polyline，供地圖示範。"""
    a = _mock_coord_for(origin)
    b = _mock_coord_for(destination)
    mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
    return polyline.encode([a, mid, b])


def estimate_drive_minutes_fallback(origin: str, destination: str) -> int:
    """當 Routes API 回傳 0 或失敗時的駕車分鐘估算。"""
    return _get_mock_drive_time_minutes(origin, destination)


# 大眾運輸候車/轉乘的固定加成（分鐘），避免估時過短。
_TRANSIT_FIXED_OVERHEAD_MINUTES = 10
_TRANSIT_DRIVE_FACTOR = 1.5


def estimate_transit_minutes_fallback(origin: str, destination: str) -> int:
    """大眾運輸時間估算：以駕車估時 × 係數 + 固定候車加成。

    用於 mock 模式，或真實 TRANSIT 無路線（例如同城近距離）時的退回估算，
    讓 public 行程也能顯示合理的「大眾運輸」分鐘數，而非沿用駕車數字。
    """
    drive = _get_mock_drive_time_minutes(origin, destination)
    return max(
        15,
        int(round(drive * _TRANSIT_DRIVE_FACTOR)) + _TRANSIT_FIXED_OVERHEAD_MINUTES,
    )


def _get_mock_drive_time_minutes(origin: str, destination: str) -> int:
    """Return mock driving time. Falls back to reasonable default."""
    key = (origin.strip(), destination.strip())
    reverse_key = (destination.strip(), origin.strip())

    if key in MOCK_DRIVE_TIMES_MINUTES:
        return MOCK_DRIVE_TIMES_MINUTES[key]
    if reverse_key in MOCK_DRIVE_TIMES_MINUTES:
        return MOCK_DRIVE_TIMES_MINUTES[reverse_key]

    # Default: random-ish but stable per pair
    return 55 + (hash(origin + destination) % 40)


# =============================================================================
# Real Google Routes API（computeRoutes）
# =============================================================================

_GOOGLE_TRAVEL_MODE = {
    "self_drive": "DRIVE",
    "mixed": "DRIVE",     # mixed 仍以自駕計幾何，靠較低駕車上限 + 多基地住宿控制
    "public": "TRANSIT",  # 純大眾運輸用 TRANSIT 計算
}

_ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"
_ROUTES_FIELD_MASK = (
    "routes.duration,routes.distanceMeters,routes.polyline.encodedPolyline"
)


def _routes_error_hint(status_code: int, error_body: dict | None = None) -> str:
    """把 HTTP / Google 錯誤轉成可操作的提示。"""
    if status_code == 404:
        return (
            "Routes API 回傳 404：請在 Google Cloud Console 啟用「Routes API」"
            "（routes.googleapis.com），並確認專案已開啟計費。"
        )
    if error_body:
        err = error_body.get("error") or {}
        msg = err.get("message") or ""
        if "API key not valid" in msg:
            return "GOOGLE_MAPS_API_KEY 無效或與 Routes API 專案不符。"
        if msg:
            return msg
    return f"HTTP {status_code}"


def _to_routes_waypoint(loc: str | tuple[float, float], region_hint: str = "") -> dict:
    """把地點轉成 Routes API waypoint；字串先 geocode 成 latLng 較穩定。"""
    if isinstance(loc, tuple):
        lat, lng = loc
        return {"location": {"latLng": {"latitude": lat, "longitude": lng}}}
    name = (loc or "").strip()
    if not name:
        return {"address": ""}
    coord = geocode_location(name, region_hint=region_hint)
    if coord:
        lat, lng = coord
        return {"location": {"latLng": {"latitude": lat, "longitude": lng}}}
    return {"address": name}


def _post_compute_routes(
    api_key: str,
    origin: dict,
    destination: dict,
    travel_mode: str,
    *,
    use_traffic: bool,
) -> tuple[int, dict | None, str | None]:
    """呼叫 computeRoutes；回傳 (status_code, json_body, error_hint)。"""
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": _ROUTES_FIELD_MASK,
    }
    payload: dict[str, Any] = {
        "origin": origin,
        "destination": destination,
        "travelMode": travel_mode,
        "polylineQuality": "HIGH_QUALITY",
    }
    if travel_mode == "DRIVE":
        payload["routingPreference"] = "TRAFFIC_AWARE" if use_traffic else "TRAFFIC_UNAWARE"

    try:
        resp = requests.post(
            _ROUTES_URL,
            json=payload,
            headers=headers,
            timeout=15,
            allow_redirects=False,
        )
    except requests.RequestException as e:
        return 0, None, str(e)

    if resp.status_code != 200:
        try:
            body = resp.json()
        except ValueError:
            body = None
        return resp.status_code, body, _routes_error_hint(resp.status_code, body)

    try:
        return 200, resp.json(), None
    except ValueError as e:
        return 200, None, f"invalid JSON: {e}"


def _route_from_response(data: dict) -> RouteResult | None:
    routes = data.get("routes") or []
    if not routes:
        return None
    route = routes[0]
    duration_sec = int(str(route.get("duration", "0s")).rstrip("s") or 0)
    distance_m = int(route.get("distanceMeters") or 0)
    encoded = (route.get("polyline") or {}).get("encodedPolyline")
    # 不足 1 分鐘的路段向上取整，避免 validate 顯示全 0
    duration_minutes = max(1, (duration_sec + 59) // 60) if duration_sec > 0 else 0
    return RouteResult(
        duration_seconds=duration_sec,
        distance_meters=distance_m,
        duration_minutes=duration_minutes,
        polyline=encoded,
    )


def _call_google_routes(
    api_key: str,
    origin: str | tuple[float, float],
    destination: str | tuple[float, float],
    *,
    use_traffic: bool,
    travel_mode: str,
    region_hint: str = "",
) -> tuple[RouteResult | None, str | None]:
    """Routes API 呼叫鏈：latLng → TRAFFIC_AWARE → TRAFFIC_UNAWARE → TRANSIT→DRIVE。"""
    origin_wp = _to_routes_waypoint(origin, region_hint=region_hint)
    dest_wp = _to_routes_waypoint(destination, region_hint=region_hint)

    g_mode = _GOOGLE_TRAVEL_MODE.get(travel_mode, "DRIVE")
    attempts: list[tuple[str, bool]] = [(g_mode, use_traffic)]
    if g_mode == "DRIVE" and use_traffic:
        attempts.append((g_mode, False))

    last_hint: str | None = None
    for mode, traffic in attempts:
        status, body, hint = _post_compute_routes(
            api_key, origin_wp, dest_wp, mode, use_traffic=traffic
        )
        if status == 200 and body:
            result = _route_from_response(body)
            if result:
                return result, None
            last_hint = "No routes returned"
            continue
        last_hint = hint or f"HTTP {status}"

    if g_mode == "TRANSIT":
        for traffic in (True, False):
            status, body, hint = _post_compute_routes(
                api_key, origin_wp, dest_wp, "DRIVE", use_traffic=traffic
            )
            if status == 200 and body:
                result = _route_from_response(body)
                if result:
                    print("[maps] TRANSIT 無路線，改以 DRIVE 估算駕車時間")
                    return result, None
            last_hint = hint or last_hint

    return None, last_hint


def check_routes_api_available() -> tuple[bool, str]:
    """啟動時快速探測 Routes API 是否可用（供 E2E / 除錯）。"""
    if is_mock_mode():
        return True, "MOCK_TOOLS=1"
    api_key = os.getenv("GOOGLE_MAPS_API_KEY")
    if not api_key or api_key.startswith("your_"):
        return False, "缺少 GOOGLE_MAPS_API_KEY"

    status, body, hint = _post_compute_routes(
        api_key,
        {"address": "Tokyo Station"},
        {"address": "Shibuya Station"},
        "DRIVE",
        use_traffic=False,
    )
    if status == 200 and body and (body.get("routes") or []):
        return True, "Routes API OK"
    return False, hint or f"Routes API probe failed (HTTP {status})"


def compute_driving_route(
    origin: str | tuple[float, float],
    destination: str | tuple[float, float],
    departure_time: str | None = None,
    use_traffic: bool = True,
    travel_mode: str = "self_drive",
) -> RouteResult:
    """
    用 Google Routes API（computeRoutes）計算真實路線。

    Args:
        origin / destination: "地點名稱/地址" 或 (lat, lng)
        departure_time: ISO 格式或 None（now）
        use_traffic: 是否使用 TRAFFIC_AWARE（僅 DRIVE 有效）
        travel_mode: self_drive / mixed → DRIVE；public → TRANSIT
    Returns:
        RouteResult(duration_minutes, distance, polyline, ...)
    """
    if is_mock_mode():
        origin_name = origin if isinstance(origin, str) else "Origin"
        dest_name = destination if isinstance(destination, str) else "Destination"
        # public → 以大眾運輸估時（不再沿用駕車分鐘）；其餘走駕車 mock。
        if travel_mode == "public":
            minutes = estimate_transit_minutes_fallback(origin_name, dest_name)
        else:
            minutes = _get_mock_drive_time_minutes(origin_name, dest_name)
        return RouteResult(
            duration_seconds=minutes * 60,
            distance_meters=int(minutes * 1000 * 0.8),
            duration_minutes=minutes,
            polyline=_mock_polyline(origin_name, dest_name),
        )

    api_key = os.getenv("GOOGLE_MAPS_API_KEY")
    if not api_key or api_key.startswith("your_"):
        print("[maps] No valid GOOGLE_MAPS_API_KEY → falling back to mock")
        o_name = origin if isinstance(origin, str) else "Origin"
        d_name = destination if isinstance(destination, str) else "Destination"
        minutes = _get_mock_drive_time_minutes(o_name, d_name)
        return RouteResult(minutes * 60, int(minutes * 1000 * 0.8), minutes,
                           polyline=_mock_polyline(o_name, d_name))

    if departure_time:
        # departureTime 僅在少數情境需要；目前仍以即時路況為主
        pass

    result, err = _call_google_routes(
        api_key,
        origin,
        destination,
        use_traffic=use_traffic,
        travel_mode=travel_mode,
    )
    if result:
        return result

    o_name = origin if isinstance(origin, str) else "Origin"
    d_name = destination if isinstance(destination, str) else "Destination"
    print(f"[maps] Google Routes API error: {err} → falling back to mock")
    minutes = _get_mock_drive_time_minutes(o_name, d_name)
    return RouteResult(
        minutes * 60,
        int(minutes * 1000 * 0.8),
        minutes,
        polyline=_mock_polyline(o_name, d_name),
        status="MOCK_FALLBACK",
        error_message=err,
    )


def compute_daily_total_drive_time(segments: list[tuple[str, str]]) -> int:
    """
    Given a list of (origin, destination) pairs for one day,
    return total driving time in minutes (sum of all segments).
    """
    total = 0
    for origin, dest in segments:
        result = compute_driving_route(origin, dest)
        total += result.duration_minutes
    return total


# =============================================================================
# High-level helper used by validator
# =============================================================================

def validate_daily_drives(
    daily_segments: list[list[tuple[str, str]]],
    max_minutes_per_day: int = 120
) -> tuple[bool, list[str], list[dict]]:
    """
    Validate a list of days, where each day is a list of (from, to) segments.

    Returns:
        (is_valid, violation_messages, detailed_violations)
    """
    violations = []
    details = []

    for day_idx, segments in enumerate(daily_segments, 1):
        total_min = compute_daily_total_drive_time(segments)
        details.append({
            "day": day_idx,
            "total_minutes": total_min,
            "segments": segments,
        })

        if total_min > max_minutes_per_day:
            msg = f"Day {day_idx}: 總駕車時間 {total_min} 分鐘 > {max_minutes_per_day} 分鐘上限"
            violations.append(msg)

    is_valid = len(violations) == 0
    return is_valid, violations, details


# =============================================================================
# LangChain Tool（供 ReAct agent 自主呼叫）
# =============================================================================

@tool
def maps_route_tool(origin: str, destination: str) -> dict:
    """計算兩地之間的自駕路線資訊。輸入起點與終點（城市或地址字串），
    回傳 {duration_minutes, distance_km, polyline}。用於評估每日駕車時間是否超過上限。"""
    result = compute_driving_route(origin, destination)
    return {
        "origin": origin,
        "destination": destination,
        "duration_minutes": result.duration_minutes,
        "distance_km": round(result.distance_meters / 1000, 1),
        "polyline": result.polyline,
    }
