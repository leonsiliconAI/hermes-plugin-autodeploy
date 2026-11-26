"""Plain pytest; Git, the hermes CLI, the gateway runner and the restart are fakes."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

spec = importlib.util.spec_from_file_location("autodeploy", Path(__file__).resolve().parents[1] / "__init__.py")
plugin = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = plugin
spec.loader.exec_module(plugin)

OLD, NEW, NEWER = "a" * 40, "b" * 40, "c" * 40
MIRROR = "https://github.com/leonsiliconAI/hermes-plugin-hermes22muse.git"
MINUTE = 60_000


def policy(function, *args):
    return plugin._policy(function, *args)


def install(name="hermes22muse", source=MIRROR, revision=OLD, enabled=True):
    return {"name": name, "source": source, "revision": revision, "enabled": enabled}


class PolicyTests(unittest.TestCase):
    def test_mirror_url_accepts_only_leonsiliconai_github_repos(self):
        self.assertEqual(policy("mirrorUrl", MIRROR), MIRROR)
        self.assertEqual(policy("mirrorUrl", "https://github.com/leonsiliconAI/slack22orca"),
                         "https://github.com/leonsiliconAI/slack22orca.git")
        for source in ("https://github.com/someone/hermes-plugin-x.git", "leonsiliconAI/hermes-plugin-x",
                       "https://github.com/leonsiliconAI/x/tree/main", None, ""):
            self.assertIsNone(policy("mirrorUrl", source), source)

    def test_pending_lists_only_mirror_installs_behind_their_head(self):
        installs = [install(), install("current", revision=NEW),
                    install("manual", source=None), install("other", source="https://example.com/x.git"),
                    install("unreachable"), install("off", enabled=False)]
        heads = {"hermes22muse": NEW, "current": NEW, "unreachable": None, "off": NEW, "manual": NEW,
                 "other": NEW}
        deploys = policy("pending", installs, heads, {}, 0)
        self.assertEqual([(d["name"], d["to"], d["enabled"]) for d in deploys],
                         [("hermes22muse", NEW, True), ("off", NEW, False)])
        self.assertEqual(deploys[0]["from"], OLD)

    def test_pending_ignores_a_head_that_is_not_a_commit(self):
        self.assertEqual(policy("pending", [install()], {"hermes22muse": "refs/heads/main"}, {}, 0), [])

    def test_failed_commit_waits_out_its_retry_delay_then_gives_up(self):
        deploy = policy("pending", [install()], {"hermes22muse": NEW}, {}, 0)[0]
        state = policy("recordResult", {}, deploy, "boom", 0)
        self.assertEqual(state["attempts"]["hermes22muse"]["count"], 1)
        self.assertEqual(policy("pending", [install()], {"hermes22muse": NEW}, state, 4 * MINUTE), [])
        self.assertEqual(len(policy("pending", [install()], {"hermes22muse": NEW}, state, 5 * MINUTE)), 1)
        state = policy("recordResult", state, deploy, "boom", 5 * MINUTE)
        self.assertEqual(policy("pending", [install()], {"hermes22muse": NEW}, state, 34 * MINUTE), [])
        state = policy("recordResult", state, deploy, "boom", 35 * MINUTE)
        self.assertEqual(state["attempts"]["hermes22muse"]["count"], 3)
        self.assertEqual(policy("pending", [install()], {"hermes22muse": NEW}, state, 10_000 * MINUTE), [])
        # A newer mirror commit starts over.
        self.assertEqual(len(policy("pending", [install()], {"hermes22muse": NEWER}, state, 35 * MINUTE)), 1)

    def test_success_clears_attempts(self):
        deploy = policy("pending", [install()], {"hermes22muse": NEW}, {}, 0)[0]
        state = policy("recordResult", {"attempts": {"other": {"target": NEW, "count": 1, "at": 0}}}, deploy,
                       "boom", 0)
        state = policy("recordResult", state, deploy, None, 1)
        self.assertEqual(list(state["attempts"]), ["other"])

    def test_install_args_pin_the_commit_and_keep_the_enabled_state(self):
        deploy = policy("pending", [install()], {"hermes22muse": NEW}, {}, 0)[0]
        self.assertEqual(policy("installArgs", deploy),
                         ["plugins", "install", MIRROR, "--force", "--ref", NEW, "--yes-deps", "--enable"])
        self.assertEqual(policy("installArgs", {**deploy, "enabled": False})[-1], "--no-enable")

    def test_idle_needs_three_quiet_samples(self):
        self.assertFalse(policy("idle", [0, 0]))
        self.assertFalse(policy("idle", [0, 1, 0]))
        self.assertTrue(policy("idle", [2, 0, 0, 0]))

    def test_summary_restarts_only_when_something_moved(self):
        deploy = policy("pending", [install()], {"hermes22muse": NEW}, {}, 0)[0]
        self.assertEqual(policy("summary", [{"deploy": deploy, "error": None}]),
                         {"restart": True, "log": "autodeploy: hermes22muse aaaaaaa..bbbbbbb; restarting the "
                                                  "gateway once it is idle"})
        self.assertFalse(policy("summary", [{"deploy": deploy, "error": "boom"}])["restart"])

    def test_failure_message_names_the_commit_and_the_next_try(self):
        deploy = policy("pending", [install()], {"hermes22muse": NEW}, {}, 0)[0]
        text = policy("failureMessage", deploy, "line\nlast line", 1)
        self.assertIn("*hermes22muse*", text)
        self.assertIn(f"https://github.com/leonsiliconAI/hermes-plugin-hermes22muse/commit/{NEW}", text)
        self.assertIn("in 5 minutes (attempt 1 of 3)", text)
        self.assertIn("last line", text)
        self.assertIn("won't try this commit again", policy("failureMessage", deploy, "x", 3))


class FakeHermes:
    """Install records, mirror heads and the hermes CLI, as the deployer sees them."""

    def __init__(self, records, heads):
        self.records = records
        self.heads = heads
        self.calls: list[list[str]] = []
        self.fail: dict[str, tuple[int, str]] = {}
        self.record_anyway = True

    def installs(self):
        return [{"name": n, **r, "enabled": r.get("enabled", True)} for n, r in self.records.items()]

    def head(self, url):
        return self.heads.get(url)

    def install(self, args):
        self.calls.append(args)
        source, ref = args[2], args[args.index("--ref") + 1]
        name = next(n for n, r in self.records.items() if policy("mirrorUrl", r["source"]) == source)
        if name in self.fail:
            return self.fail[name]
        if self.record_anyway:
            self.records[name]["revision"] = ref
        return 0, "Installed"

    def revision(self, name):
        return self.records[name]["revision"]


class DeployerTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.hermes = FakeHermes(
            {"hermes22muse": {"source": MIRROR, "revision": OLD},
             "slack22orca": {"source": "https://github.com/leonsiliconAI/slack22orca.git", "revision": OLD}},
            {MIRROR: NEW, "https://github.com/leonsiliconAI/slack22orca.git": OLD})
        self.runner = SimpleNamespace(_restart_requested=False, _draining=False)
        self.work = [0]
        self.restarts = []
        self.posts = []
        self.slept = []
        self.now = 1_000.0
        self.deployer = self._deployer()

    def _deployer(self, runner="default"):
        def work(_runner):
            return self.work.pop(0) if len(self.work) > 1 else self.work[0]

        def restart(runner):
            self.restarts.append(runner)
            runner._restart_requested = True

        return plugin.Deployer(
            self.dir / "state.json", runner=lambda: self.runner if runner == "default" else runner,
            installs=self.hermes.installs, head=self.hermes.head, install=self.hermes.install,
            revision=self.hermes.revision, work=work, restart=restart, post=self.posts.append,
            watchdog=lambda: self.dir / "pythonmonkey-deadman.log",
            sleep=self.slept.append, clock=lambda: self.now)

    def state(self):
        return json.loads((self.dir / "state.json").read_text())

    def test_outside_the_gateway_it_does_nothing(self):
        self.assertEqual(self._deployer(runner=None).tick(), [])
        self.assertEqual(self.hermes.calls, [])

    def test_a_gateway_the_watchdog_ended_is_reported_once(self):
        self.hermes.heads[MIRROR] = OLD
        log = self.dir / "pythonmonkey-deadman.log"
        log.write_text("")
        self.deployer.tick()
        self.assertEqual(self.posts, [])
        log.write_text("Timeout (0:02:00)!\nThread 0x1 [hermes-pythonmonkey] (most recent call first):\n")
        self._deployer().tick()
        self.assertEqual(len(self.posts), 1)
        self.assertIn("Hermes restarted itself at", self.posts[0])
        self.assertIn(str(log), self.posts[0])
        self._deployer().tick()
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(self.state()["watchdog"], log.stat().st_size)

    def test_moved_mirror_is_installed_pinned_then_the_gateway_restarts_once(self):
        results = self.deployer.tick()
        self.assertEqual(self.hermes.calls,
                         [["plugins", "install", MIRROR, "--force", "--ref", NEW, "--yes-deps", "--enable"]])
        self.assertEqual([r["error"] for r in results], [None])
        self.assertEqual(self.restarts, [self.runner])
        self.assertEqual(self.posts, [])
        self.assertEqual(self.state()["attempts"], {})
        # Now current, and the gateway is restarting: nothing more.
        self.assertEqual(self.deployer.tick(), [])
        self.runner._restart_requested = False
        self.assertEqual(self.deployer.tick(), [])
        self.assertEqual(len(self.hermes.calls), 1)

    def test_waits_for_the_gateway_to_go_quiet(self):
        self.work = [2, 1, 0, 0, 0]
        self.deployer.tick()
        self.assertEqual(len(self.slept), 4)
        self.assertEqual(len(self.hermes.calls), 1)

    def test_a_restart_already_underway_stops_the_wait(self):
        self.work = [1]

        def busy(_runner):
            self.runner._draining = True
            return 1

        self.deployer.work = busy
        self.assertEqual(self.deployer.tick(), [])
        self.assertEqual(self.hermes.calls, [])

    def test_deploys_the_newest_commit_when_the_mirror_moves_during_the_wait(self):
        self.work = [1, 0, 0, 0]

        def work(_runner):
            self.hermes.heads[MIRROR] = NEWER
            return self.work.pop(0) if len(self.work) > 1 else self.work[0]

        self.deployer.work = work
        self.deployer.tick()
        self.assertEqual(self.hermes.calls[0][5], NEWER)

    def test_failed_install_keeps_the_old_code_posts_and_retries_later(self):
        self.hermes.fail["hermes22muse"] = (1, "Error: scan verdict dangerous")
        results = self.deployer.tick()
        self.assertIn("exited 1", results[0]["error"])
        self.assertEqual(self.restarts, [])
        self.assertEqual(len(self.posts), 1)
        self.assertIn("scan verdict dangerous", self.posts[0])
        self.assertEqual(self.state()["attempts"]["hermes22muse"]["count"], 1)
        self.now += 60
        self.assertEqual(self.deployer.tick(), [])
        self.now += 5 * 60
        del self.hermes.fail["hermes22muse"]
        self.deployer.tick()
        self.assertEqual(len(self.hermes.calls), 2)
        self.assertEqual(len(self.restarts), 1)

    def test_an_install_that_records_another_commit_counts_as_failed(self):
        self.hermes.record_anyway = False
        results = self.deployer.tick()
        self.assertIn("recorded aaaaaaaaaaaa", results[0]["error"])
        self.assertEqual(self.restarts, [])

    def test_one_failure_still_restarts_for_the_plugin_that_moved(self):
        self.hermes.heads["https://github.com/leonsiliconAI/slack22orca.git"] = NEW
        self.hermes.fail["slack22orca"] = (2, "nope")
        results = self.deployer.tick()
        self.assertEqual([bool(r["error"]) for r in results], [False, True])
        self.assertEqual(len(self.restarts), 1)
        self.assertEqual(len(self.posts), 1)

    def test_another_updater_holding_the_lock_is_retried_next_round_without_a_failure(self):
        self.hermes.fail["hermes22muse"] = (
            1, "Error: Plugin 'hermes22muse' is already being updated by another Hermes process; nothing was changed.")
        self.assertEqual(self.deployer.tick(), [])
        self.assertEqual(self.posts, [])
        self.assertEqual(self.restarts, [])
        self.assertFalse((self.dir / "state.json").exists())
        del self.hermes.fail["hermes22muse"]
        self.deployer.tick()
        self.assertEqual(len(self.hermes.calls), 2)
        self.assertEqual(len(self.restarts), 1)

    def test_live_cron_runs_count_as_work(self):
        jobs = [{"id": "a", "live_execution": {"pid": 1}}, {"id": "b", "live_execution": None}]
        cron = type(sys)("cron")
        cron_jobs = type(sys)("cron.jobs")
        cron_jobs.list_jobs = lambda include_disabled=False: jobs if include_disabled else []
        cron.jobs = cron_jobs
        saved = {name: sys.modules.get(name) for name in ("cron", "cron.jobs")}
        sys.modules.update({"cron": cron, "cron.jobs": cron_jobs})
        try:
            runner = SimpleNamespace(_active_work_count=lambda: 2)
            self.assertEqual(plugin.active_work(runner), 3)
            jobs[0]["live_execution"] = None
            self.assertEqual(plugin.active_work(runner), 2)
        finally:
            for name, module in saved.items():
                if module is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = module


class NotifyTests(unittest.TestCase):
    def test_without_a_channel_nothing_is_posted(self):
        plugin.os.environ.pop(plugin.SLACK_CHANNEL_ENV, None)
        plugin.notify("hello")  # would raise on a missing gateway config if it tried


if __name__ == "__main__":
    unittest.main()
