"""
Streamlit UI — 旅遊行程規劃
"""

import os
import sys
from datetime import date, timedelta
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import streamlit as st
from dotenv import load_dotenv

from src.agents.constraints import Violation, build_planner_feedback
from src.agents.feedback import parse_user_feedback
from src.core.graph import run_planner_refine_stream, run_planner_stream
from src.core.models import UserQuery

load_dotenv()

_DAY_COLORS = [
    "#e53935", "#1e88e5", "#43a047", "#8e24aa", "#fb8c00",
    "#6d4c41", "#00897b", "#3949ab", "#c0ca33", "#d81b60",
]

DESTINATION_PRESETS = {
    "九州": {
        "name": "九州",
        "default_days": 5,
        "default_budget": 40000,
        "recommended_mode": "self_drive",
        "hint": "福岡出發，熊本、由布院、阿蘇",
    },
    "大阪": {
        "name": "大阪",
        "default_days": 4,
        "default_budget": 35000,
        "recommended_mode": "mixed",
        "hint": "大阪、京都、神戶",
    },
    "東京": {
        "name": "東京",
        "default_days": 5,
        "default_budget": 45000,
        "recommended_mode": "mixed",
        "hint": "東京與近郊一日遊",
    },
    "沖繩": {
        "name": "沖繩",
        "default_days": 5,
        "default_budget": 50000,
        "recommended_mode": "self_drive",
        "hint": "海島自駕與美食",
    },
    "自訂": {
        "name": "自訂",
        "default_days": 5,
        "default_budget": 40000,
        "recommended_mode": "mixed",
        "hint": "",
    },
}

_STEP_LABELS = {
    "parse_input": "解析需求",
    "research": "蒐集景點與美食",
    "generate_draft": "規劃行程",
    "validate_route": "驗證路線",
    "booker": "比價與預算",
    "supervisor": "Supervisor",
    "refine_gate": "套用回饋",
    "format_output": "產生計劃書",
}


def render_itinerary_map(daily_markers: dict, height: int = 420):
    try:
        import folium
        from streamlit_folium import st_folium
    except Exception:
        return

    if not daily_markers:
        st.caption("尚無地圖資料。")
        return

    try:
        items = sorted(daily_markers.items(), key=lambda kv: int(kv[0]))
    except (ValueError, TypeError):
        items = list(daily_markers.items())

    pts = [(float(m["lat"]), float(m["lng"])) for _, m in items if m.get("lat") is not None]
    if not pts:
        return

    avg_lat = sum(p[0] for p in pts) / len(pts)
    avg_lon = sum(p[1] for p in pts) / len(pts)
    fmap = folium.Map(location=[avg_lat, avg_lon], zoom_start=12)

    for day_key, m in items:
        if m.get("lat") is None:
            continue
        d = int(day_key)
        color = _DAY_COLORS[(d - 1) % len(_DAY_COLORS)]
        name = m.get("name", "")
        loc = m.get("location", "")
        folium.Marker(
            [float(m["lat"]), float(m["lng"])],
            tooltip=f"Day {d}：{name}",
            popup=folium.Popup(f"<b>Day {d}</b><br>{name}<br>{loc}", max_width=240),
            icon=folium.DivIcon(html=(
                f'<div style="background:{color};color:#fff;border-radius:50%;'
                f'width:28px;height:28px;line-height:28px;text-align:center;'
                f'font-weight:bold;border:2px solid #fff;">{d}</div>'
            )),
        ).add_to(fmap)

    if len(pts) >= 2:
        folium.PolyLine(pts, color="#1976d2", weight=2, opacity=0.5, dash_array="6,6").add_to(fmap)
        fmap.fit_bounds([
            [min(p[0] for p in pts), min(p[1] for p in pts)],
            [max(p[0] for p in pts), max(p[1] for p in pts)],
        ])

    st_folium(fmap, height=height, use_container_width=True, returned_objects=[])


st.set_page_config(page_title="旅遊行程規劃", page_icon="✈️", layout="wide")

st.title("旅遊行程規劃")
st.caption("輸入條件，自動產出多日行程、預算與計劃書。")

with st.sidebar:
    st.header("行程條件")

    preset_choice = st.selectbox("目的地", options=list(DESTINATION_PRESETS.keys()), index=0)
    preset = DESTINATION_PRESETS[preset_choice]
    destination = preset["name"]
    if preset_choice == "自訂":
        destination = st.text_input("目的地名稱", value="北海道", placeholder="例如：北海道、四國")

    if preset["hint"]:
        st.caption(preset["hint"])

    days = st.number_input("天數", min_value=2, max_value=14, value=preset["default_days"], step=1)
    today = date.today()
    start_date = st.date_input("出發日期", value=today + timedelta(days=30))
    st.caption(f"回程 {start_date + timedelta(days=days - 1)}")

    budget = st.slider("總預算（TWD）", 15000, 150000, preset["default_budget"], 5000)

    _mode_options = ["self_drive", "mixed", "public"]
    travel_mode = st.radio(
        "交通方式",
        _mode_options,
        index=_mode_options.index(preset["recommended_mode"])
        if preset["recommended_mode"] in _mode_options else 1,
        format_func=lambda x: {
            "self_drive": "自駕",
            "mixed": "自駕 + 大眾運輸",
            "public": "大眾運輸",
        }[x],
    )

    preferences = st.multiselect(
        "偏好",
        ["美食", "溫泉", "自然景觀", "歷史文化", "購物", "海灘度假", "親子友善", "輕鬆慢活"],
        default=["美食", "自然景觀"],
    )

    st.divider()
    with st.expander("進階設定", expanded=False):
        mock_on = st.checkbox("離線模式（模擬資料）", value=os.getenv("MOCK_TOOLS", "1") == "1")
        os.environ["MOCK_TOOLS"] = "1" if mock_on else "0"

    run_button = st.button("開始規劃", type="primary", use_container_width=True)


_ACTION_LABELS = {
    "research": "蒐集資料",
    "plan": "重排行程",
    "validate": "驗證路線",
    "book": "比價預算",
    "finish": "完成輸出",
}

_DECISION_LABELS = {
    "retry_research": "補強研究",
    "retry_planner": "重排行程",
    "proceed_to_output": "繼續後續步驟",
}


def _violations_from_state(state) -> list[Violation]:
    raw = getattr(state, "violations", None) or []
    return [
        Violation(
            category=v.get("category", ""),
            severity=v.get("severity", "soft"),
            message=v.get("message", ""),
            data=v.get("data") or {},
        )
        for v in raw
    ]


def _step_label(step: dict) -> str:
    name = step.get("step", "")
    label = _STEP_LABELS.get(name, name)
    if name == "supervisor":
        action = step.get("next_action", "")
        action_label = _ACTION_LABELS.get(action, action)
        n_hard = step.get("hard_violations", 0)
        if n_hard:
            return f"{label} — {n_hard} 項硬違規 → {action_label}"
        if action:
            return f"{label} → {action_label}"
    status = step.get("status", "")
    if status and status not in ("ok", "PASS"):
        return f"{label} — {status}"
    return label


def _step_should_expand(step: dict) -> bool:
    if step.get("step") != "supervisor":
        return False
    return step.get("hard_violations", 0) > 0 or step.get("next_action") == "plan"


def _supervisor_planner_feedback(state) -> str:
    parsed = getattr(state, "parsed", None)
    violations = _violations_from_state(state)
    if not violations:
        return ""
    return build_planner_feedback(violations, parsed).strip()


def _render_step_summary(step: dict, state):
    name = step.get("step", "")
    if name == "research":
        st.caption(f"景點 {step.get('attractions', 0)} · 餐飲 {step.get('meals', 0)}")
    elif name == "validate_route":
        ddm = step.get("daily_drive_minutes", {})
        if ddm:
            st.caption(" · ".join(f"D{d} {m}分" for d, m in ddm.items()))
        feedback = (step.get("feedback") or "").strip()
        if feedback:
            st.markdown("**駕車回饋**")
            st.markdown(feedback)
    elif name == "booker":
        total = step.get("grand_total", 0)
        if total:
            st.caption(f"預估總花費 {total:,} TWD")
    elif name == "generate_draft":
        retry = "（重試）" if step.get("is_retry") else ""
        st.caption(f"{step.get('days_generated', 0)} 天行程{retry}")
    elif name == "supervisor":
        action = step.get("next_action", "")
        decision = step.get("decision", "")
        n_viol = step.get("violations", 0)
        n_hard = step.get("hard_violations", 0)
        retry = step.get("retry_count", 0)
        max_retries = getattr(state, "max_retries", 3)

        parts = []
        if action:
            parts.append(f"下一步：{_ACTION_LABELS.get(action, action)}")
        if decision:
            parts.append(f"決策：{_DECISION_LABELS.get(decision, decision)}")
        if n_viol or n_hard:
            parts.append(f"違規：{n_hard} hard · 共 {n_viol} 項")
        if retry or max_retries:
            parts.append(f"重試 {retry}/{max_retries}")
        if parts:
            st.caption(" · ".join(parts))

        if action == "plan":
            feedback = _supervisor_planner_feedback(state)
            if feedback:
                st.markdown("**給 Planner 的修正指令**")
                st.markdown(feedback)
            elif n_viol:
                msgs = [v.message for v in _violations_from_state(state) if v.message]
                if msgs:
                    st.markdown("**違規摘要**")
                    for msg in msgs[:5]:
                        st.markdown(f"- {msg}")

        reasoning = (step.get("reasoning") or "").strip()
        if reasoning:
            display = reasoning if len(reasoning) <= 320 else reasoning[:320] + "…"
            if "護欄調整為" in reasoning and action:
                st.caption(
                    f"實際執行：{_ACTION_LABELS.get(action, action)}"
                    "（LLM 建議與護欄不同，以實際動作為準）"
                )
            with st.expander("決策理由", expanded=False):
                st.markdown(display)


def render_workflow_log(log: list[dict], state):
    if not log:
        return
    st.subheader("規劃進度")
    cur_phase = None
    for entry in log:
        if entry["phase"] != cur_phase:
            cur_phase = entry["phase"]
            if cur_phase != "初次規劃":
                st.markdown(f"**{cur_phase}**")
        step = entry["step"]
        with st.status(
            _step_label(step),
            state="complete",
            expanded=_step_should_expand(step),
        ):
            _render_step_summary(step, state)


def stream_run(stream_gen, phase: str, reset: bool):
    if reset:
        st.session_state["workflow_log"] = []
    log = st.session_state.setdefault("workflow_log", [])

    live_area = st.container()
    processed = 0
    final_state = None
    with st.spinner("規劃中…"):
        for state in stream_gen:
            final_state = state
            trace = getattr(state, "trace", []) or []
            while processed < len(trace):
                step = trace[processed]
                log.append({"phase": phase, "step": step})
                with live_area:
                    with st.status(
                        _step_label(step),
                        state="complete",
                        expanded=_step_should_expand(step),
                    ):
                        _render_step_summary(step, state)
                processed += 1
    return final_state


def render_plan(state, destination: str, days: int, budget: int):
    status_ok = state.status == "success"
    if status_ok:
        st.success("行程已就緒")
    else:
        st.warning("行程已產出，部分項目仍需留意（見計劃書文末）")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("目的地", destination)
    c2.metric("天數", f"{days} 天")
    c3.metric("預算", f"{budget:,}")
    price = state.price_summary or {}
    c4.metric("預估花費", f"{price.get('estimated_total_twd', 0):,}" if price else "—")

    if state.daily_plans:
        threshold = state.parsed.max_daily_drive_minutes if state.parsed else None
        cols = st.columns(min(len(state.daily_plans), 7) or 1)
        for idx, dp in enumerate(state.daily_plans):
            over = threshold is not None and dp.drive_total_minutes > threshold
            cols[idx % len(cols)].metric(
                f"第 {dp.day} 天駕車",
                f"{dp.drive_total_minutes} 分",
                delta="超過上限" if over else None,
                delta_color="inverse" if over else "off",
            )

    if price:
        with st.expander("預算明細", expanded=False):
            within = price.get("within_budget", True)
            st.write(
                f"機票 {price.get('flight_total', 0):,} · "
                f"住宿 {price.get('accommodation_total', 0):,} · "
                f"租車 {price.get('car_rental_estimate', 0):,} · "
                f"餐飲 {price.get('meals_estimate', 0):,} · "
                f"門票 {price.get('attractions_tickets', 0):,}"
            )
            st.write("在預算內" if within else "超出預算")

    st.subheader("行程地圖")
    render_itinerary_map(getattr(state, "daily_markers", {}) or {})

    st.subheader("行程計劃書")
    if state.final_itinerary_md:
        st.markdown(state.final_itinerary_md)
    else:
        st.info("計劃書產生中或尚未完成。")


def render_feedback_section():
    st.divider()
    st.subheader("調整行程")
    st.caption("描述想改的地方，系統會直接依回饋重排行程（保留研究與比價結果）。")

    history = st.session_state.get("feedback_history", [])
    if history:
        with st.expander("先前的調整", expanded=False):
            for i, fb in enumerate(history, 1):
                st.markdown(f"{i}. {fb}")

    feedback_text = st.text_area(
        "你的回饋",
        key="feedback_input",
        placeholder="例如：美食不要重複 · 第 2 天開車太久，每天不超過 90 分鐘",
        height=72,
        label_visibility="collapsed",
    )
    if st.button("套用回饋", use_container_width=True):
        if not feedback_text.strip():
            st.warning("請先輸入回饋。")
            return
        last_state = st.session_state["last_state"]
        last_query: UserQuery = st.session_state["last_query"]
        try:
            adj = parse_user_feedback(feedback_text, last_query)
            updated_query = last_query.model_copy(update={
                "max_daily_drive_minutes": (
                    adj.max_daily_drive_minutes
                    if adj.max_daily_drive_minutes is not None
                    else last_query.max_daily_drive_minutes
                ),
                "user_feedback": feedback_text.strip(),
                "prefer_meal_variety": adj.require_meal_diversity or last_query.prefer_meal_variety,
                "region_stays": [
                    {"region": rs.region, "nights": rs.nights}
                    for rs in adj.region_stays
                ] or last_query.region_stays,
            })
            hints = []
            if adj.require_meal_diversity:
                hints.append("將避免重複餐廳")
            if adj.region_stays:
                hints.append(
                    "、".join(f"{rs.region} {rs.nights} 天" for rs in adj.region_stays)
                )
            if adj.max_daily_drive_minutes is not None:
                hints.append(f"駕車上限 {adj.max_daily_drive_minutes} 分鐘")
            if hints:
                st.info(" · ".join(hints))

            new_state = stream_run(
                run_planner_refine_stream(last_state, updated_query),
                phase="套用回饋",
                reset=False,
            )
            st.session_state["last_state"] = new_state
            st.session_state["last_query"] = updated_query
            st.session_state["feedback_history"] = history + [feedback_text.strip()]
            st.rerun()
        except Exception as e:
            st.error("調整失敗，請稍後再試。")
            st.exception(e)


if run_button:
    query_for_agent = (
        f"我想去 {destination} 玩 {days} 天，"
        f"預算約 {budget} 元台幣，"
        f"偏好 {', '.join(preferences)}，"
        f"主要交通方式為 {travel_mode}。"
    )
    try:
        structured_input = UserQuery(
            raw_input=query_for_agent,
            destination=destination,
            days=days,
            budget_twd=budget,
            start_date=start_date,
            preferences=preferences,
            travel_mode=travel_mode,
            adults=2,
        )
        st.session_state["display"] = {"destination": destination, "days": days, "budget": budget}
        st.session_state["feedback_history"] = []
        state = stream_run(run_planner_stream(structured_input), phase="初次規劃", reset=True)
        st.session_state["last_state"] = state
        st.session_state["last_query"] = structured_input
        st.rerun()
    except Exception as e:
        st.error("規劃失敗，請稍後再試。")
        st.exception(e)

if st.session_state.get("last_state") is not None:
    disp = st.session_state.get("display", {"destination": "—", "days": 5, "budget": 40000})
    render_workflow_log(st.session_state.get("workflow_log", []), st.session_state["last_state"])
    render_plan(
        st.session_state["last_state"],
        disp["destination"],
        disp["days"],
        disp["budget"],
    )
    render_feedback_section()
else:
    st.markdown(
        "在左側設定目的地、天數與預算，點擊 **開始規劃** 即可產出完整行程與計劃書。"
    )