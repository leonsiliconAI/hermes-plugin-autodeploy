"""CI's deploy endpoint, mounted by the dashboard at ``/api/plugins/autodeploy/``.

``POST /deploy`` with ``{"plugins": [{"name", "mirror", "commit"}], "source", "run"}`` checks the
plugins against Hermes's install records, writes a deploy record and starts ``deploy.py``.
``GET /deploy?id=<id>`` answers with that record (the newest one without ``id``). Both need the
``AUTODEPLOY_TOKEN`` bearer, which the plugin's token provider checks; no browser session gets in.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

ROOT = Path(__file__).resolve().parents[1]
router = APIRouter()


def _plugin():
    """The autodeploy plugin this API belongs to: the one Hermes loaded, else a copy from ROOT."""
    loaded = sys.modules.get("hermes_plugins.autodeploy")
    if loaded is not None and Path(getattr(loaded, "__file__", "")).resolve().parent == ROOT:
        return loaded
    name = "autodeploy_dashboard"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def _require_ci(request: Request) -> None:
    principal = getattr(request.state, "token_principal", None)
    if getattr(principal, "provider", None) != _plugin().PROVIDER_NAME:
        raise HTTPException(status_code=401, detail="CI's deploy token is required")


@router.post("/deploy")
async def start_deploy(request: Request):
    _require_ci(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="the body isn't JSON") from None
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="the body isn't a JSON object")
    plugin = _plugin()
    installs = await asyncio.to_thread(plugin.read_installs)
    plan = await asyncio.to_thread(plugin._policy, "plan", body, installs)
    if plan["errors"]:
        raise HTTPException(status_code=400, detail={"errors": plan["errors"]})
    deploys = plugin.Deploys()
    record = deploys.create(body, plan)
    if plan["deploys"]:
        plugin.start_worker(record, os.getpid())
    else:
        record["status"] = "done"
        record["log"].append("every plugin is already at its commit")
        deploys.save(record)
    return record


@router.get("/deploy")
async def deploy_status(request: Request, id: str = ""):  # noqa: A002 - the query parameter's name
    _require_ci(request)
    deploys = _plugin().Deploys()
    try:
        record = deploys.get(id) if id else deploys.latest()
    except ValueError:
        raise HTTPException(status_code=400, detail="not a deploy id") from None
    if not record:
        raise HTTPException(status_code=404, detail="no such deploy")
    return record
