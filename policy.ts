/** An installed Hermes plugin, as autodeploy reads it from Hermes's install records and config. */
export interface Install {
  name: string;
  source?: string;
  revision?: string;
  enabled?: boolean;
}

/** One plugin to move from its installed commit to the commit CI asked for. */
export interface Deploy {
  name: string;
  source: string;
  from: string;
  to: string;
  enabled: boolean;
}

/** What CI sends: each plugin's Hermes name, the mirror it is installed from, and the commit. */
export interface DeployRequest {
  plugins?: { name?: unknown; mirror?: unknown; commit?: unknown }[];
}

/** The organization CI publishes the plugin mirrors to (packages/ai/leonsilicon/configs/hermes-mirrors). */
const MIRROR = /^https:\/\/github\.com\/leonsiliconAI\/([A-Za-z0-9._-]+?)(?:\.git)?\/?$/;
const COMMIT = /^[0-9a-f]{40}$/;
const SELF = "autodeploy";

/** The mirror's clone URL when `source` is one of the leonsiliconAI plugin mirrors. */
export function mirrorUrl(source: unknown): string | undefined {
  if (typeof source !== "string") return;
  const match = MIRROR.exec(source.trim());
  return match ? `https://github.com/leonsiliconAI/${match[1]}.git` : undefined;
}

/** Installs a deploy may move: those installed from a mirror. Manual copies have no source. */
export function managed(installs: Install[]): Install[] {
  return installs.filter((install) => mirrorUrl(install.source) !== undefined);
}

/**
 * The installs one request asks for. A plugin is only moved when it is already installed from that
 * same mirror, to a full commit, so the token can't install anything new or from anywhere else.
 * Plugins already at their commit are `current`. Autodeploy moves itself last.
 */
export function plan(request: DeployRequest, installs: Install[]) {
  const errors: string[] = [];
  const deploys: Deploy[] = [];
  const current: string[] = [];
  const wanted = Array.isArray(request?.plugins) ? request.plugins : [];
  if (!wanted.length) errors.push("the request names no plugins");
  const byName = new Map(managed(installs).map((install) => [install.name, install]));
  const seen = new Set<string>();
  for (const item of wanted) {
    const name = typeof item?.name === "string" ? item.name : "";
    const commit = typeof item?.commit === "string" ? item.commit : "";
    const install = byName.get(name);
    if (!install) {
      errors.push(`${name || "(no name)"} isn't installed from a leonsiliconAI mirror`);
      continue;
    }
    if (seen.has(name)) {
      errors.push(`${name} is named twice`);
      continue;
    }
    seen.add(name);
    const source = mirrorUrl(install.source)!;
    if (mirrorUrl(item.mirror) !== source) {
      errors.push(`${name} is installed from ${source}, not ${String(item.mirror)}`);
      continue;
    }
    if (!COMMIT.test(commit)) {
      errors.push(`${name}: ${JSON.stringify(commit)} isn't a full commit`);
      continue;
    }
    if (install.revision === commit) {
      current.push(name);
      continue;
    }
    deploys.push({
      name,
      source,
      from: install.revision ?? "",
      to: commit,
      enabled: install.enabled !== false,
    });
  }
  deploys.sort((a, b) => Number(a.name === SELF) - Number(b.name === SELF));
  return { deploys, current, errors };
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

const short = (commit: string) => commit.slice(0, 7) || "none";

/** The log line for the installs, and whether they end in a restart. */
export function summary(results: { deploy: Deploy; error?: string | null }[]) {
  const moved = results.filter((result) => !result.error);
  const failed = results.filter((result) => result.error);
  const parts = [
    ...moved.map(({ deploy }) => `${deploy.name} ${short(deploy.from)}..${short(deploy.to)}`),
    ...failed.map(({ deploy }) => `${deploy.name} failed at ${short(deploy.to)}`),
  ];
  return {
    restart: moved.length > 0,
    // A new autodeploy only answers CI once the dashboard runs it.
    restartDashboard: moved.some(({ deploy }) => deploy.name === SELF),
    log:
      `autodeploy: ${parts.join(", ") || "nothing to install"}` +
      (moved.length ? "; restarting the gateway once it is idle" : ""),
  };
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
