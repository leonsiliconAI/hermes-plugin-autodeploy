# @-/com.leonsilicon.hermes.plugins.autodeploy

Hermes plugin that deploys a push to `leonsilicon/leonsilicon.ai`'s `main` to Hermes Cloud. Every Hermes plugin installed from a `leonsiliconAI` mirror stays at the commit CI last published to that mirror.

CI publishes `apps/hermes-plugin-<name>/` to `leonsiliconAI/hermes-plugin-<name>` once its checks pass ([Sync Hermes plugin mirrors](https://github.com/leonsilicon/leonsilicon.ai/blob/main/.github/workflows/sync-hermes-plugins.yml)). Before this plugin, nothing on Hermes Cloud picked those commits up:

- Hermes's own updater was off (`plugins.auto_apply: false`, checked once a day).
- Even with it on, it refuses pinned installs, and it holds back any update that adds a tool or hook until someone reviews it.
- It reloads a plugin inside the running gateway without restarting it. That mixed old and new code once (`host.approvals is not a function`, 2026-11-25).

## What it does

Inside the gateway, every `AUTODEPLOY_INTERVAL` seconds (default 60, minimum 30):

1. **Check.** For each install whose recorded source is `https://github.com/leonsiliconAI/<repo>`, ask the mirror for its `main` commit (`git ls-remote`) and compare it with the commit Hermes recorded. Manual copies and installs from anywhere else are left alone.
2. **Wait for idle.** When a mirror has moved, wait until the gateway's active work (turns, cron runs, API runs, background delegations) has been zero for three checks in a row, two seconds apart. Then look at the mirrors again, in case they moved meanwhile. Work also counts every live run in Hermes's cron execution table. The gateway's own count, and so its graceful restart, misses a `no_agent` script started with the dashboard's Run now, and on 2026-11-25 a restart ended one mid-script.
3. **Install.** Run `hermes plugins install <mirror> --force --ref <commit> --yes-deps --enable` for each plugin that moved (`--no-enable` keeps a disabled one disabled). The install stays pinned to exactly that commit, which is why `hermes plugins update` and Hermes's auto-update leave it alone. Afterwards Hermes's install record must name the new commit, or the install counts as failed.
4. **Restart.** If anything moved, send the gateway SIGUSR1. That is Hermes's graceful restart, the same one `/restart` and `hermes gateway restart` use: it finishes in-flight turns, delegations and watched processes first, then exits, and the supervisor starts it again with the new code. Autodeploy updates itself this way too.

An install that finds another updater holding the plugin's lock (`hermes plugins update`, the dashboard, a script) changes nothing, and autodeploy tries again on the next round. A failed install keeps the old code. The failure is logged, posted to `AUTODEPLOY_SLACK_CHANNEL` if that is set, and retried after 5 and then 30 minutes. After three failures, that commit is left alone until the mirror moves again. The attempts are kept in `$HERMES_HOME/plugin-data/autodeploy/state.json`.

**A gateway the watchdog ended.** The plugins' [PythonMonkey bridge](../../DEVELOPMENT.md#hermes-plugin-mirrors) ends a gateway whose JavaScript has stopped responding and leaves every thread's stack in `$HERMES_HOME/logs/pythonmonkey-deadman.log`. When the next gateway starts, autodeploy posts that once to `AUTODEPLOY_SLACK_CHANNEL`, with the time and the file, so a restart nobody asked for doesn't pass unnoticed.

**The install window.** `hermes plugins install` also tells the running gateway to reload the plugin over its control socket. Nothing turns that off, and the socket's path follows `HERMES_HOME`. So from the first install until the restart, the gateway runs the new plugin code reloaded in place. A turn that starts in that window, about half a minute per plugin, can meet a half-reloaded plugin. On 2026-11-25 hermes22muse lost its Muse relay that way, because its Slack handler, which sets the relay, isn't run again on a reload. Autodeploy keeps the window short: it starts only when the gateway is idle and requests the restart as soon as the last install ends. A plugin should still survive a reload. Hermes reloads on `hermes plugins enable` and on dashboard updates too. Closing the window completely would mean stopping the gateway for the installs (what `hermes import` does with `gateway_paused`). That relies on Hermes internals, and if it failed midway it could leave Hermes down, so autodeploy doesn't.

Only the gateway process acts. Hermes loads plugins in its CLI and dashboard too, but there the plugin finds no gateway runner and does nothing.

Hermes refuses cron jobs that restart the gateway, to prevent restart loops (`cron/lifecycle_guard.py`). That's why the restart is sent from inside the gateway rather than from a cron script. A loop can't start here: autodeploy restarts only after an install recorded a new commit, and after the restart every install matches its mirror.

## Settings

| `.env` key                 | Meaning                                                                                                                                    |
| -------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------ |
| `AUTODEPLOY_INTERVAL`      | Seconds between mirror checks. Default 60, minimum 30.                                                                                     |
| `AUTODEPLOY_SLACK_CHANNEL` | Slack channel or user ID that hears about failed deploys and about a gateway the PythonMonkey watchdog ended. Unset, both are only logged. |

## Install

Once, from the mirror, pinned to its current commit, then restart the gateway when Hermes is idle:

```sh
hermes plugins install leonsiliconAI/hermes-plugin-autodeploy --ref "$(git ls-remote https://github.com/leonsiliconAI/hermes-plugin-autodeploy.git refs/heads/main | cut -f1)" --yes-deps --enable
```

The gateway log then shows `autodeploy: keeping <plugins> at their mirror's main`, and one `autodeploy: <plugin> <old>..<new>` line per deploy.

## Test

```sh
vp -C apps/hermes-plugin-autodeploy pack
uv run --no-project --python 3.14 --with pythonmonkey==1.5.1 --with pytest python -m pytest --rootdir=. --import-mode=importlib apps/hermes-plugin-autodeploy/__tests__
```
