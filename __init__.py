"""Keep Hermes's plugins at the commit CI last published to their leonsiliconAI mirrors.

CI publishes each plugin under ``apps/hermes-plugin-<name>/`` to ``leonsiliconAI/hermes-plugin-<name>``
after the checks pass on ``main``. Nothing on Hermes's side pulled those commits: Hermes's own updater
is off, refuses pinned installs, and reloads a plugin without restarting the gateway. This plugin
closes the loop from inside the gateway:

* Every ``AUTODEPLOY_INTERVAL`` seconds (default 60) it asks each mirror for its ``main`` commit
  (``git ls-remote``) and compares it with what Hermes recorded at install. Only installs whose source
  is a leonsiliconAI mirror are touched; manual copies and other sources are left alone.
* When one moved, it waits until the gateway has no work in flight for a few seconds, then runs
  ``hermes plugins install <mirror> --force --ref <commit> --yes-deps --enable`` (``--no-enable`` for a
  disabled plugin) for each, so the install stays pinned to exactly what CI published.
* If anything moved, it asks the gateway for its graceful restart (SIGUSR1, the same as ``/restart``):
  Hermes finishes every turn, delegation and watched process first, then the supervisor starts it
  again with the new code.

A failed install keeps the old code, is logged, is posted to ``AUTODEPLOY_SLACK_CHANNEL`` when that is
set, and is retried after 5 and 30 minutes; after three failures that commit is left alone until the
mirror moves again. State lives in ``$HERMES_HOME/plugin-data/autodeploy/state.json``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

from ._bridge import call

logger = logging.getLogger(__name__)

PLUGIN_NAME = "autodeploy"
INTERVAL_ENV = "AUTODEPLOY_INTERVAL"
SLACK_CHANNEL_ENV = "AUTODEPLOY_SLACK_CHANNEL"
DEFAULT_INTERVAL = 60.0
MIN_INTERVAL = 30.0
START_DELAY = 30.0
IDLE_POLL = 2.0
LS_REMOTE_TIMEOUT = 30
INSTALL_TIMEOUT = 600

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_THREAD_NAME = "autodeploy"


def _policy(function: str, *args: Any) -> Any:
    return call(__file__, function, *args)


# --------------------------------------------------------------------------------------------
# Hermes's side: install records, config, the gateway runner


def _hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home())
    except Exception:
        return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def _install_records() -> dict[str, dict]:
    try:
        from hermes_cli import plugins_cmd

        records = plugins_cmd._read_install_metadata()
    except Exception:
        try:
            path = _hermes_home() / "plugins" / ".install-metadata.json"
            records = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
    return records if isinstance(records, dict) else {}


def _plugin_lists() -> tuple[Optional[list], list]:
    """``(plugins.enabled, plugins.disabled)`` from the active profile's config."""
    try:
        from hermes_cli.config import cfg_get, load_config_readonly

        config = load_config_readonly()
        enabled = cfg_get(config, "plugins", "enabled", default=None)
        disabled = cfg_get(config, "plugins", "disabled", default=None) or []
    except Exception:
        return None, []
    return (list(enabled) if isinstance(enabled, list) else None,
            list(disabled) if isinstance(disabled, list) else [])


def read_installs() -> list[dict]:
    enabled, disabled = _plugin_lists()
    installs = []
    for name, record in sorted(_install_records().items()):
        if not isinstance(record, dict):
            continue
        on = name in enabled if enabled is not None else name not in disabled
        installs.append({"name": str(name), "source": record.get("source"),
                         "revision": record.get("revision"), "enabled": bool(on) and name not in disabled})
    return installs


def installed_revision(name: str) -> str:
    record = _install_records().get(name)
    return str(record.get("revision") or "") if isinstance(record, dict) else ""


def gateway_runner() -> Any:
    """The live ``GatewayRunner``, or None outside the gateway (CLI, dashboard, tests)."""
    ref = getattr(sys.modules.get("gateway.run"), "_gateway_runner_ref", None)
    return ref() if callable(ref) else None


def live_cron_runs() -> int:
    """Cron runs in flight, including ``no_agent`` scripts started from the dashboard's Run now.

    The gateway's own work count (and so its graceful restart) only sees runs on its scheduler's
    thread pool; a Run-now script runs under another owner, and a restart ends it mid-script."""
    try:
        from cron.jobs import list_jobs

        return sum(1 for job in list_jobs(include_disabled=True) if job.get("live_execution"))
    except Exception:
        logger.warning("autodeploy: could not read cron runs; treating them as idle", exc_info=True)
        return 0


def active_work(runner: Any) -> int:
    """Turns, cron runs, API runs and background delegations the gateway is holding."""
    count = getattr(runner, "_active_work_count", None)
    if callable(count):
        work = int(count())
    else:
        from gateway.status import read_runtime_status

        work = int((read_runtime_status() or {}).get("active_agents") or 0)
    return work + live_cron_runs()


def restarting(runner: Any) -> bool:
    return bool(getattr(runner, "_restart_requested", False) or getattr(runner, "_draining", False))


def request_restart(_runner: Any) -> None:
    """The gateway's graceful restart: drain in-flight work, exit, and let the supervisor relaunch."""
    os.kill(os.getpid(), signal.SIGUSR1)


# --------------------------------------------------------------------------------------------
# Git and the hermes CLI


def remote_head(url: str) -> Optional[str]:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        out = subprocess.run(["git", "ls-remote", "--refs", url, "refs/heads/main"], capture_output=True,
                             text=True, timeout=LS_REMOTE_TIMEOUT, env=env, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.info("autodeploy: could not reach %s (%s)", url, exc)
        return None
    if out.returncode != 0:
        logger.info("autodeploy: git ls-remote %s failed: %s", url, out.stderr.strip()[-300:])
        return None
    fields = out.stdout.split()
    return fields[0] if fields else None


def _hermes_command() -> list[str]:
    return [sys.executable, "-m", "hermes_cli.main"]


def run_install(args: list[str]) -> tuple[int, str]:
    env = {**os.environ, "NO_COLOR": "1", "TERM": "dumb", "COLUMNS": "200", "GIT_TERMINAL_PROMPT": "0"}
    try:
        out = subprocess.run([*_hermes_command(), *args], capture_output=True, text=True,
                             timeout=INSTALL_TIMEOUT, env=env, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as exc:
        return 124, f"timed out after {INSTALL_TIMEOUT}s\n{exc.stdout or ''}{exc.stderr or ''}"
    except OSError as exc:
        return 127, str(exc)
    return out.returncode, _ANSI.sub("", f"{out.stdout}{out.stderr}")


# --------------------------------------------------------------------------------------------
# Slack, for failures


def _bot_token() -> str:
    from gateway.config import Platform, load_gateway_config

    pconfig = load_gateway_config().platforms.get(Platform.SLACK)
    token = str(getattr(pconfig, "token", "") or "").split(",")[0].strip()
    if not token.startswith("xoxb-"):
        raise RuntimeError("no Slack bot token in the gateway config")
    return token


def notify(text: str) -> None:
    channel = (os.environ.get(SLACK_CHANNEL_ENV) or "").strip()
    if not channel:
        return
    try:
        request = urllib.request.Request(
            "https://slack.com/api/chat.postMessage",
            data=json.dumps({"channel": channel, "text": text, "unfurl_links": False}).encode(),
            headers={"Authorization": f"Bearer {_bot_token()}",
                     "Content-Type": "application/json; charset=utf-8"})
        with urllib.request.urlopen(request, timeout=15) as response:
            data = json.load(response)
        if not data.get("ok"):
            logger.warning("autodeploy: Slack chat.postMessage failed: %s", data.get("error"))
    except Exception:
        logger.warning("autodeploy: could not post to Slack", exc_info=True)


# --------------------------------------------------------------------------------------------
# State


def _state_path() -> Path:
    # Created on the first save, so processes that never deploy (the CLI, the dashboard) write nothing.
    return _hermes_home() / "plugin-data" / PLUGIN_NAME / "state.json"


def load_state(path: Path) -> dict:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".state-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def worker_log() -> Path:
    # Where the plugins' bridge writes a line for each policy worker it had to end.
    return _hermes_home() / "logs" / "policy-worker.log"


# --------------------------------------------------------------------------------------------
# The deployer


def _interval() -> float:
    raw = (os.environ.get(INTERVAL_ENV) or "").strip()
    try:
        return max(MIN_INTERVAL, float(raw)) if raw else DEFAULT_INTERVAL
    except ValueError:
        logger.warning("autodeploy: %s=%r is not a number; using %ss", INTERVAL_ENV, raw, DEFAULT_INTERVAL)
        return DEFAULT_INTERVAL


class Deployer:
    """One check-and-deploy round per :meth:`tick`; every side effect is a replaceable seam."""

    def __init__(self, state_path: Path, *,
                 runner: Callable[[], Any] = gateway_runner,
                 installs: Callable[[], list[dict]] = read_installs,
                 head: Callable[[str], Optional[str]] = remote_head,
                 install: Callable[[list[str]], tuple[int, str]] = run_install,
                 revision: Callable[[str], str] = installed_revision,
                 work: Callable[[Any], int] = active_work,
                 restart: Callable[[Any], None] = request_restart,
                 post: Callable[[str], None] = notify,
                 worker_log: Callable[[], Path] = worker_log,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.time,
                 stop: Optional[threading.Event] = None) -> None:
        self.state_path = state_path
        self.runner, self.installs, self.head, self.install = runner, installs, head, install
        self.revision, self.work, self.restart, self.post = revision, work, restart, post
        self.sleep, self.clock, self.worker_log = sleep, clock, worker_log
        self.stop = stop or threading.Event()
        self.announced = False

    def _report_worker(self, state: dict) -> None:
        """Say in Slack what the plugins' bridge recorded since the last look: a policy worker ended."""
        path = self.worker_log()
        try:
            size = path.stat().st_size
        except OSError:
            return
        seen = state.get("worker_log") or 0
        if size == seen:
            return
        new = ""
        if size > seen:
            with path.open("rb") as log:
                log.seek(seen)
                new = log.read().decode("utf-8", "replace")
        message = _policy("workerMessage", new.splitlines(), str(path))
        state["worker_log"] = size
        save_state(self.state_path, state)
        if message:
            self.post(message)

    def _pending(self, installs: list[dict], state: dict) -> list[dict]:
        managed = _policy("managed", installs)
        heads = {}
        for install in managed:
            heads[install["name"]] = self.head(_policy("mirrorUrl", install["source"]))
        return _policy("pending", installs, heads, state, self.clock() * 1000)

    def _wait_until_idle(self, runner: Any) -> bool:
        samples: list[int] = []
        while not self.stop.is_set():
            if restarting(runner):
                return False
            samples.append(self.work(runner))
            if _policy("idle", samples[-3:]):
                return True
            self.sleep(IDLE_POLL)
        return False

    def tick(self) -> list[dict]:
        """Deploy whatever moved; returns the ``{deploy, error}`` results (empty when nothing did)."""
        runner = self.runner()
        if runner is None or restarting(runner):
            return []
        state = load_state(self.state_path)
        installs = self.installs()
        if not self.announced:
            names = [install["name"] for install in _policy("managed", installs)]
            logger.info("autodeploy: keeping %s at their mirror's main (checked every %ss)",
                        ", ".join(names) or "no plugins", int(_interval()))
            self.announced = True
        self._report_worker(state)
        todo = self._pending(installs, state)
        if not todo:
            return []
        logger.info("autodeploy: %s moved on its mirror; deploying once the gateway is idle",
                    ", ".join(deploy["name"] for deploy in todo))
        if not self._wait_until_idle(runner):
            return []
        # The mirrors may have moved again while Hermes was busy, so look once more.
        state = load_state(self.state_path)
        installs = self.installs()
        todo = self._pending(installs, state)
        results = []
        for deploy in todo:
            code, output = self.install(_policy("installArgs", deploy))
            if code != 0 and _policy("transient", output):
                logger.info("autodeploy: %s is being updated by another Hermes process; trying again next round",
                            deploy["name"])
                continue
            error = None
            if code != 0:
                error = f"hermes plugins install exited {code}\n{output}"
            elif self.revision(deploy["name"]) != deploy["to"]:
                error = (f"the install finished but Hermes recorded {self.revision(deploy['name'])[:12] or 'nothing'},"
                         f" not {deploy['to'][:12]}\n{output}")
            state = _policy("recordResult", state, deploy, error, self.clock() * 1000)
            save_state(self.state_path, state)
            results.append({"deploy": deploy, "error": error})
            if error:
                logger.warning("autodeploy: %s did not move to %s: %s", deploy["name"], deploy["to"][:12],
                               error[-1500:])
                attempt = state["attempts"][deploy["name"]]["count"]
                self.post(_policy("failureMessage", deploy, error, attempt))
        if not results:
            return []
        summary = _policy("summary", results)
        logger.info("%s", summary["log"])
        if summary["restart"] and not restarting(runner):
            self.restart(runner)
        return results

    def run(self) -> None:
        if self.stop.wait(START_DELAY):
            return
        while not self.stop.is_set():
            try:
                self.tick()
            except Exception:
                logger.warning("autodeploy: check failed", exc_info=True)
            if self.stop.wait(_interval()):
                return


# --------------------------------------------------------------------------------------------
# Hermes wiring


def _start() -> None:
    if any(thread.name == _THREAD_NAME for thread in threading.enumerate()):
        return
    deployer = Deployer(_state_path())
    threading.Thread(target=deployer.run, name=_THREAD_NAME, daemon=True).start()


def register(ctx: Any) -> None:
    # Every Hermes process loads plugins; the thread only acts where a gateway runner exists.
    _start()
