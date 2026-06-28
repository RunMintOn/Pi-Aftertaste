#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from runtime_support import (
    build_runtime_paths,
    copy_public_files,
    finalize_route_run,
    load_json,
    prepare_route_run,
    remove_deleted_session_caches,
    route_session_cache_path,
    write_json,
)


PUBLIC_OUTPUT_FILES = [
    "project_summary.json",
    "sessions.json",
    "turns.json",
    "counters.json",
    "candidate_patterns.json",
    "report.md",
]


def session_dir_name(project_cwd: str) -> str:
    stripped = project_cwd.strip("/")
    if not stripped:
        return "----"
    return f"--{stripped.replace('/', '-')}--"


def sanitize_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", "", text)
    return text


def one_line(text: str, limit: int = 180) -> str:
    text = re.sub(r"\s+", " ", sanitize_text(text)).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def display_path(path: str, project_cwd: str) -> str:
    p = sanitize_text(path)
    if p.startswith(project_cwd.rstrip("/") + "/"):
        return "<PROJECT>/" + p[len(project_cwd.rstrip("/") + "/") :]
    home = str(Path.home())
    if p == home:
        return "~"
    if p.startswith(home + "/"):
        return "~/" + p[len(home) + 1 :]
    return p


def normalize_command(command: str, project_cwd: str) -> str:
    text = sanitize_text(command)
    text = text.replace(project_cwd, "<PROJECT>")
    text = re.sub(r"/tmp/pi-hypa-backup-\d{8}-\d{6}", "/tmp/pi-hypa-backup-<TIMESTAMP>", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def collapse_consecutive(items: list[str]) -> list[str]:
    out: list[str] = []
    for item in items:
        if not out or out[-1] != item:
            out.append(item)
    return out


def unique_head(items: list[str], limit: int = 5) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
        if len(out) >= limit:
            break
    return out


def is_validation_command(command: str) -> bool:
    lowered = command.lower()
    markers = [" build", "build ", "npm run build", " vitest", " pytest", " unittest", " test", "pnpm test", "npm test"]
    return any(marker in lowered for marker in markers)


@dataclass
class TurnSummary:
    session_file: str
    session_id: str
    turn_index: int
    user_preview: str
    tool_sequence: list[str] = field(default_factory=list)
    read_paths: list[str] = field(default_factory=list)
    edit_paths: list[str] = field(default_factory=list)
    write_paths: list[str] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_file": self.session_file,
            "session_id": self.session_id,
            "turn_index": self.turn_index,
            "user_preview": self.user_preview,
            "tool_sequence": self.tool_sequence,
            "read_paths": self.read_paths,
            "edit_paths": self.edit_paths,
            "write_paths": self.write_paths,
            "commands": self.commands,
        }


@dataclass
class SessionSummary:
    file: str
    session_id: str
    started_at: str
    user_turns: int
    message_count: int
    tool_calls: int
    tools: dict[str, int]
    models: list[str]
    first_user_preview: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "session_id": self.session_id,
            "started_at": self.started_at,
            "user_turns": self.user_turns,
            "message_count": self.message_count,
            "tool_calls": self.tool_calls,
            "tools": self.tools,
            "models": self.models,
            "first_user_preview": self.first_user_preview,
        }


class ProjectAnalyzer:
    def __init__(self, project_cwd: str, sessions_root: str):
        self.project_cwd = str(Path(project_cwd).resolve())
        self.sessions_root = Path(sessions_root).expanduser().resolve()
        self.session_dir = self.sessions_root / session_dir_name(self.project_cwd)

        self.session_summaries: list[SessionSummary] = []
        self.turns: list[TurnSummary] = []

        self.tool_counter: Counter[str] = Counter()
        self.path_counter: Counter[str] = Counter()
        self.command_counter: Counter[str] = Counter()
        self.sequence_counter: Counter[tuple[str, ...]] = Counter()
        self.read_edit_counter: Counter[str] = Counter()
        self.edit_validate_counter: Counter[str] = Counter()
        self.tool_path_counter: Counter[tuple[str, str]] = Counter()
        self.model_counter: Counter[str] = Counter()

        self.path_turn_refs: dict[str, list[str]] = defaultdict(list)
        self.command_turn_refs: dict[str, list[str]] = defaultdict(list)
        self.sequence_turn_refs: dict[tuple[str, ...], list[str]] = defaultdict(list)

    def discover_session_files(self) -> list[Path]:
        if not self.session_dir.exists():
            raise FileNotFoundError(f"Session dir not found: {self.session_dir}")
        return sorted(self.session_dir.glob("*.jsonl"))

    def analyze(self) -> dict[str, Any]:
        session_files = self.discover_session_files()
        for path in session_files:
            self._analyze_session(path)
        return self._build_output()

    def add_cached_session(self, cached: dict[str, Any]) -> None:
        session = cached["session"]
        self.session_summaries.append(
            SessionSummary(
                file=session["file"],
                session_id=session["session_id"],
                started_at=session["started_at"],
                user_turns=session["user_turns"],
                message_count=session["message_count"],
                tool_calls=session["tool_calls"],
                tools=session["tools"],
                models=session["models"],
                first_user_preview=session["first_user_preview"],
            )
        )
        self.tool_counter.update(cached["counters"].get("tools", {}))
        self.model_counter.update(cached["counters"].get("models", {}))
        self.path_counter.update(cached["counters"].get("paths", {}))
        self.command_counter.update(cached["counters"].get("commands", {}))
        self.read_edit_counter.update(cached["counters"].get("read_edit", {}))
        self.edit_validate_counter.update(cached["counters"].get("edit_validate", {}))
        for item in cached["counters"].get("tool_paths", []):
            self.tool_path_counter[(item["tool"], item["path"])] += item["count"]
        for item in cached["counters"].get("sequences", []):
            self.sequence_counter[tuple(item["sequence"])] += item["count"]
        for key, refs in cached["refs"].get("paths", {}).items():
            self.path_turn_refs[key].extend(refs)
        for key, refs in cached["refs"].get("commands", {}).items():
            self.command_turn_refs[key].extend(refs)
        for item in cached["refs"].get("sequences", []):
            self.sequence_turn_refs[tuple(item["sequence"])] .extend(item["refs"])
        for turn in cached.get("turns", []):
            self.turns.append(
                TurnSummary(
                    session_file=turn["session_file"],
                    session_id=turn["session_id"],
                    turn_index=turn["turn_index"],
                    user_preview=turn["user_preview"],
                    tool_sequence=turn.get("tool_sequence", []),
                    read_paths=turn.get("read_paths", []),
                    edit_paths=turn.get("edit_paths", []),
                    write_paths=turn.get("write_paths", []),
                    commands=turn.get("commands", []),
                )
            )

    def _iter_entries(self, path: Path):
        with path.open("r", encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if raw:
                    yield json.loads(raw)

    def _message_text(self, message: dict[str, Any]) -> str:
        parts = []
        for part in message.get("content", []):
            if part.get("type") == "text":
                parts.append(part.get("text", ""))
        return "\n".join(parts)

    def _analyze_session(self, path: Path) -> None:
        session_id = ""
        started_at = ""
        message_count = 0
        user_turns = 0
        tool_calls = 0
        tools = Counter()
        models: list[str] = []
        first_user_preview = ""
        turn: TurnSummary | None = None

        for entry in self._iter_entries(path):
            etype = entry.get("type")
            if etype == "session":
                session_id = entry.get("id", "")
                started_at = entry.get("timestamp", "")
                continue
            if etype == "model_change":
                provider = sanitize_text(entry.get("provider", ""))
                model_id = sanitize_text(entry.get("modelId", ""))
                key = f"{provider}/{model_id}".strip("/")
                if key:
                    models.append(key)
                    self.model_counter[key] += 1
                continue
            if etype != "message":
                continue

            message_count += 1
            message = entry.get("message", {})
            role = message.get("role")

            if role == "user":
                if turn is not None:
                    self._finalize_turn(turn)
                user_turns += 1
                preview = one_line(self._message_text(message))
                if not first_user_preview:
                    first_user_preview = preview
                turn = TurnSummary(
                    session_file=path.name,
                    session_id=session_id,
                    turn_index=user_turns,
                    user_preview=preview,
                )
                continue

            if role != "assistant" or turn is None:
                continue

            for part in message.get("content", []):
                if part.get("type") != "toolCall":
                    continue
                name = sanitize_text(part.get("name", ""))
                if not name:
                    continue
                tool_calls += 1
                tools[name] += 1
                self.tool_counter[name] += 1
                turn.tool_sequence.append(name)

                args = part.get("arguments", {}) or {}
                path_arg = args.get("path")
                if isinstance(path_arg, str) and path_arg:
                    self.path_counter[path_arg] += 1
                    self.tool_path_counter[(name, path_arg)] += 1
                    self.path_turn_refs[path_arg].append(f"{path.name}#turn{turn.turn_index}")
                    if name in {"read", "hypa_read"}:
                        turn.read_paths.append(path_arg)
                    elif name in {"edit", "write"}:
                        if name == "edit":
                            turn.edit_paths.append(path_arg)
                        else:
                            turn.write_paths.append(path_arg)
                command_arg = args.get("command")
                if isinstance(command_arg, str) and command_arg:
                    normalized = normalize_command(command_arg, self.project_cwd)
                    self.command_counter[normalized] += 1
                    self.command_turn_refs[normalized].append(f"{path.name}#turn{turn.turn_index}")
                    turn.commands.append(normalized)

        if turn is not None:
            self._finalize_turn(turn)

        self.session_summaries.append(
            SessionSummary(
                file=path.name,
                session_id=session_id,
                started_at=started_at,
                user_turns=user_turns,
                message_count=message_count,
                tool_calls=tool_calls,
                tools=dict(tools),
                models=models,
                first_user_preview=first_user_preview,
            )
        )

    def _finalize_turn(self, turn: TurnSummary) -> None:
        turn.tool_sequence = collapse_consecutive(turn.tool_sequence)
        self.turns.append(turn)

        if turn.tool_sequence:
            seq = tuple(turn.tool_sequence)
            self.sequence_counter[seq] += 1
            self.sequence_turn_refs[seq].append(f"{turn.session_file}#turn{turn.turn_index}")

        read_set = set(turn.read_paths)
        edit_set = set(turn.edit_paths)
        if read_set and edit_set:
            for path in sorted(read_set & edit_set):
                self.read_edit_counter[path] += 1

        has_validation = any(is_validation_command(cmd) for cmd in turn.commands)
        if has_validation and edit_set:
            for path in sorted(edit_set | set(turn.write_paths)):
                self.edit_validate_counter[path] += 1

    def _candidate_patterns(self) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []

        for path, count in self.path_counter.most_common():
            edit_count = self.tool_path_counter.get(("edit", path), 0) + self.tool_path_counter.get(("write", path), 0)
            read_count = self.tool_path_counter.get(("read", path), 0) + self.tool_path_counter.get(("hypa_read", path), 0)
            read_edit = self.read_edit_counter.get(path, 0)
            edit_validate = self.edit_validate_counter.get(path, 0)
            distinct_tools = sorted({tool for (tool, p), n in self.tool_path_counter.items() if p == path and n > 0})
            artifact_like = "." in Path(path).name
            if count < 3:
                continue
            if read_count == 0 and edit_count == 0:
                continue
            if not artifact_like and edit_count == 0 and read_count < 2:
                continue
            if len(distinct_tools) < 2 and read_edit == 0 and edit_validate == 0:
                continue
            confidence = "medium"
            if read_edit >= 2 or edit_validate >= 2 or (read_count >= 3 and edit_count >= 3):
                confidence = "high"
            candidates.append(
                {
                    "type": "hot_file_workflow",
                    "confidence": confidence,
                    "path": display_path(path, self.project_cwd),
                    "observations": {
                        "interactions": count,
                        "read_count": read_count,
                        "edit_count": edit_count,
                        "read_then_edit_turns": read_edit,
                        "edit_then_validate_turns": edit_validate,
                        "tools": distinct_tools,
                    },
                    "evidence": unique_head(self.path_turn_refs[path]),
                }
            )

        for seq, count in self.sequence_counter.most_common():
            if count < 2 or len(seq) < 2:
                continue
            candidates.append(
                {
                    "type": "repeated_tool_sequence",
                    "confidence": "medium" if len(seq) == 2 else "low",
                    "sequence": list(seq),
                    "count": count,
                    "evidence": unique_head(self.sequence_turn_refs[seq]),
                }
            )

        for cmd, count in self.command_counter.most_common():
            if count < 2:
                continue
            candidates.append(
                {
                    "type": "repeated_command",
                    "confidence": "medium",
                    "command": cmd,
                    "count": count,
                    "evidence": unique_head(self.command_turn_refs[cmd]),
                }
            )

        candidates.sort(
            key=lambda item: (
                {"high": 2, "medium": 1, "low": 0}.get(item.get("confidence", "low"), 0),
                item.get("observations", {}).get("interactions", item.get("count", 0)),
            ),
            reverse=True,
        )
        return candidates

    def _build_output(self) -> dict[str, Any]:
        total_messages = sum(s.message_count for s in self.session_summaries)
        total_user_turns = sum(s.user_turns for s in self.session_summaries)
        total_tool_calls = sum(s.tool_calls for s in self.session_summaries)
        report = {
            "project": {
                "cwd": self.project_cwd,
                "session_dir": str(self.session_dir),
                "session_count": len(self.session_summaries),
                "total_messages": total_messages,
                "total_user_turns": total_user_turns,
                "total_tool_calls": total_tool_calls,
            },
            "sessions": [s.to_dict() for s in self.session_summaries],
            "turns": [t.to_dict() for t in self.turns],
            "counters": {
                "tools": dict(self.tool_counter.most_common()),
                "models": dict(self.model_counter.most_common()),
                "paths": {display_path(k, self.project_cwd): v for k, v in self.path_counter.most_common(20)},
                "commands": dict(self.command_counter.most_common(20)),
                "sequences": {" -> ".join(k): v for k, v in self.sequence_counter.most_common(20)},
            },
            "candidates": self._candidate_patterns(),
        }
        return report


def extract_workflow_session(path: Path, project_cwd: str) -> dict[str, Any]:
    analyzer = ProjectAnalyzer(project_cwd, str(path.parent.parent))
    analyzer._analyze_session(path)
    if not analyzer.session_summaries:
        raise ValueError(f"No session summary extracted for {path}")
    return {
        "session": analyzer.session_summaries[0].to_dict(),
        "turns": [turn.to_dict() for turn in analyzer.turns],
        "counters": {
            "tools": dict(analyzer.tool_counter),
            "models": dict(analyzer.model_counter),
            "paths": dict(analyzer.path_counter),
            "commands": dict(analyzer.command_counter),
            "read_edit": dict(analyzer.read_edit_counter),
            "edit_validate": dict(analyzer.edit_validate_counter),
            "tool_paths": [
                {"tool": tool, "path": cache_path, "count": count}
                for (tool, cache_path), count in analyzer.tool_path_counter.items()
            ],
            "sequences": [
                {"sequence": list(sequence), "count": count}
                for sequence, count in analyzer.sequence_counter.items()
            ],
        },
        "refs": {
            "paths": {key: refs for key, refs in analyzer.path_turn_refs.items()},
            "commands": {key: refs for key, refs in analyzer.command_turn_refs.items()},
            "sequences": [
                {"sequence": list(sequence), "refs": refs}
                for sequence, refs in analyzer.sequence_turn_refs.items()
            ],
        },
    }


def build_output_from_session_caches(project_cwd: str, session_files: list[str], caches: list[dict[str, Any]]) -> dict[str, Any]:
    analyzer = ProjectAnalyzer(project_cwd, str(Path.home() / ".pi/agent/sessions"))
    for cached in sorted(caches, key=lambda item: session_files.index(item["session"]["file"])):
        analyzer.add_cached_session(cached)
    return analyzer._build_output()


def write_outputs(data: dict[str, Any], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "project_summary.json").write_text(json.dumps(data["project"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out_dir / "sessions.json").write_text(json.dumps(data["sessions"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out_dir / "turns.json").write_text(json.dumps(data["turns"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out_dir / "counters.json").write_text(json.dumps(data["counters"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out_dir / "candidate_patterns.json").write_text(json.dumps(data["candidates"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out_dir / "report.md").write_text(render_markdown_report(data), encoding="utf-8")


def render_markdown_report(data: dict[str, Any]) -> str:
    project = data["project"]
    sessions = data["sessions"]
    counters = data["counters"]
    candidates = data["candidates"]
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")

    lines: list[str] = []
    lines.append("# Pi Distill MVP Report")
    lines.append("")
    lines.append(f"Generated: {generated_at}")
    lines.append("")
    lines.append("## Scope")
    lines.append("")
    lines.append(f"- Project cwd: `{project['cwd']}`")
    lines.append(f"- Session dir: `{project['session_dir']}`")
    lines.append(f"- Sessions: {project['session_count']}")
    lines.append(f"- User turns: {project['total_user_turns']}")
    lines.append(f"- Messages: {project['total_messages']}")
    lines.append(f"- Tool calls: {project['total_tool_calls']}")
    lines.append("")
    lines.append("## What this MVP can extract")
    lines.append("")
    lines.append("- Session-level inventory: time, models, first prompt, tool volume")
    lines.append("- Turn-level workflow traces: user prompt preview + tool sequence + files + commands")
    lines.append("- Hot files: which files are repeatedly read/edited")
    lines.append("- Repeated commands and validation loops")
    lines.append("- Candidate reusable workflows for later skill/prompt packaging")
    lines.append("")
    lines.append("## Sessions")
    lines.append("")
    for session in sessions:
        lines.append(f"- `{session['file']}` — {session['user_turns']} turns, {session['tool_calls']} tool calls")
        if session.get("first_user_preview"):
            lines.append(f"  - First prompt: {session['first_user_preview']}")
    lines.append("")
    lines.append("## Top tools")
    lines.append("")
    for name, count in list(counters["tools"].items())[:10]:
        lines.append(f"- `{name}` × {count}")
    lines.append("")
    lines.append("## Hot files")
    lines.append("")
    for path, count in list(counters["paths"].items())[:10]:
        lines.append(f"- `{path}` × {count}")
    lines.append("")
    lines.append("## Repeated commands")
    lines.append("")
    repeated_commands = [(cmd, count) for cmd, count in counters["commands"].items() if count >= 2]
    if repeated_commands:
        for cmd, count in repeated_commands[:10]:
            lines.append(f"- ×{count} `{cmd}`")
    else:
        lines.append("- No repeated commands detected above threshold.")
    lines.append("")
    lines.append("## Candidate patterns")
    lines.append("")
    if not candidates:
        lines.append("No candidate patterns found.")
    else:
        for idx, candidate in enumerate(candidates[:12], 1):
            title = candidate["type"]
            confidence = candidate.get("confidence", "low")
            lines.append(f"### {idx}. {title} [{confidence}]")
            lines.append("")
            if candidate["type"] == "hot_file_workflow":
                obs = candidate["observations"]
                lines.append(f"- Path: `{candidate['path']}`")
                lines.append(f"- Interactions: {obs['interactions']}")
                lines.append(f"- Read count: {obs['read_count']}")
                lines.append(f"- Edit count: {obs['edit_count']}")
                lines.append(f"- Read→edit turns: {obs['read_then_edit_turns']}")
                lines.append(f"- Edit→validate turns: {obs['edit_then_validate_turns']}")
                lines.append(f"- Tools: {', '.join(obs['tools'])}")
            elif candidate["type"] == "repeated_tool_sequence":
                lines.append(f"- Sequence: `{' -> '.join(candidate['sequence'])}`")
                lines.append(f"- Count: {candidate['count']}")
            elif candidate["type"] == "repeated_command":
                lines.append(f"- Command: `{candidate['command']}`")
                lines.append(f"- Count: {candidate['count']}")
            evidence = candidate.get("evidence", [])
            if evidence:
                lines.append(f"- Evidence: {', '.join(evidence)}")
            lines.append("")
    lines.append("## Suggested next packaging")
    lines.append("")
    lines.append("- Use `candidate_patterns.json` as the shortlist source.")
    lines.append("- Let a later LLM step read only the top candidate turns from `turns.json`, not the full session corpus.")
    lines.append("- First package likely candidates as `.pi/skills` or `.pi/prompts`; keep extension automation for a later phase.")
    lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Analyze Pi project sessions and emit a distill MVP report.")
    parser.add_argument("--project-cwd", required=True, help="Project cwd whose Pi sessions should be analyzed")
    parser.add_argument("--sessions-root", default=str(Path.home() / ".pi/agent/sessions"), help="Pi sessions root")
    parser.add_argument("--out", help="Output directory; omit to use ~/.pi-distill runtime layout")
    parser.add_argument("--runtime-root", default=str(Path.home() / ".pi-distill"), help="Runtime state/output root")
    parser.add_argument("--record-state", action="store_true", help="Also update runtime state/history when --out is explicitly set")
    args = parser.parse_args(argv)

    runtime_layout = args.out is None
    record_state = runtime_layout or args.record_state

    if not record_state:
        out_dir = Path(args.out)
        analyzer = ProjectAnalyzer(args.project_cwd, args.sessions_root)
        data = analyzer.analyze()
        write_outputs(data, out_dir)
        print(f"wrote {out_dir / 'report.md'}")
        return 0

    ctx = prepare_route_run(
        project_cwd=args.project_cwd,
        sessions_root=args.sessions_root,
        runtime_root=args.runtime_root,
        route="workflow",
    )
    runtime_paths = ctx.paths
    latest_dir = runtime_paths.latest_route_dir("workflow")
    latest_dir.mkdir(parents=True, exist_ok=True)
    runtime_paths.latest_route_sessions_dir("workflow").mkdir(parents=True, exist_ok=True)

    remove_deleted_session_caches(runtime_paths, "workflow", ctx.diff.deleted_sessions)
    session_dir = Path(args.sessions_root).expanduser().resolve() / session_dir_name(str(Path(args.project_cwd).resolve()))
    entry_by_file = {item["file"]: item for item in ctx.diff.entries}
    sessions_to_process = sorted(set(ctx.diff.new_sessions + ctx.diff.changed_sessions))
    for session_file in ctx.diff.session_files:
        cache_path = route_session_cache_path(runtime_paths, "workflow", session_file)
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
            cache = extract_workflow_session(session_dir / session_file, args.project_cwd)
            cache["fingerprint"] = entry_by_file[session_file]["fingerprint"]
            write_json(route_session_cache_path(runtime_paths, "workflow", session_file), cache)

        caches = [
            load_json(route_session_cache_path(runtime_paths, "workflow", session_file), None)
            for session_file in ctx.diff.session_files
        ]
        caches = [item for item in caches if item is not None]
        data = build_output_from_session_caches(args.project_cwd, ctx.diff.session_files, caches)
        write_outputs(data, latest_dir)

    if args.out:
        copy_public_files(latest_dir, Path(args.out), PUBLIC_OUTPUT_FILES)
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
