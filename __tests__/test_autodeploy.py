"""Plain pytest; the hermes CLI, the gateway's PID and its restart are fakes."""

from __future__ import annotations

import importlib.util
import json
import secrets
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("autodeploy", ROOT / "__init__.py")
plugin = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = plugin
spec.loader.exec_module(plugin)

OLD, NEW = "a" * 40, "b" * 40
LEON = "https://github.com/leonsiliconAI/hermes-plugin-leon.git"
SELF = "https://github.com/leonsiliconAI/hermes-plugin-autodeploy.git"


def policy(function, *args):
    return plugin._policy(function, *args)


def install(name="leon", source=LEON, revision=OLD, enabled=True):
    return {"name": name, "source": source, "revision": revision, "enabled": enabled}


def want(name="leon", mirror="https://github.com/leonsiliconAI/hermes-plugin-leon", commit=NEW):
    return {"name": name, "mirror": mirror, "commit": commit}


class PolicyTests(unittest.TestCase):
    def test_mirror_url_accepts_only_leonsiliconai_github_repos(self):
        self.assertEqual(policy("mirrorUrl", LEON), LEON)
        self.assertEqual(policy("mirrorUrl", "https://github.com/leonsiliconAI/hermes-plugin-leon"), LEON)
        for source in ("https://github.com/someone/hermes-plugin-x.git", "leonsiliconAI/hermes-plugin-x",
                       "https://github.com/leonsiliconAI/x/tree/main", None, ""):
            self.assertIsNone(policy("mirrorUrl", source), source)

    def test_plan_moves_installed_mirror_plugins_and_autodeploy_last(self):
        installs = [install("autodeploy", SELF), install(), install("current", revision=NEW),
                    install("off", "https://github.com/leonsiliconAI/hermes-plugin-off.git", enabled=False)]
        result = policy("plan", {"plugins": [
            want("autodeploy", SELF), want(), want("current"), want("off", "https://github.com/leonsiliconAI/hermes-plugin-off")]},
            installs)
        self.assertEqual(result["errors"], [])
        self.assertEqual([(d["name"], d["from"], d["to"], d["enabled"]) for d in result["deploys"]],
                         [("leon", OLD, NEW, True), ("off", OLD, NEW, False), ("autodeploy", OLD, NEW, True)])
        self.assertEqual(result["current"], ["current"])

    def test_plan_refuses_anything_new_or_from_elsewhere(self):
        installs = [install(), install("manual", source=None)]
        cases = {
            "x": [want("unknown")],
            "manual": [want("manual")],
            "other mirror": [want(mirror="https://github.com/leonsiliconAI/hermes-plugin-other")],
            "branch": [want(commit="main")],
            "short": [want(commit="b" * 12)],
            "twice": [want(), want()],
            "none": [],
        }
        for label, plugins in cases.items():
            result = policy("plan", {"plugins": plugins}, installs)
            self.assertTrue(result["errors"], label)
        self.assertTrue(policy("plan", {}, installs)["errors"])
        self.assertTrue(policy("plan", {"plugins": "leon"}, installs)["errors"])

    def test_install_args_pin_the_commit_and_keep_the_enabled_state(self):
        deploy = policy("plan", {"plugins": [want()]}, [install()])["deploys"][0]
        self.assertEqual(policy("installArgs", deploy),
                         ["plugins", "install", LEON, "--force", "--ref", NEW, "--yes-deps", "--enable"])
        self.assertEqual(policy("installArgs", {**deploy, "enabled": False})[-1], "--no-enable")

    def test_summary_restarts_only_when_something_moved(self):
        deploy = policy("plan", {"plugins": [want()]}, [install()])["deploys"][0]
        moved = policy("summary", [{"deploy": deploy, "error": None}])
        self.assertEqual(moved, {"restart": True, "restartDashboard": False,
                                 "log": "autodeploy: leon aaaaaaa..bbbbbbb; restarting the gateway once it is idle"})
        self.assertFalse(policy("summary", [{"deploy": deploy, "error": "boom"}])["restart"])
        self.assertTrue(policy("summary", [{"deploy": {**deploy, "name": "autodeploy"}, "error": None}])["restartDashboard"])


class FakeHermes:
    def __init__(self):
        self.records = {"leon": OLD, "autodeploy": OLD}
        self.calls = []
        self.fail = {}
        self.pids = [100]
        self.restarted = []
        self.dashboard = []
        self.slept = 0.0

    def install(self, args):
        self.calls.append(args)
        name = "autodeploy" if args[2] == SELF else "leon"
        if name in self.fail:
            return self.fail[name], "boom"
        self.records[name] = args[args.index("--ref") + 1]
        return 0, "Installed"

    def pid(self):
        return self.pids[0] if len(self.pids) == 1 else self.pids.pop(0)

    def run(self, record, deploys, **kwargs):
        clock = lambda: self.slept  # noqa: E731
        return plugin.run_deploy(record, deploys, install=self.install, revision=self.records.get,
                                 pid=self.pid, restart=self.restarted.append,
                                 restart_dashboard=lambda: self.dashboard.append(True),
                                 sleep=lambda seconds: setattr(self, "slept", self.slept + seconds),
                                 clock=clock, **kwargs)


class DeployTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.deploys = plugin.Deploys(self.dir)
        self.hermes = FakeHermes()

    def record(self, *names):
        installs = [install(), install("autodeploy", SELF)]
        plan = policy("plan", {"plugins": [want(n, SELF if n == "autodeploy" else LEON.removesuffix(".git"))
                                           for n in names]}, installs)
        return self.deploys.create({"source": "c" * 40, "run": "https://github.com/x/actions/runs/1"}, plan)

    def test_installs_pinned_then_restarts_the_gateway_when_idle(self):
        self.hermes.pids = [100, 100, 100, 200]
        record = self.hermes.run(self.record("leon"), self.deploys)
        self.assertEqual(self.hermes.calls,
                         [["plugins", "install", LEON, "--force", "--ref", NEW, "--yes-deps", "--enable"]])
        self.assertEqual(self.hermes.restarted, [100])
        self.assertEqual(record["status"], "done")
        self.assertEqual(record["restart"], {"from": 100, "to": 200})
        self.assertEqual(self.hermes.dashboard, [])
        saved = self.deploys.get(record["id"])
        self.assertEqual(saved["status"], "done")
        self.assertTrue(saved["steps"][0]["ok"])
        self.assertEqual(self.deploys.latest()["id"], record["id"])

    def test_a_restart_still_waiting_for_idle_ends_as_installed(self):
        record = self.hermes.run(self.record("leon"), self.deploys, restart_wait=30)
        self.assertEqual(record["status"], "installed")
        self.assertIn("still finishing work", record["log"][-1])

    def test_a_failed_install_keeps_the_old_code_and_restarts_for_the_rest(self):
        self.hermes.fail["leon"] = 1
        self.hermes.pids = [100, 200]
        record = self.hermes.run(self.record("leon", "autodeploy"), self.deploys)
        self.assertEqual([step["ok"] for step in record["steps"]], [False, True])
        self.assertEqual(record["steps"][0]["error"], "hermes plugins install exited 1")
        self.assertEqual(record["status"], "failed")
        self.assertEqual(self.hermes.restarted, [100])
        # autodeploy moved, so the dashboard runs the new API afterwards.
        self.assertEqual(self.hermes.dashboard, [True])

    def test_nothing_installed_means_no_restart(self):
        self.hermes.fail["leon"] = 1
        record = self.hermes.run(self.record("leon"), self.deploys)
        self.assertEqual(record["status"], "failed")
        self.assertEqual(self.hermes.restarted, [])

    def test_an_install_that_records_another_commit_counts_as_failed(self):
        self.hermes.install = lambda args: (self.hermes.calls.append(args), (0, "ok"))[1]
        record = self.hermes.run(self.record("leon"), self.deploys)
        self.assertIn("Hermes recorded aaaaaaaaaaaa", record["steps"][0]["error"])
        self.assertEqual(record["status"], "failed")

    def test_ids_are_checked_before_they_name_a_file(self):
        with self.assertRaises(ValueError):
            self.deploys.get("../../etc/passwd")


class TokenTests(unittest.TestCase):
    def test_a_weak_token_is_refused(self):
        self.assertIsNone(plugin.token_strength(secrets.token_urlsafe(32)))
        self.assertIsNotNone(plugin.token_strength("short"))
        self.assertIsNotNone(plugin.token_strength("a" * 64))

    def test_the_provider_accepts_only_the_token(self):
        class Base:
            pass

        auth = types.ModuleType("hermes_cli.dashboard_auth")
        auth.DashboardAuthProvider = Base
        auth.TokenPrincipal = lambda **kwargs: kwargs
        token = secrets.token_urlsafe(32)
        with patch.dict(sys.modules, {"hermes_cli": types.ModuleType("hermes_cli"), "hermes_cli.dashboard_auth": auth}):
            provider = plugin.make_provider(token)
        self.assertEqual(provider.verify_token(token=token)["provider"], plugin.PROVIDER_NAME)
        self.assertIsNone(provider.verify_token(token=token + "x"))
        self.assertIsNone(provider.verify_token(token=""))
        self.assertFalse(provider.supports_session)

    def test_without_a_token_nothing_is_registered(self):
        ctx = types.SimpleNamespace(register_dashboard_auth_provider=lambda provider: self.fail("registered"))
        with patch.dict("os.environ", {plugin.TOKEN_ENV: ""}):
            plugin.register_token(ctx)
        with patch.dict("os.environ", {plugin.TOKEN_ENV: "too-short"}):
            plugin.register_token(ctx)


class ReporterTests(unittest.TestCase):
    def test_a_policy_worker_that_had_to_be_ended_is_reported_once(self):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        log = directory / "policy-worker.log"
        posts = []
        reporter = plugin.Reporter(directory / "state.json", post=posts.append, worker_log=lambda: log)
        reporter.report()
        self.assertEqual(posts, [])
        first = "2026-11-26 17:00:00 UTC The policy worker was ended: it was running slackChunks in hermes22muse.\n"
        log.write_text(first)
        reporter.report()
        reporter.report()
        self.assertEqual(len(posts), 1)
        self.assertIn("slackChunks in hermes22muse", posts[0])
        log.write_text(first + "2026-11-26 18:00:00 UTC The policy worker exited with status -9.\n")
        reporter.report()
        self.assertEqual(len(posts), 2)
        self.assertNotIn("slackChunks", posts[1])
        self.assertEqual(json.loads((directory / "state.json").read_text())["worker_log"], log.stat().st_size)


try:
    import fastapi  # noqa: F401
    from fastapi.testclient import TestClient
except ImportError:  # the API's tests need FastAPI, which Hermes ships
    TestClient = None


@unittest.skipIf(TestClient is None, "needs fastapi and httpx")
class ApiTests(unittest.TestCase):
    def setUp(self):
        from fastapi import FastAPI

        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        api_spec = importlib.util.spec_from_file_location("autodeploy_api_test", ROOT / "dashboard" / "api.py")
        self.api = importlib.util.module_from_spec(api_spec)
        api_spec.loader.exec_module(self.api)
        self.started = []
        fake = types.SimpleNamespace(
            PROVIDER_NAME=plugin.PROVIDER_NAME, _policy=plugin._policy,
            read_installs=lambda: [install()], Deploys=lambda: plugin.Deploys(self.dir),
            start_worker=lambda record, pid: self.started.append(record["id"]))
        self.enterContext(patch.object(self.api, "_plugin", lambda: fake))
        app = FastAPI()
        self.provider = plugin.PROVIDER_NAME

        @app.middleware("http")
        async def token(request, call_next):
            # What Hermes's token-auth seam attaches for a recognised bearer.
            if request.headers.get("authorization") == "Bearer ok":
                request.state.token_principal = types.SimpleNamespace(provider=self.provider)
            return await call_next(request)

        app.include_router(self.api.router, prefix="/api/plugins/autodeploy")
        self.client = TestClient(app)

    def post(self, body, token="ok"):
        return self.client.post("/api/plugins/autodeploy/deploy", json=body, headers={"Authorization": f"Bearer {token}"})

    def test_ci_starts_a_deploy_and_reads_it_back(self):
        response = self.post({"plugins": [want()], "source": "c" * 40})
        self.assertEqual(response.status_code, 200, response.text)
        record = response.json()
        self.assertEqual(record["status"], "queued")
        self.assertEqual(self.started, [record["id"]])
        status = self.client.get(f"/api/plugins/autodeploy/deploy?id={record['id']}",
                                 headers={"Authorization": "Bearer ok"})
        self.assertEqual(status.json()["deploys"][0]["to"], NEW)

    def test_a_deploy_with_nothing_to_move_is_done_at_once(self):
        record = self.post({"plugins": [want(commit=OLD)]}).json()
        self.assertEqual(record["status"], "done")
        self.assertEqual(self.started, [])

    def test_a_bad_request_is_refused_and_starts_nothing(self):
        response = self.post({"plugins": [want("unknown")]})
        self.assertEqual(response.status_code, 400)
        self.assertIn("isn't installed", json.dumps(response.json()))
        self.assertEqual(self.started, [])

    def test_only_cis_token_gets_in(self):
        self.assertEqual(self.post({"plugins": [want()]}, token="nope").status_code, 401)
        self.assertEqual(self.client.get("/api/plugins/autodeploy/deploy").status_code, 401)
        self.assertEqual(self.started, [])


if __name__ == "__main__":
    unittest.main()
