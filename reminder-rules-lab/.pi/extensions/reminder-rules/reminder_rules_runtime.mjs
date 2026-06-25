import { resolve } from "node:path";

export function normalizeText(text) {
  return (text || "").toLowerCase();
}

function containsPattern(normalized, pattern) {
  return normalized.includes(String(pattern || "").toLowerCase());
}

function countMatches(normalized, patterns) {
  return (patterns || []).reduce((total, pattern) => total + (containsPattern(normalized, pattern) ? 1 : 0), 0);
}

export function matchesRule(text, rule) {
  const normalized = normalizeText(text);
  const antiPatterns = rule.anti_patterns || [];
  if (antiPatterns.some((pattern) => containsPattern(normalized, pattern))) {
    return { matched: false, score: 0, reason: "anti_pattern" };
  }

  const mustMatchAny = rule.must_match_any || [];
  const anySatisfied = mustMatchAny.length === 0 || mustMatchAny.some((pattern) => containsPattern(normalized, pattern));

  const groups = rule.must_match_all_groups || [];
  const groupsSatisfied = groups.length === 0 || groups.every((group) => (group || []).some((pattern) => containsPattern(normalized, pattern)));

  if (!anySatisfied || !groupsSatisfied) {
    return { matched: false, score: 0, reason: "positive_conditions" };
  }

  const score = countMatches(normalized, mustMatchAny) + groups.reduce((total, group) => total + Math.min(1, countMatches(normalized, group || [])), 0);
  return { matched: true, score, reason: "matched" };
}

function confidenceWeight(value) {
  return value === "high" ? 3 : value === "medium" ? 2 : 1;
}

export function findMatchingRules(text, rules) {
  const matches = [];
  for (const rule of rules || []) {
    const result = matchesRule(text, rule);
    if (!result.matched) continue;
    matches.push({
      rule,
      score: result.score,
      modeWeight: rule.mode === "confirm" ? 10 : 0,
      confidenceWeight: confidenceWeight(rule.confidence),
    });
  }
  return matches.sort((a, b) => (b.modeWeight + b.confidenceWeight + b.score) - (a.modeWeight + a.confidenceWeight + a.score));
}

export function selectVisibleMatches(matches, shownRuleIds = new Set()) {
  const unseen = (matches || []).filter((entry) => !shownRuleIds.has(entry.rule.id));
  const confirm = unseen.find((entry) => entry.rule.mode === "confirm");
  if (confirm) {
    return { confirm: confirm.rule, notifies: [] };
  }
  const notifies = unseen.filter((entry) => entry.rule.mode === "notify").slice(0, 2).map((entry) => entry.rule);
  return { confirm: null, notifies };
}

export function resolveReminderRulesPath(cwd, envPath) {
  if (envPath) return resolve(envPath);
  return null;
}
