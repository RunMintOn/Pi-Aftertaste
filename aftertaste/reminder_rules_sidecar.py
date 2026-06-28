#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_MODEL = "openai-codex/gpt-5.4-mini"
REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROMPT_PATH = REPO_ROOT / ".scratch" / "reminder-rules" / "prompt.md"
DEFAULT_RUNS_ROOT = REPO_ROOT / ".scratch" / "reminder-rules" / "runs"


def utc_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S-%fZ")


def load_prompt_template(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def build_runtime_prompt(prompt_template: str, guide_text: str) -> str:
    return (
        prompt_template.strip()
        + "\n\n---\n"
        + "下面是输入的 collaboration_guide.md，请只基于它生成 `reminder_rules.json`。\n\n"
        + guide_text.strip()
        + "\n"
    )


def extract_json_object(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.startswith("json"):
            stripped = stripped[4:].strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise
        return json.loads(stripped[start : end + 1])


def parse_pi_json_output(stdout: str) -> tuple[str, str | None, str | None]:
    deltas: list[str] = []
    final_text: str | None = None
    model: str | None = None
    error_summary: str | None = None
    for raw_line in stdout.splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        event_type = event.get("type")
        if event_type == "message_update":
            assistant_event = event.get("assistantMessageEvent", {}) or {}
            if assistant_event.get("type") == "text_delta":
                deltas.append(assistant_event.get("delta", ""))
            message = event.get("message", {}) or {}
            model = message.get("model", model)
        elif event_type == "message_end":
            message = event.get("message", {}) or {}
            if message.get("role") == "assistant":
                model = message.get("model", model)
                text_parts = [part.get("text", "") for part in message.get("content", []) if part.get("type") == "text"]
                if text_parts:
                    final_text = "".join(text_parts)
        elif event_type == "auto_retry_end" and not event.get("success", False):
            error_summary = event.get("finalError") or error_summary
        elif event_type == "compaction_end" and event.get("aborted"):
            error_summary = event.get("errorMessage") or error_summary
    text = final_text or "".join(deltas)
    return text, model, error_summary


def build_pi_cli_command(*, pi_command: str, model: str, thinking: str | None, session_dir: Path) -> list[str]:
    command = [
        pi_command,
        "--print",
        "--mode",
        "json",
        "--approve",
        "--model",
        model,
        "--no-session",
        "--session-dir",
        str(session_dir),
        "--no-extensions",
        "--no-skills",
        "--no-prompt-templates",
        "--no-themes",
        "--no-context-files",
        "--tools",
        "read",
    ]
    if thinking:
        command.extend(["--thinking", thinking])
    return command


def evidence_lines(guide_text: str, terms: list[str], limit: int = 3) -> list[str]:
    found: list[str] = []
    for line in guide_text.splitlines():
        compact = line.strip()
        if not compact:
            continue
        if any(term in compact for term in terms):
            found.append(compact)
        if len(found) >= limit:
            break
    return found


def has_any(text: str, terms: list[str]) -> bool:
    return any(term in text for term in terms)


def make_rule(
    *,
    rule_id: str,
    mode: str,
    must_match_any: list[str],
    must_match_all_groups: list[list[str]],
    anti_patterns: list[str],
    message: str,
    suggestion: str,
    evidence: list[str],
    cooldown: str,
    confidence: str,
) -> dict[str, Any]:
    return {
        "id": rule_id,
        "mode": mode,
        "must_match_any": must_match_any,
        "must_match_all_groups": must_match_all_groups,
        "anti_patterns": anti_patterns,
        "message": message,
        "suggestion": suggestion,
        "evidence": evidence,
        "cooldown": cooldown,
        "confidence": confidence,
    }


def fake_generate_reminder_rules(guide_text: str) -> dict[str, Any]:
    rules: list[dict[str, Any]] = []
    if has_any(guide_text, ["设计", "边界", "方案", "路线"]):
        rules.append(
            make_rule(
                rule_id="design-boundary-clarify",
                mode="notify",
                must_match_any=[],
                must_match_all_groups=[["设计", "方案", "边界", "路线"], ["判断", "确认", "评审", "先看", "先审"]],
                anti_patterns=["直接改", "直接实现", "不用讨论"],
                message="这类请求通常先确认：先审方案/边界，还是直接实现。",
                suggestion="可补一句：先判断目标和路线，再动手。",
                evidence=evidence_lines(guide_text, ["设计", "边界", "路线", "方案"], limit=3),
                cooldown="session_once",
                confidence="high",
            )
        )
    if has_any(guide_text, ["README", "包页", "pi.image", "徽章", "元数据", "预览图"]):
        rules.append(
            make_rule(
                rule_id="readme-display-scope-clarify",
                mode="notify",
                must_match_any=["README", "包页", "pi.image", "徽章", "元数据", "预览图"],
                must_match_all_groups=[],
                anti_patterns=["只看页面文案，不动发布物"],
                message="这类请求最好先分清：根 README、包内同步、pi.image、包页展示是否都要一起处理。",
                suggestion="可补一句：只改根 README，还是要连包内同步和 pi.image 一起处理？",
                evidence=evidence_lines(guide_text, ["README", "包页", "pi.image", "元数据"], limit=3),
                cooldown="turn_once",
                confidence="high",
            )
        )
        rules.append(
            make_rule(
                rule_id="preview-before-release",
                mode="notify",
                must_match_any=["README", "包页", "展示", "预览", "图片", "badge", "npm pack"],
                must_match_all_groups=[["提交", "发布", "push", "publish"]],
                anti_patterns=["不用预览", "直接发", "直接发布"],
                message="建议先预览 README / 包页效果，再决定是否提交或发布。",
                suggestion="可补一句：先预览，不要先提交/发布。",
                evidence=evidence_lines(guide_text, ["预览", "提交", "发布", "README", "包页"], limit=3),
                cooldown="turn_once",
                confidence="medium",
            )
        )
    if has_any(guide_text, ["push", "publish", "commit", "发布", "发版"]):
        rules.append(
            make_rule(
                rule_id="publish-confirm",
                mode="confirm",
                must_match_any=["push", "publish", "npm publish", "commit", "发版", "发布"],
                must_match_all_groups=[],
                anti_patterns=["不要 push", "不要 publish", "只本地", "先别发", "暂不发布"],
                message="检测到外部发布/推送动作。这类操作最好和本地修改分开确认。",
                suggestion="建议先明确：这次只本地修好，还是要一起 commit / push / publish？",
                evidence=evidence_lines(guide_text, ["push", "publish", "commit", "发布", "发版"], limit=3),
                cooldown="turn_once",
                confidence="high",
            )
        )
    if has_any(guide_text, ["网络", "HTTP", "curl", "ping"]):
        rules.append(
            make_rule(
                rule_id="network-validation-clarify",
                mode="notify",
                must_match_any=[],
                must_match_all_groups=[["网络", "联网", "HTTP", "curl", "ping"], ["验证", "测试", "检查", "可用", "能不能", "通不通"]],
                anti_patterns=["只改 network 配置说明", "不是在测网络"],
                message="网络验证最好先区分：你要确认的是 HTTP/curl，可不一定要用 ping。",
                suggestion="可补一句：这次是测 HTTP 可用，还是连 ping 也要确认？",
                evidence=evidence_lines(guide_text, ["网络", "HTTP", "curl", "ping"], limit=3),
                cooldown="turn_once",
                confidence="high",
            )
        )
    if has_any(guide_text, ["readonly", "workspace-write", "network", "沙盒", "权限模式"]):
        rules.append(
            make_rule(
                rule_id="sandbox-mode-clarify",
                mode="notify",
                must_match_any=["readonly", "workspace-write", "network", "沙盒", "权限模式"],
                must_match_all_groups=[["验证", "测试", "检查", "模式"]],
                anti_patterns=["只是在解释文档里的模式"],
                message="涉及沙盒/权限时，最好先说明要验证哪种模式和哪类命令。",
                suggestion="可补一句：这次测 readonly / workspace-write / network 哪一种？",
                evidence=evidence_lines(guide_text, ["readonly", "workspace-write", "network", "沙盒", "权限"], limit=3),
                cooldown="turn_once",
                confidence="medium",
            )
        )
    return {"version": "v1", "default_enabled": True, "rules": rules}


def run_pi_cli(*, prompt_text: str, run_root: Path, cwd: Path, pi_command: str, model: str, thinking: str | None) -> tuple[dict[str, Any], dict[str, Any]]:
    worker_session_dir = run_root / "worker_session"
    worker_session_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = run_root / "runtime.prompt.md"
    prompt_path.write_text(prompt_text, encoding="utf-8")
    command = build_pi_cli_command(pi_command=pi_command, model=model, thinking=thinking, session_dir=worker_session_dir) + [f"@{prompt_path}"]
    completed = subprocess.run(command, cwd=cwd, capture_output=True, text=True, check=False)
    (run_root / "stdout.jsonl").write_text(completed.stdout, encoding="utf-8")
    (run_root / "stderr.log").write_text(completed.stderr, encoding="utf-8")
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or f"pi exited with code {completed.returncode}")
    text, used_model, error_summary = parse_pi_json_output(completed.stdout)
    if not text.strip():
        raise RuntimeError(error_summary or "No assistant text captured from pi json output")
    parsed = extract_json_object(text)
    return parsed, {
        "runner": "pi-cli-json",
        "model": used_model or model,
        "thinking": thinking,
        "prompt_path": str(prompt_path),
        "stdout_path": str(run_root / "stdout.jsonl"),
        "stderr_path": str(run_root / "stderr.log"),
        "command": command,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate reminder_rules.json from a collaboration_guide.md sidecar flow")
    parser.add_argument("--guide", required=True, help="Path to collaboration_guide.md")
    parser.add_argument("--out", help="Path to write reminder_rules.json")
    parser.add_argument("--prompt-path", default=str(DEFAULT_PROMPT_PATH), help="Prompt markdown path")
    parser.add_argument("--runner", choices=["fake", "pi-cli-json"], default="fake")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--thinking")
    parser.add_argument("--pi-command", default="pi")
    parser.add_argument("--run-root", help="Run artifact directory")
    parser.add_argument("--cwd", help="Working directory for pi runner")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    guide_path = Path(args.guide).resolve()
    prompt_path = Path(args.prompt_path).resolve()
    run_root = Path(args.run_root).resolve() if args.run_root else DEFAULT_RUNS_ROOT / utc_run_id()
    run_root.mkdir(parents=True, exist_ok=True)
    out_path = Path(args.out).resolve() if args.out else run_root / "reminder_rules.json"
    cwd = Path(args.cwd).resolve() if args.cwd else guide_path.parent

    guide_text = guide_path.read_text(encoding="utf-8")
    prompt_template = load_prompt_template(prompt_path)
    runtime_prompt = build_runtime_prompt(prompt_template, guide_text)
    (run_root / "input.collaboration_guide.md").write_text(guide_text, encoding="utf-8")
    (run_root / "prompt.template.md").write_text(prompt_template, encoding="utf-8")
    (run_root / "runtime.prompt.md").write_text(runtime_prompt, encoding="utf-8")

    if args.runner == "fake":
        parsed = fake_generate_reminder_rules(guide_text)
        runner_meta = {
            "runner": "fake",
            "prompt_path": str(prompt_path),
        }
    else:
        parsed, runner_meta = run_pi_cli(
            prompt_text=runtime_prompt,
            run_root=run_root,
            cwd=cwd,
            pi_command=args.pi_command,
            model=args.model,
            thinking=args.thinking,
        )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(parsed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    manifest = {
        "guide_path": str(guide_path),
        "output_path": str(out_path),
        "prompt_path": str(prompt_path),
        "runner": args.runner,
        "cwd": str(cwd),
        "artifacts_root": str(run_root),
        "rule_count": len(parsed.get("rules", [])),
        "runner_meta": runner_meta,
    }
    (run_root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(str(out_path))


if __name__ == "__main__":
    main()
