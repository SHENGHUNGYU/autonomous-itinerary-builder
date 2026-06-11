"""
Mock tools and high-quality fixture loader for early development.

When MOCK_TOOLS=1 (set in .env), all external calls return deterministic,
realistic data from kyushu_5day_realistic.json that is GUARANTEED to satisfy:
- Daily driving ≤ 120 minutes
- Start/End in Fukuoka
- 5 full days with meals + attractions + hotel
- Reasonable budget
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

FIXTURE_PATH = Path(__file__).parent.parent.parent / "tests" / "fixtures" / "kyushu_5day_realistic.json"


def is_mock_mode() -> bool:
    """Return True if MOCK_TOOLS=1 (or true/yes) is set."""
    val = os.getenv("MOCK_TOOLS", "1").lower()
    return val in ("1", "true", "yes", "on")


def load_kyushu_fixture() -> dict[str, Any]:
    """Load the canonical high-quality 5-day Kyushu self-drive fixture."""
    if not FIXTURE_PATH.exists():
        raise FileNotFoundError(
            f"Fixture not found at {FIXTURE_PATH}. "
            "Run the project setup steps in the development plan first."
        )
    with open(FIXTURE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def get_mock_plan() -> dict[str, Any]:
    """
    Return the full realistic 5-day plan as if the entire agent pipeline
    (Research → Planner → Booker → Validator) had succeeded.
    Used by early-phase graph mocks and integration tests.
    """
    return load_kyushu_fixture()


def validate_fixture_constraints(fixture: dict[str, Any]) -> list[str]:
    """
    Basic self-check that the fixture itself satisfies the hard constraints.
    Returns list of violation messages (empty = perfect).
    This will later be moved to src/tools/validators.py and used in the real graph.
    """
    violations: list[str] = []
    daily = fixture.get("daily_plans", [])
    meta = fixture.get("trip_meta", {})

    if len(daily) != 5:
        violations.append(f"Expected 5 days, got {len(daily)}")

    # Start/End Fukuoka
    if meta.get("origin_city") != "Fukuoka" or meta.get("destination_city") != "Fukuoka":
        violations.append("Trip must start and end in Fukuoka")

    for d in daily:
        drive = d.get("drive_total_minutes", 999)
        if drive > 120:
            violations.append(f"Day {d['day']} drive {drive} min > 120 min limit")

    # At least one meal + one attraction per day
    for d in daily:
        if not d.get("meals"):
            violations.append(f"Day {d['day']} missing meals")
        if not d.get("attractions"):
            violations.append(f"Day {d['day']} missing attractions")

    # Blog sources
    sources = fixture.get("blog_sources", [])
    if len(sources) < 3:
        violations.append(f"Need at least 3 blog sources, got {len(sources)}")

    return violations


if __name__ == "__main__":
    fx = load_kyushu_fixture()
    errs = validate_fixture_constraints(fx)
    if errs:
        print("❌ Fixture violations:")
        for e in errs:
            print("  -", e)
    else:
        print("✅ Fixture satisfies all hard constraints (daily drive ≤2h, 5 days, Fukuoka start/end, meals+attractions, 3+ sources)")
        print(f"   Trip: {fx['trip_meta']['start_date']} ~ {fx['trip_meta']['end_date']}")
        print(f"   Grand total est: {fx['price_summary']['grand_total_twd']} TWD")
