from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CHARS_PER_TOKEN = 4
TARGET_TOKENS = 60000
TARGET_MIN_TOKENS = 40000
SOFT_LIMIT_TOKENS = 80000
HARD_LIMIT_TOKENS = 90000
DOCUMENT_NAMES = [
    "project_context.md",
    "collaboration_guide.md",
    "patterns.md",
]


@dataclass(frozen=True)
class PipelineConfig:
    mode: str
    project_root: Path | None
    sessions_root: Path | None
    compiled_root: Path | None
    runtime_root: Path
    final_root: Path
    experiment_name: str
    runner: str
    prompt_version: str
    model: str | None
    thinking: str | None
    only_chunk: str | None
    analysis_only: bool
    pi_command: str
    write_final_to_project_root: bool
    agents_integration: str
    max_validation_retries: int
    validation_threshold: float
    repo_root: Path
    workspace_root: Path
    config_path: Path
    state_path: Path
    runs_root: Path

    @property
    def should_call_llm(self) -> bool:
        return self.mode != "dry-run"

    @property
    def should_run_synthesis(self) -> bool:
        return self.should_call_llm and not self.analysis_only and not self.only_chunk

    @property
    def model_or_default(self) -> str:
        return self.model or "openai-codex/gpt-5.4-mini"

    @property
    def thinking_or_default(self) -> str:
        return self.thinking or "medium"

    @property
    def chunk_policy(self) -> dict[str, int]:
        return {
            "chars_per_token": CHARS_PER_TOKEN,
            "target_tokens": TARGET_TOKENS,
            "target_min_tokens": TARGET_MIN_TOKENS,
            "soft_limit_tokens": SOFT_LIMIT_TOKENS,
            "hard_limit_tokens": HARD_LIMIT_TOKENS,
            "target_chars": TARGET_TOKENS * CHARS_PER_TOKEN,
            "target_min_chars": TARGET_MIN_TOKENS * CHARS_PER_TOKEN,
            "soft_limit_chars": SOFT_LIMIT_TOKENS * CHARS_PER_TOKEN,
            "hard_limit_chars": HARD_LIMIT_TOKENS * CHARS_PER_TOKEN,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "project_root": str(self.project_root) if self.project_root else None,
            "sessions_root": str(self.sessions_root) if self.sessions_root else None,
            "compiled_root": str(self.compiled_root) if self.compiled_root else None,
            "runtime_root": str(self.runtime_root),
            "final_root": str(self.final_root),
            "experiment_name": self.experiment_name,
            "runner": self.runner,
            "prompt_version": self.prompt_version,
            "model": self.model,
            "thinking": self.thinking,
            "only_chunk": self.only_chunk,
            "analysis_only": self.analysis_only,
            "pi_command": self.pi_command,
            "write_final_to_project_root": self.write_final_to_project_root,
            "agents_integration": self.agents_integration,
            "max_validation_retries": self.max_validation_retries,
            "validation_threshold": self.validation_threshold,
            "workspace_root": str(self.workspace_root),
            "config_path": str(self.config_path),
            "state_path": str(self.state_path),
            "runs_root": str(self.runs_root),
            "chunk_policy": self.chunk_policy,
        }


@dataclass(frozen=True)
class RunContext:
    config: PipelineConfig
    run_id: str
    started_at: str
    run_root: Path
    manifest_path: Path
    compiled_dir: Path
    chunk_dir: Path
    report_dir: Path
    synthesis_dir: Path
    validation_dir: Path

    def to_paths_dict(self) -> dict[str, str]:
        return {
            "run_root": str(self.run_root),
            "manifest_path": str(self.manifest_path),
            "compiled_dir": str(self.compiled_dir),
            "chunk_dir": str(self.chunk_dir),
            "report_dir": str(self.report_dir),
            "synthesis_dir": str(self.synthesis_dir),
            "validation_dir": str(self.validation_dir),
        }


@dataclass(frozen=True)
class RunnerRequest:
    task_type: str
    prompt_text: str
    input_text: str
    cwd: Path
    output_path: Path
    metadata: dict[str, Any]


@dataclass(frozen=True)
class RunnerResult:
    status: str
    text: str
    runner_type: str
    started_at: str
    ended_at: str
    model: str | None = None
    thinking: str | None = None
    error_summary: str | None = None
    artifacts: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "runner_type": self.runner_type,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "model": self.model,
            "thinking": self.thinking,
            "error_summary": self.error_summary,
            "artifacts": self.artifacts or {},
        }


@dataclass(frozen=True)
class ValidationResult:
    score: float
    passed: bool
    rubric: dict[str, float]
    critique: str
    needs_review: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "passed": self.passed,
            "rubric": self.rubric,
            "needs_review": self.needs_review,
        }


class DistillRunner:
    runner_type = "base"

    def run(self, request: RunnerRequest) -> RunnerResult:
        raise NotImplementedError


class FakeRunner(DistillRunner):
    runner_type = "fake"

    def __init__(self, validation_scores: list[float] | None = None):
        self.validation_scores = list(validation_scores or [42.5])

    def run(self, request: RunnerRequest) -> RunnerResult:
        started_at = utc_now_iso()
        if request.task_type == "chunk_analysis":
            text = self._chunk_report(request.metadata["chunk"])
        elif request.task_type == "synthesis_bundle":
            text = self._synthesis_bundle(
                request.metadata["used_chunks"],
                request.metadata.get("ignored_chunks", []),
                request.metadata.get("critique"),
            )
        elif request.task_type == "synthesis_self_validate":
            text = self._self_validation_output(
                attempt=request.metadata.get("attempt", 1),
                threshold=request.metadata.get("threshold", 35.0),
            )
        else:
            return RunnerResult(
                status="failed",
                text="",
                runner_type=self.runner_type,
                started_at=started_at,
                ended_at=utc_now_iso(),
                error_summary=f"Unsupported fake task_type: {request.task_type}",
            )
        return RunnerResult(
            status="completed",
            text=text,
            runner_type=self.runner_type,
            started_at=started_at,
            ended_at=utc_now_iso(),
            model="fake-deterministic",
            thinking="off",
        )

    def _chunk_report(self, chunk: dict[str, Any]) -> str:
        sessions = ", ".join(chunk.get("source_sessions", [])) or "none"
        referenced = head(chunk.get("referenced_paths", []), 6)
        changed = head(chunk.get("changed_paths", []), 6)
        commands = head(chunk.get("commands", []), 6)
        return (
            f"# Chunk Report: {chunk['chunk_id']}\n\n"
            "## Chunk story\n"
            f"- 当前 chunk 覆盖 `{chunk['start_time']}` 到 `{chunk['end_time']}`，source sessions: {sessions}。\n"
            f"- 相关性初判为 `{chunk['estimated_relevance']}`，后续 synthesis 应按这个权重看待。\n"
            f"- 明确改动路径数：{len(changed)}；明确引用路径数：{len(referenced)}；关键命令数：{len(commands)}。\n\n"
            "## Distill signals\n"
            f"### Signal 1：文件与命令轨迹适合作为项目事实候选\n"
            f"- 观察：当前 chunk 暴露了清晰的路径和命令轨迹。\n"
            f"- 证据：changed={changed or ['none']} referenced={referenced or ['none']} commands={commands or ['none']}。\n"
            f"- 含义：这些路径/命令能帮助 reduce 阶段判断项目工作面和验证方式。\n"
            "- 归宿：`project_context`\n"
            "- scope：`likely-project-level`\n"
            "- cross_chunk_need：需要；要看其他 chunk 是否重复出现。\n"
            "- priority_for_synthesis：high\n"
            f"- 建议写入：该项目在 `{', '.join((changed or referenced)[:3]) or '当前 chunk 记录的路径'}` 等路径上推进。\n"
            "- 置信度：中\n"
            "- 注意边界：不能仅凭单 chunk 就断言长期稳定模块边界。\n\n"
            f"### Signal 2：本 chunk 只提供局部证据，不能直接推出长期偏好\n"
            f"- 观察：当前 chunk 仍需要跨 chunk 复核。\n"
            f"- 证据：source_sessions={sessions}，estimated_relevance=`{chunk['estimated_relevance']}`。\n"
            "- 含义：reduce 阶段要区分单 chunk 现象和项目级事实。\n"
            "- 归宿：`discard`\n"
            "- scope：`chunk-local`\n"
            "- cross_chunk_need：不需要；它本身就是边界提醒。\n"
            "- priority_for_synthesis：low\n"
            "- 建议写入：当前 chunk 只能作为局部证据，不应单独升级为长期结论。\n"
            "- 置信度：高\n"
            "- 注意边界：不要把这条提醒写成项目事实。\n\n"
            "## User-facing opportunities\n"
            "- 建议：长任务开始前先明确本轮要关注的路径、命令和验证动作。\n"
            "- 依据：当前 chunk 的路径/命令轨迹可以直接帮助 reduce 阶段落项目事实。\n"
            "- 可直接复用的话术：这轮请重点关注这些文件/命令，其他部分先不要扩散。\n"
            "- agent 可代劳提醒：这轮要以哪些路径和验证命令作为主线？\n"
            "- 适用场景：多文件实现、发布、调试。\n"
            "- 注意边界：这是当前项目任务级建议，不是长期人格判断。\n\n"
            "## Not supported\n"
            "- 当前 chunk 不能单独支持长期用户偏好或跨项目模式结论。\n"
        )

    def _synthesis_bundle(
        self,
        used_chunks: list[dict[str, Any]],
        ignored_chunks: list[dict[str, Any]],
        critique: str | None,
    ) -> str:
        sessions = ordered_unique(flatten(chunk.get("source_sessions", []) for chunk in used_chunks))
        changed_paths = head(ordered_unique(flatten(chunk.get("changed_paths", []) for chunk in used_chunks)), 10)
        referenced_paths = head(ordered_unique(flatten(chunk.get("referenced_paths", []) for chunk in used_chunks)), 10)
        commands = head(ordered_unique(flatten(chunk.get("commands", []) for chunk in used_chunks)), 8)
        ignored_ids = [chunk["chunk_id"] for chunk in ignored_chunks]
        evidence = ", ".join(chunk["chunk_id"] for chunk in used_chunks) or "none"
        revision_note = "\n## Revision note\n- 本轮已根据上一轮 critique 收紧证据绑定。\n" if critique else ""
        docs = {
            "project_context.md": (
                "# Project Context\n\n"
                "## Overview\n"
                f"- 本文综合 chunk：{evidence}。\n"
                f"- 覆盖 sessions：{', '.join(sessions) or 'none'}。\n"
                "- 重要结论只基于当前 synthesis packet。\n\n"
                "## Goals and non-goals\n"
                f"- 当前工作面集中在：{', '.join((changed_paths or referenced_paths)[:5]) or 'none'}（evidence: {evidence}）。\n"
                f"- 低相关 chunk：{', '.join(ignored_ids) or 'none'}，只作弱参考。\n\n"
                "## Important files and modules\n"
                f"{format_bullets(changed_paths + referenced_paths, '- `{}`')}\n\n"
                "## Validation approach\n"
                f"{format_bullets(commands, '- `{}`')}\n\n"
                "## Known gotchas\n"
                "- 单 chunk 现象不能直接升级为项目级长期规律。\n"
                f"- 重要结论需要回绑 chunk evidence：{evidence}。\n"
                f"{revision_note}"
            ),
            "collaboration_guide.md": (
                "# Collaboration Guide\n\n"
                "## How to start\n"
                "- 先确认本轮是先判断路线，还是直接实现。\n"
                "- 开始前先圈定关键路径、命令和验证动作。\n\n"
                "## Clarify early\n"
                f"- 用户可直接说：这轮只看 `{', '.join((changed_paths or referenced_paths)[:3]) or '当前路径'}`，其他先不要扩散。\n"
                f"- Agent 应主动问：本轮以哪些命令作为验收？例如 {', '.join(commands[:3]) or '局部测试/检查命令'}。\n\n"
                "## Validation style\n"
                "- 每步做最小充分验证，并把证据留在 issue/manifest。\n"
                f"- 常见验证命令：{', '.join(commands[:5]) or 'none'}（evidence: {evidence}）。\n\n"
                "## Avoid friction\n"
                "- 不要在证据不足时把单次现象写成人格判断或长期偏好。\n"
                "- 不要把 chunk report 直接拼成最终文档；先综合再落三份文档。\n"
                f"{revision_note}"
            ),
            "patterns.md": (
                "# Patterns\n\n"
                "## Reusable patterns\n"
                "### Pattern：先保留 chunk 证据，再做 bundle 级综合\n"
                "- 适用场景：多 chunk 项目 distill。\n"
                "- 具体做法：先把路径、命令、边界判断保留在 chunk report，再在 reduce 阶段去重综合。\n"
                f"- 为什么有用：能把 evidence 和最终文档解耦，减少直接拼贴。\n"
                f"- 证据：{evidence}；paths={', '.join((changed_paths or referenced_paths)[:4]) or 'none'}。\n"
                "- 注意边界：如果 chunk 本身证据弱，pattern 也只能保守表述。\n\n"
                "### Pattern：最小充分验证优先于大范围重跑\n"
                "- 适用场景：pipeline、发布、局部修复。\n"
                f"- 具体做法：优先使用局部命令 {', '.join(commands[:4]) or 'none'} 做验证，再决定是否扩大。\n"
                "- 为什么有用：能降低成本并保留可追溯性。\n"
                f"- 证据：{evidence}。\n"
                "- 注意边界：前提是局部验证真的覆盖当前改动面。\n\n"
                "## Caveats\n"
                "- 这里的 pattern 仍来自单项目证据，迁移时要保留上下文。\n"
                f"{revision_note}"
            ),
        }
        return render_bundle_text(docs)

    def _self_validation_output(self, *, attempt: int, threshold: float) -> str:
        score = float(self.validation_scores[min(attempt - 1, len(self.validation_scores) - 1)])
        passed = score >= threshold
        failed = [] if passed else ["chunk_coverage", "grounding"]
        ratio = max(0.0, min(score / 50.0, 1.0))
        payload = {
            "raw_score": round(score, 2),
            "passed": passed,
            "dimension_scores": {
                "routing": round(8.0 * ratio, 2),
                "chunk_coverage": round(10.0 * ratio, 2),
                "cross_chunk_synthesis": round(8.0 * ratio, 2),
                "grounding": round(8.0 * ratio, 2),
                "specificity": round(6.0 * ratio, 2),
                "actionability": round(6.0 * ratio, 2),
                "boundary_control": round(4.0 * ratio, 2),
            },
            "failed_dimensions": failed,
            "must_fix": [] if passed else ["补回被低估 chunk 的高价值信号，尤其是项目主干而不是只写最新/最具体阶段。", "把关键结论更明确地绑定到 chunk/report 证据，并区分项目主干与阶段性工作。"],
            "which_chunk_underrepresented": [] if passed else ["chunk_001：项目主干、安全边界、验证主线信号被压弱。"],
            "which_chunk_overweighted": [] if passed else ["chunk_002：近期/具体工程问题权重过高，容易替代整个项目。"],
            "rewrite_direction": {
                "project_context.md": [] if passed else ["先补回稳定项目定位、安全边界与主设计，再写近期阶段性工作。"],
                "collaboration_guide.md": [] if passed else ["同时覆盖设计/边界确认、实现/验证、发布/展示三类协作阶段。"],
                "patterns.md": [] if passed else ["补回安全边界、真实验证、runtime 副作用处理等被压弱的模式，不要只写最新工程问题。"],
            },
            "summary": "通过" if passed else "未达阈值，需要在同一会话里按 critique 重写 bundle。",
        }
        return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


class PiCliJsonRunner(DistillRunner):
    runner_type = "pi-cli-json"

    def __init__(self, *, pi_command: str, model: str, thinking: str | None):
        self.pi_command = pi_command
        self.model = model
        self.thinking = thinking

    def run(self, request: RunnerRequest) -> RunnerResult:
        started_at = utc_now_iso()
        artifacts = report_artifact_paths(request.output_path)
        worker_session_dir = Path(request.metadata.get("worker_session_dir") or request.output_path.parent / "worker_sessions")
        worker_session_dir.mkdir(parents=True, exist_ok=True)
        prompt = build_real_runner_prompt(request)
        prompt_path = request.output_path.parent / f"{request.output_path.name}.prompt.md"
        prompt_path.write_text(prompt, encoding="utf-8")
        artifacts["prompt_path"] = str(prompt_path)
        command = build_pi_cli_command(
            pi_command=self.pi_command,
            model=self.model,
            thinking=self.thinking,
            session_dir=worker_session_dir,
            persist_session=bool(request.metadata.get("persist_session")),
            session_id=request.metadata.get("session_id"),
        ) + [f"@{prompt_path}"]
        completed = subprocess.run(
            command,
            cwd=request.cwd,
            capture_output=True,
            text=True,
            env=os.environ.copy(),
            timeout=60 * 20,
        )
        artifacts["command"] = command
        Path(artifacts["events_path"]).write_text(completed.stdout or "", encoding="utf-8")
        Path(artifacts["stderr_path"]).write_text(completed.stderr or "", encoding="utf-8")
        assistant_text, model_from_events, error_summary = parse_pi_json_output(completed.stdout)
        if completed.returncode != 0:
            error_summary = error_summary or normalize_whitespace(completed.stderr) or f"pi exited with code {completed.returncode}"
            return RunnerResult(
                status="failed",
                text=assistant_text,
                runner_type=self.runner_type,
                started_at=started_at,
                ended_at=utc_now_iso(),
                model=self.model or model_from_events,
                thinking=self.thinking,
                error_summary=error_summary,
                artifacts=artifacts,
            )
        if not assistant_text.strip():
            return RunnerResult(
                status="failed",
                text="",
                runner_type=self.runner_type,
                started_at=started_at,
                ended_at=utc_now_iso(),
                model=self.model or model_from_events,
                thinking=self.thinking,
                error_summary=error_summary or "No assistant text captured from pi json output",
                artifacts=artifacts,
            )
        return RunnerResult(
            status="completed",
            text=assistant_text,
            runner_type=self.runner_type,
            started_at=started_at,
            ended_at=utc_now_iso(),
            model=self.model or model_from_events,
            thinking=self.thinking,
            artifacts=artifacts,
        )


class DeterministicValidator:
    def validate(self, docs: dict[str, str], attempt: int, threshold: float) -> ValidationResult:
        rubric = {
            "groundedness": 9.0 if all("chunk" in text.lower() for text in docs.values()) else 6.5,
            "project_specificity": 9.0 if any("`" in text for text in docs.values()) else 6.5,
            "routing_correctness": 9.0 if self._is_distinct(docs) else 5.5,
            "actionability": 8.5 if "How to start" in docs.get("collaboration_guide.md", "") else 6.0,
            "non_overgeneralization": 8.5 if "Caveats" in docs.get("patterns.md", "") else 6.0,
        }
        score = round(sum(rubric.values()) / len(rubric), 2)
        passed = score >= threshold
        critique_lines = [f"# Validation critique (attempt {attempt})", ""]
        for name, value in rubric.items():
            critique_lines.append(f"- {name}: {value}")
        if passed:
            critique_lines.append("- 结论：通过。")
        else:
            critique_lines.append("- 结论：未达标，需按 rubric 补强。")
        return ValidationResult(
            score=score,
            passed=passed,
            rubric=rubric,
            critique="\n".join(critique_lines) + "\n",
            needs_review=not passed,
        )

    def _is_distinct(self, docs: dict[str, str]) -> bool:
        texts = [normalize_whitespace(value) for value in docs.values()]
        return len(set(texts)) == len(texts)


class SequenceValidator(DeterministicValidator):
    def __init__(self, scores: list[float]):
        self._scores = list(scores)

    def validate(self, docs: dict[str, str], attempt: int, threshold: float) -> ValidationResult:
        score = self._scores[min(attempt - 1, len(self._scores) - 1)]
        rubric = {
            "groundedness": score,
            "project_specificity": score,
            "routing_correctness": score,
            "actionability": score,
            "non_overgeneralization": score,
        }
        passed = score >= threshold
        critique = f"# Validation critique (attempt {attempt})\n\n- forced_score: {score}\n"
        return ValidationResult(score=score, passed=passed, rubric=rubric, critique=critique, needs_review=not passed)


class DistillPipeline:
    def __init__(
        self,
        config: PipelineConfig,
        *,
        runner: DistillRunner | None = None,
        validator: DeterministicValidator | None = None,
        run_id: str | None = None,
        started_at: str | None = None,
    ):
        if validator is not None:
            raise ValueError("validator override is no longer supported; reduce stage now uses same-session self-validation")
        self.config = config
        self.runner = runner or build_runner(config)
        self.run_id = run_id or utc_run_id()
        self.started_at = started_at or utc_now_iso()

    def run(self) -> dict[str, Any]:
        ctx = prepare_run_context(self.config, self.run_id, self.started_at)
        ensure_run_layout(self.config, ctx)
        state = load_json(self.config.state_path, self._initial_state())
        manifest = self._initial_manifest(ctx)
        write_json(self.config.config_path, self.config.to_dict())
        write_json(ctx.manifest_path, manifest)

        chunk_result = self._build_chunks(ctx)
        manifest["sessions"] = chunk_result["sessions"]
        manifest["chunks"] = chunk_result["chunks"]
        manifest["summary"].update(chunk_result["summary"])
        manifest["status"] = "chunks_built"
        write_json(ctx.manifest_path, manifest)

        if self.config.should_call_llm:
            analysis_summary = self._analyze_chunks(ctx, manifest, state)
            manifest["chunk_report_summary"] = analysis_summary
            manifest["status"] = "chunk_reports_done"
            write_json(ctx.manifest_path, manifest)

            if self.config.should_run_synthesis:
                synthesis_summary = self._synthesize_with_validation(ctx, manifest, state)
                manifest["synthesis"] = synthesis_summary["synthesis"]
                manifest["validation"] = synthesis_summary["validation"]
                manifest["status"] = synthesis_summary["status"]
                if self.config.agents_integration == "write":
                    manifest["agents_integration"] = apply_agents_reference(self.config)
            else:
                manifest["status"] = "analysis_only_completed"
                manifest["synthesis"] = {
                    "status": "skipped",
                    "reason": analysis_skip_reason(self.config),
                }
            write_json(ctx.manifest_path, manifest)

        finished_at = utc_now_iso()
        manifest["finished_at"] = finished_at
        state["config"] = self.config.to_dict()
        state["last_run_id"] = self.run_id
        state["updated_at"] = finished_at
        state.setdefault("runs", []).append(
            {
                "run_id": self.run_id,
                "started_at": self.started_at,
                "finished_at": finished_at,
                "mode": self.config.mode,
                "status": manifest["status"],
            }
        )
        write_json(self.config.state_path, state)
        write_json(ctx.manifest_path, manifest)
        return manifest

    def _initial_state(self) -> dict[str, Any]:
        return {
            "created_at": self.started_at,
            "updated_at": self.started_at,
            "last_run_id": None,
            "config": self.config.to_dict(),
            "chunks": {},
            "synthesis": {},
            "validation": {},
            "runs": [],
        }

    def _initial_manifest(self, ctx: RunContext) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "mode": self.config.mode,
            "started_at": self.started_at,
            "finished_at": None,
            "status": "initialized",
            "resolved_config": self.config.to_dict(),
            "paths": ctx.to_paths_dict(),
            "summary": {},
            "sessions": [],
            "chunks": [],
        }

    def _build_chunks(self, ctx: RunContext) -> dict[str, Any]:
        sessions = load_compiled_sessions(self.config)
        chunks = build_chunks_from_sessions(sessions, self.config.chunk_policy)
        chunk_ids = [chunk["chunk_id"] for chunk in chunks]
        if self.config.only_chunk and self.config.only_chunk not in chunk_ids:
            raise ValueError(f"Requested --only-chunk {self.config.only_chunk} not found; available: {', '.join(chunk_ids)}")
        for chunk in chunks:
            chunk_path = ctx.chunk_dir / f"{chunk['chunk_id']}.md"
            chunk["chunk_path"] = str(chunk_path)
            chunk_path.write_text(render_chunk_file(chunk), encoding="utf-8")
        return {
            "sessions": [session_manifest_entry(session) for session in sessions],
            "chunks": chunks,
            "summary": {
                "session_count": len(sessions),
                "chunk_count": len(chunks),
                "chunk_policy": self.config.chunk_policy,
                "only_chunk": self.config.only_chunk,
                "analysis_only": self.config.analysis_only,
            },
        }

    def _analyze_chunks(self, ctx: RunContext, manifest: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        completed = 0
        cached = 0
        failed = 0
        skipped = 0
        selected = 0
        worker_session_dir = ctx.run_root / "worker_sessions"
        worker_session_dir.mkdir(parents=True, exist_ok=True)
        for chunk in manifest["chunks"]:
            report_path = ctx.report_dir / f"{chunk['chunk_id']}.report.md"
            if self.config.only_chunk and chunk["chunk_id"] != self.config.only_chunk:
                chunk["report"] = {
                    "path": str(report_path),
                    "status": "not_selected",
                    "prompt_version": self.config.prompt_version,
                    "runner": {},
                }
                skipped += 1
                continue

            selected += 1
            cache_entry = state.setdefault("chunks", {}).get(chunk["chunk_id"], {})
            runner_cache = cache_entry.get("runner", {})
            cached_ok = (
                cache_entry.get("content_hash") == chunk["content_hash"]
                and cache_entry.get("prompt_version") == self.config.prompt_version
                and cache_entry.get("status") == "completed"
                and runner_cache.get("runner_type") == self.runner.runner_type
                and (
                    self.runner.runner_type != "pi-cli-json"
                    or (
                        runner_cache.get("model") == self.config.model_or_default
                        and runner_cache.get("thinking") == self.config.thinking_or_default
                    )
                )
                and cache_entry.get("report_path")
                and Path(cache_entry["report_path"]).exists()
            )
            if cached_ok:
                shutil.copy2(cache_entry["report_path"], report_path)
                chunk["report"] = {
                    "path": str(report_path),
                    "status": "cached",
                    "prompt_version": self.config.prompt_version,
                    "runner": cache_entry.get("runner", {}),
                    "source_report_path": cache_entry.get("report_path"),
                }
                cached += 1
                continue

            request = RunnerRequest(
                task_type="chunk_analysis",
                prompt_text=load_chunk_prompt(
                    self.config.repo_root,
                    chunk_path=chunk["chunk_path"],
                    prompt_version=self.config.prompt_version,
                ),
                input_text=Path(chunk["chunk_path"]).read_text(encoding="utf-8"),
                cwd=self.config.project_root or self.config.repo_root,
                output_path=report_path,
                metadata={
                    "chunk": chunk,
                    "prompt_version": self.config.prompt_version,
                    "worker_session_dir": str(worker_session_dir),
                },
            )
            result = self.runner.run(request)
            if result.status == "completed":
                report_path.write_text(result.text, encoding="utf-8")
                chunk["report"] = {
                    "path": str(report_path),
                    "status": "completed",
                    "prompt_version": self.config.prompt_version,
                    "runner": result.to_dict(),
                }
                state["chunks"][chunk["chunk_id"]] = {
                    "content_hash": chunk["content_hash"],
                    "prompt_version": self.config.prompt_version,
                    "report_path": str(report_path),
                    "status": "completed",
                    "runner": result.to_dict(),
                }
                completed += 1
            else:
                report_path.write_text(
                    f"# Chunk Report Failure: {chunk['chunk_id']}\n\n- error: {result.error_summary or 'unknown'}\n",
                    encoding="utf-8",
                )
                chunk["report"] = {
                    "path": str(report_path),
                    "status": "failed",
                    "prompt_version": self.config.prompt_version,
                    "runner": result.to_dict(),
                }
                state["chunks"][chunk["chunk_id"]] = {
                    "content_hash": chunk["content_hash"],
                    "prompt_version": self.config.prompt_version,
                    "report_path": str(report_path),
                    "status": "failed",
                    "runner": result.to_dict(),
                }
                failed += 1
        return {"selected": selected, "completed": completed, "cached": cached, "failed": failed, "skipped": skipped}

    def _synthesize_with_validation(self, ctx: RunContext, manifest: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        used_chunks, ignored_chunks = select_synthesis_chunks(manifest["chunks"])
        if not used_chunks:
            raise ValueError("No completed chunk reports available for synthesis")

        attempts: list[dict[str, Any]] = []
        critique: str | None = None
        final_validation: ValidationResult | None = None
        final_validation_payload: dict[str, Any] = {}
        latest_bundle_text = ""
        latest_bundle_path: Path | None = None
        latest_docs: dict[str, str] = {}
        used_report_paths = [chunk.get("report", {}).get("path") for chunk in used_chunks if chunk.get("report", {}).get("path")]
        ignored_report_paths = [chunk.get("report", {}).get("path") for chunk in ignored_chunks if chunk.get("report", {}).get("path")]
        session_dir = ctx.synthesis_dir / "worker_sessions"
        session_dir.mkdir(parents=True, exist_ok=True)
        session_id = stable_session_id(f"distill-reduce-{self.run_id}")
        synthesis_input = build_synthesis_input(used_chunks, ignored_chunks)
        status = "synthesized"

        for attempt in range(1, self.config.max_validation_retries + 2):
            bundle_attempt_path = ctx.synthesis_dir / f"bundle_attempt_{attempt:03d}.md"
            generation_request = RunnerRequest(
                task_type="synthesis_bundle",
                prompt_text=build_bundle_synthesis_prompt(self.config.repo_root, critique),
                input_text=synthesis_input if attempt == 1 else "",
                cwd=self.config.project_root or self.config.repo_root,
                output_path=bundle_attempt_path,
                metadata={
                    "attempt": attempt,
                    "used_chunks": used_chunks,
                    "ignored_chunks": ignored_chunks,
                    "critique": critique,
                    "prompt_version": self.config.prompt_version,
                    "worker_session_dir": str(session_dir),
                    "persist_session": True,
                    "session_id": session_id,
                },
            )
            generation_result = self.runner.run(generation_request)
            if generation_result.status != "completed":
                raise RuntimeError(generation_result.error_summary or "Failed to generate synthesis bundle")
            bundle_attempt_path.write_text(generation_result.text, encoding="utf-8")
            docs = split_bundle_text(generation_result.text)
            latest_bundle_text = generation_result.text
            latest_bundle_path = bundle_attempt_path
            latest_docs = docs

            validation_raw_path = ctx.validation_dir / f"attempt_{attempt:03d}.self_validation.raw.txt"
            validation_json_path = ctx.validation_dir / f"attempt_{attempt:03d}.self_validation.json"
            critique_path = ctx.validation_dir / f"attempt_{attempt:03d}.critique.md"
            validation_request = RunnerRequest(
                task_type="synthesis_self_validate",
                prompt_text=build_self_validation_prompt(
                    self.config.repo_root,
                    self.config.validation_threshold,
                    self.config.max_validation_retries,
                ),
                input_text="",
                cwd=self.config.project_root or self.config.repo_root,
                output_path=validation_raw_path,
                metadata={
                    "attempt": attempt,
                    "threshold": self.config.validation_threshold,
                    "prompt_version": self.config.prompt_version,
                    "worker_session_dir": str(session_dir),
                    "persist_session": True,
                    "session_id": session_id,
                    "bundle_text": generation_result.text,
                },
            )
            validation_runner_result = self.runner.run(validation_request)
            if validation_runner_result.status != "completed":
                raise RuntimeError(validation_runner_result.error_summary or "Failed to self-validate synthesis bundle")
            validation_raw_path.write_text(validation_runner_result.text, encoding="utf-8")
            validation, validation_payload = parse_self_validation_response(
                validation_runner_result.text,
                self.config.validation_threshold,
            )
            final_validation = validation
            final_validation_payload = validation_payload
            write_json(validation_json_path, validation_payload)
            critique_path.write_text(validation.critique, encoding="utf-8")
            attempts.append(
                {
                    "attempt": attempt,
                    "bundle_path": str(bundle_attempt_path),
                    "validation_raw_path": str(validation_raw_path),
                    "validation_path": str(validation_json_path),
                    "critique_path": str(critique_path),
                    "score": validation.score,
                    "passed": validation.passed,
                    "rubric": validation.rubric,
                    "generation_runner": generation_result.to_dict(),
                    "validation_runner": validation_runner_result.to_dict(),
                    "failed_dimensions": validation_payload.get("failed_dimensions", []),
                    "must_fix": validation_payload.get("must_fix", []),
                    "rewrite_direction": validation_payload.get("rewrite_direction", {}),
                }
            )
            write_json(
                ctx.validation_dir / "score.json",
                {
                    "attempts": attempts,
                    "final": validation.to_dict(),
                    "final_payload": validation_payload,
                    "threshold": self.config.validation_threshold,
                    "max_retries": self.config.max_validation_retries,
                },
            )
            (ctx.validation_dir / "critique.md").write_text(validation.critique, encoding="utf-8")
            if validation.passed:
                status = "completed"
                state["validation"] = {
                    "score": validation.score,
                    "passed": True,
                    "attempts": attempts,
                    "needs_review": False,
                }
                break
            critique = validation.critique
            if attempt > self.config.max_validation_retries:
                status = "needs_review"
                state["validation"] = {
                    "score": validation.score,
                    "passed": False,
                    "attempts": attempts,
                    "needs_review": True,
                }
                break

        if latest_bundle_path is None:
            raise RuntimeError("Synthesis bundle was not generated")

        final_bundle_path = ctx.synthesis_dir / "bundle.md"
        final_bundle_path.write_text(latest_bundle_text, encoding="utf-8")
        self._publish_final_docs(ctx, latest_docs)

        state["synthesis"] = {
            "run_id": self.run_id,
            "session_id": session_id,
            "session_dir": str(session_dir),
            "used_chunk_ids": [chunk["chunk_id"] for chunk in used_chunks],
            "used_report_paths": used_report_paths,
            "ignored_chunk_ids": [chunk["chunk_id"] for chunk in ignored_chunks],
            "ignored_report_paths": ignored_report_paths,
            "status": status,
            "prompt_version": self.config.prompt_version,
            "bundle_path": str(final_bundle_path),
            "documents": {name: str(ctx.synthesis_dir / name) for name in DOCUMENT_NAMES},
            "attempts": attempts,
        }
        return {
            "status": status,
            "synthesis": {
                "session_id": session_id,
                "session_dir": str(session_dir),
                "used_chunk_ids": [chunk["chunk_id"] for chunk in used_chunks],
                "used_report_paths": used_report_paths,
                "ignored_chunk_ids": [chunk["chunk_id"] for chunk in ignored_chunks],
                "ignored_report_paths": ignored_report_paths,
                "prompt_version": self.config.prompt_version,
                "bundle_path": str(final_bundle_path),
                "documents": {name: str(ctx.synthesis_dir / name) for name in DOCUMENT_NAMES},
                "final_documents": {name: str(self.config.final_root / name) for name in DOCUMENT_NAMES},
                "attempts": attempts,
            },
            "validation": {
                "score_path": str(ctx.validation_dir / "score.json"),
                "critique_path": str(ctx.validation_dir / "critique.md"),
                "attempts": attempts,
                "final_score": final_validation.score if final_validation else None,
                "final_passed": final_validation.passed if final_validation else None,
                "final_payload": final_validation_payload,
                "needs_review": status == "needs_review",
            },
        }

    def _publish_final_docs(self, ctx: RunContext, docs: dict[str, str]) -> None:
        for document_name, text in docs.items():
            output_path = ctx.synthesis_dir / document_name
            output_path.write_text(text, encoding="utf-8")
            self.config.final_root.mkdir(parents=True, exist_ok=True)
            shutil.copy2(output_path, self.config.final_root / document_name)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the project distill pipeline")
    parser.add_argument("--mode", choices=["experiment", "project", "dry-run"], default=os.environ.get("PI_DISTILL_MODE", "experiment"))
    parser.add_argument("--project-root", default=os.environ.get("PI_DISTILL_PROJECT_ROOT"))
    parser.add_argument("--sessions-root", default=os.environ.get("PI_DISTILL_SESSIONS_ROOT"))
    parser.add_argument("--compiled-root", default=os.environ.get("PI_DISTILL_COMPILED_ROOT"))
    parser.add_argument("--runtime-root", default=os.environ.get("PI_DISTILL_RUNTIME_ROOT"))
    parser.add_argument("--final-root", default=os.environ.get("PI_DISTILL_FINAL_ROOT"))
    parser.add_argument("--experiment-name", default=os.environ.get("PI_DISTILL_EXPERIMENT_NAME", "default"))
    parser.add_argument("--runner", default=os.environ.get("PI_DISTILL_RUNNER", "fake"))
    parser.add_argument("--prompt-version", default=os.environ.get("PI_DISTILL_PROMPT_VERSION", "v1"))
    parser.add_argument("--model", default=os.environ.get("PI_DISTILL_MODEL"))
    parser.add_argument("--thinking", default=os.environ.get("PI_DISTILL_THINKING"))
    parser.add_argument("--only-chunk", default=os.environ.get("PI_DISTILL_ONLY_CHUNK"))
    parser.add_argument("--analysis-only", action="store_true", default=env_flag("PI_DISTILL_ANALYSIS_ONLY"))
    parser.add_argument("--pi-command", default=os.environ.get("PI_DISTILL_PI_COMMAND", "pi"))
    parser.add_argument("--write-final-to-project-root", action="store_true")
    parser.add_argument("--agents-integration", choices=["off", "write"], default=os.environ.get("PI_DISTILL_AGENTS_INTEGRATION", "off"))
    parser.add_argument("--max-validation-retries", type=int, default=int(os.environ.get("PI_DISTILL_MAX_VALIDATION_RETRIES", "2")))
    parser.add_argument("--validation-threshold", type=float, default=float(os.environ.get("PI_DISTILL_VALIDATION_THRESHOLD", "35.0")))
    return parser.parse_args(argv)


def resolve_config(args: argparse.Namespace) -> PipelineConfig:
    repo_root = Path(__file__).resolve().parent
    project_root = Path(args.project_root).expanduser().resolve() if args.project_root else None
    sessions_root = Path(args.sessions_root).expanduser().resolve() if args.sessions_root else None
    compiled_root = Path(args.compiled_root).expanduser().resolve() if args.compiled_root else None

    if not project_root and compiled_root:
        inferred = infer_project_root_from_manifest(compiled_root, repo_root)
        if inferred:
            project_root = inferred

    experiment_root = repo_root / "experiments" / args.experiment_name
    if args.mode in {"experiment", "dry-run"}:
        workspace_root = experiment_root
        runtime_root = Path(args.runtime_root).expanduser().resolve() if args.runtime_root else (workspace_root / "runtime").resolve()
        final_root = Path(args.final_root).expanduser().resolve() if args.final_root else (workspace_root / "final").resolve()
    else:
        if not project_root:
            raise ValueError("--project-root is required for project mode when it cannot be inferred")
        workspace_root = (project_root / ".pi-distill").resolve()
        runtime_root = Path(args.runtime_root).expanduser().resolve() if args.runtime_root else workspace_root
        if args.write_final_to_project_root:
            final_root = project_root
        else:
            final_root = Path(args.final_root).expanduser().resolve() if args.final_root else (runtime_root / "final").resolve()

    return PipelineConfig(
        mode=args.mode,
        project_root=project_root,
        sessions_root=sessions_root,
        compiled_root=compiled_root,
        runtime_root=runtime_root,
        final_root=final_root,
        experiment_name=args.experiment_name,
        runner=args.runner,
        prompt_version=args.prompt_version,
        model=args.model,
        thinking=args.thinking,
        only_chunk=args.only_chunk,
        analysis_only=args.analysis_only,
        pi_command=args.pi_command,
        write_final_to_project_root=args.write_final_to_project_root,
        agents_integration=args.agents_integration,
        max_validation_retries=args.max_validation_retries,
        validation_threshold=args.validation_threshold,
        repo_root=repo_root,
        workspace_root=workspace_root,
        config_path=workspace_root / "config.json",
        state_path=runtime_root / "state.json",
        runs_root=runtime_root / "runs",
    )


def prepare_run_context(config: PipelineConfig, run_id: str, started_at: str) -> RunContext:
    run_root = config.runs_root / run_id
    return RunContext(
        config=config,
        run_id=run_id,
        started_at=started_at,
        run_root=run_root,
        manifest_path=run_root / "manifest.json",
        compiled_dir=run_root / "compiled",
        chunk_dir=run_root / "session_chunks",
        report_dir=run_root / "chunk_reports",
        synthesis_dir=run_root / "synthesis",
        validation_dir=run_root / "validation",
    )


def ensure_run_layout(config: PipelineConfig, ctx: RunContext) -> None:
    config.workspace_root.mkdir(parents=True, exist_ok=True)
    config.runtime_root.mkdir(parents=True, exist_ok=True)
    config.runs_root.mkdir(parents=True, exist_ok=True)
    config.final_root.mkdir(parents=True, exist_ok=True)
    for path in [ctx.run_root, ctx.compiled_dir, ctx.chunk_dir, ctx.report_dir, ctx.synthesis_dir, ctx.validation_dir]:
        path.mkdir(parents=True, exist_ok=True)


def build_runner(config: PipelineConfig) -> DistillRunner:
    if config.runner == "fake":
        return FakeRunner()
    if config.runner == "pi-cli-json":
        return PiCliJsonRunner(
            pi_command=config.pi_command,
            model=config.model_or_default,
            thinking=config.thinking_or_default,
        )
    raise ValueError(f"Unsupported runner: {config.runner}")


def load_compiled_sessions(config: PipelineConfig) -> list[dict[str, Any]]:
    if not config.compiled_root:
        raise ValueError("--compiled-root is required for deterministic chunk building")
    compiled_root = config.compiled_root
    manifest_path = compiled_root.parent / "manifest.json"
    items: list[dict[str, Any]] = []
    if manifest_path.exists():
        manifest = load_json(manifest_path, {})
        for item in manifest.get("items", []):
            name = item.get("name")
            outputs = item.get("outputs", {})
            min_path = resolve_artifact_path(outputs.get("min"), config.repo_root, manifest_path.parent)
            txt_path = resolve_artifact_path(outputs.get("txt"), config.repo_root, manifest_path.parent)
            if not name or not min_path.exists():
                continue
            items.append(build_session_record(name, min_path, txt_path, item.get("source_jsonl")))
    if items:
        return sorted(items, key=lambda item: item["start_time"])

    for min_path in sorted(compiled_root.glob("*.min.txt")):
        txt_path = min_path.with_suffix("").with_suffix(".txt")
        items.append(build_session_record(min_path.name[:-8], min_path, txt_path if txt_path.exists() else None, None))
    return sorted(items, key=lambda item: item["start_time"])


def build_session_record(name: str, min_path: Path, txt_path: Path | None, source_jsonl: str | None) -> dict[str, Any]:
    text = min_path.read_text(encoding="utf-8")
    line_count = text.count("\n") + 1 if text else 0
    blocks = split_blocks(text)
    start_time = name.split("_", 1)[0]
    referenced_paths, changed_paths, commands = extract_trace_metadata(text)
    return {
        "session_name": name,
        "start_time": start_time,
        "end_time": start_time,
        "min_path": str(min_path.resolve()),
        "txt_path": str(txt_path.resolve()) if txt_path and txt_path.exists() else None,
        "source_jsonl": source_jsonl,
        "char_count": len(text),
        "line_count": line_count,
        "estimated_tokens": estimate_tokens(len(text)),
        "text": text,
        "blocks": blocks,
        "referenced_paths": referenced_paths,
        "changed_paths": changed_paths,
        "commands": commands,
    }


def split_blocks(text: str) -> list[dict[str, Any]]:
    if not text:
        return []
    lines = text.splitlines()
    starts = [index for index, line in enumerate(lines) if re.match(r"^\[(user|assistant)\]", line.strip())]
    if not starts or starts[0] != 0:
        starts = [0] + starts
    starts = sorted(set(starts))
    blocks: list[dict[str, Any]] = []
    for idx, start in enumerate(starts):
        end = starts[idx + 1] - 1 if idx + 1 < len(starts) else len(lines) - 1
        block_lines = lines[start : end + 1]
        block_text = "\n".join(block_lines)
        if block_text and text.endswith("\n"):
            block_text += "\n"
        blocks.append(
            {
                "line_start": start + 1,
                "line_end": end + 1,
                "text": block_text,
                "char_count": len(block_text),
            }
        )
    return blocks


def build_chunks_from_sessions(sessions: list[dict[str, Any]], policy: dict[str, int]) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    for session in sessions:
        segments.extend(split_session_segments(session, policy["hard_limit_chars"]))

    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_chars = 0
    for segment in segments:
        segment_chars = segment["char_count"]
        next_is_new_session = bool(current and current[-1]["session_name"] != segment["session_name"])
        if current:
            if next_is_new_session and current_chars >= policy["target_min_chars"]:
                chunks.append(current)
                current = []
                current_chars = 0
            elif current_chars + segment_chars > policy["soft_limit_chars"]:
                chunks.append(current)
                current = []
                current_chars = 0
        if current and current_chars + segment_chars > policy["hard_limit_chars"]:
            chunks.append(current)
            current = []
            current_chars = 0
        current.append(segment)
        current_chars += segment_chars
    if current:
        chunks.append(current)

    out: list[dict[str, Any]] = []
    for index, segments_in_chunk in enumerate(chunks, start=1):
        chunk_id = f"chunk_{index:03d}"
        chunk = build_chunk_record(chunk_id, segments_in_chunk)
        out.append(chunk)
    return out


def split_session_segments(session: dict[str, Any], hard_limit_chars: int) -> list[dict[str, Any]]:
    if session["char_count"] <= hard_limit_chars or not session["blocks"]:
        return [
            {
                **session,
                "source_range": {"line_start": 1, "line_end": session["line_count"]},
                "segment_text": session["text"],
            }
        ]

    segments: list[dict[str, Any]] = []
    current_blocks: list[dict[str, Any]] = []
    current_chars = 0
    for block in session["blocks"]:
        if current_blocks and current_chars + block["char_count"] > hard_limit_chars:
            segments.append(build_segment_from_blocks(session, current_blocks))
            current_blocks = []
            current_chars = 0
        current_blocks.append(block)
        current_chars += block["char_count"]
    if current_blocks:
        segments.append(build_segment_from_blocks(session, current_blocks))
    return segments


def build_segment_from_blocks(session: dict[str, Any], blocks: list[dict[str, Any]]) -> dict[str, Any]:
    text = "".join(block["text"] for block in blocks)
    return {
        **session,
        "source_range": {"line_start": blocks[0]["line_start"], "line_end": blocks[-1]["line_end"]},
        "segment_text": text,
        "char_count": len(text),
        "line_count": sum(block["line_end"] - block["line_start"] + 1 for block in blocks),
        "estimated_tokens": estimate_tokens(len(text)),
    }


def build_chunk_record(chunk_id: str, segments: list[dict[str, Any]]) -> dict[str, Any]:
    chunk_text = render_chunk_body(chunk_id, segments)
    referenced_paths = ordered_unique(flatten(segment.get("referenced_paths", []) for segment in segments))
    changed_paths = ordered_unique(flatten(segment.get("changed_paths", []) for segment in segments))
    commands = ordered_unique(flatten(segment.get("commands", []) for segment in segments))
    unique_sessions = ordered_unique(segment["session_name"] for segment in segments)
    source_files = ordered_unique(
        [segment.get("source_jsonl") for segment in segments if segment.get("source_jsonl")]
        or [segment["min_path"] for segment in segments]
    )
    source_ranges = [
        {
            "session_name": segment["session_name"],
            "min_path": segment["min_path"],
            "txt_path": segment.get("txt_path"),
            "line_start": segment["source_range"]["line_start"],
            "line_end": segment["source_range"]["line_end"],
        }
        for segment in segments
    ]
    char_count = len(chunk_text)
    return {
        "chunk_id": chunk_id,
        "source_sessions": unique_sessions,
        "source_files": source_files,
        "start_time": segments[0]["start_time"],
        "end_time": segments[-1]["end_time"],
        "line_count": chunk_text.count("\n") + 1 if chunk_text else 0,
        "char_count": char_count,
        "estimated_tokens": estimate_tokens(char_count),
        "source_ranges": source_ranges,
        "content_hash": sha256_text(chunk_text),
        "referenced_paths": referenced_paths,
        "changed_paths": changed_paths,
        "commands": commands,
        "estimated_relevance": estimate_relevance(referenced_paths, changed_paths, commands, char_count),
        "content": chunk_text,
    }


def render_chunk_body(chunk_id: str, segments: list[dict[str, Any]]) -> str:
    lines = [f"# {chunk_id}", "", "## Sources", ""]
    for segment in segments:
        lines.extend(
            [
                f"### Session {segment['session_name']}",
                f"- min: `{segment['min_path']}`",
                f"- txt: `{segment.get('txt_path') or 'n/a'}`",
                f"- line_range: `{segment['source_range']['line_start']}-{segment['source_range']['line_end']}`",
                "",
                "```text",
                segment["segment_text"].rstrip("\n"),
                "```",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def render_chunk_file(chunk: dict[str, Any]) -> str:
    return chunk.pop("content")


def session_manifest_entry(session: dict[str, Any]) -> dict[str, Any]:
    return {
        "session_name": session["session_name"],
        "start_time": session["start_time"],
        "end_time": session["end_time"],
        "min_path": session["min_path"],
        "txt_path": session.get("txt_path"),
        "source_jsonl": session.get("source_jsonl"),
        "char_count": session["char_count"],
        "line_count": session["line_count"],
        "estimated_tokens": session["estimated_tokens"],
    }


def prompt_docs_dir(repo_root: Path) -> Path:
    return repo_root / "docs" / "prompts"


def load_prompt_template(repo_root: Path, filename: str) -> str:
    return (prompt_docs_dir(repo_root) / filename).read_text(encoding="utf-8")


def render_prompt_template(template: str, values: dict[str, str]) -> str:
    rendered = template
    for key, value in values.items():
        rendered = rendered.replace("{{" + key + "}}", value)
    return rendered


def bundle_format_template() -> str:
    return (
        "=== project_context.md ===\n"
        "...markdown...\n\n"
        "=== collaboration_guide.md ===\n"
        "...markdown...\n\n"
        "=== patterns.md ===\n"
        "...markdown...\n"
    )


def load_chunk_prompt(repo_root: Path, *, chunk_path: str, prompt_version: str) -> str:
    template = load_prompt_template(repo_root, "session_analysis_prompt.md")
    rendered = render_prompt_template(
        template,
        {
            "CHUNK_FILE": chunk_path,
        },
    )
    return rendered.rstrip() + f"\n\n---\nPrompt version: {prompt_version}\n"


def build_bundle_synthesis_prompt(repo_root: Path, critique: str | None) -> str:
    template = load_prompt_template(repo_root, "build_synthesis_prompt.md")
    critique_block = critique.strip() if critique else "（本轮无 critique；直接生成首版 bundle。）"
    return render_prompt_template(
        template,
        {
            "CRITIQUE_BLOCK": critique_block,
            "BUNDLE_FORMAT": bundle_format_template().rstrip(),
        },
    )


def build_self_validation_prompt(repo_root: Path, threshold: float, max_retries: int) -> str:
    template = load_prompt_template(repo_root, "validator-rubric.md")
    return render_prompt_template(
        template,
        {
            "THRESHOLD": f"{threshold:.0f}",
            "MAX_RETRIES": str(max_retries),
        },
    )


def build_synthesis_input(used_chunks: list[dict[str, Any]], ignored_chunks: list[dict[str, Any]]) -> str:
    lines = [
        "# Synthesis packet",
        "",
        "## Used chunk reports",
        "",
    ]
    for chunk in used_chunks:
        report_path = chunk.get("report", {}).get("path")
        report_text = Path(report_path).read_text(encoding="utf-8") if report_path and Path(report_path).exists() else ""
        lines.extend(
            [
                f"### {chunk['chunk_id']}",
                f"- relevance: {chunk.get('estimated_relevance')}",
                f"- source_sessions: {', '.join(chunk.get('source_sessions', [])) or 'none'}",
                f"- report_path: {report_path or 'missing'}",
                "",
                report_text.strip(),
                "",
            ]
        )
    lines.extend(["## Ignored or lower-weight chunks", ""])
    if ignored_chunks:
        for chunk in ignored_chunks:
            lines.append(f"- {chunk['chunk_id']}: relevance={chunk.get('estimated_relevance')} report={chunk.get('report', {}).get('path', 'missing')}")
    else:
        lines.append("- none")
    lines.append("")
    return "\n".join(lines)


def render_bundle_text(docs: dict[str, str]) -> str:
    sections: list[str] = []
    for document_name in DOCUMENT_NAMES:
        text = docs.get(document_name, "").strip()
        if not text:
            raise ValueError(f"Missing bundle document content for {document_name}")
        sections.append(f"=== {document_name} ===\n{text}")
    return "\n\n".join(sections).rstrip() + "\n"


def split_bundle_text(text: str) -> dict[str, str]:
    pattern = re.compile(
        r"=== project_context\.md ===\s*(.*?)\s*=== collaboration_guide\.md ===\s*(.*?)\s*=== patterns\.md ===\s*(.*)\Z",
        re.S,
    )
    match = pattern.search(text.strip())
    if not match:
        raise ValueError("Invalid synthesis bundle format; expected 3 fixed markers")
    docs = {
        "project_context.md": match.group(1).strip(),
        "collaboration_guide.md": match.group(2).strip(),
        "patterns.md": match.group(3).strip(),
    }
    for name, body in docs.items():
        if not body:
            raise ValueError(f"Bundle section {name} was empty")
    return docs


def parse_self_validation_response(text: str, threshold: float) -> tuple[ValidationResult, dict[str, Any]]:
    payload = json.loads(extract_json_object(text))
    raw_score_value = payload.get("raw_score")
    normalized_value = payload.get("normalized_score")
    raw_score = float(raw_score_value) if raw_score_value is not None else None
    if raw_score is None and normalized_value is not None:
        raw_score = float(normalized_value) * 5.0
    score = round(raw_score or 0.0, 2)
    rubric_raw = payload.get("dimension_scores") or {}
    rubric = {str(key): round(float(value), 2) for key, value in rubric_raw.items()}
    passed = score >= threshold
    summary = payload.get("summary") or ("通过" if passed else "未通过")
    failed_dimensions = payload.get("failed_dimensions") or []
    must_fix = payload.get("must_fix") or []
    which_chunk_underrepresented = payload.get("which_chunk_underrepresented") or []
    which_chunk_overweighted = payload.get("which_chunk_overweighted") or []
    rewrite_direction = payload.get("rewrite_direction") or {}
    critique_lines = [f"# Self-validation critique (score {score}/50)", "", f"- summary: {summary}"]
    if rubric:
        critique_lines.append("- dimension_scores:")
        for name, value in rubric.items():
            critique_lines.append(f"  - {name}: {value}")
    if failed_dimensions:
        critique_lines.append("- failed_dimensions:")
        for item in failed_dimensions:
            critique_lines.append(f"  - {item}")
    if must_fix:
        critique_lines.append("- must_fix:")
        for item in must_fix:
            critique_lines.append(f"  - {item}")
    if which_chunk_underrepresented:
        critique_lines.append("- which_chunk_underrepresented:")
        for item in which_chunk_underrepresented:
            critique_lines.append(f"  - {item}")
    if which_chunk_overweighted:
        critique_lines.append("- which_chunk_overweighted:")
        for item in which_chunk_overweighted:
            critique_lines.append(f"  - {item}")
    if rewrite_direction:
        critique_lines.append("- rewrite_direction:")
        for name, items in rewrite_direction.items():
            critique_lines.append(f"  - {name}:")
            for item in items or []:
                critique_lines.append(f"    - {item}")
    critique_lines.append(f"- threshold: {threshold}")
    critique_lines.append(f"- passed: {passed}")
    critique = "\n".join(critique_lines) + "\n"
    payload.pop("normalized_score", None)
    payload["raw_score"] = score
    payload["passed_by_threshold"] = passed
    return ValidationResult(score=score, passed=passed, rubric=rubric, critique=critique, needs_review=not passed), payload


def extract_json_object(text: str) -> str:
    stripped = text.strip()
    fenced = re.match(r"^```(?:json)?\s*(.*?)\s*```$", stripped, re.S)
    if fenced:
        stripped = fenced.group(1).strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("No JSON object found in self-validation output")
    return stripped[start : end + 1]


def stable_session_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-") or "distill-session"


def select_synthesis_chunks(chunks: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    completed = [chunk for chunk in chunks if chunk.get("report", {}).get("status") in {"completed", "cached"}]
    if not completed:
        return [], []
    used = [chunk for chunk in completed if chunk.get("estimated_relevance") != "low"]
    if not used:
        used = completed
    ignored = [chunk for chunk in completed if chunk not in used]
    return used, ignored


def extract_trace_metadata(text: str) -> tuple[list[str], list[str], list[str]]:
    referenced: list[str] = []
    changed: list[str] = []
    commands: list[str] = []
    for line in text.splitlines():
        read_match = re.match(r'^\*\s+(Read|read)\s+"([^"]+)"', line)
        if read_match:
            referenced.append(read_match.group(2))
            continue
        change_match = re.match(r'^\*\s+(Write|write|Edit|edit)\s+"([^"]+)"', line)
        if change_match:
            changed.append(change_match.group(2))
            continue
        bash_match = re.match(r'^\*\s+(Bash|bash)\s+"([^"]+)"', line)
        if bash_match:
            commands.append(normalize_whitespace(bash_match.group(2)))
    return ordered_unique(referenced), ordered_unique(changed), ordered_unique(commands)


def estimate_relevance(referenced_paths: list[str], changed_paths: list[str], commands: list[str], char_count: int) -> str:
    if changed_paths:
        return "high"
    if referenced_paths or commands:
        return "medium"
    if char_count < 2000:
        return "low"
    return "unknown"


def resolve_artifact_path(value: str | None, repo_root: Path, manifest_root: Path) -> Path:
    if not value:
        return manifest_root / "missing"
    path = Path(value).expanduser()
    if path.is_absolute():
        return path.resolve()
    candidates = [repo_root.parent / path, repo_root / path, manifest_root / path]
    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()
    return (manifest_root / path).resolve()


def infer_project_root_from_manifest(compiled_root: Path, repo_root: Path) -> Path | None:
    manifest_path = compiled_root.parent / "manifest.json"
    if not manifest_path.exists():
        return None
    manifest = load_json(manifest_path, {})
    project = manifest.get("project")
    if not project:
        return None
    return resolve_artifact_path(project, repo_root, manifest_path.parent)


def env_flag(name: str) -> bool:
    value = os.environ.get(name)
    return str(value).lower() in {"1", "true", "yes", "on"}


def analysis_skip_reason(config: PipelineConfig) -> str:
    if config.only_chunk:
        return f"only_chunk={config.only_chunk}"
    if config.analysis_only:
        return "analysis_only"
    return "disabled"


def report_artifact_paths(report_path: Path) -> dict[str, Any]:
    stem = report_path.name
    return {
        "events_path": str(report_path.parent / f"{stem}.events.jsonl"),
        "stderr_path": str(report_path.parent / f"{stem}.stderr.log"),
        "prompt_path": str(report_path.parent / f"{stem}.prompt.md"),
    }


def build_real_runner_prompt(request: RunnerRequest) -> str:
    if request.task_type == "synthesis_bundle":
        envelope = (
            "You must answer in Chinese and output only one bundle with the exact required markers.\n"
            "Use the provided synthesis packet and prior same-session context as the only evidence base.\n"
            "Do not add preface, code fences, or commentary outside the bundle.\n"
            "Do not read unrelated files.\n"
        )
    elif request.task_type == "synthesis_self_validate":
        envelope = (
            "You must answer in Chinese and output only one valid JSON object.\n"
            "Evaluate the bundle from this same session; do not rewrite it in this turn.\n"
            "Do not add preface, code fences, or commentary outside the JSON object.\n"
            "Do not read unrelated files.\n"
        )
    else:
        envelope = (
            "You must answer in Chinese and produce the requested chunk analysis report only.\n"
            "Use the provided chunk content below as the primary input.\n"
            "Do not read unrelated files. Do not use `.view.txt`.\n"
        )
    return request.prompt_text.strip() + "\n\n---\n" + envelope + ("\n" + request.input_text if request.input_text else "")


def build_pi_cli_command(
    *,
    pi_command: str,
    model: str,
    thinking: str | None,
    session_dir: Path,
    persist_session: bool,
    session_id: str | None,
) -> list[str]:
    command = [
        pi_command,
        "--print",
        "--mode",
        "json",
        "--approve",
        "--model",
        model,
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
    if not persist_session:
        command.insert(7, "--no-session")
    if session_id:
        command.extend(["--session-id", session_id])
    if thinking:
        command.extend(["--thinking", thinking])
    return command


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


def apply_agents_reference(config: PipelineConfig) -> dict[str, Any]:
    if not config.project_root:
        return {"status": "skipped", "reason": "project_root_unavailable"}
    agents_path = config.project_root / "AGENTS.md"
    relative_context = os.path.relpath(config.final_root / "project_context.md", config.project_root)
    block = (
        "\n## Project context\n"
        "Before working on this project, read:\n"
        f"- `{relative_context}`\n"
    )
    original = agents_path.read_text(encoding="utf-8") if agents_path.exists() else ""
    if "## Project context" in original and relative_context in original:
        return {"status": "unchanged", "path": str(agents_path)}
    agents_path.write_text(original.rstrip() + block + "\n", encoding="utf-8")
    return {"status": "written", "path": str(agents_path), "reference": relative_context}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S-%fZ")


def estimate_tokens(char_count: int) -> int:
    return (char_count + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def ordered_unique(items: Any) -> list[Any]:
    out: list[Any] = []
    seen: set[Any] = set()
    for item in items:
        if item in (None, ""):
            continue
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def flatten(groups: Any) -> list[Any]:
    out: list[Any] = []
    for group in groups:
        out.extend(group)
    return out


def normalize_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def head(items: list[Any], limit: int) -> list[Any]:
    return list(items[:limit])


def format_bullets(items: list[str], template: str) -> str:
    if not items:
        return "- none"
    return "\n".join(template.format(item) for item in items)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = resolve_config(args)
    manifest = DistillPipeline(config).run()
    manifest["finished_at"] = utc_now_iso()
    write_json(Path(manifest["paths"]["manifest_path"]), manifest)
    print(str(Path(manifest["paths"]["manifest_path"])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
