"""The tools a bot keeps its dashboard with: read it, set a panel, change some items, remove a panel.

Each call acts on the dashboard of the bot that makes it (the profile whose turn this is). What the bot sends is checked in
`board.py`; a mistake comes back as an error the bot can read and fix.
"""

from __future__ import annotations

import json
import logging

try:
    from . import board as board_module
except ImportError:  # the tests import the modules flat
    import board as board_module  # type: ignore[no-redef]

log = logging.getLogger("hermes-dashboard")

TOOLSET = "dashboard"


def current_profile() -> str:
    """The bot whose turn this is: `default` for the root home, else its profile name."""
    from hermes_constants import get_hermes_home, profile_name_for_home

    return profile_name_for_home(get_hermes_home()) or "default"


def _result(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False)


def _error(message: str) -> str:
    try:
        from tools.registry import tool_error

        return tool_error(message)
    except Exception:  # outside Hermes (tests)
        return json.dumps({"error": message}, ensure_ascii=False)


_ITEM = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "description": "Stable id from the source (message id, file name): lower-case letters, digits, '-', '_'. Omit and the title is used."},
        "title": {"type": "string", "description": "A few plain words the person scans, e.g. 'Guest in Cabin 3'."},
        "detail": {"type": "string", "description": "One line on what it is or why it matters."},
        "body": {"type": "string", "description": "Markdown write-up shown when the item is opened: what happened, what to decide, your suggestion. Summarise; never paste whole emails or logs."},
        "needs_you": {"type": "boolean", "description": "True only when the person must act (call, decide, reply). Lights an amber dot on the bot's face."},
        "at": {"type": "string", "description": "When it happened: ISO 8601 with offset (2026-10-13T09:14:00-04:00) or epoch seconds."},
        "due": {"type": "string", "description": "For checklist items: YYYY-MM-DD."},
        "link": {"type": "string", "description": "Where it lives: a file on this server (/home/...), a plugin document route (/api/plugins/...), or an https:// page."},
        "action": {
            "type": "object",
            "description": "The one next step: {label, url} with url tel:, sms:, mailto: or https://.",
            "properties": {"label": {"type": "string"}, "url": {"type": "string"}},
            "required": ["label", "url"],
        },
    },
    "required": ["title"],
}

READ_SCHEMA = {
    "name": "dashboard_read",
    "description": "Read the panels on your person's dashboard in the HermesBot app (yours only), with done_at on items they marked done. Load the hermes-dashboard:dashboard skill before setting up or changing panels.",
    "parameters": {"type": "object", "properties": {}, "required": []},
}

SET_PANEL_SCHEMA = {
    "name": "dashboard_set_panel",
    "description": "Create or replace one panel on your person's dashboard (same id replaces it; their done marks are kept). Kinds: list (things that arrived), checklist (things they must do), metrics (up to 6 numbers), summary (short Markdown status). Load the hermes-dashboard:dashboard skill first.",
    "parameters": {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "Panel id, e.g. 'voicemails'. Omit and the title is used."},
            "title": {"type": "string", "description": "Short title, e.g. 'Voicemails'."},
            "kind": {"type": "string", "enum": list(board_module.KINDS)},
            "note": {"type": "string", "description": "A few words beside the title, e.g. '3 today'."},
            "order": {"type": "integer", "description": "Lower comes first. Omit to keep its place."},
            "items": {"type": "array", "items": _ITEM, "description": "For list and checklist panels, at most 50."},
            "metrics": {
                "type": "array",
                "description": "For metrics panels, at most 6.",
                "items": {"type": "object", "properties": {"label": {"type": "string"}, "value": {"type": "string"}, "change": {"type": "string"}}, "required": ["label", "value"]},
            },
            "summary": {"type": "string", "description": "For summary panels: Markdown."},
        },
        "required": ["title", "kind"],
    },
}

UPDATE_ITEMS_SCHEMA = {
    "name": "dashboard_update_items",
    "description": "Add or replace some items of a list or checklist panel (by id) and remove others, leaving the rest as they are.",
    "parameters": {
        "type": "object",
        "properties": {
            "panel_id": {"type": "string"},
            "upsert": {"type": "array", "items": _ITEM, "description": "Items to add, or to replace when the id exists."},
            "remove": {"type": "array", "items": {"type": "string"}, "description": "Ids of items to remove."},
        },
        "required": ["panel_id"],
    },
}

REMOVE_PANEL_SCHEMA = {
    "name": "dashboard_remove_panel",
    "description": "Remove a panel from your person's dashboard (pause or remove the routine that fed it too).",
    "parameters": {"type": "object", "properties": {"panel_id": {"type": "string"}}, "required": ["panel_id"]},
}


def _counts(panel: dict) -> str:
    items = panel.get("items")

    if items is None:
        return f"{len(panel.get('metrics', []))} metrics" if panel["kind"] == "metrics" else "summary"

    open_items = [item for item in items if not item.get("done_at")]
    needs = sum(1 for item in open_items if item.get("needs_you"))

    return f"{len(open_items)} open items, {needs} need the person"


class DashboardTools:
    def __init__(self, board=None, profile=current_profile):
        self._board = board or board_module.Board()
        self._profile = profile

    def read(self, args: dict, **_kw) -> str:
        try:
            panels = self._board.read(self._profile())
        except Exception as error:
            log.exception("dashboard_read failed")
            return _error(f"Could not read the dashboard: {error}")

        return _result({"panels": panels, "message": f"{len(panels)} panels." if panels else "No panels yet."})

    def set_panel(self, args: dict, **_kw) -> str:
        try:
            panel = self._board.set_panel(self._profile(), args or {})
        except board_module.DashboardError as error:
            return _error(str(error))
        except Exception as error:
            log.exception("dashboard_set_panel failed")
            return _error(f"Could not save the panel: {error}")

        return _result({"panel": panel, "message": f"Saved '{panel['title']}' ({_counts(panel)}). The person sees it on your dashboard in the HermesBot app."})

    def update_items(self, args: dict, **_kw) -> str:
        args = args or {}

        try:
            panel = self._board.update_items(self._profile(), args.get("panel_id"), upsert=args.get("upsert"), remove=args.get("remove"))
        except board_module.DashboardError as error:
            return _error(str(error))
        except Exception as error:
            log.exception("dashboard_update_items failed")
            return _error(f"Could not change the items: {error}")

        return _result({"panel": panel, "message": f"Updated '{panel['title']}' ({_counts(panel)})."})

    def remove_panel(self, args: dict, **_kw) -> str:
        try:
            removed = self._board.remove_panel(self._profile(), (args or {}).get("panel_id"))
        except board_module.DashboardError as error:
            return _error(str(error))

        return _result({"removed": removed, "message": "Removed." if removed else "There was no such panel."})

    def registrations(self) -> list[tuple[str, dict, object]]:
        return [
            ("dashboard_read", READ_SCHEMA, self.read),
            ("dashboard_set_panel", SET_PANEL_SCHEMA, self.set_panel),
            ("dashboard_update_items", UPDATE_ITEMS_SCHEMA, self.update_items),
            ("dashboard_remove_panel", REMOVE_PANEL_SCHEMA, self.remove_panel),
        ]
