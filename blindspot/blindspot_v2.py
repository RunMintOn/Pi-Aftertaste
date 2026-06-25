#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from runtime_support import (
    copy_public_files,
    finalize_route_run,
    load_json,
    mirror_tree,
    prepare_route_run,
    remove_deleted_session_caches,
    route_session_cache_path,
    write_json,
)


PUBLIC_OUTPUT_FILES = [
    "events.json",
    "profile.json",
    "report.md",
]


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


def extract_blindspot_v2_session(path: Path) -> dict[str, Any]:
    events = extract_events(parse_session(path))
    return {
        "session_file": path.name,
        "event_count": len(events),
        "events": [asdict(item) for item in events],
    }


def confidence_bucket(events: list[Event]) -> str:
    avg = sum(event.confidence for event in events) / len(events)
    if avg >= 0.86:
        return "high"
    if avg >= 0.76:
        return "medium"
    return "low"


def sorted_counter_map(counter: Counter[str]) -> dict[str, int]:
    items = sorted(counter.items(), key=lambda item: (-item[1], item[0]))
    return {key: value for key, value in items}


def load_events_from_session_caches(session_files: list[str], caches: list[dict[str, Any]]) -> list[Event]:
    cache_by_file = {item.get("session_file", ""): item for item in caches}
    events: list[Event] = []
    for session_file in session_files:
        cached = cache_by_file.get(session_file)
        if not cached:
            continue
        events.extend(Event(**item) for item in cached.get("events", []))
    return sorted(events, key=lambda item: (item.session_file, item.trigger_turn, item.original_user_turn, item.event_type))


def build_profile(events: list[Event]) -> dict[str, Any]:
    by_type: dict[str, list[Event]] = defaultdict(list)
    for event in events:
        by_type[event.event_type].append(event)

    stable_patterns: list[PatternSummary] = []
    for event_type, items in sorted(by_type.items(), key=lambda kv: (-len(kv[1]), kv[0])):
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

    missing_constraints = sorted_counter_map(Counter(event.missing_constraint for event in events))
    impacts = sorted_counter_map(Counter(event.impact for event in events))
    event_types = sorted_counter_map(Counter(event.event_type for event in events))

    return {
        "event_count": len(events),
        "event_types": event_types,
        "top_missing_constraints": missing_constraints,
        "impact_counts": impacts,
        "stable_patterns": [asdict(item) for item in stable_patterns],
        "profiles": user_profiles,
    }


def render_report(project_cwd: str, total_sessions: int, events: list[Event], profile: dict[str, Any]) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    lines = [
        "# Blindspot v2 Report",
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

    if profile["event_types"]:
        for event_type, count in profile["event_types"].items():
            lines.append(f"- `{event_type}` × {count} — {CATEGORY_LABELS.get(event_type, event_type)}")
    else:
        lines.append("- 无事件")
    lines.extend(["", "## Top 返工原因", ""])

    if profile["top_missing_constraints"]:
        for reason, count in list(profile["top_missing_constraints"].items())[:8]:
            lines.append(f"- `{reason}` × {count}")
    else:
        lines.append("- 无")

    lines.extend(["", "## 代表性事件", ""])
    representative = sorted(events, key=lambda item: (-item.confidence, -item.assistant_tool_calls, item.session_file, item.trigger_turn))[:12]
    if representative:
        for idx, event in enumerate(representative, 1):
            lines.append(f"### {idx}. {event.event_type} ({event.session_file}#turn{event.trigger_turn})")
            lines.append(f"- 原始请求：{event.original_user_preview}")
            lines.append(f"- 被纠偏 assistant：{event.assistant_preview}")
            lines.append(f"- evidence：{event.evidence}")
            lines.append(f"- missing_constraint：{event.missing_constraint}")
            lines.append(f"- impact：{event.impact}")
            lines.append(f"- suggestion：{event.suggestion}")
            lines.append(f"- confidence：{event.confidence}")
            lines.append(f"- assistant_tool_calls：{event.assistant_tool_calls}")
            lines.append(f"- avoidable：{event.avoidable}")
            if event.secondary_types:
                lines.append(f"- secondary_types：{', '.join(event.secondary_types)}")
            lines.append("")
    else:
        lines.append("- 当前没有抽到足够清晰的返工事件。")
        lines.append("")

    lines.append("## Profile 总结")
    lines.append("")
    if profile["profiles"]:
        for item in profile["profiles"]:
            lines.append(f"### {item['name']} ({item['confidence']})")
            if item.get("support"):
                lines.append(f"- support：{', '.join(item['support'])}")
            lines.append(f"- 用户建议：{item['user_suggestion']}")
            lines.append(f"- Agent 建议：{item['agent_suggestion']}")
            lines.append("")
    else:
        lines.append("- 当前样本还不足以形成稳定 profile。")
        lines.append("")

    lines.append("## 判断")
    lines.append("")
    if events:
        lines.append("- 当前 route 已能稳定输出按 session 增量复用的返工事件。")
        lines.append("- 价值主要来自：用户纠偏语句 + 前一轮 assistant 行为的配对。")
        lines.append("- 但分类仍是规则驱动，复杂返工原因会漏判或合并过粗。")
    else:
        lines.append("- 当前样本未形成足够明显的返工链。")
    lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_route_outputs(out_dir: Path, project_cwd: str, total_sessions: int, session_caches: list[dict[str, Any]], events: list[Event], profile: dict[str, Any]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    sessions_dir = out_dir / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    for cache in session_caches:
        session_file = cache.get("session_file", "session")
        cache_name = f"{Path(session_file).stem}.json"
        write_json(sessions_dir / cache_name, cache)

    write_json(out_dir / "events.json", [asdict(item) for item in events])
    write_json(out_dir / "profile.json", profile)
    (out_dir / "report.md").write_text(render_report(project_cwd, total_sessions, events, profile), encoding="utf-8")


def copy_route_outputs(src_dir: Path, dst_dir: Path) -> None:
    copy_public_files(src_dir, dst_dir, PUBLIC_OUTPUT_FILES)
    src_sessions = src_dir / "sessions"
    if src_sessions.exists():
        mirror_tree(src_sessions, dst_dir / "sessions")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Blindspot v2: event-based collaboration diagnosis with runtime incremental cache.")
    parser.add_argument("--project-cwd", required=True)
    parser.add_argument("--sessions-root", default=str(Path.home() / ".pi/agent/sessions"))
    parser.add_argument("--out", help="Output directory; omit to use ~/.pi-distill runtime layout")
    parser.add_argument("--runtime-root", default=str(Path.home() / ".pi-distill"), help="Runtime state/output root")
    parser.add_argument("--record-state", action="store_true", help="Also update runtime state/history when --out is explicitly set")
    args = parser.parse_args(argv)

    runtime_layout = args.out is None
    record_state = runtime_layout or args.record_state
    session_dir = Path(args.sessions_root).expanduser().resolve() / session_dir_name(str(Path(args.project_cwd).resolve()))

    if not record_state:
        out_dir = Path(args.out)
        session_paths = sorted(session_dir.glob("*.jsonl"))
        session_caches = [extract_blindspot_v2_session(path) for path in session_paths]
        events = load_events_from_session_caches([path.name for path in session_paths], session_caches)
        profile = build_profile(events)
        write_route_outputs(out_dir, args.project_cwd, len(session_paths), session_caches, events, profile)
        print(f"wrote {out_dir / 'report.md'}")
        return 0

    ctx = prepare_route_run(
        project_cwd=args.project_cwd,
        sessions_root=args.sessions_root,
        runtime_root=args.runtime_root,
        route="blindspot-v2",
    )
    runtime_paths = ctx.paths
    latest_dir = runtime_paths.latest_route_dir("blindspot-v2")
    latest_dir.mkdir(parents=True, exist_ok=True)
    runtime_paths.latest_route_sessions_dir("blindspot-v2").mkdir(parents=True, exist_ok=True)

    remove_deleted_session_caches(runtime_paths, "blindspot-v2", ctx.diff.deleted_sessions)
    entry_by_file = {item["file"]: item for item in ctx.diff.entries}
    sessions_to_process = sorted(set(ctx.diff.new_sessions + ctx.diff.changed_sessions))

    for session_file in ctx.diff.session_files:
        cache_path = route_session_cache_path(runtime_paths, "blindspot-v2", session_file)
        cached = load_json(cache_path, None)
        if not cached or cached.get("fingerprint") != entry_by_file[session_file]["fingerprint"]:
            if session_file not in sessions_to_process:
                sessions_to_process.append(session_file)

    report_path = latest_dir / "report.md"
    reused_latest = False

    if not sessions_to_process and report_path.exists():
        reused_latest = True
    else:
        for session_file in sessions_to_process:
            cache = extract_blindspot_v2_session(session_dir / session_file)
            cache["fingerprint"] = entry_by_file[session_file]["fingerprint"]
            write_json(route_session_cache_path(runtime_paths, "blindspot-v2", session_file), cache)

        session_caches = []
        for session_file in ctx.diff.session_files:
            cached = load_json(route_session_cache_path(runtime_paths, "blindspot-v2", session_file), None)
            if not cached:
                continue
            session_caches.append(cached)

        events = load_events_from_session_caches(ctx.diff.session_files, session_caches)
        profile = build_profile(events)
        write_route_outputs(latest_dir, args.project_cwd, len(ctx.diff.session_files), session_caches, events, profile)

    if args.out:
        copy_route_outputs(latest_dir, Path(args.out))
        public_report = Path(args.out) / "report.md"
    else:
        public_report = latest_dir / "report.md"

    state_result = finalize_route_run(
        ctx,
        processed_sessions=sessions_to_process,
        reused_latest=reused_latest,
    )
    print(f"wrote {public_report}")
    print(f"updated runtime state: {state_result['paths']['project_root']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
