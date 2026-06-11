"""
Researcher Agent - 研究與 grounding

職責：
- 蒐集目的地的最新景點、美食與部落格 grounding 來源
- 搜尋流程：serper_search_tool 撈部落格/社群 URL → firecrawl_extract_tool 解析
- 輸出結構化的 ResearchBundle 供 Planner 使用

實作：
- 在真實模式（有 vLLM）下，以 LangGraph create_react_agent 讓 LLM 自主編排多次工具呼叫；
- 結構化資料則由底層工具函式可靠組裝（避免 LLM 自由文字解析不穩）。
- Mock / 無 LLM 時，直接走確定性工具組裝，保證 demo 穩定。
"""

from __future__ import annotations

import os

from src.agents.skills_loader import compose_system_prompt, load_agent_skill
from src.core.models import ResearchBundle
from src.services.llm import get_chat_llm
from src.tools.mocks import is_mock_mode
from src.tools.web_research import (
    firecrawl_extract_tool,
    run_researcher,
    serper_search_tool,
)


def run_researcher_crew(
    query: str,
    destination: str = "九州",
    llm=None,
    days: int | None = None,
) -> ResearchBundle:
    """
    執行研究階段，回傳結構化 ResearchBundle。

    真實模式下會啟動會用多工具的 ReAct agent（可在 LangSmith 觀察思考軌跡），
    結構化結果由工具函式組裝以確保可靠。
    """
    bundle = run_researcher(query, destination=destination, days=days)

    if is_mock_mode():
        return bundle

    # 結構化結果已由 run_researcher 可靠組裝（含不足補足）。下方 ReAct agent 僅供
    # LangSmith 觀察「多工具自主編排」軌跡，其輸出不影響 bundle；預設關閉以免拖慢/逾時，
    # 需要展示時設 RESEARCH_REACT_OBSERVE=1 開啟。
    if os.getenv("RESEARCH_REACT_OBSERVE", "0") != "1":
        return bundle

    # 真實模式：讓 ReAct agent 自主編排工具呼叫（多工具工作流）
    try:
        from langgraph.prebuilt import create_react_agent

        system = compose_system_prompt(
            "researcher",
            "你是專業旅遊研究員。請務必使用提供的工具：先用 serper 關鍵字搜尋撈出"
            "部落格/社群文章作為 grounding，再用 firecrawl 解析這些頁面擷取景點與美食，最後總結重點。",
        )
        agent = create_react_agent(
            llm or get_chat_llm(temperature=0.2),
            [serper_search_tool, firecrawl_extract_tool, load_agent_skill],
        )
        agent.invoke(
            {
                "messages": [
                    ("system", system),
                    ("user", f"請研究目的地「{destination}」。使用者需求：{query}"),
                ]
            },
            config={"recursion_limit": 8},
        )
    except Exception as e:
        print(f"[Researcher] ReAct agent 執行備註（不影響結果）：{e}")

    return bundle
