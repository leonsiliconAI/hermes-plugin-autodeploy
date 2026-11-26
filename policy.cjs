Object.defineProperty(exports, Symbol.toStringTag, { value: "Module" });
// policy.ts
/** The organization CI publishes the plugin mirrors to (packages/ai/leonsilicon/configs/hermes-mirrors). */
const MIRROR = /^https:\/\/github\.com\/leonsiliconAI\/([A-Za-z0-9._-]+?)(?:\.git)?\/?$/;
const COMMIT = /^[0-9a-f]{40}$/;
/** Tries per commit, and how long to wait after each failure before the next one (milliseconds). */
const maxAttempts = 3;
const retryDelays = [
	0,
	3e5,
	18e5
];
/** The mirror's clone URL when `source` is one of the leonsiliconAI plugin mirrors. */
function mirrorUrl(source) {
	if (typeof source !== "string") return;
	const match = MIRROR.exec(source.trim());
	return match ? `https://github.com/leonsiliconAI/${match[1]}.git` : void 0;
}
/** Installs autodeploy keeps current: those installed from a mirror. Manual copies have no source. */
function managed(installs) {
	return installs.filter((install) => mirrorUrl(install.source) !== void 0);
}
/**
* The plugins whose mirror moved past what's installed. `heads` maps a plugin name to its mirror's
* `main` commit (missing when the lookup failed). A commit that already failed waits out its retry
* delay, and after `maxAttempts` failures it is left alone until the mirror moves again.
*/
function pending(installs, heads, state, now) {
	const deploys = [];
	for (const install of managed(installs)) {
		const head = heads[install.name];
		if (!head || !COMMIT.test(head) || head === install.revision) continue;
		const attempt = state.attempts?.[install.name];
		if (attempt?.target === head) {
			if (attempt.count >= 3) continue;
			if (now - attempt.at < (retryDelays[attempt.count] ?? retryDelays.at(-1))) continue;
		}
		deploys.push({
			name: install.name,
			source: mirrorUrl(install.source),
			from: install.revision ?? "",
			to: head,
			enabled: install.enabled !== false
		});
	}
	return deploys;
}
/** The arguments to `hermes` that move one plugin: pinned to the exact commit, enabled state kept. */
function installArgs(deploy) {
	return [
		"plugins",
		"install",
		deploy.source,
		"--force",
		"--ref",
		deploy.to,
		"--yes-deps",
		deploy.enabled ? "--enable" : "--no-enable"
	];
}
/** The state after one install. A success clears the plugin's attempts; a failure counts one. */
function recordResult(state, deploy, error, now) {
	const attempts = { ...state.attempts };
	if (!error) delete attempts[deploy.name];
	else {
		const previous = attempts[deploy.name];
		const count = previous?.target === deploy.to ? previous.count + 1 : 1;
		attempts[deploy.name] = {
			target: deploy.to,
			count,
			at: now,
			error: error.slice(0, 2e3)
		};
	}
	return {
		...state,
		attempts
	};
}
/** True when the install changed nothing because another updater held the plugin's lock: try next round. */
function transient(output) {
	return /is already being updated by another Hermes process/.test(output);
}
/** True once the last `quiet` samples of the gateway's active-work count are all zero. */
function idle(samples, quiet = 3) {
	const recent = samples.slice(-quiet);
	return recent.length === quiet && recent.every((count) => count === 0);
}
const short = (commit) => commit.slice(0, 7) || "none";
/** The gateway log line for a finished round, and whether it ends in a restart. */
function summary(results) {
	const moved = results.filter((result) => !result.error);
	const failed = results.filter((result) => result.error);
	const parts = [...moved.map(({ deploy }) => `${deploy.name} ${short(deploy.from)}..${short(deploy.to)}`), ...failed.map(({ deploy }) => `${deploy.name} failed at ${short(deploy.to)}`)];
	return {
		restart: moved.length > 0,
		log: `autodeploy: ${parts.join(", ")}` + (moved.length ? "; restarting the gateway once it is idle" : "")
	};
}
/** The Slack message for a failed install. */
function failureMessage(deploy, error, attempt) {
	const next = attempt >= 3 ? "I won't try this commit again; the next mirror commit starts over." : `I'll try again in ${Math.round((retryDelays[attempt] ?? 0) / 6e4)} minutes (attempt ${attempt} of 3).`;
	const detail = error.trim().split("\n").slice(-12).join("\n").slice(-1500);
	return [
		`:warning: autodeploy couldn't move *${deploy.name}* to \`${short(deploy.to)}\` (${deploy.source.replace(/\.git$/, "")}/commit/${deploy.to}). It stays on \`${short(deploy.from)}\`.`,
		next,
		"```",
		detail || "(no output)",
		"```"
	].join("\n");
}
/**
* The Slack message for what the plugins' bridge wrote since the last look: each line is a policy
* worker it had to end, or one that died (see `_bridge.py`). Null when there is nothing new.
*/
function workerMessage(lines, path) {
	const ended = lines.map((line) => line.trim()).filter(Boolean);
	if (!ended.length) return null;
	return [
		":rotating_light: The process that runs Hermes's plugin policies had to be replaced. Hermes kept running; the calls that were in it failed.",
		"```",
		...ended.slice(-5),
		"```",
		`The full record is \`${path}\`.`
	].join("\n");
}
exports.failureMessage = failureMessage;
exports.idle = idle;
exports.installArgs = installArgs;
exports.managed = managed;
exports.maxAttempts = maxAttempts;
exports.mirrorUrl = mirrorUrl;
exports.pending = pending;
exports.recordResult = recordResult;
exports.summary = summary;
exports.transient = transient;
exports.workerMessage = workerMessage;
