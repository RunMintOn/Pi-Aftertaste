#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

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

import numpy as np
from sklearn.cluster import DBSCAN, KMeans
from sklearn.feature_extraction.text import CountVectorizer, TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity


PUBLIC_OUTPUT_FILES = [
    "user_turns.json",
    "method_phrases.json",
    "method_kmeans_clusters.json",
    "method_dbscan_clusters.json",
    "method_blindspot_rules.json",
    "method_correction_chains.json",
    "blindspot_profile.json",
    "comparison.md",
    "blindspot_report.md",
]


BLINDSPOT_RULES = {
    "scope_clarification": {
        "patterns": [
            r"我指的是",
            r"不是这个意思",
            r"先对齐",
            r"明白我意思吗",
            r"你为什么",
            r"我说的是",
            r"这里指的其实就是",
        ],
        "description": "用户需要反复收窄对象或纠正 agent 的理解范围。",
        "suggestion": "把对象、边界、不要做什么放进首条需求里，而不是等 agent 跑偏后再纠正。",
    },
    "token_budget_control": {
        "patterns": [
            r"最小充分操作",
            r"控制 token",
            r"避免.?大范围 grep",
            r"无目标搜索",
            r"过量上下文",
            r"低价值步骤",
            r"大范围 grep",
            r"注意控制 token",
        ],
        "description": "用户明显在意 token 成本，并多次强调约束。",
        "suggestion": "把 token 预算、禁止大搜索、优先局部检查写成固定前置模板。",
    },
    "exploratory_request": {
        "patterns": [
            r"随便用",
            r"看一下工具的效果",
            r"试一下",
            r"什么情况",
            r"看一下",
        ],
        "description": "请求偏探索/观察，没有明确成功标准。",
        "suggestion": "测试型请求最好明确输入、预期输出、是否只做演示。",
    },
    "late_constraint": {
        "patterns": [
            r"注意控制 token",
            r"先对齐",
            r"你先等一下",
            r"我们先对齐",
            r"先看一下",
        ],
        "description": "关键约束出现在后续纠偏回合，而不是最开始。",
        "suggestion": "把关键限制（对象、边界、验证方式）提前到首轮 prompt。",
    },
}


@dataclass
class UserTurn:
    session_file: str
    session_id: str
    turn_index: int
    timestamp: str
    text: str
    preview: str
    normalized: str


@dataclass
class RuleHit:
    label: str
    session_file: str
    turn_index: int
    preview: str


@dataclass
class SessionInsight:
    session_file: str
    first_prompt: str
    later_constraints: list[str]
    correction_turns: list[int]


def session_dir_name(project_cwd: str) -> str:
    stripped = project_cwd.strip("/")
    return f"--{stripped.replace('/', '-')}--" if stripped else "----"


def sanitize_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", "", text)
    return text


def one_line(text: str, limit: int = 180) -> str:
    text = re.sub(r"\s+", " ", sanitize_text(text)).strip()
    return text if len(text) <= limit else text[: max(0, limit - 1)].rstrip() + "…"


def normalize_text(text: str, project_cwd: str) -> str:
    text = sanitize_text(text)
    text = text.replace(project_cwd, " <PROJECT> ")
    text = re.sub(r"/home/\w+(?:/[\w.@-]+)+", " <PATH> ", text)
    text = re.sub(r"`[^`]+`", " <CODE> ", text)
    text = re.sub(r"https?://\S+", " <URL> ", text)
    text = re.sub(r"\d+", " <NUM> ", text)
    text = text.lower()
    text = re.sub(r"\s+", " ", text).strip()
    return text


def is_resume_artifact(text: str) -> bool:
    sample = sanitize_text(text).strip()
    return sample.startswith("**Resume Session") or sample.startswith("Resume Session (Current Folder)")


def iter_user_turns_from_session(path: Path, project_cwd: str) -> list[UserTurn]:
    turns: list[UserTurn] = []
    session_id = ""
    turn_index = 0
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        entry = json.loads(raw)
        if entry.get("type") == "session":
            session_id = entry.get("id", "")
            continue
        if entry.get("type") != "message":
            continue
        message = entry.get("message", {})
        if message.get("role") != "user":
            continue
        turn_index += 1
        text = "\n".join(part.get("text", "") for part in message.get("content", []) if part.get("type") == "text")
        if is_resume_artifact(text):
            continue
        turns.append(
            UserTurn(
                session_file=path.name,
                session_id=session_id,
                turn_index=turn_index,
                timestamp=entry.get("timestamp", ""),
                text=text,
                preview=one_line(text),
                normalized=normalize_text(text, str(Path(project_cwd).resolve())),
            )
        )
    return turns


def iter_user_turns(project_cwd: str, sessions_root: str) -> list[UserTurn]:
    session_dir = Path(sessions_root).expanduser().resolve() / session_dir_name(str(Path(project_cwd).resolve()))
    turns: list[UserTurn] = []
    for path in sorted(session_dir.glob("*.jsonl")):
        turns.extend(iter_user_turns_from_session(path, project_cwd))
    return turns


def extract_blindspot_session(path: Path, project_cwd: str) -> dict[str, Any]:
    return {
        "session_file": path.name,
        "user_turns": [asdict(turn) for turn in iter_user_turns_from_session(path, project_cwd)],
    }


def write_route_outputs(out_dir: Path, project_cwd: str, turns: list[UserTurn], phrases: dict[str, Any], kmeans: dict[str, Any], dbscan: dict[str, Any], rules: dict[str, Any], chains: dict[str, Any], profile: dict[str, Any]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    write_json(out_dir / "user_turns.json", [asdict(t) for t in turns])
    write_json(out_dir / "method_phrases.json", phrases)
    write_json(out_dir / "method_kmeans_clusters.json", kmeans)
    write_json(out_dir / "method_dbscan_clusters.json", dbscan)
    write_json(out_dir / "method_blindspot_rules.json", rules)
    write_json(out_dir / "method_correction_chains.json", chains)
    write_json(out_dir / "blindspot_profile.json", profile)
    (out_dir / "comparison.md").write_text(compare_methods(phrases, kmeans, dbscan, rules, chains), encoding="utf-8")
    (out_dir / "blindspot_report.md").write_text(render_report(project_cwd, turns, phrases, kmeans, dbscan, rules, chains, profile), encoding="utf-8")


def frequent_char_phrases(turns: list[UserTurn]) -> dict[str, Any]:
    texts = [t.normalized for t in turns]
    vectorizer = CountVectorizer(analyzer="char", ngram_range=(3, 8), min_df=2)
    matrix = vectorizer.fit_transform(texts)
    counts = np.asarray(matrix.sum(axis=0)).ravel()
    vocab = np.array(vectorizer.get_feature_names_out())
    stop_phrases = {
        "这个", "就是", "然后", "那个", "我们", "一下", "对吧", "应该", "没有", "直接", "可以", "所以", "的话", "现在", "你看", "这个是", "然后呢",
        "ctrl", "resume", "session", "current", "folder", "num",
    }
    items: list[dict[str, Any]] = []
    kept: list[tuple[str, int]] = []
    for phrase, count in sorted(zip(vocab, counts), key=lambda x: (x[1] * len(x[0]), x[1], len(x[0])), reverse=True):
        if count < 2:
            continue
        raw = phrase.strip()
        if len(raw) < 3 or len(raw) > 18:
            continue
        if raw.count("<") or raw.count(">"):
            continue
        if raw in stop_phrases:
            continue
        if any(part in raw for part in ["ctrl", "resume", "session", "current folder", "num"]):
            continue
        if not re.search(r"[a-z\u4e00-\u9fff]", raw):
            continue
        if re.fullmatch(r"[\W_]+", raw):
            continue
        if re.fullmatch(r"[a-z]{1,3}", raw):
            continue
        if any(raw in existing and count <= existing_count for existing, existing_count in kept):
            continue
        kept.append((raw, int(count)))
        items.append({"phrase": raw, "count": int(count), "score": int(count * len(raw))})
        if len(items) >= 80:
            break
    return {"method": "frequent_char_phrases", "items": items}


def cluster_turns_kmeans(turns: list[UserTurn]) -> dict[str, Any]:
    texts = [t.normalized for t in turns]
    if len(texts) < 4:
        return {"method": "tfidf_kmeans", "clusters": []}
    vectorizer = TfidfVectorizer(analyzer="char", ngram_range=(2, 5), min_df=2)
    x = vectorizer.fit_transform(texts)
    n_clusters = min(6, max(2, round(math.sqrt(len(turns) / 2))))
    model = KMeans(n_clusters=n_clusters, n_init=20, random_state=0)
    labels = model.fit_predict(x)
    feature_names = vectorizer.get_feature_names_out()
    clusters = []
    for label in sorted(set(labels)):
        idxs = [i for i, value in enumerate(labels) if value == label]
        center = model.cluster_centers_[label]
        top_idx = center.argsort()[::-1][:8]
        top_terms = [feature_names[i] for i in top_idx if center[i] > 0][:6]
        sims = cosine_similarity(x[idxs], model.cluster_centers_[label].reshape(1, -1)).ravel()
        ordered = [turns[idxs[i]] for i in np.argsort(-sims)[:4]]
        clusters.append(
            {
                "cluster": int(label),
                "size": len(idxs),
                "top_terms": top_terms,
                "examples": [
                    {
                        "session_file": t.session_file,
                        "turn_index": t.turn_index,
                        "preview": t.preview,
                    }
                    for t in ordered
                ],
            }
        )
    clusters.sort(key=lambda item: item["size"], reverse=True)
    return {"method": "tfidf_kmeans", "cluster_count": len(clusters), "clusters": clusters}


def cluster_turns_dbscan(turns: list[UserTurn]) -> dict[str, Any]:
    texts = [t.normalized for t in turns]
    if len(texts) < 4:
        return {"method": "tfidf_dbscan", "clusters": []}
    vectorizer = TfidfVectorizer(analyzer="char", ngram_range=(2, 5), min_df=2)
    x = vectorizer.fit_transform(texts)
    model = DBSCAN(eps=0.62, min_samples=2, metric="cosine")
    labels = model.fit_predict(x)
    clusters = []
    noise = 0
    for label in sorted(set(labels)):
        idxs = [i for i, value in enumerate(labels) if value == label]
        if label == -1:
            noise = len(idxs)
            continue
        subset = x[idxs]
        centroid = np.asarray(subset.mean(axis=0))
        sims = cosine_similarity(subset, centroid).ravel()
        ordered = [turns[idxs[i]] for i in np.argsort(-sims)[:4]]
        clusters.append(
            {
                "cluster": int(label),
                "size": len(idxs),
                "examples": [
                    {
                        "session_file": t.session_file,
                        "turn_index": t.turn_index,
                        "preview": t.preview,
                    }
                    for t in ordered
                ],
            }
        )
    clusters.sort(key=lambda item: item["size"], reverse=True)
    return {"method": "tfidf_dbscan", "cluster_count": len(clusters), "noise_count": noise, "clusters": clusters}


def blindspot_rules(turns: list[UserTurn]) -> dict[str, Any]:
    hits: list[RuleHit] = []
    counts = Counter()
    session_hits: dict[str, list[RuleHit]] = defaultdict(list)
    for turn in turns:
        for label, meta in BLINDSPOT_RULES.items():
            if label == "late_constraint" and turn.turn_index <= 1:
                continue
            if any(re.search(pattern, turn.text, re.I) for pattern in meta["patterns"]):
                hit = RuleHit(label=label, session_file=turn.session_file, turn_index=turn.turn_index, preview=turn.preview)
                hits.append(hit)
                counts[label] += 1
                session_hits[turn.session_file].append(hit)
    grouped = []
    for label, count in counts.most_common():
        meta = BLINDSPOT_RULES[label]
        examples = [asdict(hit) for hit in hits if hit.label == label][:6]
        grouped.append(
            {
                "label": label,
                "count": count,
                "description": meta["description"],
                "suggestion": meta["suggestion"],
                "examples": examples,
            }
        )
    return {"method": "blindspot_rules", "summary": grouped, "hits": [asdict(hit) for hit in hits]}


def session_correction_chains(turns: list[UserTurn], rule_hits: dict[str, Any]) -> dict[str, Any]:
    hit_map: dict[tuple[str, int], list[str]] = defaultdict(list)
    for hit in rule_hits["hits"]:
        hit_map[(hit["session_file"], hit["turn_index"])] .append(hit["label"])
    by_session: dict[str, list[UserTurn]] = defaultdict(list)
    for turn in turns:
        by_session[turn.session_file].append(turn)

    insights: list[SessionInsight] = []
    for session_file, session_turns in by_session.items():
        correction_turns = [t.turn_index for t in session_turns if any(label in {"scope_clarification", "token_budget_control", "late_constraint"} for label in hit_map.get((session_file, t.turn_index), []))]
        later_constraints = []
        for t in session_turns[1:]:
            labels = hit_map.get((session_file, t.turn_index), [])
            if any(label in {"late_constraint", "token_budget_control", "scope_clarification"} for label in labels):
                later_constraints.append(f"turn{t.turn_index}: {t.preview}")
        if later_constraints:
            insights.append(
                SessionInsight(
                    session_file=session_file,
                    first_prompt=session_turns[0].preview,
                    later_constraints=later_constraints[:6],
                    correction_turns=correction_turns[:12],
                )
            )
    return {"method": "session_correction_chains", "sessions": [asdict(item) for item in insights]}


def build_blindspot_profile(turns: list[UserTurn], rules: dict[str, Any], chains: dict[str, Any]) -> dict[str, Any]:
    counts = {item['label']: item['count'] for item in rules.get('summary', [])}
    dense_answer_pref = sum(1 for t in turns if '请保持分析深度' in t.text)
    profile = []
    recommendations = []

    if counts.get('token_budget_control', 0) >= 3:
        profile.append({
            'trait': 'token_sensitive',
            'evidence_count': counts['token_budget_control'],
            'meaning': '用户明显在意 token 消耗和无效搜索。',
        })
        recommendations.append('把“最小充分操作 + 禁止大范围搜索”做成固定开场模板。')

    if counts.get('scope_clarification', 0) + counts.get('late_constraint', 0) >= 5:
        profile.append({
            'trait': 'scope_often_clarified_late',
            'evidence_count': counts.get('scope_clarification', 0) + counts.get('late_constraint', 0),
            'meaning': '对象和边界经常在 agent 跑起来后才补充。',
        })
        recommendations.append('首条需求先写对象、修改边界、不做什么、验证方式。')

    if counts.get('exploratory_request', 0) >= 8:
        profile.append({
            'trait': 'exploratory_tester',
            'evidence_count': counts['exploratory_request'],
            'meaning': '用户经常通过“试一下/看一下效果”推进任务。',
        })
        recommendations.append('测试型请求改成“输入 + 预期输出 + 只演示/是否落盘”三段式。')

    if dense_answer_pref >= 3:
        profile.append({
            'trait': 'prefers_dense_brief_answers',
            'evidence_count': dense_answer_pref,
            'meaning': '用户偏好高密度短答案，而不是长解释。',
        })
        recommendations.append('可默认启用“短答案 + 必要时展开”的回答模板。')

    return {
        'traits': profile,
        'recommendations': recommendations,
        'late_correction_sessions': len(chains.get('sessions', [])),
    }


def compare_methods(phrases: dict[str, Any], kmeans: dict[str, Any], dbscan: dict[str, Any], rules: dict[str, Any], chains: dict[str, Any]) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    lines = ["# NLP blindspot comparison", "", f"Generated: {now}", "", "## Methods", ""]
    lines.append("1. Frequent char phrases — 看重复表达表面形态")
    lines.append("2. TF-IDF + KMeans — 看比较粗的主题簇")
    lines.append("3. TF-IDF + DBSCAN — 看比较近似的提示语家族")
    lines.append("4. Rule-based blindspot labels — 直接产出可执行建议")
    lines.append("5. Session correction chains — 看约束是不是总在后面才补")
    lines.append("")
    lines.append("## Quick read")
    lines.append("")
    top_phrases = ", ".join(item["phrase"] for item in phrases["items"][:12])
    lines.append(f"- 高频短语最能看到：{top_phrases}")
    lines.append(f"- KMeans 主题簇数：{kmeans.get('cluster_count', 0)}")
    lines.append(f"- DBSCAN 近似簇数：{dbscan.get('cluster_count', 0)}，噪声点：{dbscan.get('noise_count', 0)}")
    lines.append(f"- 规则命中的盲区标签数：{len(rules.get('summary', []))}")
    lines.append(f"- 发现需要后置纠偏的 session 数：{len(chains.get('sessions', []))}")
    lines.append("")
    lines.append("## Which method is best for what")
    lines.append("")
    lines.append("- 高频短语：最适合看用户口头禅、反复强调的约束。")
    lines.append("- KMeans：最适合看大主题，比如 bug 修复 / token 控制 / 试验型请求。")
    lines.append("- DBSCAN：最适合抓相似纠正句、近似重复表达。")
    lines.append("- 规则标签：最适合直接落成用户建议。")
    lines.append("- correction chains：最适合证明“关键约束经常说晚了”。")
    lines.append("")
    lines.append("## MVP judgment")
    lines.append("")
    lines.append("- 只靠聚类，不足以直接得出“用户盲区”。")
    lines.append("- 但聚类很适合先把用户回合分堆，再交给规则层解释。")
    lines.append("- 这批样本里，最清晰的价值来自：`规则标签 + correction chain`，而不是单纯主题聚类。")
    lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_report(project_cwd: str, turns: list[UserTurn], phrases: dict[str, Any], kmeans: dict[str, Any], dbscan: dict[str, Any], rules: dict[str, Any], chains: dict[str, Any], profile: dict[str, Any]) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    lines = ["# Pi Distill NLP Report", "", f"Generated: {now}", "", "## Scope", "", f"- Project: `{project_cwd}`", f"- User turns analyzed: {len(turns)}", ""]
    lines.append("## Frequent phrases")
    lines.append("")
    for item in phrases["items"][:20]:
        lines.append(f"- `{item['phrase']}` × {item['count']}")
    lines.append("")
    lines.append("## KMeans topic clusters")
    lines.append("")
    for cluster in kmeans.get("clusters", [])[:6]:
        lines.append(f"### Cluster {cluster['cluster']} ({cluster['size']})")
        lines.append(f"- Top terms: {', '.join(cluster['top_terms'])}")
        for ex in cluster["examples"][:3]:
            lines.append(f"- `{ex['session_file']}#turn{ex['turn_index']}` {ex['preview']}")
        lines.append("")
    lines.append("## DBSCAN near-duplicate families")
    lines.append("")
    for cluster in dbscan.get("clusters", [])[:6]:
        lines.append(f"### Cluster {cluster['cluster']} ({cluster['size']})")
        for ex in cluster["examples"][:4]:
            lines.append(f"- `{ex['session_file']}#turn{ex['turn_index']}` {ex['preview']}")
        lines.append("")
    lines.append("## Blindspot labels")
    lines.append("")
    for item in rules.get("summary", []):
        lines.append(f"### {item['label']} ({item['count']})")
        lines.append(f"- Meaning: {item['description']}")
        lines.append(f"- Suggestion: {item['suggestion']}")
        for ex in item["examples"][:4]:
            lines.append(f"- `{ex['session_file']}#turn{ex['turn_index']}` {ex['preview']}")
        lines.append("")
    lines.append("## Session correction chains")
    lines.append("")
    for item in chains.get("sessions", [])[:8]:
        lines.append(f"### {item['session_file']}")
        lines.append(f"- First prompt: {item['first_prompt']}")
        if item["correction_turns"]:
            lines.append(f"- Correction turns: {item['correction_turns']}")
        for x in item["later_constraints"][:5]:
            lines.append(f"- {x}")
        lines.append("")
    lines.append("## Blindspot profile")
    lines.append("")
    for item in profile.get("traits", []):
        lines.append(f"- `{item['trait']}` ({item['evidence_count']}) — {item['meaning']}")
    if profile.get("recommendations"):
        lines.append("")
        lines.append("### Suggested user-side improvements")
        for rec in profile["recommendations"]:
            lines.append(f"- {rec}")
    lines.append("")
    lines.append("## Takeaways")
    lines.append("")
    lines.append("- 这批样本能清楚识别：scope clarification、token budget control、late constraint。")
    lines.append("- 主题聚类能看出大方向，但真正可执行的用户建议仍要靠规则标签解释。")
    lines.append("- 下一步最自然的是：把这些标签做成长期 profile，而不是一次性报告。")
    lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Lightweight NLP analysis for Pi user blindspots without LLM.")
    parser.add_argument("--project-cwd", required=True)
    parser.add_argument("--sessions-root", default=str(Path.home() / ".pi/agent/sessions"))
    parser.add_argument("--out", help="Output directory; omit to use ~/.pi-distill runtime layout")
    parser.add_argument("--runtime-root", default=str(Path.home() / ".pi-distill"), help="Runtime state/output root")
    parser.add_argument("--record-state", action="store_true", help="Also update runtime state/history when --out is explicitly set")
    args = parser.parse_args(argv)

    runtime_layout = args.out is None
    record_state = runtime_layout or args.record_state

    if not record_state:
        out_dir = Path(args.out)
        out_dir.mkdir(parents=True, exist_ok=True)
        turns = iter_user_turns(args.project_cwd, args.sessions_root)
        phrases = frequent_char_phrases(turns)
        kmeans = cluster_turns_kmeans(turns)
        dbscan = cluster_turns_dbscan(turns)
        rules = blindspot_rules(turns)
        chains = session_correction_chains(turns, rules)
        profile = build_blindspot_profile(turns, rules, chains)
        write_route_outputs(out_dir, args.project_cwd, turns, phrases, kmeans, dbscan, rules, chains, profile)
        print(f"wrote {out_dir / 'blindspot_report.md'}")
        return 0

    ctx = prepare_route_run(
        project_cwd=args.project_cwd,
        sessions_root=args.sessions_root,
        runtime_root=args.runtime_root,
        route="blindspot",
    )
    runtime_paths = ctx.paths
    latest_dir = runtime_paths.latest_route_dir("blindspot")
    latest_dir.mkdir(parents=True, exist_ok=True)
    runtime_paths.latest_route_sessions_dir("blindspot").mkdir(parents=True, exist_ok=True)

    remove_deleted_session_caches(runtime_paths, "blindspot", ctx.diff.deleted_sessions)
    session_dir = Path(args.sessions_root).expanduser().resolve() / session_dir_name(str(Path(args.project_cwd).resolve()))
    entry_by_file = {item["file"]: item for item in ctx.diff.entries}
    sessions_to_process = sorted(set(ctx.diff.new_sessions + ctx.diff.changed_sessions))
    for session_file in ctx.diff.session_files:
        cache_path = route_session_cache_path(runtime_paths, "blindspot", session_file)
        cached = load_json(cache_path, None)
        if not cached or cached.get("fingerprint") != entry_by_file[session_file]["fingerprint"]:
            if session_file not in sessions_to_process:
                sessions_to_process.append(session_file)

    report_path = latest_dir / "blindspot_report.md"
    reused_latest = False

    if not sessions_to_process and report_path.exists():
        reused_latest = True
    else:
        for session_file in sessions_to_process:
            cache = extract_blindspot_session(session_dir / session_file, args.project_cwd)
            cache["fingerprint"] = entry_by_file[session_file]["fingerprint"]
            write_json(route_session_cache_path(runtime_paths, "blindspot", session_file), cache)

        turns: list[UserTurn] = []
        for session_file in ctx.diff.session_files:
            cached = load_json(route_session_cache_path(runtime_paths, "blindspot", session_file), None)
            if not cached:
                continue
            turns.extend(UserTurn(**item) for item in cached.get("user_turns", []))

        phrases = frequent_char_phrases(turns)
        kmeans = cluster_turns_kmeans(turns)
        dbscan = cluster_turns_dbscan(turns)
        rules = blindspot_rules(turns)
        chains = session_correction_chains(turns, rules)
        profile = build_blindspot_profile(turns, rules, chains)
        write_route_outputs(latest_dir, args.project_cwd, turns, phrases, kmeans, dbscan, rules, chains, profile)

    if args.out:
        copy_public_files(latest_dir, Path(args.out), PUBLIC_OUTPUT_FILES)
        public_report = Path(args.out) / "blindspot_report.md"
    else:
        public_report = latest_dir / "blindspot_report.md"

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
