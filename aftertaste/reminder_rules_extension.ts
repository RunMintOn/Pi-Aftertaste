import { existsSync, mkdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import { findMatchingRules, resolveReminderRulesPath, selectVisibleMatches } from "./reminder_rules_runtime.mjs";

type ReminderMode = "notify" | "confirm";
type CooldownMode = "session_once" | "turn_once";
type Confidence = "high" | "medium" | "low";

interface ReminderRule {
	id: string;
	mode: ReminderMode;
	must_match_any: string[];
	must_match_all_groups: string[][];
	anti_patterns: string[];
	message: string;
	suggestion: string;
	evidence: string[];
	cooldown: CooldownMode;
	confidence: Confidence;
}

interface ReminderRulesDocument {
	version: string;
	default_enabled: boolean;
	rules: ReminderRule[];
}

interface LoadedRules {
	path: string;
	mtimeMs: number;
	doc: ReminderRulesDocument;
}

interface IgnoredRulesDocument {
	ignored_rule_ids: string[];
}

interface LoadedIgnoredRules {
	path: string;
	mtimeMs: number;
	doc: IgnoredRulesDocument;
}

const seenRuleIds = new Set<string>();
let cachedRules: LoadedRules | null = null;
let warnedInvalidKey: string | null = null;
let cachedIgnoredRules: LoadedIgnoredRules | null = null;
let warnedInvalidIgnoredKey: string | null = null;

export default function reminderRulesExtension(pi: ExtensionAPI) {
	pi.registerCommand("reminder-rules-status", {
		description: "Show current reminder rules status for this project",
		handler: async (_args, ctx) => {
			const state = loadRules(ctx, { silent: true });
			if (!ctx.hasUI) return;
			if (!state) {
				ctx.ui.notify("Reminder rules: 当前项目未找到可用规则文件。", "info");
				return;
			}
			const ignoredCount = loadIgnoredRuleIds(ctx, { silent: true }).size;
			ctx.ui.notify(`Reminder rules: 已加载 ${state.doc.rules.length} 条规则，已忽略 ${ignoredCount} 条\n${state.path}`, "info");
		},
	});

	pi.registerCommand("reminder-rules-check", {
		description: "Preview reminder rule matches for arbitrary text",
		handler: async (args, ctx) => {
			if (!ctx.hasUI) return;
			const text = (args || "").trim();
			if (!text) {
				ctx.ui.notify("用法: /reminder-rules-check <文本>", "warning");
				return;
			}
			const state = loadRules(ctx, { silent: true });
			if (!state) {
				ctx.ui.notify("Reminder rules: 当前项目未找到可用规则文件。", "warning");
				return;
			}
			const ignoredRuleIds = loadIgnoredRuleIds(ctx, { silent: true });
			const matches = findMatchingRules(text, state.doc.rules).filter((entry) => !ignoredRuleIds.has(entry.rule.id));
			if (matches.length === 0) {
				ctx.ui.notify("未命中任何 reminder rule。", "info");
				return;
			}
			const lines = matches.slice(0, 4).map((entry) => `- ${entry.rule.id} (${entry.rule.mode})`);
			ctx.ui.notify(`命中规则:\n${lines.join("\n")}`, "info");
		},
	});

	pi.registerCommand("reminder-rules-reset-ignored", {
		description: "Clear ignored reminder rules for the current project",
		handler: async (_args, ctx) => {
			clearIgnoredRuleIds(ctx);
			if (ctx.hasUI) {
				ctx.ui.notify("已清空当前项目的 reminder rules 忽略列表。", "info");
			}
		},
	});

	pi.on("input", async (event, ctx) => {
		if (event.source === "extension") return { action: "continue" };
		const state = loadRules(ctx, { silent: false });
		if (!state || state.doc.default_enabled === false) return { action: "continue" };

		const ignoredRuleIds = loadIgnoredRuleIds(ctx, { silent: false });
		const matches = findMatchingRules(event.text, state.doc.rules).filter((entry) => !ignoredRuleIds.has(entry.rule.id));
		if (matches.length === 0) return { action: "continue" };

		const visible = selectVisibleMatches(matches, seenRuleIds);
		if (visible.confirm && ctx.hasUI) {
			const rule = visible.confirm;
			const choice = await ctx.ui.select(`Reminder rules\n\n${rule.message}\n\n${rule.suggestion}`, [
				"直接发送，不修改",
				"取消发送，继续修改输入",
				"直接发送，并永久忽略此提醒",
			]);
			if (choice === "直接发送，不修改") {
				remember(rule);
				return { action: "continue" };
			}
			if (choice === "直接发送，并永久忽略此提醒") {
				ignoreRuleId(ctx, rule.id);
				remember(rule);
				ctx.ui.notify(`已忽略提醒：${rule.id}`, "info");
				return { action: "continue" };
			}
			ctx.ui.setEditorText(event.text);
			ctx.ui.notify("已取消发送，原输入已放回编辑框，可继续修改。", "info");
			return { action: "handled" };
		}

		if (visible.notifies.length > 0 && ctx.hasUI) {
			const message = visible.notifies
				.map((rule) => `- ${rule.message}\n  建议: ${rule.suggestion}`)
				.join("\n");
			ctx.ui.notify(message, "info");
			for (const rule of visible.notifies) remember(rule);
		}
		return { action: "continue" };
	});
}

function remember(rule: ReminderRule): void {
	if (rule.cooldown === "session_once") {
		seenRuleIds.add(rule.id);
	}
}

function candidatePaths(cwd: string): string[] {
	const envPath = resolveReminderRulesPath(cwd, process.env.PI_REMINDER_RULES_PATH);
	const paths = [] as string[];
	if (envPath) paths.push(envPath);
	paths.push(join(resolve(cwd), ".pi-distill", "final", "reminder_rules.json"));
	paths.push(join(resolve(cwd), "reminder_rules.json"));
	return paths;
}

function ignoredRulesPath(cwd: string): string {
	return join(resolve(cwd), ".pi-distill", "reminder_rules_ignored.json");
}

function loadRules(ctx: ExtensionContext, options: { silent: boolean }): LoadedRules | null {
	for (const path of candidatePaths(ctx.cwd)) {
		if (!existsSync(path)) continue;
		try {
			const stat = statSync(path);
			if (cachedRules && cachedRules.path === path && cachedRules.mtimeMs === stat.mtimeMs) {
				return cachedRules;
			}
			const parsed = JSON.parse(readFileSync(path, "utf-8")) as ReminderRulesDocument;
			validateRulesDocument(parsed);
			cachedRules = { path, mtimeMs: stat.mtimeMs, doc: parsed };
			warnedInvalidKey = null;
			return cachedRules;
		} catch (error) {
			const key = `${path}:${safeMtime(path)}`;
			if (!options.silent && ctx.hasUI && warnedInvalidKey !== key) {
				warnedInvalidKey = key;
				ctx.ui.notify(`Reminder rules 已禁用：规则文件无效\n${path}\n${formatError(error)}`, "warning");
			}
			return null;
		}
	}
	return null;
}

function safeMtime(path: string): string {
	try {
		return String(statSync(path).mtimeMs);
	} catch {
		return "missing";
	}
}

function loadIgnoredRuleIds(ctx: ExtensionContext, options: { silent: boolean }): Set<string> {
	const path = ignoredRulesPath(ctx.cwd);
	if (!existsSync(path)) return new Set<string>();
	try {
		const stat = statSync(path);
		if (cachedIgnoredRules && cachedIgnoredRules.path === path && cachedIgnoredRules.mtimeMs === stat.mtimeMs) {
			return new Set(cachedIgnoredRules.doc.ignored_rule_ids);
		}
		const parsed = JSON.parse(readFileSync(path, "utf-8")) as IgnoredRulesDocument;
		if (!parsed || !Array.isArray(parsed.ignored_rule_ids)) {
			throw new Error("ignored_rule_ids must be an array");
		}
		cachedIgnoredRules = { path, mtimeMs: stat.mtimeMs, doc: parsed };
		warnedInvalidIgnoredKey = null;
		return new Set(parsed.ignored_rule_ids);
	} catch (error) {
		const key = `${path}:${safeMtime(path)}`;
		if (!options.silent && ctx.hasUI && warnedInvalidIgnoredKey !== key) {
			warnedInvalidIgnoredKey = key;
			ctx.ui.notify(`Reminder rules 忽略列表无效，已按空列表处理\n${path}\n${formatError(error)}`, "warning");
		}
		return new Set<string>();
	}
}

function ignoreRuleId(ctx: ExtensionContext, ruleId: string): void {
	const path = ignoredRulesPath(ctx.cwd);
	const current = loadIgnoredRuleIds(ctx, { silent: true });
	current.add(ruleId);
	persistIgnoredRuleIds(path, current);
}

function clearIgnoredRuleIds(ctx: ExtensionContext): void {
	const path = ignoredRulesPath(ctx.cwd);
	persistIgnoredRuleIds(path, new Set<string>());
}

function persistIgnoredRuleIds(path: string, ids: Set<string>): void {
	mkdirSync(dirname(path), { recursive: true });
	const doc: IgnoredRulesDocument = { ignored_rule_ids: Array.from(ids).sort() };
	writeFileSync(path, JSON.stringify(doc, null, 2) + "\n", "utf-8");
	const stat = statSync(path);
	cachedIgnoredRules = { path, mtimeMs: stat.mtimeMs, doc };
	warnedInvalidIgnoredKey = null;
}

function validateRulesDocument(doc: ReminderRulesDocument): void {
	if (!doc || !Array.isArray(doc.rules)) throw new Error("rules must be an array");
	for (const rule of doc.rules) {
		if (!rule.id || !rule.mode || !rule.message || !rule.suggestion) {
			throw new Error(`invalid rule: ${JSON.stringify(rule)}`);
		}
	}
}

function formatError(error: unknown): string {
	return error instanceof Error ? error.message : String(error);
}
