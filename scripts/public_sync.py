#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable


IgnoreFn = Callable[[str, list[str]], set[str]]


def load_manifest(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def check_entry(root: Path, entry: dict[str, Any]) -> tuple[bool, str]:
    source = root / entry["source"]
    kind = entry.get("kind", "file")
    if kind == "file":
        return source.is_file(), str(source)
    if kind == "dir":
        return source.is_dir(), str(source)
    return False, str(source)


def render_plan(manifest_path: Path, manifest: dict[str, Any], root: Path, target_root: Path) -> str:
    lines: list[str] = []
    lines.append(f"manifest: {manifest_path}")
    lines.append(f"source_root: {root}")
    lines.append(f"target_root: {target_root}")
    lines.append(f"default_policy: {manifest.get('default_policy')}")
    lines.append(f"target_repo_suggestion: {manifest.get('target_repo_suggestion')}")
    ignore_names = manifest.get("ignore_names", [])
    ignore_suffixes = manifest.get("ignore_suffixes", [])
    if ignore_names or ignore_suffixes:
        lines.append(f"ignore_names: {ignore_names}")
        lines.append(f"ignore_suffixes: {ignore_suffixes}")
    lines.append("")
    lines.append("INCLUDE")
    for item in manifest.get("include", []):
        ok, resolved = check_entry(root, item)
        status = "ok" if ok else "missing"
        target = item.get("target", item["source"])
        reason = item.get("reason", "")
        extra = " follow_symlinks" if item.get("follow_symlinks") else ""
        lines.append(f"- [{status}] {item['source']} -> {target} ({item.get('kind', 'file')}{extra})")
        if reason:
            lines.append(f"    reason: {reason}")
        if not ok:
            lines.append(f"    resolved: {resolved}")
    lines.append("")
    lines.append("EXCLUDE")
    for item in manifest.get("exclude", []):
        lines.append(f"- {item['path']}")
        reason = item.get("reason", "")
        if reason:
            lines.append(f"    reason: {reason}")
    return "\n".join(lines)


def clean_target_dir(target_root: Path) -> None:
    target_root.mkdir(parents=True, exist_ok=True)
    for child in target_root.iterdir():
        if child.name == ".git":
            continue
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def build_ignore_fn(manifest: dict[str, Any]) -> IgnoreFn:
    ignore_names = set(manifest.get("ignore_names", []))
    ignore_suffixes = tuple(manifest.get("ignore_suffixes", []))

    def _ignore(_dir: str, names: list[str]) -> set[str]:
        ignored = {name for name in names if name in ignore_names}
        if ignore_suffixes:
            ignored.update({name for name in names if name.endswith(ignore_suffixes)})
        return ignored

    return _ignore


def copy_entry(source_root: Path, target_root: Path, entry: dict[str, Any], ignore_fn: IgnoreFn) -> None:
    source = source_root / entry["source"]
    target = target_root / entry.get("target", entry["source"])
    follow_symlinks = bool(entry.get("follow_symlinks", False))
    kind = entry.get("kind", "file")

    if kind == "file":
        ensure_parent(target)
        shutil.copy2(source, target, follow_symlinks=follow_symlinks)
        return

    if kind == "dir":
        if target.exists():
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            else:
                target.unlink()
        ensure_parent(target)
        shutil.copytree(
            source,
            target,
            symlinks=not follow_symlinks,
            dirs_exist_ok=False,
            copy_function=_copy_following(follow_symlinks),
            ignore=ignore_fn,
        )
        return

    raise ValueError(f"Unsupported kind: {kind}")


def _copy_following(follow_symlinks: bool):
    def _copy(src: str, dst: str) -> str:
        return shutil.copy2(src, dst, follow_symlinks=follow_symlinks)

    return _copy


def init_git_repo(path: Path) -> None:
    if (path / ".git").exists():
        return
    subprocess.run(["git", "init", str(path)], check=True)


def prune_target_noise(target_root: Path, manifest: dict[str, Any]) -> None:
    ignore_names = set(manifest.get("ignore_names", []))
    ignore_suffixes = tuple(manifest.get("ignore_suffixes", []))
    for path in sorted(target_root.rglob("*"), reverse=True):
        name = path.name
        if name == ".git":
            continue
        if name in ignore_names or (ignore_suffixes and name.endswith(ignore_suffixes)):
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)


def write_sync_readme(target_root: Path, manifest: dict[str, Any]) -> None:
    content = (
        "# Public sync output\n\n"
        "This directory is generated from the private Pi-Aftertaste repo via `public_sync.py`.\n"
        "Edit the private repo and its sync manifest instead of changing files here manually.\n\n"
        "Source of truth: `.scratch/public-sync/sync_manifest.json`\n"
        f"Suggested public repo name: `{manifest.get('target_repo_suggestion')}`\n"
    )
    (target_root / "SYNC_SOURCE.md").write_text(content, encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Preview or execute public repo sync from sync_manifest.json")
    parser.add_argument(
        "--manifest",
        default=".scratch/public-sync/sync_manifest.json",
        help="Path to sync manifest relative to pi-distill root",
    )
    parser.add_argument(
        "--target",
        help="Target public repo directory. Default: <repo_root>/<target_dir_default>",
    )
    parser.add_argument(
        "--fail-missing",
        action="store_true",
        help="Exit non-zero if any include path is missing",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually sync files to target root",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    repo_root = Path(__file__).resolve().parents[1]
    manifest_path = (repo_root / args.manifest).resolve()
    manifest = load_manifest(manifest_path)
    default_target = manifest.get("target_dir_default") or manifest.get("target_repo_suggestion") or "public-repo"
    target_root = Path(args.target).resolve() if args.target else (repo_root / default_target).resolve()

    plan = render_plan(manifest_path, manifest, repo_root, target_root)
    print(plan)

    missing = [item for item in manifest.get("include", []) if not check_entry(repo_root, item)[0]]
    if args.fail_missing and missing:
        sys.exit(1)

    if not args.apply:
        return

    if missing:
        print("\nRefusing to sync because some include paths are missing.", file=sys.stderr)
        sys.exit(1)

    if manifest.get("clean_target_before_sync", False):
        clean_target_dir(target_root)
    else:
        target_root.mkdir(parents=True, exist_ok=True)

    init_git_repo(target_root)
    ignore_fn = build_ignore_fn(manifest)
    for entry in manifest.get("include", []):
        copy_entry(repo_root, target_root, entry, ignore_fn)
    prune_target_noise(target_root, manifest)
    write_sync_readme(target_root, manifest)
    print(f"\nSynced public repo to: {target_root}")


if __name__ == "__main__":
    main()
