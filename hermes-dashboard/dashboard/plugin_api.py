"""The phone's side of hermes-dashboard: read each bot's panels and mark items done.

Mounted by Hermes under `/api/plugins/hermes-dashboard/`, behind the dashboard's own sign-in, so only someone who can already
use the server can reach it. Hermes imports this file on its own, apart from the plugin package, so it loads the package's
`board` module by path and shares state with the bots' tools through its files.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

_PACKAGE = "_hermes_dashboard_package"


def _board_module():
    """The plugin's `board` module, loaded once by path the way Hermes loads the package."""
    if _PACKAGE not in sys.modules:
        root = Path(__file__).resolve().parent.parent
        spec = importlib.util.spec_from_file_location(_PACKAGE, root / "__init__.py", submodule_search_locations=[str(root)])
        module = importlib.util.module_from_spec(spec)
        module.__package__ = _PACKAGE
        module.__path__ = [str(root)]
        sys.modules[_PACKAGE] = module
        spec.loader.exec_module(module)

    return importlib.import_module(f"{_PACKAGE}.board")


def _version() -> str:
    try:
        import yaml

        manifest = yaml.safe_load((Path(__file__).resolve().parent.parent / "plugin.yaml").read_text(encoding="utf-8")) or {}
        return str(manifest.get("version") or "")
    except Exception:
        return ""


router = APIRouter()


@router.get("/status")
async def status() -> dict:
    board = _board_module()
    return {"version": _version(), "bots": len(board.Board().summary())}


@router.get("/bots")
async def bots() -> dict:
    board = _board_module()
    return {"bots": board.Board().summary(), "now": int(time.time())}


@router.get("/bots/{profile}")
async def bot(profile: str) -> dict:
    board = _board_module()

    try:
        panels = board.Board().read(profile)
    except board.DashboardError:
        raise HTTPException(status_code=400, detail="not a bot's name") from None

    return {"profile": profile, "panels": panels, "now": int(time.time())}


class Mark(BaseModel):
    done: bool


@router.post("/bots/{profile}/panels/{panel_id}/items/{item_id}")
async def mark(profile: str, panel_id: str, item_id: str, body: Mark) -> dict:
    board = _board_module()

    try:
        panel = board.Board().mark(profile, panel_id, item_id, body.done)
    except board.DashboardError as error:
        raise HTTPException(status_code=400, detail=str(error)) from None
    except KeyError:
        raise HTTPException(status_code=404, detail="That item is no longer on the dashboard.") from None

    return {"panel": panel}
