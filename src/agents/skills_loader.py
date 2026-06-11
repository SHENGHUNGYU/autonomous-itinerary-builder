"""
Agent skills loader — LangChain progressive disclosure pattern.

Each worker agent owns a SKILL.md under ``src/agents/skills/<agent>/``.
Frontmatter carries lightweight metadata (name, description); the body holds
full operational instructions loaded on-demand when the agent runs.

Reference: https://docs.langchain.com/oss/python/langchain/multi-agent/skills
"""

from __future__ import annotations

import re
from pathlib import Path

from langchain_core.tools import tool

_SKILLS_ROOT = Path(__file__).parent / "skills"
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)

# Canonical agent skill names (directory names under skills/)
AGENT_SKILL_NAMES = (
    "researcher",
    "planner",
    "supervisor",
    "route_validator",
    "booker",
    "output_formatter",
    "feedback",
)


def _parse_skill_file(text: str) -> tuple[dict[str, str], str]:
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}, text.strip()
    meta: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        meta[key.strip()] = value.strip().strip('"').strip("'")
    return meta, text[match.end() :].strip()


def _skill_path(skill_name: str) -> Path:
    return _SKILLS_ROOT / skill_name / "SKILL.md"


def list_skills() -> list[dict[str, str]]:
    """Lightweight skill catalog for discovery (metadata only)."""
    catalog: list[dict[str, str]] = []
    for name in AGENT_SKILL_NAMES:
        path = _skill_path(name)
        if not path.exists():
            continue
        meta, _ = _parse_skill_file(path.read_text(encoding="utf-8"))
        catalog.append({
            "name": meta.get("name", name),
            "description": meta.get("description", ""),
            "agent": name,
        })
    return catalog


def load_skill(skill_name: str) -> str:
    """Load full skill body for injection into an agent's system prompt."""
    path = _skill_path(skill_name)
    if not path.exists():
        available = ", ".join(s["agent"] for s in list_skills())
        return f"Skill '{skill_name}' not found. Available skills: {available}"
    _, body = _parse_skill_file(path.read_text(encoding="utf-8"))
    return body


def skill_description(skill_name: str) -> str:
    path = _skill_path(skill_name)
    if not path.exists():
        return ""
    meta, _ = _parse_skill_file(path.read_text(encoding="utf-8"))
    return meta.get("description", "")


def compose_system_prompt(
    agent_name: str,
    base_prompt: str,
    *,
    include_skill: bool = True,
) -> str:
    """Merge a base system prompt with the agent's SKILL.md content."""
    if not include_skill:
        return base_prompt.strip()
    content = load_skill(agent_name)
    if content.startswith("Skill '"):
        return base_prompt.strip()
    return f"{base_prompt.strip()}\n\n---\n\n{content}"


def skills_prompt_addendum() -> str:
    """Short catalog block (descriptions only) for ReAct agents that may call load_agent_skill."""
    lines = ["## Available agent skills", ""]
    for item in list_skills():
        lines.append(f"- **{item['agent']}**: {item['description']}")
    lines.append("")
    lines.append(
        "Use load_agent_skill(skill_name) when you need detailed policies for a worker role."
    )
    return "\n".join(lines)


@tool
def load_agent_skill(skill_name: str) -> str:
    """Load specialized travel-planning skill instructions into context.

    Available skills:
    - researcher: web grounding, Serper + Firecrawl workflow
    - planner: multi-day itinerary structured output
    - supervisor: hub-and-spoke routing and guardrails
    - route_validator: drive/transit time validation
    - booker: flight/hotel/car price comparison
    - output_formatter: final markdown itinerary
    - feedback: parse user refinement requests

    Args:
        skill_name: Agent skill identifier (e.g. "planner", "researcher")
    """
    body = load_skill(skill_name)
    if body.startswith("Skill '"):
        return body
    return f"Loaded skill: {skill_name}\n\n{body}"