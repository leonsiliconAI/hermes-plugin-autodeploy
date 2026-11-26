/** An installed Hermes plugin, as autodeploy reads it from Hermes's install records and config. */
export interface Install {
  name: string;
  source?: string;
  revision?: string;
  enabled?: boolean;
}

/** One plugin to move from its installed commit to its mirror's newest commit on `main`. */
export interface Deploy {
  name: string;
  source: string;
  from: string;
  to: string;
  enabled: boolean;
}

/** Failed attempts at the commit a plugin is being moved to. A new commit starts over. */
export interface Attempt {
  target: string;
  count: number;
  at: number;
  error?: string;
}

export interface State {
  attempts?: Record<string, Attempt>;
}

/** The organization CI publishes the plugin mirrors to (packages/ai/leonsilicon/configs/hermes-mirrors). */
const MIRROR = /^https:\/\/github\.com\/leonsiliconAI\/([A-Za-z0-9._-]+?)(?:\.git)?\/?$/;
const COMMIT = /^[0-9a-f]{40}$/;

/** Tries per commit, and how long to wait after each failure before the next one (milliseconds). */
export const maxAttempts = 3;
const retryDelays = [0, 5 * 60_000, 30 * 60_000];

/** The mirror's clone URL when `source` is one of the leonsiliconAI plugin mirrors. */
export function mirrorUrl(source: unknown): string | undefined {
  if (typeof source !== "string") return;
  const match = MIRROR.exec(source.trim());
  return match ? `https://github.com/leonsiliconAI/${match[1]}.git` : undefined;
}

/** Installs autodeploy keeps current: those installed from a mirror. Manual copies have no source. */
export function managed(installs: Install[]): Install[] {
  return installs.filter((install) => mirrorUrl(install.source) !== undefined);
}

/**
 * The plugins whose mirror moved past what's installed. `heads` maps a plugin name to its mirror's
 * `main` commit (missing when the lookup failed). A commit that already failed waits out its retry
 * delay, and after `maxAttempts` failures it is left alone until the mirror moves again.
 */
export function pending(
  installs: Install[],
  heads: Record<string, string | undefined>,
  state: State,
  now: number,
): Deploy[] {
  const deploys: Deploy[] = [];
  for (const install of managed(installs)) {
    const head = heads[install.name];
    if (!head || !COMMIT.test(head) || head === install.revision) continue;
    const attempt = state.attempts?.[install.name];
    if (attempt?.target === head) {
      if (attempt.count >= maxAttempts) continue;
      if (now - attempt.at < (retryDelays[attempt.count] ?? retryDelays.at(-1)!)) continue;
    }
    deploys.push({
      name: install.name,
      source: mirrorUrl(install.source)!,
      from: install.revision ?? "",
      to: head,
      enabled: install.enabled !== false,
    });
  }
  return deploys;
}

/** The arguments to `hermes` that move one plugin: pinned to the exact commit, enabled state kept. */
export function installArgs(deploy: Deploy): string[] {
  return [
    "plugins",
    "install",
    deploy.source,
    "--force",
    "--ref",
    deploy.to,
    "--yes-deps",
    deploy.enabled ? "--enable" : "--no-enable",
  ];
}

/** The state after one install. A success clears the plugin's attempts; a failure counts one. */
export function recordResult(
  state: State,
  deploy: Deploy,
  error: string | null | undefined,
  now: number,
): State {
  const attempts = { ...state.attempts };
  if (!error) {
    delete attempts[deploy.name];
  } else {
    const previous = attempts[deploy.name];
    const count = previous?.target === deploy.to ? previous.count + 1 : 1;
    attempts[deploy.name] = { target: deploy.to, count, at: now, error: error.slice(0, 2000) };
  }
  return { ...state, attempts };
}

/** True when the install changed nothing because another updater held the plugin's lock: try next round. */
export function transient(output: string): boolean {
  return /is already being updated by another Hermes process/.test(output);
}

/** True once the last `quiet` samples of the gateway's active-work count are all zero. */
export function idle(samples: number[], quiet = 3): boolean {
  const recent = samples.slice(-quiet);
  return recent.length === quiet && recent.every((count) => count === 0);
}

const short = (commit: string) => commit.slice(0, 7) || "none";

/** The gateway log line for a finished round, and whether it ends in a restart. */
export function summary(results: { deploy: Deploy; error?: string | null }[]) {
  const moved = results.filter((result) => !result.error);
  const failed = results.filter((result) => result.error);
  const parts = [
    ...moved.map(({ deploy }) => `${deploy.name} ${short(deploy.from)}..${short(deploy.to)}`),
    ...failed.map(({ deploy }) => `${deploy.name} failed at ${short(deploy.to)}`),
  ];
  return {
    restart: moved.length > 0,
    log:
      `autodeploy: ${parts.join(", ")}` +
      (moved.length ? "; restarting the gateway once it is idle" : ""),
  };
}

/** The Slack message for a failed install. */
export function failureMessage(deploy: Deploy, error: string, attempt: number): string {
  const next =
    attempt >= maxAttempts
      ? "I won't try this commit again; the next mirror commit starts over."
      : `I'll try again in ${Math.round((retryDelays[attempt] ?? 0) / 60_000)} minutes (attempt ${attempt} of ${maxAttempts}).`;
  const detail = error.trim().split("\n").slice(-12).join("\n").slice(-1500);
  return [
    `:warning: autodeploy couldn't move *${deploy.name}* to \`${short(deploy.to)}\` (${deploy.source.replace(/\.git$/, "")}/commit/${deploy.to}). It stays on \`${short(deploy.from)}\`.`,
    next,
    "```",
    detail || "(no output)",
    "```",
  ].join("\n");
}

/**
 * The Slack message for what the plugins' bridge wrote since the last look: each line is a policy
 * worker it had to end, or one that died (see `_bridge.py`). Null when there is nothing new.
 */
export function workerMessage(lines: string[], path: string): string | null {
  const ended = lines.map((line) => line.trim()).filter(Boolean);
  if (!ended.length) return null;
  return [
    ":rotating_light: The process that runs Hermes's plugin policies had to be replaced. Hermes kept running; the calls that were in it failed.",
    "```",
    ...ended.slice(-5),
    "```",
    `The full record is \`${path}\`.`,
  ].join("\n");
}
