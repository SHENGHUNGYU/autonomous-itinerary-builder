"""Mock demo 回饋意圖辨識。"""

from __future__ import annotations

import re

_DAY4_TRANSIT_RE = re.compile(
    r"(?:"
    r"(?:第?\s*4\s*天|D\s*4|Day\s*4|第四天)"
    r".{0,24}(?:大眾運輸|公共交通|地鐵|地铁|電車|火車|改搭|改乘|搭乘)"
    r"|"
    r"(?:大眾運輸|公共交通|地鐵|地铁|電車)"
    r".{0,24}(?:第?\s*4\s*天|D\s*4|Day\s*4|第四天)"
    r")",
    re.IGNORECASE,
)


def wants_day4_transit(feedback: str) -> bool:
    """使用者回饋是否要求第 4 天改搭大眾運輸。"""
    return bool(_DAY4_TRANSIT_RE.search(feedback or ""))