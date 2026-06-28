#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


CATEGORY_PATTERNS: dict[str, list[str]] = {
    "direction_mismatch": [
        r"不是这个意思",
        r"我指的是",
        r"这里指的其实就是",
        r"我们先对齐",
        r"先对齐一下",
        r"明白我意思吗",
        r"你理解错",
        r"你为什么会执行",
        r"真正要解决的目标",
        r"根目标",
        r"风险边界",
        r"定义清楚",
    ],
    "verbosity_mismatch": [
        r"简洁一点",
        r"高密度短答案",
        r"只在必要时展开",
        r"不要输出长篇大论",
        r"太长了",
        r"短一点",
        r"别展开太多",
        r"简洁总结",
        r"简短一点",
    ],
    "style_mismatch": [
        r"不要这么\s*ai",
        r"太\s*ai",
        r"太正式",
        r"太死板",
        r"这个介绍有点太土",
        r"风格不对",
    ],
    "format_mismatch": [
        r"代码块",
        r"markdown",
        r"方便复制",
        r"竖着的",
        r"列向量",
        r"control o to expand",
        r"显示其实有点奇怪",
        r"格式不对",
        r"图片有点太大",
    ],
    "late_constraint": [
        r"先不要",
        r"先别",
        r"先讨论",
        r"先判断",
        r"不要直接执行",
        r"最小充分",
        r"控制 token",
        r"避免.?大范围 grep",
        r"无目标搜索",
        r"过量上下文",
        r"低价值步骤",
        r"不要做任何改动",
    ],
}

PRIMARY_ORDER = [
    "direction_mismatch",
    "verbosity_mismatch",
    "style_mismatch",
    "format_mismatch",
    "late_constraint",
]

CATEGORY_LABELS = {
    "direction_mismatch": "方向/对象理解偏了",
    "verbosity_mismatch": "长度/密度不对",
    "style_mismatch": "表达风格不对",
    "format_mismatch": "输出形式/展示方式不对",
    "late_constraint": "关键限制说晚了",
}

CATEGORY_SUGGESTIONS = {
    "direction_mismatch": "首条 prompt 先写清对象、边界、不要处理什么，再开始执行。",
    "verbosity_mismatch": "把长度、密度、是否展开写成固定前置要求。",
    "style_mismatch": "提前说明语气、正式度、是否允许 AI 味。",
    "format_mismatch": "提前说明输出形式：代码块/列表/可复制文本/不要表格。",
    "late_constraint": "把“先讨论还是先执行、是否允许大搜索、token 预算”放到首条 prompt。",
}

CATEGORY_MISSING_CONSTRAINT = {
    "direction_mismatch": "对象/边界",
    "verbosity_mismatch": "长度/信息密度",
    "style_mismatch": "风格/语气",
    "format_mismatch": "输出格式/展示方式",
    "late_constraint": "执行顺序/操作边界/预算限制",
}


@dataclass
class Msg:
    session_file: str
    role: str
    index: int
    timestamp: str
    text: str
    preview: str
    tool_calls: int = 0


@dataclass
class Event:
    session_file: str
    trigger_turn: int
    event_type: str
    secondary_types: list[str]
    original_user_turn: int
    original_user_preview: str
    assistant_preview: str
    assistant_tool_calls: int
    evidence: str
    missing_constraint: str
    impact: str
    avoidable: bool
    confidence: float
    suggestion: str


@dataclass
class PatternSummary:
    pattern: str
    count: int
    sessions: int
    confidence: str
    evidence_examples: list[str]
    user_suggestion: str
    agent_suggestion: str



def session_dir_name(project_cwd: str) -> str:
    stripped = project_cwd.strip("/")
    return f"--{stripped.replace('/', '-')}--" if stripped else "----"



def sanitize_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", "", text)



def one_line(text: str, limit: int = 160) -> str:
    text = re.sub(r"\s+", " ", sanitize_text(text)).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"



def is_resume_artifact(text: str) -> bool:
    sample = sanitize_text(text).strip()
    return sample.startswith("**Resume Session") or sample.startswith("Resume Session (Current Folder)")



def message_text(message: dict[str, Any]) -> str:
    return "\n".join(part.get("text", "") for part in message.get("content", []) if part.get("type") == "text")



def text_matches_category(text: str, category: str) -> bool:
    return any(re.search(pattern, text, re.I) for pattern in CATEGORY_PATTERNS[category])



def matched_categories(text: str) -> list[str]:
    return [category for category in PRIMARY_ORDER if text_matches_category(text, category)]



def primary_category(categories: list[str]) -> str:
    for category in PRIMARY_ORDER:
        if category in categories:
            return category
    return "direction_mismatch"



def impact_label(assistant: Msg, has_followup_assistant: bool) -> str:
    if assistant.tool_calls > 0:
        return "assistant_already_executed"
    if has_followup_assistant:
        return "answer_revised"
    return "correction_without_revision"



def confidence_score(categories: list[str], assistant: Msg) -> float:
    score = 0.72 + min(0.18, 0.06 * max(0, len(categories) - 1))
    if assistant.tool_calls > 0:
        score += 0.05
    return round(min(0.95, score), 2)



def classify_missing_constraint(category: str, evidence: str) -> str:
    lines = [line.strip() for line in sanitize_text(evidence).splitlines() if line.strip()]
    if lines:
        first = lines[0]
        if len(first) <= 40:
            return first
    return CATEGORY_MISSING_CONSTRAINT[category]



def parse_session(path: Path) -> list[Msg]:
    messages: list[Msg] = []
    user_turn = 0
    assistant_turn = 0
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        entry = json.loads(raw)
        if entry.get("type") != "message":
            continue
        message = entry.get("message", {})
        role = message.get("role")
        if role not in {"user", "assistant"}:
            continue
        text = message_text(message)
        if role == "user" and is_resume_artifact(text):
            continue
        if role == "user":
            user_turn += 1
            idx = user_turn
        else:
            assistant_turn += 1
            idx = assistant_turn
        tool_calls = 0
        if role == "assistant":
            tool_calls = sum(1 for part in message.get("content", []) if part.get("type") == "toolCall")
        messages.append(
            Msg(
                session_file=path.name,
                role=role,
                index=idx,
                timestamp=entry.get("timestamp", ""),
                text=text,
                preview=one_line(text),
                tool_calls=tool_calls,
            )
        )
    return messages



def extract_events(messages: list[Msg]) -> list[Event]:
    events: list[Event] = []
    for i, current in enumerate(messages):
        if current.role != "user":
            continue
        if i == 0 or messages[i - 1].role != "assistant":
            continue
        categories = matched_categories(current.text)
        if not categories:
            continue
        assistant = messages[i - 1]
        original_user = None
        for j in range(i - 2, -1, -1):
            if messages[j].role == "user":
                original_user = messages[j]
                break
        if original_user is None:
            continue
        next_assistant = None
        for j in range(i + 1, len(messages)):
            if messages[j].role == "assistant":
                next_assistant = messages[j]
                break
        primary = primary_category(categories)
        avoidable = not text_matches_category(original_user.text, primary)
        events.append(
            Event(
                session_file=current.session_file,
                trigger_turn=current.index,
                event_type=primary,
                secondary_types=[item for item in categories if item != primary],
                original_user_turn=original_user.index,
                original_user_preview=original_user.preview,
                assistant_preview=assistant.preview,
                assistant_tool_calls=assistant.tool_calls,
                evidence=current.preview,
                missing_constraint=classify_missing_constraint(primary, current.text),
                impact=impact_label(assistant, next_assistant is not None),
                avoidable=avoidable,
                confidence=confidence_score(categories, assistant),
                suggestion=CATEGORY_SUGGESTIONS[primary],
            )
        )
    return events



def confidence_bucket(events: list[Event]) -> str:
    avg = sum(event.confidence for event in events) / len(events)
    if avg >= 0.86:
        return "high"
    if avg >= 0.76:
        return "medium"
    return "low"



def build_profile(events: list[Event]) -> dict[str, Any]:
    by_type: dict[str, list[Event]] = defaultdict(list)
    for event in events:
        by_type[event.event_type].append(event)

    stable_patterns: list[PatternSummary] = []
    for event_type, items in sorted(by_type.items(), key=lambda kv: len(kv[1]), reverse=True):
        sessions = len({item.session_file for item in items})
        stable_patterns.append(
            PatternSummary(
                pattern=event_type,
                count=len(items),
                sessions=sessions,
                confidence=confidence_bucket(items),
                evidence_examples=[f"{item.session_file}#turn{item.trigger_turn} {item.evidence}" for item in items[:4]],
                user_suggestion=CATEGORY_SUGGESTIONS[event_type],
                agent_suggestion=f"默认先防 {CATEGORY_LABELS[event_type]}，必要时先确认再执行。",
            )
        )

    user_profiles = []
    if len(by_type.get("late_constraint", [])) >= 2:
        user_profiles.append(
            {
                "name": "constraints_arrive_late",
                "confidence": "medium" if len(by_type["late_constraint"]) < 4 else "high",
                "support": [f"{item.session_file}#turn{item.trigger_turn}" for item in by_type["late_constraint"][:4]],
                "counter_examples": [],
                "user_suggestion": CATEGORY_SUGGESTIONS["late_constraint"],
                "agent_suggestion": "开头先问清是否允许直接执行、是否允许大搜索、是否有 token 预算。",
            }
        )
    if len(by_type.get("direction_mismatch", [])) >= 2:
        user_profiles.append(
            {
                "name": "semantic_alignment_needs_repair",
                "confidence": "medium" if len(by_type["direction_mismatch"]) < 4 else "high",
                "support": [f"{item.session_file}#turn{item.trigger_turn}" for item in by_type["direction_mismatch"][:4]],
                "counter_examples": [],
                "user_suggestion": CATEGORY_SUGGESTIONS["direction_mismatch"],
                "agent_suggestion": "在开始前先复述对象、范围、目标，少脑补。",
            }
        )
    if len(by_type.get("verbosity_mismatch", [])) >= 2:
        user_profiles.append(
            {
                "name": "prefers_dense_brief_answers",
                "confidence": "medium" if len(by_type["verbosity_mismatch"]) < 4 else "high",
                "support": [f"{item.session_file}#turn{item.trigger_turn}" for item in by_type["verbosity_mismatch"][:4]],
                "counter_examples": [],
                "user_suggestion": CATEGORY_SUGGESTIONS["verbosity_mismatch"],
                "agent_suggestion": "默认先短结论，再按需展开。",
            }
        )

    return {
        "event_count": len(events),
        "event_types": dict(Counter(event.event_type for event in events)),
        "stable_patterns": [asdict(item) for item in stable_patterns],
        "profiles": user_profiles,
    }



def render_report(project_cwd: str, total_sessions: int, events: list[Event], profile: dict[str, Any]) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    lines = [
        "# Blindspot v2 MVP Report",
        "",
        f"Generated: {now}",
        "",
        "## 范围",
        "",
        f"- Project: `{project_cwd}`",
        f"- Sessions: {total_sessions}",
        f"- Events: {len(events)}",
        "",
        "## 事件类型统计",
        "",
    ]
    for event_type, count in profile["event_types"].items():
        lines.append(f"- `{event_type}` × {count} — {CATEGORY_LABELS[event_type]}")
    lines.append("")
    lines.append("## 代表性事件")
    lines.append("")
    for idx, event in enumerate(events[:12], 1):
        lines.append(f"### {idx}. {event.event_type} ({event.session_file}#turn{event.trigger_turn})")
        lines.append(f"- 原始请求：{event.original_user_preview}")
        lines.append(f"- 被纠偏的 assistant：{event.assistant_preview}")
        lines.append(f"- 用户纠偏：{event.evidence}")
        lines.append(f"- 缺失约束：{event.missing_constraint}")
        lines.append(f"- 影响：{event.impact}")
        lines.append(f"- 可避免：{event.avoidable}")
        lines.append(f"- 建议：{event.suggestion}")
        lines.append("")
    lines.append("## 稳定模式")
    lines.append("")
    for item in profile["stable_patterns"]:
        lines.append(f"### {item['pattern']} ({item['count']})")
        lines.append(f"- 说明：{CATEGORY_LABELS[item['pattern']]}")
        lines.append(f"- Sessions: {item['sessions']}")
        lines.append(f"- Confidence: {item['confidence']}")
        lines.append(f"- 用户建议：{item['user_suggestion']}")
        lines.append(f"- Agent 建议：{item['agent_suggestion']}")
        for evidence in item["evidence_examples"]:
            lines.append(f"- {evidence}")
        lines.append("")
    lines.append("## 判断")
    lines.append("")
    if events:
        lines.append("- 这个 MVP 已经能抓到多轮返工链，不再只是词频/标签。")
        lines.append("- 最有价值的信号来自：用户纠偏语句 + 前一轮 assistant 行为。")
        lines.append("- 但事件分类仍然是规则驱动，复杂场景还会漏判或错判。")
    else:
        lines.append("- 当前样本里没有抓到足够清晰的纠偏事件，MVP 还不能证明价值。")
    lines.append("")
    return "\n".join(lines).rstrip() + "\n"



def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Blindspot v2 MVP: event-based collaboration diagnosis.")
    parser.add_argument("--project-cwd", required=True)
    parser.add_argument("--sessions-root", default=str(Path.home() / ".pi/agent/sessions"))
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)

    session_dir = Path(args.sessions_root).expanduser().resolve() / session_dir_name(str(Path(args.project_cwd).resolve()))
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    all_messages: list[Msg] = []
    session_files = sorted(session_dir.glob("*.jsonl"))
    for path in session_files:
        all_messages.extend(parse_session(path))

    by_session: dict[str, list[Msg]] = defaultdict(list)
    for msg in all_messages:
        by_session[msg.session_file].append(msg)

    events: list[Event] = []
    for session_file in sorted(by_session):
        events.extend(extract_events(by_session[session_file]))

    profile = build_profile(events)
    (out_dir / "events.json").write_text(json.dumps([asdict(item) for item in events], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out_dir / "profile.json").write_text(json.dumps(profile, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out_dir / "report.md").write_text(render_report(args.project_cwd, len(session_files), events, profile), encoding="utf-8")
    print(f"wrote {out_dir / 'report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
