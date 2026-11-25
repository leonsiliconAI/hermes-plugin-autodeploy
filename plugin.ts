import { fileURLToPath } from "node:url";

/** Hermes plugin name, as listed in `plugins.enabled`. */
export const pluginName = "autodeploy";

/** Directory published to `leonsiliconAI/hermes-plugin-autodeploy` and installed to `$HERMES_HOME/plugins/autodeploy/`. */
export const pluginDirectory = fileURLToPath(new URL(".", import.meta.url));

/** Files that make up the deployed plugin. */
export const pluginFiles = [
  "plugin.yaml",
  "__init__.py",
  "pyproject.toml",
  "_pythonmonkey.py",
  "policy.cjs",
] as const;

/** `.env` setting: seconds between mirror checks (default 60, minimum 30). */
export const intervalEnv = "AUTODEPLOY_INTERVAL";

/** `.env` setting: Slack channel or user ID that hears about failed deploys. Unset, failures are only logged. */
export const slackChannelEnv = "AUTODEPLOY_SLACK_CHANNEL";
