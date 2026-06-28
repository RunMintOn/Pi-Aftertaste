#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from runtime_support import build_runtime_paths, write_json


OUTPUT_FILES = ["review_packet.json", "review_packet.md"]
MAX_WORKFLOW_PATTERNS = 8
MAX_WORKFLOW_TURNS = 8
MAX_BLINDSPOT_EVENTS = 10
MAX_REPORT_LINES = 32
MAX_BULLETS = 10
MAX_LIST_ITEMS = 8


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def one_line(value: Any, limit: int = 180) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def compact_path_list(items: list[str], limit: int = 3) -> list[str]:
    return [one_line(item, 140) for item in items[:limit]]


def compact_markdown_excerpt(path: Path, *, max_lines: int = MAX_REPORT_LINES) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    kept: list[str] = []
    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            continue
        if line.startswith("#") or line.startswith("- ") or line[:3].isdigit() and line[3:5] == ". ":
            kept.append(line)
        elif re.match(r"^Generated:", line):
            kept.append(line)
        if len(kept) >= max_lines:
            break
    return kept


def detect_input_dirs(project_cwd: str, runtime_root: str, input_root: str | None) -> tuple[Path, Path, str]:
    if input_root:
        root = Path(input_root).expanduser().resolve()
        return root / "workflow", root / "blindspot-v2", str(root)

    runtime_paths = build_runtime_paths(project_cwd, runtime_root)
    return (
        runtime_paths.latest_route_dir("workflow"),
        runtime_paths.latest_route_dir("blindspot-v2"),
        str(runtime_paths.latest_root),
    )


def required_path(path: Path) -> Path:
    if not path.exists():
        raise FileNotFoundError(f"Required input not found: {path}")
    return path


def compact_counter(counter: dict[str, Any], limit: int = MAX_LIST_ITEMS) -> list[dict[str, Any]]:
    items = sorted(counter.items(), key=lambda item: (-int(item[1]), str(item[0])))
    return [{"name": key, "count": int(value)} for key, value in items[:limit]]


def summarize_pattern(pattern: dict[str, Any]) -> dict[str, Any]:
    summary = {
        "type": pattern.get("type"),
        "confidence": pattern.get("confidence"),
        "evidence": pattern.get("evidence", [])[:4],
    }
    if "path" in pattern:
        summary["path"] = pattern["path"]
    if "command" in pattern:
        summary["command"] = one_line(pattern["command"], 160)
    if "sequence" in pattern:
        summary["sequence"] = pattern["sequence"]
    if "count" in pattern:
        summary["count"] = int(pattern["count"])
    observations = pattern.get("observations")
    if observations:
        summary["observations"] = observations
    return summary


def parse_turn_ref(value: str) -> tuple[str, int] | None:
    match = re.match(r"^(?P<session>.+)#turn(?P<turn>\d+)$", value)
    if not match:
        return None
    return match.group("session"), int(match.group("turn"))


def build_turn_lookup(turns: list[dict[str, Any]]) -> dict[tuple[str, int], dict[str, Any]]:
    return {
        (str(item.get("session_file", "")), int(item.get("turn_index", 0))): item
        for item in turns
        if item.get("session_file") and item.get("turn_index")
    }


def compact_turn(turn: dict[str, Any]) -> dict[str, Any]:
    return {
        "session_file": turn.get("session_file"),
        "turn_index": turn.get("turn_index"),
        "user_preview": turn.get("user_preview"),
        "tool_sequence": turn.get("tool_sequence", [])[:8],
        "read_paths": compact_path_list(turn.get("read_paths", []), 3),
        "edit_paths": compact_path_list(turn.get("edit_paths", []), 3),
        "write_paths": compact_path_list(turn.get("write_paths", []), 3),
        "commands": [one_line(item, 160) for item in turn.get("commands", [])[:2]],
    }


def select_workflow_turns(turns: list[dict[str, Any]], patterns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    lookup = build_turn_lookup(turns)
    selected: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()

    for pattern in patterns:
        for ref in pattern.get("evidence", [])[:4]:
            parsed = parse_turn_ref(ref)
            if not parsed or parsed in seen:
                continue
            turn = lookup.get(parsed)
            if not turn:
                continue
            selected.append(compact_turn(turn))
            seen.add(parsed)
            if len(selected) >= MAX_WORKFLOW_TURNS:
                return selected

    scored: list[tuple[int, dict[str, Any]]] = []
    for turn in turns:
        score = 0
        score += len(turn.get("commands", [])) * 3
        score += len(turn.get("edit_paths", [])) * 3
        score += len(turn.get("write_paths", [])) * 2
        score += len(turn.get("tool_sequence", []))
        if score > 0:
            scored.append((score, turn))
    for _, turn in sorted(scored, key=lambda item: (-item[0], str(item[1].get("session_file", "")), int(item[1].get("turn_index", 0)))):
        key = (str(turn.get("session_file", "")), int(turn.get("turn_index", 0)))
        if key in seen:
            continue
        selected.append(compact_turn(turn))
        seen.add(key)
        if len(selected) >= MAX_WORKFLOW_TURNS:
            break
    return selected


def summarize_workflow(workflow_dir: Path) -> dict[str, Any]:
    summary = load_json(required_path(workflow_dir / "project_summary.json"))
    counters = load_json(required_path(workflow_dir / "counters.json"))
    patterns = load_json(required_path(workflow_dir / "candidate_patterns.json"))
    turns = load_json(required_path(workflow_dir / "turns.json"))
    report_excerpt = compact_markdown_excerpt(required_path(workflow_dir / "report.md"))

    return {
        "project_summary": summary,
        "top_tools": compact_counter(counters.get("tools", {}), 8),
        "top_hot_paths": compact_counter(counters.get("paths", {}), 8),
        "top_commands": compact_counter(counters.get("commands", {}), 8),
        "top_sequences": compact_counter(counters.get("sequences", {}), 8),
        "candidate_patterns": [summarize_pattern(item) for item in patterns[:MAX_WORKFLOW_PATTERNS]],
        "representative_turns": select_workflow_turns(turns, patterns),
        "report_excerpt": report_excerpt,
    }


def summarize_event(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "session_file": event.get("session_file"),
        "trigger_turn": event.get("trigger_turn"),
        "event_type": event.get("event_type"),
        "secondary_types": event.get("secondary_types", []),
        "original_user_preview": event.get("original_user_preview"),
        "assistant_preview": event.get("assistant_preview"),
        "evidence": event.get("evidence"),
        "missing_constraint": event.get("missing_constraint"),
        "impact": event.get("impact"),
        "avoidable": bool(event.get("avoidable", False)),
        "confidence": float(event.get("confidence", 0)),
        "suggestion": event.get("suggestion"),
    }


def select_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted(
        events,
        key=lambda item: (
            -float(item.get("confidence", 0)),
            -int(item.get("assistant_tool_calls", 0)),
            str(item.get("session_file", "")),
            int(item.get("trigger_turn", 0)),
        ),
    )
    return [summarize_event(item) for item in ordered[:MAX_BLINDSPOT_EVENTS]]


def summarize_blindspot_v2(route_dir: Path) -> dict[str, Any]:
    events = load_json(required_path(route_dir / "events.json"))
    profile = load_json(required_path(route_dir / "profile.json"))
    report_excerpt = compact_markdown_excerpt(required_path(route_dir / "report.md"))

    stable_patterns = []
    for item in profile.get("stable_patterns", [])[:MAX_LIST_ITEMS]:
        stable_patterns.append(
            {
                "pattern": item.get("pattern"),
                "count": int(item.get("count", 0)),
                "sessions": int(item.get("sessions", 0)),
                "confidence": item.get("confidence"),
                "user_suggestion": item.get("user_suggestion"),
                "agent_suggestion": item.get("agent_suggestion"),
                "evidence_examples": item.get("evidence_examples", [])[:3],
            }
        )

    return {
        "event_count": int(profile.get("event_count", len(events))),
        "event_types": profile.get("event_types", {}),
        "top_missing_constraints": profile.get("top_missing_constraints", {}),
        "impact_counts": profile.get("impact_counts", {}),
        "stable_patterns": stable_patterns,
        "profiles": profile.get("profiles", [])[:MAX_LIST_ITEMS],
        "representative_events": select_events(events),
        "report_excerpt": report_excerpt,
    }


def build_suggested_assets(workflow: dict[str, Any], blindspot_v2: dict[str, Any]) -> list[dict[str, Any]]:
    event_types = blindspot_v2.get("event_types", {})
    patterns = workflow.get("candidate_patterns", [])
    suggestions: list[dict[str, Any]] = []

    if event_types.get("late_constraint", 0) >= 2:
        suggestions.append(
            {
                "candidate": "pre_execution_constraint_checklist",
                "why": "late_constraint events repeat, suggesting important constraints often arrive after execution starts.",
                "evidence": [
                    f"late_constraint x{event_types.get('late_constraint', 0)}",
                    "blindspot-v2 profiles suggest asking about execution order, search budget, and boundaries earlier.",
                ],
                "possible_asset": "checklist or prompt preamble",
            }
        )
    if event_types.get("verbosity_mismatch", 0) >= 2:
        suggestions.append(
            {
                "candidate": "response_style_preamble",
                "why": "verbosity mismatch repeats often enough to justify a reusable answer-style template.",
                "evidence": [f"verbosity_mismatch x{event_types.get('verbosity_mismatch', 0)}"],
                "possible_asset": "prompt snippet",
            }
        )
    if event_types.get("direction_mismatch", 0) >= 2:
        suggestions.append(
            {
                "candidate": "scope_alignment_template",
                "why": "direction mismatch recurs, indicating the need for an explicit object/scope/goal alignment step.",
                "evidence": [f"direction_mismatch x{event_types.get('direction_mismatch', 0)}"],
                "possible_asset": "request template or checklist",
            }
        )
    repeated_commands = [item for item in patterns if item.get("type") == "repeated_command"]
    if repeated_commands:
        suggestions.append(
            {
                "candidate": "command_loop_sop",
                "why": "workflow already shows repeated command loops that may be worth standardizing.",
                "evidence": [one_line(item.get("command", ""), 120) for item in repeated_commands[:3]],
                "possible_asset": "SOP or skill",
            }
        )
    read_edit = [item for item in patterns if item.get("type") == "repeated_tool_sequence" and item.get("sequence") == ["read", "edit"]]
    if read_edit:
        suggestions.append(
            {
                "candidate": "read_then_edit_micro_workflow",
                "why": "the route sees a repeated read->edit micro pattern that may be reusable as a code-editing habit.",
                "evidence": [f"read->edit x{int(read_edit[0].get('count', 0))}"],
                "possible_asset": "micro-SOP",
            }
        )
    return suggestions


def build_output_contract() -> dict[str, Any]:
    return {
        "final_prompt_inputs": ["review_packet.json", "review_packet.md"],
        "final_outputs": [
            {
                "file": "action_report.md",
                "purpose": "short user-facing action report focused on the few most valuable changes to make now",
            },
            {
                "file": "memory_candidates.json",
                "purpose": "conservative candidate memory pool with evidence and confidence",
            },
            {
                "file": "distill_shortlist.md",
                "purpose": "shortlist of reusable collaboration assets worth keeping, skipping, or gathering more evidence for",
            },
        ],
        "global_rules": [
            "use only evidence present in the review packet",
            "prefer repeated and higher-confidence signals over one-off details",
            "keep outputs short, specific, and reusable",
            "do not browse raw route outputs unless explicitly asked",
        ],
    }


def build_review_packet(project_cwd: str, workflow_dir: Path, blindspot_v2_dir: Path, source_root: str) -> dict[str, Any]:
    workflow = summarize_workflow(workflow_dir)
    blindspot_v2 = summarize_blindspot_v2(blindspot_v2_dir)
    return {
        "project": {
            "cwd": str(Path(project_cwd).expanduser().resolve()),
            "source_root": source_root,
        },
        "sources": {
            "workflow_dir": str(workflow_dir),
            "blindspot_v2_dir": str(blindspot_v2_dir),
            "files_read": [
                str(workflow_dir / "project_summary.json"),
                str(workflow_dir / "counters.json"),
                str(workflow_dir / "candidate_patterns.json"),
                str(workflow_dir / "turns.json"),
                str(workflow_dir / "report.md"),
                str(blindspot_v2_dir / "events.json"),
                str(blindspot_v2_dir / "profile.json"),
                str(blindspot_v2_dir / "report.md"),
            ],
        },
        "workflow": workflow,
        "blindspot_v2": blindspot_v2,
        "suggested_asset_candidates": build_suggested_assets(workflow, blindspot_v2),
        "final_output_contract": build_output_contract(),
    }


def render_review_packet_md(packet: dict[str, Any]) -> str:
    workflow = packet["workflow"]
    blindspot_v2 = packet["blindspot_v2"]
    lines = [
        "# Review Packet",
        "",
        "This file is the compiled input for the final distill synthesis prompt.",
        "Read this packet instead of traversing all raw route outputs.",
        "",
        "## Scope",
        "",
        f"- Project: `{packet['project']['cwd']}`",
        f"- Source root: `{packet['project']['source_root']}`",
        f"- Workflow sessions: {workflow['project_summary'].get('session_count', 0)}",
        f"- Workflow user turns: {workflow['project_summary'].get('total_user_turns', 0)}",
        f"- Workflow tool calls: {workflow['project_summary'].get('total_tool_calls', 0)}",
        f"- Blindspot-v2 events: {blindspot_v2.get('event_count', 0)}",
        "",
        "## Workflow distilled signals",
        "",
        "### Top tools",
    ]
    for item in workflow["top_tools"]:
        lines.append(f"- `{item['name']}` × {item['count']}")
    lines.extend(["", "### Hot paths", ""])
    for item in workflow["top_hot_paths"]:
        lines.append(f"- `{item['name']}` × {item['count']}")
    lines.extend(["", "### Repeated commands", ""])
    for item in workflow["top_commands"][:MAX_BULLETS]:
        lines.append(f"- ×{item['count']} `{item['name']}`")
    lines.extend(["", "### Candidate workflow patterns", ""])
    for item in workflow["candidate_patterns"]:
        title_bits = [item.get("type", "pattern")]
        if item.get("confidence"):
            title_bits.append(item["confidence"])
        lines.append(f"- {' / '.join(title_bits)}")
        if item.get("path"):
            lines.append(f"  - path: `{item['path']}`")
        if item.get("command"):
            lines.append(f"  - command: `{item['command']}`")
        if item.get("sequence"):
            lines.append(f"  - sequence: `{ ' -> '.join(item['sequence']) }`")
        if item.get("count") is not None:
            lines.append(f"  - count: {item['count']}")
        if item.get("observations"):
            obs = item["observations"]
            if isinstance(obs, dict):
                if "interactions" in obs:
                    lines.append(f"  - interactions: {obs['interactions']}")
                if "read_count" in obs or "edit_count" in obs:
                    lines.append(f"  - read/edit: {obs.get('read_count', 0)}/{obs.get('edit_count', 0)}")
        for evidence in item.get("evidence", [])[:3]:
            lines.append(f"  - evidence: {evidence}")
    lines.extend(["", "### Representative workflow turns", ""])
    for turn in workflow["representative_turns"]:
        lines.append(f"- `{turn['session_file']}#turn{turn['turn_index']}` {turn['user_preview']}")
        if turn["tool_sequence"]:
            lines.append(f"  - tools: {' -> '.join(turn['tool_sequence'])}")
        if turn["edit_paths"]:
            lines.append(f"  - edit_paths: {', '.join(turn['edit_paths'])}")
        if turn["commands"]:
            for command in turn["commands"]:
                lines.append(f"  - command: `{command}`")
    lines.extend(["", "## Blindspot-v2 distilled signals", "", "### Event types", ""])
    for name, count in blindspot_v2.get("event_types", {}).items():
        lines.append(f"- `{name}` × {count}")
    lines.extend(["", "### Top missing constraints", ""])
    for name, count in list(blindspot_v2.get("top_missing_constraints", {}).items())[:MAX_BULLETS]:
        lines.append(f"- `{name}` × {count}")
    lines.extend(["", "### Stable patterns", ""])
    for item in blindspot_v2.get("stable_patterns", []):
        lines.append(f"- `{item['pattern']}` × {item['count']} ({item['confidence']})")
        if item.get("user_suggestion"):
            lines.append(f"  - user_suggestion: {item['user_suggestion']}")
        for evidence in item.get("evidence_examples", [])[:2]:
            lines.append(f"  - evidence: {evidence}")
    lines.extend(["", "### Profiles", ""])
    for item in blindspot_v2.get("profiles", []):
        lines.append(f"- `{item.get('name')}` ({item.get('confidence')})")
        if item.get("support"):
            lines.append(f"  - support: {', '.join(item['support'][:4])}")
        if item.get("user_suggestion"):
            lines.append(f"  - user_suggestion: {item['user_suggestion']}")
    lines.extend(["", "### Representative events", ""])
    for item in blindspot_v2.get("representative_events", []):
        lines.append(f"- `{item['session_file']}#turn{item['trigger_turn']}` `{item['event_type']}` ({item['confidence']})")
        lines.append(f"  - original: {item['original_user_preview']}")
        if item.get("assistant_preview"):
            lines.append(f"  - assistant: {item['assistant_preview']}")
        lines.append(f"  - evidence: {item['evidence']}")
        lines.append(f"  - missing_constraint: {item['missing_constraint']}")
        lines.append(f"  - impact: {item['impact']}")
        lines.append(f"  - suggestion: {item['suggestion']}")
    lines.extend(["", "## Suggested asset candidates", ""])
    for item in packet.get("suggested_asset_candidates", []):
        lines.append(f"- `{item['candidate']}`")
        lines.append(f"  - why: {item['why']}")
        lines.append(f"  - possible_asset: {item['possible_asset']}")
        for evidence in item.get("evidence", [])[:3]:
            lines.append(f"  - evidence: {evidence}")
    lines.extend(["", "## Final output contract", ""])
    for item in packet["final_output_contract"]["final_outputs"]:
        lines.append(f"- `{item['file']}` — {item['purpose']}")
    lines.extend(["", "## Rules for final synthesis", ""])
    for item in packet["final_output_contract"]["global_rules"]:
        lines.append(f"- {item}")
    return "\n".join(lines).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a compiled review packet from workflow + blindspot-v2 outputs.")
    parser.add_argument("--project-cwd", required=True)
    parser.add_argument("--input-root", help="Directory containing workflow/ and blindspot-v2/ subdirs; omit to read runtime latest outputs")
    parser.add_argument("--out", help="Output directory; omit to write to ~/.pi-distill/projects/<project-id>/latest/final")
    parser.add_argument("--runtime-root", default=str(Path.home() / ".pi-distill"))
    args = parser.parse_args(argv)

    workflow_dir, blindspot_v2_dir, source_root = detect_input_dirs(args.project_cwd, args.runtime_root, args.input_root)

    if args.out:
        out_dir = Path(args.out).expanduser().resolve()
    else:
        out_dir = build_runtime_paths(args.project_cwd, args.runtime_root).latest_root / "final"

    packet = build_review_packet(args.project_cwd, workflow_dir, blindspot_v2_dir, source_root)
    out_dir.mkdir(parents=True, exist_ok=True)
    write_json(out_dir / "review_packet.json", packet)
    (out_dir / "review_packet.md").write_text(render_review_packet_md(packet), encoding="utf-8")
    print(f"wrote {out_dir / 'review_packet.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
