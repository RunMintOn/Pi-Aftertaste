from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class RuntimePaths:
    project_cwd: str
    project_id: str
    runtime_root: Path
    project_root: Path
    latest_root: Path
    history_root: Path
    state_path: Path
    session_index_path: Path
    registry_path: Path

    def latest_route_dir(self, route: str) -> Path:
        return self.latest_root / route

    def latest_route_sessions_dir(self, route: str) -> Path:
        return self.latest_route_dir(route) / "sessions"

    def history_run_dir(self, run_id: str) -> Path:
        return self.history_root / run_id


@dataclass(frozen=True)
class SessionMeta:
    file: str
    session_id: str
    started_at: str
    last_message_at: str
    message_count: int
    user_turn_count: int
    tool_call_count: int
    size_bytes: int
    mtime: int
    fingerprint: str

    def to_dict(self, processed_at: str, status: str) -> dict[str, Any]:
        return {
            "file": self.file,
            "session_id": self.session_id,
            "started_at": self.started_at,
            "last_message_at": self.last_message_at,
            "message_count": self.message_count,
            "user_turn_count": self.user_turn_count,
            "tool_call_count": self.tool_call_count,
            "size_bytes": self.size_bytes,
            "mtime": self.mtime,
            "fingerprint": self.fingerprint,
            "last_processed_at": processed_at,
            "status": status,
        }


@dataclass(frozen=True)
class SessionDiff:
    entries: list[dict[str, Any]]
    session_files: list[str]
    new_sessions: list[str]
    changed_sessions: list[str]
    unchanged_sessions: list[str]
    deleted_sessions: list[str]

    @property
    def has_changes(self) -> bool:
        return bool(self.new_sessions or self.changed_sessions or self.deleted_sessions)


@dataclass(frozen=True)
class RouteRunContext:
    route: str
    now_iso: str
    run_id: str
    paths: RuntimePaths
    previous_state: dict[str, Any]
    previous_index: list[dict[str, Any]]
    diff: SessionDiff


ROUTES = {"workflow", "blindspot", "blindspot-v2"}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_run_id(now_iso: str | None = None) -> str:
    value = now_iso or utc_now_iso()
    date_part, time_part = value.split("T", 1)
    return f"{date_part}T{time_part.replace(':', '-')}"


def project_id_from_cwd(project_cwd: str) -> str:
    resolved = Path(project_cwd).expanduser().resolve().as_posix().strip("/")
    if not resolved:
        return "root"
    value = resolved.replace("/", "-")
    value = re.sub(r"[^a-zA-Z0-9._-]", "_", value)
    value = re.sub(r"-{2,}", "-", value)
    return value.strip("-_.") or "project"


def session_dir_name(project_cwd: str) -> str:
    stripped = project_cwd.strip("/")
    return f"--{stripped.replace('/', '-')}--" if stripped else "----"


def build_runtime_paths(project_cwd: str, runtime_root: str | Path | None = None) -> RuntimePaths:
    resolved_cwd = str(Path(project_cwd).expanduser().resolve())
    root = Path(runtime_root or (Path.home() / ".pi-distill")).expanduser().resolve()
    project_id = project_id_from_cwd(resolved_cwd)
    project_root = root / "projects" / project_id
    return RuntimePaths(
        project_cwd=resolved_cwd,
        project_id=project_id,
        runtime_root=root,
        project_root=project_root,
        latest_root=project_root / "latest",
        history_root=project_root / "history",
        state_path=project_root / "state.json",
        session_index_path=project_root / "session_index.json",
        registry_path=root / "registry.json",
    )


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def mirror_tree(src: Path, dst: Path) -> None:
    if src.resolve() == dst.resolve():
        return
    if dst.exists():
        shutil.rmtree(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dst)


def copy_public_files(src_dir: Path, dst_dir: Path, file_names: list[str]) -> None:
    dst_dir.mkdir(parents=True, exist_ok=True)
    for name in file_names:
        src = src_dir / name
        if src.exists():
            shutil.copy2(src, dst_dir / name)


def session_cache_name(session_file: str) -> str:
    return f"{Path(session_file).stem}.json"


def route_session_cache_path(paths: RuntimePaths, route: str, session_file: str) -> Path:
    return paths.latest_route_sessions_dir(route) / session_cache_name(session_file)


def remove_deleted_session_caches(paths: RuntimePaths, route: str, deleted_sessions: list[str]) -> None:
    for session_file in deleted_sessions:
        cache_path = route_session_cache_path(paths, route, session_file)
        if cache_path.exists():
            cache_path.unlink()


def scan_session_metadata(project_cwd: str, sessions_root: str | Path) -> list[SessionMeta]:
    resolved_cwd = str(Path(project_cwd).expanduser().resolve())
    session_dir = Path(sessions_root).expanduser().resolve() / session_dir_name(resolved_cwd)
    if not session_dir.exists():
        raise FileNotFoundError(f"Session dir not found: {session_dir}")

    items: list[SessionMeta] = []
    for path in sorted(session_dir.glob("*.jsonl")):
        stat = path.stat()
        session_id = ""
        started_at = ""
        last_message_at = ""
        message_count = 0
        user_turn_count = 0
        tool_call_count = 0

        with path.open("r", encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                entry = json.loads(raw)
                etype = entry.get("type")
                if etype == "session":
                    session_id = entry.get("id", "")
                    started_at = entry.get("timestamp", "")
                    continue
                if etype != "message":
                    continue
                message_count += 1
                last_message_at = entry.get("timestamp", last_message_at)
                message = entry.get("message", {}) or {}
                role = message.get("role")
                if role == "user":
                    user_turn_count += 1
                elif role == "assistant":
                    for part in message.get("content", []):
                        if part.get("type") == "toolCall":
                            tool_call_count += 1

        items.append(
            SessionMeta(
                file=path.name,
                session_id=session_id,
                started_at=started_at,
                last_message_at=last_message_at,
                message_count=message_count,
                user_turn_count=user_turn_count,
                tool_call_count=tool_call_count,
                size_bytes=stat.st_size,
                mtime=int(stat.st_mtime),
                fingerprint=f"{stat.st_size}:{int(stat.st_mtime)}",
            )
        )
    return items


def diff_session_index(previous_entries: list[dict[str, Any]], current_meta: list[SessionMeta], processed_at: str) -> SessionDiff:
    prev_by_file = {item.get("file", ""): item for item in previous_entries}
    current_files = {item.file for item in current_meta}
    deleted_sessions = sorted(file for file in prev_by_file if file not in current_files)

    entries: list[dict[str, Any]] = []
    session_files: list[str] = []
    new_sessions: list[str] = []
    changed_sessions: list[str] = []
    unchanged_sessions: list[str] = []

    for meta in current_meta:
        previous = prev_by_file.get(meta.file)
        if previous is None:
            status = "new"
            new_sessions.append(meta.file)
        elif previous.get("fingerprint") != meta.fingerprint:
            status = "changed"
            changed_sessions.append(meta.file)
        else:
            status = "unchanged"
            unchanged_sessions.append(meta.file)
        entries.append(meta.to_dict(processed_at=processed_at, status=status))
        session_files.append(meta.file)

    return SessionDiff(
        entries=entries,
        session_files=session_files,
        new_sessions=new_sessions,
        changed_sessions=changed_sessions,
        unchanged_sessions=unchanged_sessions,
        deleted_sessions=deleted_sessions,
    )


def build_state(previous_state: dict[str, Any], paths: RuntimePaths, processed_at: str, run_id: str, route: str, session_entries: list[dict[str, Any]]) -> dict[str, Any]:
    total_messages = sum(int(item.get("message_count", 0)) for item in session_entries)
    total_user_turns = sum(int(item.get("user_turn_count", 0)) for item in session_entries)
    total_tool_calls = sum(int(item.get("tool_call_count", 0)) for item in session_entries)
    state = {
        "project_cwd": paths.project_cwd,
        "project_id": paths.project_id,
        "created_at": previous_state.get("created_at", processed_at),
        "last_analyzed_at": processed_at,
        "last_workflow_run": previous_state.get("last_workflow_run"),
        "last_blindspot_run": previous_state.get("last_blindspot_run"),
        "last_session_count": len(session_entries),
        "last_message_count": total_messages,
        "last_user_turn_count": total_user_turns,
        "last_tool_call_count": total_tool_calls,
        "latest_history_run": run_id,
    }
    state[f"last_{route}_run"] = processed_at
    return state


def update_registry(paths: RuntimePaths, state: dict[str, Any]) -> None:
    registry = load_json(paths.registry_path, {"projects": []})
    projects = registry.get("projects", [])
    entries = [item for item in projects if item.get("project_id") != paths.project_id]
    entries.append(
        {
            "project_id": paths.project_id,
            "project_cwd": paths.project_cwd,
            "created_at": state.get("created_at"),
            "last_analyzed_at": state.get("last_analyzed_at"),
            "latest_history_run": state.get("latest_history_run"),
        }
    )
    entries.sort(key=lambda item: item.get("project_id", ""))
    write_json(paths.registry_path, {"projects": entries})


def prepare_route_run(*, project_cwd: str, sessions_root: str | Path, runtime_root: str | Path | None, route: str) -> RouteRunContext:
    if route not in ROUTES:
        raise ValueError(f"Unsupported route: {route}")
    now_iso = utc_now_iso()
    run_id = utc_run_id(now_iso)
    paths = build_runtime_paths(project_cwd, runtime_root)
    previous_state = load_json(paths.state_path, {})
    previous_index = load_json(paths.session_index_path, [])
    current_meta = scan_session_metadata(project_cwd, sessions_root)
    diff = diff_session_index(previous_index, current_meta, now_iso)
    return RouteRunContext(
        route=route,
        now_iso=now_iso,
        run_id=run_id,
        paths=paths,
        previous_state=previous_state,
        previous_index=previous_index,
        diff=diff,
    )


def finalize_route_run(ctx: RouteRunContext, *, processed_sessions: list[str], reused_latest: bool) -> dict[str, Any]:
    paths = ctx.paths
    route = ctx.route
    latest_route_dir = paths.latest_route_dir(route)
    latest_route_dir.mkdir(parents=True, exist_ok=True)

    history_run_dir = paths.history_run_dir(ctx.run_id)
    history_run_dir.mkdir(parents=True, exist_ok=True)
    history_route_dir: str | None = None
    if not reused_latest and latest_route_dir.exists():
        history_route = history_run_dir / route
        mirror_tree(latest_route_dir, history_route)
        history_route_dir = str(history_route)

    manifest = {
        "run_id": ctx.run_id,
        "project_id": paths.project_id,
        "project_cwd": paths.project_cwd,
        "ran_at": ctx.now_iso,
        "route": route,
        "routes": [route],
        "session_files": ctx.diff.session_files,
        "new_sessions": ctx.diff.new_sessions,
        "changed_sessions": ctx.diff.changed_sessions,
        "unchanged_sessions": ctx.diff.unchanged_sessions,
        "deleted_sessions": ctx.diff.deleted_sessions,
        "processed_sessions": processed_sessions,
        "reused_latest": reused_latest,
        "latest_route_dir": str(latest_route_dir),
        "history_route_dir": history_route_dir,
        "session_cache_dir": str(paths.latest_route_sessions_dir(route)),
    }
    write_json(history_run_dir / "run_manifest.json", manifest)
    write_json(paths.latest_root / "run_manifest.json", manifest)
    write_json(paths.session_index_path, ctx.diff.entries)

    state = build_state(ctx.previous_state, paths, ctx.now_iso, ctx.run_id, route, ctx.diff.entries)
    write_json(paths.state_path, state)
    update_registry(paths, state)

    return {
        "run_id": ctx.run_id,
        "paths": {
            "project_root": str(paths.project_root),
            "latest_route_dir": str(latest_route_dir),
            "history_route_dir": history_route_dir,
            "state_path": str(paths.state_path),
            "session_index_path": str(paths.session_index_path),
            "registry_path": str(paths.registry_path),
            "session_cache_dir": str(paths.latest_route_sessions_dir(route)),
        },
        "manifest": manifest,
        "state": state,
    }
