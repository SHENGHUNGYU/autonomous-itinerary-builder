"""
機票搜尋工具（SerpAPI google_flights）

職責：
- 用 SerpAPI 的 `google_flights` 引擎查詢去回程機票選項與價格（結構化、穩定）。
- 不執行預訂，只回傳比價選項供 Booker / 行程預算使用。
- 支援 MOCK_TOOLS=1（離線確定性資料）；無 SERPAPI_KEY 或查詢失敗時退回 mock 估算。

需要環境變數 SERPAPI_KEY（與飯店比價共用）。價格幣別固定 TWD。
"""

from __future__ import annotations

import os

from dotenv import load_dotenv
from langchain_core.tools import tool
from pydantic import BaseModel, Field, field_validator

from src.tools.mocks import is_mock_mode

load_dotenv()


# 目的地（自由文字）→ 主要機場代碼的簡單對應；找不到時用 NRT（東京）保底。
_DEST_AIRPORT = {
    "九州": "FUK", "福岡": "FUK", "fukuoka": "FUK", "kyushu": "FUK",
    "熊本": "KMJ", "鹿兒島": "KOJ", "kagoshima": "KOJ",
    "大阪": "KIX", "osaka": "KIX", "京都": "KIX", "關西": "KIX",
    "東京": "NRT", "tokyo": "NRT",
    "沖繩": "OKA", "okinawa": "OKA",
    "北海道": "CTS", "札幌": "CTS", "sapporo": "CTS", "hokkaido": "CTS",
    "名古屋": "NGO", "nagoya": "NGO",
}

DEFAULT_ORIGIN_AIRPORT = os.getenv("DEFAULT_ORIGIN_AIRPORT", "TPE")  # 預設台北出發


# 日本主要機場（給 LLM 約束可選範圍，避免幻覺出不存在的代碼）。
_KNOWN_AIRPORTS = {
    "FUK": "福岡（九州北部）", "KMJ": "熊本", "KOJ": "鹿兒島（九州南部）",
    "OIT": "大分／別府・由布院", "NGS": "長崎", "KMI": "宮崎",
    "KIX": "大阪關西（京都・奈良・神戶）", "ITM": "大阪伊丹",
    "NRT": "東京成田", "HND": "東京羽田",
    "CTS": "札幌（北海道）", "OKA": "沖繩那霸", "NGO": "名古屋中部",
    "HIJ": "廣島", "SDJ": "仙台",
}


class _AirportResolution(BaseModel):
    """LLM 將自由文字目的地對應到機場（用 json_schema 由 vLLM 端強制約束）。"""
    iata_code: str = Field(..., description="3 碼大寫 IATA 機場代碼，只能從提供的清單中選")
    confidence: str = Field("low", description="high / medium / low")


def _dict_resolve_airport(destination: str) -> str:
    """字典子字串比對；找不到回退 NRT（東京）。LLM 不可用時的 fallback。"""
    d = (destination or "").lower()
    for key, code in _DEST_AIRPORT.items():
        if key.lower() in d:
            return code
    return "NRT"


def _llm_resolve_airport(destination: str) -> str | None:
    """用 LLM 把自由文字目的地（可能無明確城市名）對應到最合適的機場代碼。"""
    try:
        from src.services.llm import call_structured

        options = "\n".join(f"- {code}: {desc}" for code, desc in _KNOWN_AIRPORTS.items())
        res = call_structured(
            system=(
                "你是機票查詢助理。會收到使用者的旅遊目的地描述（可能是自由文字、"
                "景點名、地區名而非城市名），請判斷最適合『入境』的機場，"
                "只能從提供的機場清單中選一個。若描述橫跨多區，選最主要目的地。"
            ),
            prompt=f"目的地描述：{destination}\n\n可選機場：\n{options}",
            schema=_AirportResolution,
        )
        if res:
            code = (res.iata_code or "").strip().upper()
            if code in _KNOWN_AIRPORTS:
                return code
    except Exception as e:
        print(f"[flights] LLM 機場判讀失敗：{e}")
    return None


def resolve_airport(destination: str, use_real_api: bool | None = None) -> str:
    """把使用者輸入的目的地文字對應到機場代碼。

    LLM 優先（能理解『關西想去京都奈良』『湯布院泡溫泉』這類自由文字）；
    mock 模式 / LLM 不可達時，退回字典子字串比對（最終回退 NRT）。
    """
    d = (destination or "").strip()
    if not d:
        return "NRT"
    if use_real_api is None:
        use_real_api = not is_mock_mode()

    if use_real_api:
        code = _llm_resolve_airport(d)
        if code:
            return code
    return _dict_resolve_airport(d)


def _format_minutes(total_minutes: int | float | None) -> str:
    """把總分鐘數格式化成 'Xh Ym'（SerpAPI total_duration 為分鐘）。"""
    if not total_minutes:
        return "—"
    try:
        m = int(total_minutes)
    except (TypeError, ValueError):
        return "—"
    return f"{m // 60}h {m % 60}m"


def search_flights(
    destination: str,
    depart_date: str,
    return_date: str | None = None,
    adults: int = 2,
    origin_airport: str | None = None,
    arrival_airport: str | None = None,
    num_results: int = 4,
    use_real_api: bool | None = None,
) -> list[dict]:
    """
    查詢機票比價選項（不預訂）。

    參數：
        destination: 目的地文字（會對應到到達機場）
        depart_date: 去程日期 'YYYY-MM-DD'
        return_date: 回程日期（None = 單程）
        adults: 成人人數
        origin_airport: 出發機場代碼（預設 TPE / 環境變數）
    回傳：list[dict]，每筆含 airline / departure / arrival / stops / duration /
          price_twd / price_label / link。
    """
    if use_real_api is None:
        use_real_api = not is_mock_mode()

    origin = (origin_airport or DEFAULT_ORIGIN_AIRPORT).upper()
    dest = (arrival_airport or "").strip().upper() or resolve_airport(
        destination, use_real_api=use_real_api,
    )

    if not use_real_api:
        return _mock_flights(origin, dest, return_date is not None, adults, num_results)

    api_key = os.getenv("SERPAPI_KEY")
    if not api_key or api_key.startswith("your_"):
        print("[flights] 無有效 SERPAPI_KEY → 使用 mock")
        return _mock_flights(origin, dest, return_date is not None, adults, num_results)

    try:
        import serpapi

        params = {
            "engine": "google_flights",
            "departure_id": origin,
            "arrival_id": dest,
            "outbound_date": depart_date,
            "currency": "TWD",
            "hl": "zh-tw",
            "gl": "tw",
            "adults": adults,
            # type：1=來回、2=單程（來回必須帶 return_date）
            "type": 1 if return_date else 2,
        }
        if return_date:
            params["return_date"] = return_date

        client = serpapi.Client(api_key=api_key)
        results = client.search(params)
        data = results.as_dict() if hasattr(results, "as_dict") else results

        raw = (data.get("best_flights") or []) + (data.get("other_flights") or [])
        options: list[dict] = []
        for item in raw[:num_results]:
            legs = item.get("flights") or []
            if not legs:
                continue
            dep = legs[0].get("departure_airport", {}) or {}
            arr = legs[-1].get("arrival_airport", {}) or {}
            airlines = list(dict.fromkeys(l.get("airline", "") for l in legs if l.get("airline")))
            price = int(item.get("price") or 0)
            options.append({
                "airline": " / ".join(airlines) or "航空公司未提供",
                "departure": f"{dep.get('id', origin)} {dep.get('time', '')}".strip(),
                "arrival": f"{arr.get('id', dest)} {arr.get('time', '')}".strip(),
                "stops": len(item.get("layovers") or []),
                "duration": _format_minutes(item.get("total_duration")),
                "price_label": f"NT${price:,}" if price else "",
                "price_twd": price,
                "link": f"https://www.google.com/travel/flights?q=Flights%20{origin}%20to%20{dest}",
            })

        # 若價格全數無法取得，補上 mock 估算值以利預算計算
        if options and all(o["price_twd"] == 0 for o in options):
            est = _mock_flights(origin, dest, return_date is not None, adults, 1)[0]["price_twd"]
            for o in options:
                o["price_twd"] = est
                o["price_label"] = f"NT${o['price_twd']:,}"

        return options or _mock_flights(origin, dest, return_date is not None, adults, num_results)

    except Exception as e:
        print(f"[flights] SerpAPI google_flights 查詢失敗，使用 mock：{e}")
        return _mock_flights(origin, dest, return_date is not None, adults, num_results)


def _mock_flights(
    origin: str, dest: str, round_trip: bool, adults: int, num_results: int
) -> list[dict]:
    """離線確定性機票資料（價格為每人台幣，已乘人數）。"""
    base_per_person = 9000 if round_trip else 5200
    link = f"https://www.google.com/travel/flights?q=Flights%20{origin}%20to%20{dest}"
    options = [
        {"airline": "中華航空 China Airlines", "departure": f"{origin} 08:30", "arrival": f"{dest} 12:10",
         "stops": 0, "duration": "3h 40m", "price_twd": base_per_person * adults},
        {"airline": "長榮航空 EVA Air", "departure": f"{origin} 10:15", "arrival": f"{dest} 13:55",
         "stops": 0, "duration": "3h 40m", "price_twd": int(base_per_person * 1.15) * adults},
        {"airline": "星宇航空 STARLUX", "departure": f"{origin} 13:40", "arrival": f"{dest} 17:20",
         "stops": 0, "duration": "3h 40m", "price_twd": int(base_per_person * 1.25) * adults},
        {"airline": "樂桃航空 Peach（廉航）", "departure": f"{origin} 06:50", "arrival": f"{dest} 10:25",
         "stops": 0, "duration": "3h 35m", "price_twd": int(base_per_person * 0.7) * adults},
    ]
    for o in options:
        o["price_label"] = f"TWD {o['price_twd']:,}"
        o["link"] = link
    return options[:num_results]


def _coerce_date_str(v):
    """把 LLM 可能生成的各種日期形狀壓成 'YYYY-MM-DD' 字串。

    弱模型常把日期參數輸出成 dict（而非字串），例如：
      {'value': '2023-11-04'} / {'label': '2023-11-04'} / {'year':2023,'month':11,'day':4}
    這裡統一容錯轉換，避免工具參數驗證直接失敗。
    """
    if v is None or isinstance(v, str):
        return v
    if isinstance(v, dict):
        for k in ("value", "label", "date"):
            val = v.get(k)
            if isinstance(val, str) and val.strip():
                return val.strip()
        if all(k in v for k in ("year", "month", "day")):
            try:
                return f"{int(v['year']):04d}-{int(v['month']):02d}-{int(v['day']):02d}"
            except (TypeError, ValueError):
                pass
    return str(v)


class _FlightSearchArgs(BaseModel):
    """flight_search_tool 的參數 schema（含日期容錯）。"""
    destination: str
    depart_date: str
    return_date: str | None = None
    adults: int = 2

    @field_validator("depart_date", "return_date", mode="before")
    @classmethod
    def _coerce_dates(cls, v):
        return _coerce_date_str(v)


@tool(args_schema=_FlightSearchArgs)
def flight_search_tool(
    destination: str,
    depart_date: str,
    return_date: str | None = None,
    adults: int = 2,
) -> list[dict]:
    """查詢前往目的地的機票比價選項（航空公司、起降、轉機、總價TWD、連結）。不執行預訂。"""
    return search_flights(destination, depart_date, return_date=return_date, adults=adults)
