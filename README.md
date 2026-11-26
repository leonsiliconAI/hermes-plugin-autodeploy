# @-/com.leonsilicon.hermes.plugins.autodeploy

Hermes plugin that deploys a push to `leonsilicon/leonsilicon.ai`'s `main` to Hermes Cloud, when CI asks. Once a push passes CI, [Deploy Hermes plugins](../../.github/workflows/sync-hermes-plugins.yml) publishes each `apps/hermes-plugin-<name>/` to its `leonsiliconAI/hermes-plugin-<name>` mirror, then calls this plugin on Hermes's dashboard and follows the deploy to its end. The GitHub run shows what was installed and whether the gateway restarted.

Until 2026-11-26 this plugin polled the mirrors from inside the gateway instead, because no Hermes credential was to be in GitHub (Leon's rule of 2026-11-19). Leon then allowed one so that CI could deploy directly. Hermes's own updater still doesn't fit: it refuses pinned installs, holds back an update that adds a tool or hook until someone reviews it, and reloads plugins without restarting the gateway.

## What it does

1. **CI asks.** `_deploy-hermes` ([`deploy.ts`](../../packages/ai/leonsilicon/configs/hermes-mirrors/deploy.ts)) sends `POST /api/plugins/autodeploy/deploy` with `Authorization: Bearer <HERMES_DEPLOY_TOKEN>` and each mirror's `main` commit: `{"plugins": [{"name": "leon", "mirror": "https://github.com/leonsiliconAI/hermes-plugin-leon", "commit": "<40 hex>"}, …], "source", "run"}`.
2. **The dashboard checks it** ([`dashboard/api.py`](dashboard/api.py), [`policy.ts`](policy.ts)). A plugin moves only when it is already installed from that same mirror, and only to a full commit, so the token can't install anything new or from anywhere else. A plugin already at its commit is left alone. If nothing moves, the deploy is `done` at once. Otherwise it writes a deploy record and starts [`deploy.py`](deploy.py) in its own session, so the deploy outlives a dashboard restart.
3. **Install.** `deploy.py` runs `hermes plugins install <mirror> --force --ref <commit> --yes-deps --enable` (`--no-enable` for a disabled plugin) for each, autodeploy itself last, and checks that Hermes recorded that commit. The install stays pinned, which is why `hermes plugins update` and Hermes's auto-update leave it alone. A failed install keeps the old code.
4. **Restart when idle.** If anything moved, it sends the gateway SIGUSR1. That is Hermes's restart when idle: it keeps serving, waits until no turn, delegation or watched process is running, then exits, and its supervisor starts it again with the new code. Nothing running is interrupted. (The dashboard's Restart button and `hermes gateway restart --interrupt` do interrupt: a turn killed that way is never resumed.) `deploy.py` waits up to 20 minutes for the new gateway. A restart still waiting after that ends the deploy as `installed`, and happens on its own once Hermes is idle.
5. **A new autodeploy** only answers CI once the dashboard runs it, so after installing itself, `deploy.py` restarts the dashboard. That never touches the gateway.

`GET /api/plugins/autodeploy/deploy?id=<id>` answers with the record: `status` (`queued`, `installing`, `restarting`, then `done`, `installed` or `failed`), each install's output, and a log. Without `id` it answers with the newest. One deploy runs at a time; the next waits for it. Records are kept in `$HERMES_HOME/plugin-data/autodeploy/deploys/`, with `deploy.py`'s own output beside each.

**The install window.** `hermes plugins install` also tells the running gateway to reload the plugin, and nothing turns that off. So from the install until the restart, the gateway runs the new plugin code reloaded in place. A plugin should survive that; Hermes reloads on `hermes plugins enable` and on dashboard updates too.

**A policy worker that had to be replaced.** The plugins' [bridge](../../DEVELOPMENT.md#hermes-plugin-mirrors) runs their policies in a Node process and ends one whose JavaScript has stopped answering, with a line in `$HERMES_HOME/logs/policy-worker.log`. Autodeploy posts each new line to `AUTODEPLOY_SLACK_CHANNEL` within a minute, from the gateway.

## Settings

| Where                   | Key                        | Meaning                                                                                                                                           |
| ----------------------- | -------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| Hermes `.env`           | `AUTODEPLOY_TOKEN`         | The bearer token CI sends: 43 or more random characters (`python3 -c 'import secrets; print(secrets.token_urlsafe(32))'`). Unset, CI can't deploy |
| Hermes `.env`           | `AUTODEPLOY_SLACK_CHANNEL` | Slack channel or user ID that hears about a policy worker the bridge had to replace. Unset, that is only logged                                   |
| GitHub Actions secret   | `HERMES_DEPLOY_TOKEN`      | The same token as `AUTODEPLOY_TOKEN`                                                                                                              |
| GitHub Actions variable | `HERMES_URL`               | The dashboard, `https://captain-claude-9595.agents.nousresearch.com`                                                                              |

The dashboard reads `AUTODEPLOY_TOKEN` when it starts. To change the token, set it in Hermes's `.env` (`PUT /api/env`) and in GitHub (`gh secret set HERMES_DEPLOY_TOKEN`), then restart the dashboard, which leaves the gateway alone. Its log then shows `autodeploy: CI deploys accepted at /api/plugins/autodeploy/deploy`.

## Install

Once, from the mirror, pinned to its current commit, then restart the dashboard:

```sh
hermes plugins install leonsiliconAI/hermes-plugin-autodeploy --ref "$(git ls-remote https://github.com/leonsiliconAI/hermes-plugin-autodeploy.git refs/heads/main | cut -f1)" --yes-deps --enable
```

A plugin CI should deploy is installed the same way, once, from its own mirror. From then on each push deploys it.

## Test

```sh
vp -C apps/hermes-plugin-autodeploy pack
uv run --no-project --python 3.14 --with pygls==2.1.1 --with fastapi --with httpx --with pytest python -m pytest --rootdir=. --import-mode=importlib apps/hermes-plugin-autodeploy/__tests__
node --test packages/ai/leonsilicon/configs/hermes-mirrors/__tests__/deploy.test.ts
```
