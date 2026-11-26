"""Run one deploy the dashboard queued: ``python deploy.py <id> <dashboard pid>``.

The dashboard starts this in its own session, so the deploy outlives a dashboard restart, including
the one it asks for when autodeploy itself moved. One deploy runs at a time; the next waits here.
"""

from __future__ import annotations

import importlib.util
import os
import signal
import site
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def load_plugin():
    spec = importlib.util.spec_from_file_location("autodeploy_worker", ROOT / "__init__.py",
                                                  submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    if os.environ.get("HERMES_SITE"):
        # Hermes's own code, as the dashboard that started this sees it.
        site.addsitedir(os.environ["HERMES_SITE"])
    deploy_id, dashboard_pid = sys.argv[1], int(sys.argv[2])
    plugin = load_plugin()
    deploys = plugin.Deploys()
    with deploys.lock():
        record = deploys.get(deploy_id)
        if not record:
            print(f"no deploy {deploy_id}", file=sys.stderr)
            return 1
        try:
            record = plugin.run_deploy(record, deploys,
                                       restart_dashboard=lambda: os.kill(dashboard_pid, signal.SIGTERM))
        except Exception as exc:
            record["status"] = "failed"
            record["log"].append(f"the deploy crashed: {exc!r}")
            deploys.save(record)
            raise
    return 0 if record["status"] in ("done", "installed") else 1


if __name__ == "__main__":
    sys.exit(main())
