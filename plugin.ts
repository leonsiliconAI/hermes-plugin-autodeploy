import { fileURLToPath } from "node:url";

/** Hermes plugin name, as listed in `plugins.enabled`. */
export const pluginName = "autodeploy";

/** Directory published to `leonsiliconAI/hermes-plugin-autodeploy` and installed to `$HERMES_HOME/plugins/autodeploy/`. */
export const pluginDirectory = fileURLToPath(new URL(".", import.meta.url));

/** Files that make up the deployed plugin. */
export const pluginFiles = [
  "plugin.yaml",
  "__init__.py",
  "deploy.py",
  "pyproject.toml",
  "_bridge.py",
  "bridge.cjs",
  "policy.cjs",
  "dashboard/manifest.json",
  "dashboard/api.py",
  "dashboard/index.js",
] as const;

/** The dashboard route CI calls: `POST` starts a deploy, `GET ?id=` reports one. */
export const deployPath = "/api/plugins/autodeploy/deploy";

/** `.env` setting: the bearer token CI sends (`HERMES_DEPLOY_TOKEN` in GitHub). Unset, CI can't deploy. */
export const tokenEnv = "AUTODEPLOY_TOKEN";

/** `.env` setting: Slack channel or user ID that hears about a policy worker the bridge had to replace. */
export const slackChannelEnv = "AUTODEPLOY_SLACK_CHANNEL";
