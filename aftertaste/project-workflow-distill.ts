import { homedir } from "node:os";
import { dirname, join, resolve } from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const scriptPath = join(here, "workflow_distill.py");

export default function projectDistill(pi: ExtensionAPI) {
	pi.registerCommand("project-workflow-distill", {
		description: "Run workflow-pattern distill for the current project",
		handler: async (_args, ctx) => {
			const result = await pi.exec(
				"python3",
				[
					scriptPath,
					"--project-cwd",
					ctx.cwd,
				],
				{ cwd: ctx.cwd, timeout: 120_000 },
			);

			if (result.code !== 0) {
				throw new Error(result.stderr || result.stdout || "project-workflow-distill failed");
			}

			if (ctx.hasUI) {
				const reportPath = join(homedir(), ".pi-distill", "projects", projectId(ctx.cwd), "latest", "workflow", "report.md");
				ctx.ui.notify(`Workflow distill wrote report to ${reportPath}`, "info");
			}
		},
	});
}

function projectId(value: string): string {
	const normalized = resolve(value).replace(/\\/g, "/").replace(/^\/+/, "");
	const joined = normalized.replace(/\/+/g, "-").replace(/[^a-zA-Z0-9._-]/g, "_").replace(/-{2,}/g, "-");
	return joined.replace(/^[-_.]+|[-_.]+$/g, "") || "project";
}
