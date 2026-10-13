"""Each bot's dashboard: the panels it keeps for its person, in one small JSON file per bot under the Hermes root.

The bot writes panels with the plugin's tools (often from a routine); the phone reads them and marks items done through the
dashboard routes. The two run in different modules (Hermes imports the routes apart from the plugin package), so they share
these files, written atomically under a lock and re-read on every use.

What the person marked done is kept apart from what the bot writes (`done`, by panel and item id), so a routine that rewrites
a panel never brings back something the person already handled. Everything a bot sends is checked and trimmed here: this is
text shown on a phone, never run.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

try:  # POSIX only; Hermes servers run on Linux and macOS, and the in-process lock covers the rest.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

KINDS = ("list", "checklist", "metrics", "summary")

MAX_PANELS = 12
MAX_ITEMS = 50
MAX_METRICS = 6
LIMITS = {"title": 80, "panel_title": 40, "note": 40, "detail": 200, "body": 8000, "summary": 6000, "link": 1000, "label": 24, "metric_label": 30, "metric_value": 16, "change": 16}

#: Done marks for items a bot no longer lists are forgotten after this long.
DONE_KEPT_SECONDS = 30 * 86_400

#: Hermes profile names, and the ids a bot gives its panels and items.
PROFILE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

#: What an item's action may open on the phone: a call, a text, an email, a web page.
ACTION_URL = re.compile(r"^(tel:[+\d][\d\s().-]{2,30}|sms:[+\d][\d\s().-]{2,30}|mailto:[^\s<>]{3,300}|https://[^\s<>]{3,1000})$", re.IGNORECASE)
#: What an item's link may point at: a file or plugin route on this server (the app's viewer opens it) or a web page.
LINK = re.compile(r"^(/[^\s<>]*|file:///[^\s<>]*|https://[^\s<>]+)$")

CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def default_dir() -> Path:
    from hermes_constants import get_default_hermes_root

    return get_default_hermes_root() / "hermes-dashboard"


class DashboardError(ValueError):
    """Something a bot sent that can't go on a dashboard; the message says what to change."""


# -- cleaning what a bot sends -----------------------------------------------------------------------------------


def _text(value, field: str, limit_key: str | None = None, *, single_line: bool = True, required: bool = False) -> str:
    if value is None or value == "":
        if required:
            raise DashboardError(f"{field} is required")
        return ""

    if not isinstance(value, (str, int, float)):
        raise DashboardError(f"{field} must be text")

    text = CONTROL.sub("", str(value))
    text = re.sub(r"\s+", " ", text).strip() if single_line else text.strip()
    limit = LIMITS[limit_key or field]

    if required and not text:
        raise DashboardError(f"{field} is required")

    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _id(value, field: str, fallback: str = "", hashed: bool = True) -> str:
    raw = str(value or "").strip().lower()

    if not raw and fallback:
        # No id given: the title stands for it, so the same item written again keeps the person's done mark. Items get a
        # short hash too ("Call back" twice in different words still differs); a panel's title is short and unique.
        slug = re.sub(r"[^a-z0-9]+", "-", fallback.lower()).strip("-")[:40] or "item"
        return f"{slug}-{hashlib.sha1(fallback.encode()).hexdigest()[:6]}" if hashed else slug

    if not ID.match(raw):
        raise DashboardError(f"{field} must be lower-case letters, digits, '-' or '_' (got {raw[:40]!r})")

    return raw


def _when(value, field: str) -> int | None:
    """Epoch seconds from a number or an ISO 8601 time; None when absent."""
    if value in (None, ""):
        return None

    if isinstance(value, bool):
        raise DashboardError(f"{field} must be a time")

    if isinstance(value, (int, float)):
        seconds = float(value) / 1000 if value > 1e12 else float(value)
        return int(seconds)

    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        raise DashboardError(f"{field} must be an ISO 8601 time like 2026-10-13T09:14:00-04:00, or epoch seconds") from None

    if parsed.tzinfo is None:
        parsed = parsed.astimezone()  # the server's own time zone, which is the person's on a home server

    return int(parsed.timestamp())


def _due(value) -> str:
    if value in (None, ""):
        return ""

    match = re.match(r"^(\d{4})-(\d{2})-(\d{2})", str(value).strip())

    if not match:
        raise DashboardError("due must be a date like 2026-10-31")

    try:
        datetime(int(match[1]), int(match[2]), int(match[3]))
    except ValueError:
        raise DashboardError("due must be a real date") from None

    return match[0]


def clean_item(raw) -> dict:
    if not isinstance(raw, dict):
        raise DashboardError("each item must be an object")

    title = _text(raw.get("title"), "title", required=True)
    item: dict = {"id": _id(raw.get("id"), "item id", fallback=title), "title": title}

    for field, limit_key, single in (("detail", "detail", True), ("body", "body", False)):
        text = _text(raw.get(field), field, limit_key, single_line=single)
        if text:
            item[field] = text

    if raw.get("needs_you"):
        item["needs_you"] = True

    at = _when(raw.get("at"), "at")
    if at:
        item["at"] = at

    due = _due(raw.get("due"))
    if due:
        item["due"] = due

    link = str(raw.get("link") or "").strip()
    if link:
        if not LINK.match(link) or len(link) > LIMITS["link"]:
            raise DashboardError("link must be a path on this server (/home/…, /api/plugins/…) or an https:// page")
        item["link"] = link

    action = raw.get("action")
    if action:
        if not isinstance(action, dict):
            raise DashboardError("action must be {label, url}")
        url = str(action.get("url") or "").strip()
        if not ACTION_URL.match(url):
            raise DashboardError("action url must be tel:, sms:, mailto: or https://")
        item["action"] = {"label": _text(action.get("label"), "action label", "label", required=True), "url": url}

    return item


def clean_metric(raw) -> dict:
    if not isinstance(raw, dict):
        raise DashboardError("each metric must be an object")

    metric = {"label": _text(raw.get("label"), "metric label", "metric_label", required=True), "value": _text(raw.get("value"), "metric value", "metric_value", required=True)}
    change = _text(raw.get("change"), "change")

    if change:
        metric["change"] = change

    return metric


def clean_panel(raw, now: float) -> dict:
    if not isinstance(raw, dict):
        raise DashboardError("a panel must be an object")

    title = _text(raw.get("title"), "panel title", "panel_title", required=True)
    kind = str(raw.get("kind") or "list").strip().lower()

    if kind not in KINDS:
        raise DashboardError(f"kind must be one of {', '.join(KINDS)}")

    panel: dict = {"id": _id(raw.get("id"), "panel id", fallback=title, hashed=False), "title": title, "kind": kind, "updated": int(now)}
    note = _text(raw.get("note"), "note")

    if note:
        panel["note"] = note

    if raw.get("order") is not None:
        try:
            panel["order"] = int(raw["order"])
        except (TypeError, ValueError):
            raise DashboardError("order must be a whole number") from None

    if kind in ("list", "checklist"):
        items = raw.get("items") or []
        if not isinstance(items, list):
            raise DashboardError("items must be a list")
        if len(items) > MAX_ITEMS:
            raise DashboardError(f"at most {MAX_ITEMS} items in a panel; keep the ones that matter")
        cleaned = [clean_item(item) for item in items]
        ids = [item["id"] for item in cleaned]
        if len(set(ids)) != len(ids):
            raise DashboardError("two items share an id; give each its own")
        panel["items"] = cleaned
    elif kind == "metrics":
        metrics = raw.get("metrics") or []
        if not isinstance(metrics, list) or not metrics:
            raise DashboardError("a metrics panel needs metrics: [{label, value, change?}]")
        if len(metrics) > MAX_METRICS:
            raise DashboardError(f"at most {MAX_METRICS} metrics in a panel")
        panel["metrics"] = [clean_metric(metric) for metric in metrics]
    else:
        panel["summary"] = _text(raw.get("summary"), "summary", single_line=False, required=True)

    return panel


# -- the files -------------------------------------------------------------------------------------------------


class _JsonFile:
    """One small JSON document with atomic writes and a lock around read-modify-write."""

    _guards: dict[str, threading.Lock] = {}
    _guards_lock = threading.Lock()

    def __init__(self, path: Path):
        self.path = Path(path)

        with _JsonFile._guards_lock:
            self._guard = _JsonFile._guards.setdefault(str(self.path), threading.Lock())

    def read(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (FileNotFoundError, ValueError):
            return {}

        return data if isinstance(data, dict) else {}

    def update(self, change):
        self.path.parent.mkdir(parents=True, exist_ok=True)

        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:  # pragma: no cover
            pass

        with self._guard:
            lock = open(self.path.with_suffix(self.path.suffix + ".lock"), "a+")

            try:
                if fcntl is not None:
                    fcntl.flock(lock, fcntl.LOCK_EX)

                data = self.read()
                result = change(data)
                self._write(data)

                return result
            finally:
                lock.close()

    def _write(self, data) -> None:
        handle, temp = tempfile.mkstemp(dir=self.path.parent, prefix=self.path.name, suffix=".tmp")

        try:
            with os.fdopen(handle, "w", encoding="utf-8") as out:
                json.dump(data, out, indent=2, sort_keys=True, ensure_ascii=False)

            os.chmod(temp, 0o600)  # what a bot found in the person's mail and calls
            os.replace(temp, self.path)
        except BaseException:
            try:
                os.unlink(temp)
            except OSError:
                pass

            raise


def _check_profile(profile: str) -> str:
    if not PROFILE.match(profile or ""):
        raise DashboardError("not a bot's name")

    return profile


class Board:
    def __init__(self, folder: Path | None = None, clock=time.time):
        self._folder = Path(folder) if folder else None
        self._clock = clock

    @property
    def folder(self) -> Path:
        return self._folder or default_dir()

    def _file(self, profile: str) -> _JsonFile:
        return _JsonFile(self.folder / f"{_check_profile(profile)}.json")

    @staticmethod
    def _shape(data: dict) -> None:
        if not isinstance(data.get("panels"), dict):
            data["panels"] = {}
        if not isinstance(data.get("done"), dict):
            data["done"] = {}

    @staticmethod
    def _shown(panel: dict, done: dict) -> dict:
        """A panel as the phone and the bot see it: each item with `done_at` when the person marked it done."""
        marks = done.get(panel["id"]) or {}
        shown = dict(panel)

        if "items" in panel:
            shown["items"] = [{**item, "done_at": marks[item["id"]]} if item["id"] in marks else dict(item) for item in panel["items"]]

        return shown

    def _prune(self, done: dict, panel: dict) -> None:
        """Forget done marks for items the bot has not listed for a month."""
        marks = done.get(panel["id"])

        if not marks:
            return

        listed = {item["id"] for item in panel.get("items", [])}
        cutoff = self._clock() - DONE_KEPT_SECONDS
        done[panel["id"]] = {item: at for item, at in marks.items() if item in listed or at >= cutoff}

    # -- what the bot does --

    def read(self, profile: str) -> list[dict]:
        data = self._file(profile).read()
        self._shape(data)

        return sorted((self._shown(panel, data["done"]) for panel in data["panels"].values()), key=lambda panel: (panel.get("order", 0), panel["title"]))

    def set_panel(self, profile: str, raw) -> dict:
        panel = clean_panel(raw, self._clock())

        def change(data):
            self._shape(data)
            if panel["id"] not in data["panels"] and len(data["panels"]) >= MAX_PANELS:
                raise DashboardError(f"a dashboard holds at most {MAX_PANELS} panels; remove one first")
            if "order" not in panel:
                previous = data["panels"].get(panel["id"])
                panel["order"] = previous.get("order", len(data["panels"])) if previous else len(data["panels"])
            data["panels"][panel["id"]] = panel
            self._prune(data["done"], panel)
            return self._shown(panel, data["done"])

        return self._file(profile).update(change)

    def update_items(self, profile: str, panel_id: str, upsert=None, remove=None) -> dict:
        """Adds or replaces some items of a list or checklist (by id), and removes others, leaving the rest as they are."""
        panel_id = _id(panel_id, "panel id")
        new_items = [clean_item(item) for item in (upsert or [])]
        gone = {_id(item_id, "item id") for item_id in (remove or [])}

        def change(data):
            self._shape(data)
            panel = data["panels"].get(panel_id)
            if panel is None:
                raise DashboardError(f"no panel {panel_id!r}; create it with dashboard_set_panel")
            if panel["kind"] not in ("list", "checklist"):
                raise DashboardError("only a list or checklist panel has items")
            items = [item for item in panel.get("items", []) if item["id"] not in gone]
            index = {item["id"]: position for position, item in enumerate(items)}
            for item in new_items:
                if item["id"] in index:
                    items[index[item["id"]]] = item
                else:
                    index[item["id"]] = len(items)
                    items.append(item)
            if len(items) > MAX_ITEMS:
                raise DashboardError(f"at most {MAX_ITEMS} items in a panel; remove some first")
            panel = {**panel, "items": items, "updated": int(self._clock())}
            data["panels"][panel_id] = panel
            return self._shown(panel, data["done"])

        return self._file(profile).update(change)

    def remove_panel(self, profile: str, panel_id: str) -> bool:
        panel_id = _id(panel_id, "panel id")

        def change(data):
            self._shape(data)
            data["done"].pop(panel_id, None)
            return data["panels"].pop(panel_id, None) is not None

        return self._file(profile).update(change)

    # -- what the person does --

    def mark(self, profile: str, panel_id: str, item_id: str, done: bool) -> dict:
        panel_id, item_id = _id(panel_id, "panel id"), _id(item_id, "item id")

        def change(data):
            self._shape(data)
            panel = data["panels"].get(panel_id)
            if panel is None or item_id not in {item["id"] for item in panel.get("items", [])}:
                raise KeyError(item_id)
            marks = data["done"].setdefault(panel_id, {})
            if done:
                marks[item_id] = int(self._clock())
            else:
                marks.pop(item_id, None)
            return self._shown(panel, data["done"])

        return self._file(profile).update(change)

    def summary(self) -> dict:
        """For each bot with a dashboard: how many open items need its person, how many panels, when it last changed."""
        bots: dict = {}

        try:
            files = sorted(self.folder.glob("*.json"))
        except OSError:
            return bots

        for path in files:
            profile = path.stem

            if not PROFILE.match(profile):
                continue

            panels = self.read(profile)

            if not panels:
                continue

            needs = sum(1 for panel in panels for item in panel.get("items", []) if item.get("needs_you") and not item.get("done_at"))
            bots[profile] = {"needs_you": needs, "panels": len(panels), "updated": max(panel["updated"] for panel in panels)}

        return bots
