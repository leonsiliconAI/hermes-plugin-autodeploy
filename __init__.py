"""Deploy a push to main to Hermes Cloud, when CI asks for it.

CI publishes each plugin under ``apps/hermes-plugin-<name>/`` to ``leonsiliconAI/hermes-plugin-<name>``,
then calls ``POST /api/plugins/autodeploy/deploy`` on Hermes's dashboard with each mirror's commit and
the ``AUTODEPLOY_TOKEN`` bearer token (``HERMES_DEPLOY_TOKEN`` in GitHub). Until 2026-11-26 this plugin
polled the mirrors instead; Leon then put a Hermes credential in GitHub so CI could deploy directly and
show the result.

* **The dashboard** (``dashboard/api.py``) checks the request against Hermes's install records: only a
  plugin already installed from that same leonsiliconAI mirror moves, and only to a full commit. It
  writes a deploy record and starts ``deploy.py`` on its own, then answers with the record's id. CI
  polls ``GET /api/plugins/autodeploy/deploy?id=<id>`` until it ends.
* **``deploy.py``** runs ``hermes plugins install <mirror> --force --ref <commit> --yes-deps --enable``
  (``--no-enable`` for a disabled plugin) for each, checks the commit Hermes recorded, then sends the
  gateway SIGUSR1: Hermes's restart when idle, which finishes every turn, delegation and watched
  process first. It waits up to ``RESTART_WAIT`` seconds for the new gateway; a restart still waiting
  for Hermes to go idle after that ends the deploy as ``installed``, and happens later on its own.
  When autodeploy itself moved, it then restarts the dashboard, which runs this API.
* **The gateway** keeps reporting, to ``AUTODEPLOY_SLACK_CHANNEL``, each policy worker the plugins'
  bridge had to replace.

Records live in ``$HERMES_HOME/plugin-data/autodeploy/deploys/``. One deploy runs at a time.
"""

from __future__ import annotations

import fcntl
import hmac
import json
import logging
import math
import os
import re
import secrets
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Optional

from ._bridge import call

logger = logging.getLogger(__name__)

PLUGIN_NAME = "autodeploy"
TOKEN_ENV = "AUTODEPLOY_TOKEN"
SLACK_CHANNEL_ENV = "AUTODEPLOY_SLACK_CHANNEL"
PROVIDER_NAME = "autodeploy-ci"
DEPLOY_PATH = f"/api/plugins/{PLUGIN_NAME}/deploy"
INSTALL_TIMEOUT = 900
RESTART_WAIT = 20 * 60
REPORT_INTERVAL = 60.0
START_DELAY = 30.0
MIN_TOKEN_CHARS = 43

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_THREAD_NAME = "autodeploy"
_ID = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$")


def _policy(function: str, *args: Any) -> Any:
    return call(__file__, function, *args)


# --------------------------------------------------------------------------------------------
# Hermes's side: install records, config, the gateway


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


def gateway_pid() -> Optional[int]:
    """The running gateway's PID, from its PID record."""
    try:
        from gateway.status import get_running_pid

        pid = get_running_pid()
    except Exception:
        logger.warning("autodeploy: could not read the gateway's PID", exc_info=True)
        return None
    return int(pid) if pid else None


def request_restart(pid: int) -> None:
    """Hermes's restart when idle (SIGUSR1): finish in-flight work, exit, let the supervisor relaunch."""
    os.kill(pid, signal.SIGUSR1)


def run_install(args: list[str]) -> tuple[int, str]:
    env = {**os.environ, "NO_COLOR": "1", "TERM": "dumb", "COLUMNS": "200", "GIT_TERMINAL_PROMPT": "0"}
    try:
        out = subprocess.run([sys.executable, "-m", "hermes_cli.main", *args], capture_output=True, text=True,
                             timeout=INSTALL_TIMEOUT, env=env, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as exc:
        return 124, f"timed out after {INSTALL_TIMEOUT}s\n{exc.stdout or ''}{exc.stderr or ''}"
    except OSError as exc:
        return 127, str(exc)
    return out.returncode, _ANSI.sub("", f"{out.stdout}{out.stderr}")


# --------------------------------------------------------------------------------------------
# Slack, for policy workers that had to be replaced


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
# Files


def data_dir() -> Path:
    return _hermes_home() / "plugin-data" / PLUGIN_NAME


def load_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def save_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def worker_log() -> Path:
    # Where the plugins' bridge writes a line for each policy worker it had to end.
    return _hermes_home() / "logs" / "policy-worker.log"


# --------------------------------------------------------------------------------------------
# Deploys: the record the dashboard writes, the steps deploy.py runs


class Deploys:
    """Deploy records under ``directory``; one file per deploy, written whole on every change."""

    def __init__(self, directory: Optional[Path] = None, clock: Callable[[], float] = time.time):
        self.directory = directory or data_dir() / "deploys"
        self.clock = clock

    def path(self, deploy_id: str) -> Path:
        if not _ID.match(deploy_id or ""):
            raise ValueError("not a deploy id")
        return self.directory / f"{deploy_id}.json"

    def create(self, request: dict, plan: dict) -> dict:
        now = self.clock()
        deploy_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now)) + "-" + secrets.token_hex(4)
        record = {"id": deploy_id, "status": "queued", "created": now, "updated": now,
                  "source": request.get("source"), "run": request.get("run"),
                  "deploys": plan["deploys"], "current": plan["current"], "steps": [], "log": []}
        self.save(record)
        return record

    def save(self, record: dict) -> None:
        record["updated"] = self.clock()
        save_json(self.path(record["id"]), record)

    def get(self, deploy_id: str) -> dict:
        return load_json(self.path(deploy_id))

    def latest(self) -> dict:
        records = sorted(self.directory.glob("*.json")) if self.directory.exists() else []
        return load_json(records[-1]) if records else {}

    def lock(self):
        """Held by the deploy that runs; the next one waits for it."""
        self.directory.mkdir(parents=True, exist_ok=True)
        handle = open(self.directory / ".lock", "w")
        fcntl.flock(handle, fcntl.LOCK_EX)
        return handle


def run_deploy(record: dict, deploys: Deploys, *,
               install: Callable[[list[str]], tuple[int, str]] = run_install,
               revision: Callable[[str], str] = installed_revision,
               pid: Callable[[], Optional[int]] = gateway_pid,
               restart: Callable[[int], None] = request_restart,
               restart_dashboard: Callable[[], None] = lambda: None,
               sleep: Callable[[float], None] = time.sleep,
               clock: Callable[[], float] = time.monotonic,
               restart_wait: float = RESTART_WAIT) -> dict:
    """Install each planned commit, then restart the gateway when idle. Returns the final record."""

    def log(line: str) -> None:
        record["log"].append(line)
        logger.info("%s", line)
        deploys.save(record)

    record["status"] = "installing"
    deploys.save(record)
    results = []
    for deploy in record["deploys"]:
        log(f"installing {deploy['name']} at {deploy['to'][:12]}")
        code, output = install(_policy("installArgs", deploy))
        error = None
        if code != 0:
            error = f"hermes plugins install exited {code}"
        elif revision(deploy["name"]) != deploy["to"]:
            error = f"the install finished but Hermes recorded {revision(deploy['name'])[:12] or 'nothing'}"
        record["steps"].append({"name": deploy["name"], "from": deploy["from"], "to": deploy["to"],
                                "ok": error is None, "error": error, "output": output[-4000:]})
        results.append({"deploy": deploy, "error": error})
        log(f"{deploy['name']}: {'installed' if error is None else error}")
    summary = _policy("summary", results)
    log(summary["log"])
    failed = any(result["error"] for result in results)
    if not summary["restart"]:
        record["status"] = "failed" if failed else "done"
        deploys.save(record)
        return record

    before = pid()
    if before is None:
        record["status"] = "failed"
        log("no running gateway to restart")
        return record
    restart(before)
    record["status"] = "restarting"
    record["restart"] = {"from": before}
    log(f"asked gateway {before} to restart once it is idle")
    started = clock()
    after = None
    while clock() - started < restart_wait:
        sleep(5)
        current = pid()
        if current and current != before:
            after = current
            break
    if after is None:
        record["status"] = "failed" if failed else "installed"
        log(f"gateway {before} is still finishing work; it restarts once it is idle")
    else:
        record["restart"]["to"] = after
        record["status"] = "failed" if failed else "done"
        log(f"gateway restarted as {after}")
    if summary["restartDashboard"]:
        log("restarting the dashboard, which runs this API")
        deploys.save(record)
        restart_dashboard()
    deploys.save(record)
    return record


def start_worker(record: dict, dashboard_pid: int) -> None:
    """``deploy.py`` in its own session, so a dashboard restart doesn't end it."""
    directory = data_dir() / "deploys"
    directory.mkdir(parents=True, exist_ok=True)
    out = open(directory / f"{record['id']}.log", "ab")
    subprocess.Popen([sys.executable, str(Path(__file__).with_name("deploy.py")), record["id"], str(dashboard_pid)],
                     stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                     start_new_session=True, env=os.environ.copy(), close_fds=True)


# --------------------------------------------------------------------------------------------
# The token CI sends


def token_strength(token: str) -> Optional[str]:
    """Why ``token`` is too weak to accept, or None (the bar Hermes's drain secret uses)."""
    if len(token) < MIN_TOKEN_CHARS:
        return f"{TOKEN_ENV} is shorter than {MIN_TOKEN_CHARS} characters"
    if len(set(token)) < 16:
        return f"{TOKEN_ENV} has fewer than 16 distinct characters"
    bits = -sum(c / len(token) * math.log2(c / len(token)) for c in Counter(token).values()) * len(token)
    if bits < 128:
        return f"{TOKEN_ENV} has less than 128 bits of entropy"
    return None


def make_provider(token: str):
    """A dashboard auth provider that accepts ``token`` as a bearer, on the deploy route only."""
    from hermes_cli.dashboard_auth import DashboardAuthProvider, TokenPrincipal

    secret = token.encode()

    class AutodeployTokenProvider(DashboardAuthProvider):
        name = PROVIDER_NAME
        display_name = "Autodeploy (CI deploy token)"
        supports_token = True
        supports_session = False

        def verify_token(self, *, token: str):  # noqa: A002 - Hermes's keyword
            if token and hmac.compare_digest(token.encode(), secret):
                return TokenPrincipal(principal="github-actions", provider=PROVIDER_NAME, scopes=("deploy",))
            return None

        def start_login(self, *, redirect_uri: str):
            raise NotImplementedError("a CI token; there is no login")

        def complete_login(self, *, code: str, state: str, code_verifier: str, redirect_uri: str):
            raise NotImplementedError("a CI token; there is no login")

        def verify_session(self, *, access_token: str):
            return None

        def refresh_session(self, *, refresh_token: str):
            raise NotImplementedError("a CI token; there is no session")

        def revoke_session(self, *, refresh_token: str) -> None:
            return None

    return AutodeployTokenProvider()


def register_token(ctx: Any) -> None:
    token = (os.environ.get(TOKEN_ENV) or "").strip()
    if not token:
        logger.info("autodeploy: %s is unset; CI can't deploy", TOKEN_ENV)
        return
    weak = token_strength(token)
    if weak:
        logger.warning("autodeploy: %s; CI can't deploy", weak)
        return
    try:
        ctx.register_dashboard_auth_provider(make_provider(token))
        from hermes_cli.dashboard_auth.token_auth import register_token_route

        register_token_route(DEPLOY_PATH)
    except Exception:
        logger.warning("autodeploy: could not accept CI's token", exc_info=True)
        return
    logger.info("autodeploy: CI deploys accepted at %s", DEPLOY_PATH)


# --------------------------------------------------------------------------------------------
# The gateway: report policy workers that had to be replaced


class Reporter:
    def __init__(self, state_path: Path, *, post: Callable[[str], None] = notify,
                 worker_log: Callable[[], Path] = worker_log, stop: Optional[threading.Event] = None):
        self.state_path, self.post, self.worker_log = state_path, post, worker_log
        self.stop = stop or threading.Event()

    def report(self) -> None:
        """Say in Slack what the plugins' bridge recorded since the last look: a policy worker ended."""
        state = load_json(self.state_path)
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
        save_json(self.state_path, state)
        if message:
            self.post(message)

    def run(self) -> None:
        if self.stop.wait(START_DELAY):
            return
        while not self.stop.is_set():
            try:
                if gateway_runner() is not None:
                    self.report()
            except Exception:
                logger.warning("autodeploy: policy worker report failed", exc_info=True)
            if self.stop.wait(REPORT_INTERVAL):
                return


def _start_reporter() -> None:
    # The runner may not exist yet while the gateway loads plugins, so each round checks for it.
    if any(thread.name == _THREAD_NAME for thread in threading.enumerate()):
        return
    reporter = Reporter(data_dir() / "state.json")
    threading.Thread(target=reporter.run, name=_THREAD_NAME, daemon=True).start()


def register(ctx: Any) -> None:
    # Every Hermes process loads plugins. The dashboard serves CI's deploys; the gateway reports.
    register_token(ctx)
    _start_reporter()
