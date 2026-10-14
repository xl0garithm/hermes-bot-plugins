"""The dashboards' database: collections a bot designs, their records and history, and the views on each bot's dashboard.

One SQLite file for every bot, under the Hermes root (`~/.hermes/hermes-dashboard/dashboard.db`, 0600), so collections can
be shared between bots. The dashboard process, the messaging gateway and routines all write to it, so it runs in WAL mode
with a busy timeout, and each call opens its own connection (cheap; no connection crosses threads).

Who acts is an `Actor`: a bot (by profile) or the person (through the app). A bot creates and changes records in the
collections it owns or that are shared with it for writing; the person can change any record but never creates one.
**The person's edits win**: a field the person changed is left alone by a bot's later writes unless the bot overrides it
on purpose, and every change is kept in the record's history.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

try:
    from . import fields as F
    from . import query as Q
except ImportError:  # the tests import the modules flat
    import fields as F  # type: ignore[no-redef]
    import query as Q  # type: ignore[no-redef]

SCHEMA_VERSION = 3
MAX_COLLECTIONS = 50
MAX_OPEN_RECORDS = 5000
MAX_VIEWS = 30
MAX_BATCH = 100
MAX_NOTE = 2000
MAX_HISTORY = 100
MAX_ACTIONS = 4
# "Until I unmute": a mute far enough ahead that it never lifts by itself.
MUTE_FOREVER = 32_503_680_000
#: Closed records older than this are left out of views and numbers (they stay in the database).
CLOSED_WINDOW_DAYS = 400

PROFILE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
SOURCE_KEY_MAX = 300

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS collections (
  key TEXT PRIMARY KEY, owner TEXT NOT NULL, id TEXT NOT NULL, title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
  fields TEXT NOT NULL, actions TEXT NOT NULL DEFAULT '[]', created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, retired_at INTEGER
);
CREATE TABLE IF NOT EXISTS access (
  collection TEXT NOT NULL, profile TEXT NOT NULL, can_write INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (collection, profile)
);
CREATE TABLE IF NOT EXISTS records (
  id TEXT PRIMARY KEY, collection TEXT NOT NULL, source_key TEXT, vals TEXT NOT NULL, person_fields TEXT NOT NULL DEFAULT '{}',
  created_by TEXT NOT NULL, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, closed_at INTEGER, running TEXT,
  muted_until INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS records_source ON records (collection, source_key) WHERE source_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS records_collection ON records (collection, closed_at);
CREATE TABLE IF NOT EXISTS changes (
  id INTEGER PRIMARY KEY AUTOINCREMENT, record TEXT NOT NULL, at INTEGER NOT NULL, by_kind TEXT NOT NULL, by_name TEXT NOT NULL,
  field TEXT NOT NULL, old TEXT, new TEXT
);
CREATE INDEX IF NOT EXISTS changes_record ON changes (record, id);
CREATE TABLE IF NOT EXISTS views (
  key TEXT PRIMARY KEY, dashboard TEXT NOT NULL, id TEXT NOT NULL, collection TEXT, title TEXT NOT NULL, kind TEXT NOT NULL,
  config TEXT NOT NULL, position INTEGER NOT NULL, updated_at INTEGER NOT NULL
);
"""


def _migrate(conn) -> None:
    """Schema 2 adds a collection's buttons (`actions`) and the button job a record waits on (`running`); schema 3 the time
    a record the person muted speaks up again (`muted_until`)."""
    for table, column, kind in (("collections", "actions", "TEXT NOT NULL DEFAULT '[]'"), ("records", "running", "TEXT"), ("records", "muted_until", "INTEGER")):
        if column not in {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")


class DashboardError(ValueError):
    """Something that can't be done; the message says what to change. Field and query errors come through as these."""


class NotFound(LookupError):
    pass


@dataclass(frozen=True)
class Actor:
    kind: str  # "bot" or "person"
    name: str  # the bot's profile, or "you"

    @staticmethod
    def bot(profile: str) -> "Actor":
        if not PROFILE.match(profile or ""):
            raise DashboardError("not a bot's name")

        return Actor("bot", profile)

    @staticmethod
    def person() -> "Actor":
        return Actor("person", "you")


def default_dir() -> Path:
    from hermes_constants import get_default_hermes_root

    return get_default_hermes_root() / "hermes-dashboard"


def _default_title(fields: list[dict]) -> str | None:
    """The field that names a record when no view says: the first required text field, else the first text field."""
    text = [field for field in fields if field["type"] == "text" and not field.get("retired")]

    return next((field["key"] for field in text if field.get("required")), text[0]["key"] if text else None)


def _slug(value, what: str) -> str:
    text = str(value or "").strip().lower()

    if not SLUG.match(text):
        raise DashboardError(f"{what} must be lower-case letters, digits, '-' or '_' (got {text[:40]!r})")

    return text


def _slugify(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:48] or "collection"


def _dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _text(value, what: str, limit: int, *, required: bool = False, single_line: bool = True) -> str:
    try:
        text = F.clean_value({"key": what, "type": "text" if single_line else "longtext", "required": required}, value) or ""
    except F.FieldError as error:
        raise DashboardError(str(error)) from None

    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


SESSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


def _clean_actions(raw, fields: list[dict]) -> list[dict]:
    """A collection's buttons, `[{id, label, prompt, when?, draft?}]`, at most MAX_ACTIONS, checked against its fields."""
    if raw in (None, ""):
        return []

    if not isinstance(raw, list):
        raise DashboardError("actions is a list of buttons: [{id, label, prompt, when?, draft?}]")

    if len(raw) > MAX_ACTIONS:
        raise DashboardError(f"a collection has at most {MAX_ACTIONS} buttons")

    drafts = {field["key"] for field in fields if field["type"] == "draft" and not field.get("retired")}
    cleaned = []

    for item in raw:
        if not isinstance(item, dict):
            raise DashboardError("each button is {id, label, prompt, when?, draft?}")

        label = _text(item.get("label"), "button label", 24, required=True)
        aid = _slug(item.get("id") or _slugify(label), "button id")
        action = {"id": aid, "label": label, "prompt": _text(item.get("prompt"), f"button {aid} prompt", 2000, required=True, single_line=False)}

        if item.get("when"):
            try:
                action["when"] = Q.clean_filter(item["when"], fields)
            except (Q.QueryError, F.FieldError) as error:
                raise DashboardError(f"button {aid}: {error}") from None

        if item.get("draft"):
            key = str(item["draft"]).strip().lower()

            if key not in drafts:
                raise DashboardError(f"button {aid}: draft names one of this collection's draft fields")

            action["draft"] = key

        cleaned.append(action)

    ids = [action["id"] for action in cleaned]

    if len(set(ids)) != len(ids):
        raise DashboardError("two buttons share an id")

    return cleaned


def _offered(action: dict, record: dict, fields: list[dict], now: float) -> bool:
    """Whether a record offers a button now: its filter matches, and the draft it sends (if any) has been written."""
    if action.get("draft") and not str(record["values"].get(action["draft"]) or "").strip():
        return False

    return not action.get("when") or Q.matches(action["when"], record, fields, now)


def _plain(field: dict, value) -> str:
    """A value in words for the message a pressed button sends the bot."""
    if field["type"] == "datetime" and isinstance(value, (int, float)):
        return time.strftime("%a %Y-%m-%d %H:%M", time.localtime(value))

    if field["type"] == "checkbox":
        return "yes" if value else "no"

    if field["type"] == "money":
        return f"{value} {field.get('currency', 'USD')}"

    return str(value)


def action_message(collection: dict, record: dict, action: dict, show: dict) -> str:
    """What a pressed button asks the bot, sent in a fresh side chat: the job, the record as it is now (with the person's
    edited draft word for word) and how to finish. Its first line is what the app shows in that chat."""
    values = record["values"]
    title = str(values.get(show.get("title") or "") or "") or collection["title"]
    lines = [
        f'[Dashboard button "{action["label"]}" · {title}]',
        f'Your person pressed "{action["label"]}" on this record in the HermesBot app. Do this now: {action["prompt"]}',
        "",
        f'Record {record["id"]} in {collection["key"]} ("{collection["title"]}"):',
    ]
    drafts = []

    for field in collection["fields"]:
        value = values.get(field["key"])

        if field.get("retired") or value in (None, ""):
            continue

        if field["type"] == "draft":
            drafts.append((field, value))
        else:
            lines.append(f"- {field['label']}: {_plain(field, value)}")

    for field, value in drafts:
        if action.get("draft") and field["key"] != action["draft"]:
            continue

        edited = "your person edited it; use it exactly as written" if field["key"] in record["person_fields"] else "use it exactly as written"
        lines += ["", f"{field['label']} ({edited}):", '"""', str(value), '"""']

    lines += [
        "",
        f'When you are done, update this record with dashboard_record (action "update", record_id "{record["id"]}"): set what '
        'changed, such as the status, and add a note saying what you did (action "note"). If you could not do it, add a note '
        'saying why. Load skill_view("hermes-dashboard:dashboard") for the details.',
    ]

    return "\n".join(lines)


class Store:
    def __init__(self, folder: Path | None = None, clock=time.time):
        self._folder = Path(folder) if folder else None
        self._clock = clock
        self._ready = False

    # -- the database --------------------------------------------------------------------------------------------

    @property
    def folder(self) -> Path:
        return self._folder or default_dir()

    @property
    def path(self) -> Path:
        return self.folder / "dashboard.db"

    def _now(self) -> int:
        return int(self._clock())

    @contextmanager
    def _db(self, write: bool = False):
        folder = self.folder
        folder.mkdir(parents=True, exist_ok=True)

        try:
            os.chmod(folder, 0o700)
        except OSError:  # pragma: no cover
            pass

        fresh = not self.path.exists()
        conn = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        conn.row_factory = sqlite3.Row

        try:
            if fresh:
                os.chmod(self.path, 0o600)

            conn.execute("PRAGMA busy_timeout = 15000")

            if not self._ready:
                conn.execute("PRAGMA journal_mode = WAL")
                conn.executescript(SCHEMA)
                _migrate(conn)
                conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('schema', ?)", (str(SCHEMA_VERSION),))
                self._ready = True

            if write:
                conn.execute("BEGIN IMMEDIATE")

            yield conn

            if write:
                conn.execute("COMMIT")
        except BaseException:
            if write and conn.in_transaction:
                conn.execute("ROLLBACK")

            raise
        finally:
            conn.close()

    # -- collections -------------------------------------------------------------------------------------------

    @staticmethod
    def _collection(row) -> dict:
        return {
            "key": row["key"],
            "owner": row["owner"],
            "id": row["id"],
            "title": row["title"],
            "description": row["description"],
            "fields": json.loads(row["fields"]),
            "actions": json.loads(row["actions"] or "[]"),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "retired": bool(row["retired_at"]),
        }

    def _find_collection(self, conn, actor: Actor, ref, *, write: bool = False) -> dict:
        """A collection by `id` (the actor's own, else one shared with it) or `owner/id`, checked for access."""
        ref = str(ref or "").strip().lower()

        if "/" in ref:
            rows = conn.execute("SELECT * FROM collections WHERE key = ?", (ref,)).fetchall()
        elif actor.kind == "bot":
            rows = conn.execute("SELECT * FROM collections WHERE key = ?", (f"{actor.name}/{ref}",)).fetchall()

            if not rows:
                rows = conn.execute("SELECT c.* FROM collections c JOIN access a ON a.collection = c.key WHERE c.id = ? AND a.profile = ?", (ref, actor.name)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM collections WHERE id = ?", (ref,)).fetchall()

        if not rows:
            raise NotFound(f"no collection {ref!r}; create it with dashboard_collection")

        if len(rows) > 1:
            raise DashboardError(f"more than one collection is called {ref!r}; name it as owner/{ref}")

        collection = self._collection(rows[0])

        if collection["retired"]:
            raise NotFound(f"collection {ref!r} is retired")

        if actor.kind == "bot" and collection["owner"] != actor.name:
            grant = conn.execute("SELECT can_write FROM access WHERE collection = ? AND profile = ?", (collection["key"], actor.name)).fetchone()

            if grant is None:
                raise NotFound(f"no collection {ref!r} shared with {actor.name}")

            if write and not grant["can_write"]:
                raise DashboardError(f"{collection['key']} is shared with {actor.name} to read only")

        return collection

    def create_collection(self, actor: Actor, raw: dict) -> dict:
        if actor.kind != "bot":
            raise DashboardError("only a bot creates collections")

        title = _text(raw.get("title"), "title", 60, required=True)
        cid = _slug(raw.get("id") or _slugify(title), "collection id")

        try:
            fields = F.clean_fields(raw.get("fields"))
        except F.FieldError as error:
            raise DashboardError(str(error)) from None

        actions = _clean_actions(raw.get("actions"), fields)
        now = self._now()

        with self._db(write=True) as conn:
            owned = conn.execute("SELECT COUNT(*) FROM collections WHERE owner = ? AND retired_at IS NULL", (actor.name,)).fetchone()[0]

            if owned >= MAX_COLLECTIONS:
                raise DashboardError(f"a bot keeps at most {MAX_COLLECTIONS} collections; retire one first")

            key = f"{actor.name}/{cid}"

            if conn.execute("SELECT 1 FROM collections WHERE key = ?", (key,)).fetchone():
                raise DashboardError(f"collection {cid!r} exists; change it with action update")

            conn.execute(
                "INSERT INTO collections (key, owner, id, title, description, fields, actions, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (key, actor.name, cid, title, _text(raw.get("description"), "description", 300), _dump(fields), _dump(actions), now, now),
            )

            return self._collection(conn.execute("SELECT * FROM collections WHERE key = ?", (key,)).fetchone())

    def update_collection(self, actor: Actor, ref, raw: dict) -> dict:
        """Change the title or description; add fields; relabel, re-option or retire existing ones (by key)."""
        with self._db(write=True) as conn:
            collection = self._find_collection(conn, actor, ref, write=True)

            if actor.kind == "bot" and collection["owner"] != actor.name:
                raise DashboardError("only the bot that owns a collection changes its fields")

            fields = {field["key"]: field for field in collection["fields"]}

            for field in raw.get("add_fields") or []:
                key = str((field or {}).get("key") or "")

                if key in fields:
                    raise DashboardError(f"field {key!r} exists; change it with change_fields")

                fields[key] = field

            for change in raw.get("change_fields") or []:
                key = str((change or {}).get("key") or "")

                if key not in fields:
                    raise DashboardError(f"no field {key!r} to change")

                if change.get("type") and change["type"] != fields[key]["type"]:
                    raise DashboardError(f"field {key}: its type can't change; add a new field instead")

                fields[key] = {**fields[key], **{name: change[name] for name in ("label", "stages", "options", "required", "currency", "to") if name in change}}

            for key in raw.get("retire_fields") or []:
                if key not in fields:
                    raise DashboardError(f"no field {key!r} to retire")

                fields[key] = {**fields[key], "retired": True}

            try:
                cleaned = F.clean_fields(list(fields.values()))
            except F.FieldError as error:
                raise DashboardError(str(error)) from None

            # A status's stages may be renamed only by adding new ones: records keep the values they have.
            title = _text(raw.get("title"), "title", 60) or collection["title"]
            description = _text(raw["description"], "description", 300) if "description" in raw else collection["description"]
            # Buttons are replaced as a whole list when given, and checked against the fields as they are now.
            actions = _clean_actions(raw["actions"], cleaned) if "actions" in raw else _clean_actions(collection["actions"], cleaned)
            conn.execute("UPDATE collections SET title = ?, description = ?, fields = ?, actions = ?, updated_at = ? WHERE key = ?", (title, description, _dump(cleaned), _dump(actions), self._now(), collection["key"]))

            return self._collection(conn.execute("SELECT * FROM collections WHERE key = ?", (collection["key"],)).fetchone())

    def retire_collection(self, actor: Actor, ref) -> dict:
        with self._db(write=True) as conn:
            collection = self._find_collection(conn, actor, ref, write=True)

            if actor.kind == "bot" and collection["owner"] != actor.name:
                raise DashboardError("only the bot that owns a collection retires it")

            now = self._now()
            conn.execute("UPDATE collections SET retired_at = ?, updated_at = ? WHERE key = ?", (now, now, collection["key"]))
            conn.execute("DELETE FROM views WHERE collection = ?", (collection["key"],))

            return {**collection, "retired": True}

    def share(self, actor: Actor, ref, profile: str, can_write: bool) -> dict:
        profile = Actor.bot(profile).name

        with self._db(write=True) as conn:
            collection = self._find_collection(conn, actor, ref, write=True)

            if collection["owner"] != actor.name:
                raise DashboardError("only the bot that owns a collection shares it")

            if profile == actor.name:
                raise DashboardError("a bot already has its own collections")

            conn.execute("INSERT INTO access (collection, profile, can_write) VALUES (?, ?, ?) ON CONFLICT (collection, profile) DO UPDATE SET can_write = excluded.can_write", (collection["key"], profile, int(can_write)))

            return {"collection": collection["key"], "profile": profile, "can_write": bool(can_write)}

    def unshare(self, actor: Actor, ref, profile: str) -> bool:
        with self._db(write=True) as conn:
            collection = self._find_collection(conn, actor, ref, write=True)

            if collection["owner"] != actor.name:
                raise DashboardError("only the bot that owns a collection unshares it")

            removed = conn.execute("DELETE FROM access WHERE collection = ? AND profile = ?", (collection["key"], profile)).rowcount
            conn.execute("DELETE FROM views WHERE collection = ? AND dashboard = ?", (collection["key"], profile))

            return bool(removed)

    def list_collections(self, actor: Actor) -> list[dict]:
        with self._db() as conn:
            if actor.kind == "person":
                rows = conn.execute("SELECT *, 1 AS can_write FROM collections WHERE retired_at IS NULL ORDER BY owner, title").fetchall()
            else:
                rows = conn.execute(
                    "SELECT c.*, 1 AS can_write FROM collections c WHERE c.owner = ? AND c.retired_at IS NULL "
                    "UNION ALL SELECT c.*, a.can_write FROM collections c JOIN access a ON a.collection = c.key WHERE a.profile = ? AND c.retired_at IS NULL",
                    (actor.name, actor.name),
                ).fetchall()

            listed = []

            for row in rows:
                collection = self._collection(row)
                counts = conn.execute("SELECT COUNT(*), SUM(closed_at IS NULL) FROM records WHERE collection = ?", (collection["key"],)).fetchone()
                shared = [dict(entry) for entry in conn.execute("SELECT profile, can_write FROM access WHERE collection = ?", (collection["key"],))]
                listed.append({**collection, "records": counts[0] or 0, "open": counts[1] or 0, "can_write": bool(row["can_write"]), "shared_with": shared})

            return listed

    def describe(self, actor: Actor, ref) -> dict:
        with self._db() as conn:
            collection = self._find_collection(conn, actor, ref)
            counts = conn.execute("SELECT COUNT(*), SUM(closed_at IS NULL) FROM records WHERE collection = ?", (collection["key"],)).fetchone()
            shared = [dict(entry) for entry in conn.execute("SELECT profile, can_write FROM access WHERE collection = ?", (collection["key"],))]

            return {**collection, "records": counts[0] or 0, "open": counts[1] or 0, "shared_with": shared}

    # -- records ----------------------------------------------------------------------------------------------

    @staticmethod
    def _record(row) -> dict:
        return {
            "id": row["id"],
            "collection": row["collection"],
            "source_key": row["source_key"],
            "values": json.loads(row["vals"]),
            "person_fields": json.loads(row["person_fields"]),
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "closed_at": row["closed_at"],
            "running": json.loads(row["running"]) if row["running"] else None,
            "muted_until": row["muted_until"],
        }

    def _log(self, conn, record_id: str, actor: Actor, field: str, old, new, at: int) -> None:
        conn.execute(
            "INSERT INTO changes (record, at, by_kind, by_name, field, old, new) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (record_id, at, actor.kind, actor.name, field, None if old is None else _dump(old), None if new is None else _dump(new)),
        )

    def _apply(self, conn, collection: dict, record: dict, values: dict, actor: Actor, override_person: bool) -> tuple[dict, list[str], list[str]]:
        """Writes changed values (and keeps closed_at in step with a status). Returns the record, changed and kept keys."""
        now = self._now()
        current = dict(record["values"])
        person = dict(record["person_fields"])
        changed, kept = [], []

        for key, value in values.items():
            if actor.kind == "bot" and key in person and not override_person:
                if current.get(key) != value:
                    kept.append(key)

                continue

            if current.get(key) == value:
                continue

            self._log(conn, record["id"], actor, key, current.get(key), value, now)
            current[key] = value
            changed.append(key)

            if actor.kind == "person":
                person[key] = now

        closed_at = record["closed_at"]
        status = F.status_field(collection["fields"])

        if status and status["key"] in changed:
            done = current.get(status["key"]) in F.done_stages(status)
            closed_at = (closed_at or now) if done else None

        if changed or closed_at != record["closed_at"]:
            conn.execute("UPDATE records SET vals = ?, person_fields = ?, updated_at = ?, closed_at = ? WHERE id = ?", (_dump(current), _dump(person), now, closed_at, record["id"]))

        # The bot writing the record is the end of a pressed button's job, whatever it changed.
        if actor.kind == "bot" and record.get("running"):
            conn.execute("UPDATE records SET running = NULL WHERE id = ?", (record["id"],))

        return self._record(conn.execute("SELECT * FROM records WHERE id = ?", (record["id"],)).fetchone()), changed, kept

    def _clean(self, collection: dict, values, partial: bool) -> dict:
        try:
            return F.clean_values(collection["fields"], values or {}, partial=partial)
        except F.FieldError as error:
            raise DashboardError(str(error)) from None

    def upsert(self, actor: Actor, ref, records: list[dict], *, override_person: bool = False) -> list[dict]:
        """Adds records, or updates the one with the same `source_key`. Each item: `{values, source_key?}`."""
        if actor.kind != "bot":
            raise DashboardError("only a bot adds records")

        if not isinstance(records, list) or not records:
            raise DashboardError("records must be a non-empty list of {values, source_key}")

        if len(records) > MAX_BATCH:
            raise DashboardError(f"at most {MAX_BATCH} records at a time")

        results = []

        with self._db(write=True) as conn:
            collection = self._find_collection(conn, actor, ref, write=True)

            for item in records:
                if not isinstance(item, dict):
                    raise DashboardError("each record is {values, source_key}")

                source = str(item.get("source_key") or "").strip()[:SOURCE_KEY_MAX] or None
                existing = conn.execute("SELECT * FROM records WHERE collection = ? AND source_key = ?", (collection["key"], source)).fetchone() if source else None

                if existing is not None:
                    values = self._clean(collection, item.get("values"), partial=True)
                    record, changed, kept = self._apply(conn, collection, self._record(existing), values, actor, override_person)
                    results.append({"record": record, "created": False, "changed": changed, "kept": kept})
                    continue

                values = self._clean(collection, item.get("values"), partial=False)
                status = F.status_field(collection["fields"])

                if status and values.get(status["key"]) is None:
                    values[status["key"]] = F.first_stage(status, done=False)

                open_count = conn.execute("SELECT COUNT(*) FROM records WHERE collection = ? AND closed_at IS NULL", (collection["key"],)).fetchone()[0]

                if open_count >= MAX_OPEN_RECORDS:
                    raise DashboardError(f"{collection['key']} has {MAX_OPEN_RECORDS} open records; close some first")

                now = self._now()
                closed_at = now if status and values.get(status["key"]) in F.done_stages(status) else None
                record_id = secrets.token_hex(6)
                conn.execute(
                    "INSERT INTO records (id, collection, source_key, vals, created_by, created_at, updated_at, closed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (record_id, collection["key"], source, _dump(values), actor.name, now, now, closed_at),
                )
                self._log(conn, record_id, actor, "_created", None, None, now)
                results.append({"record": self._record(conn.execute("SELECT * FROM records WHERE id = ?", (record_id,)).fetchone()), "created": True, "changed": list(values), "kept": []})

        return results

    def _find_record(self, conn, actor: Actor, record_id: str, *, write: bool) -> tuple[dict, dict]:
        row = conn.execute("SELECT * FROM records WHERE id = ?", (str(record_id or ""),)).fetchone()

        if row is None:
            raise NotFound(f"no record {record_id!r}")

        record = self._record(row)
        collection = self._find_collection(conn, actor, record["collection"], write=write)

        return collection, record

    def update(self, actor: Actor, record_id: str, values, *, override_person: bool = False) -> dict:
        with self._db(write=True) as conn:
            collection, record = self._find_record(conn, actor, record_id, write=True)
            record, changed, kept = self._apply(conn, collection, record, self._clean(collection, values, partial=True), actor, override_person)

            return {"record": record, "changed": changed, "kept": kept}

    def set_closed(self, actor: Actor, record_id: str, closed: bool) -> dict:
        """Closes a record (its status moves to the first done stage) or opens it again (the first open stage)."""
        with self._db(write=True) as conn:
            collection, record = self._find_record(conn, actor, record_id, write=True)
            status = F.status_field(collection["fields"])

            if status:
                stage = record["values"].get(status["key"])
                is_done = stage in F.done_stages(status)

                if is_done == closed:
                    return {"record": record, "changed": [], "kept": []}

                record, changed, kept = self._apply(conn, collection, record, {status["key"]: F.first_stage(status, done=closed)}, actor, override_person=actor.kind == "person")

                return {"record": record, "changed": changed, "kept": kept}

            if bool(record["closed_at"]) == closed:
                return {"record": record, "changed": [], "kept": []}

            now = self._now()
            conn.execute("UPDATE records SET closed_at = ?, updated_at = ? WHERE id = ?", (now if closed else None, now, record["id"]))
            self._log(conn, record["id"], actor, "_closed" if closed else "_reopened", None, None, now)

            return {"record": self._record(conn.execute("SELECT * FROM records WHERE id = ?", (record["id"],)).fetchone()), "changed": ["_closed"], "kept": []}

    def add_note(self, actor: Actor, record_id: str, text) -> dict:
        note = _text(text, "note", MAX_NOTE, required=True, single_line=False)[:MAX_NOTE]

        with self._db(write=True) as conn:
            _, record = self._find_record(conn, actor, record_id, write=actor.kind == "bot")
            now = self._now()
            self._log(conn, record["id"], actor, "_note", None, note, now)
            # A bot's note ends a pressed button's job too (it may report that it could not do it).
            conn.execute("UPDATE records SET updated_at = ?, running = CASE WHEN ? THEN NULL ELSE running END WHERE id = ?", (now, actor.kind == "bot", record["id"]))

            return {"record": record["id"], "note": note, "at": now, "by": actor.name}

    def mute(self, actor: Actor, record_id: str, until) -> dict:
        """The person mutes a record until a time (epoch seconds; MUTE_FOREVER for "until I unmute") or unmutes it (None):
        while muted it lights no amber dot, leaves Needs you and waits at the bottom of its panels."""
        if actor.kind != "person":
            raise DashboardError("a record is muted by the person")

        now = self._now()

        if until is not None:
            try:
                until = min(int(until), MUTE_FOREVER)
            except (TypeError, ValueError):
                raise DashboardError("until is a time in epoch seconds, or null to unmute") from None

            if until <= now:
                raise DashboardError("pick a time that is still ahead")

        with self._db(write=True) as conn:
            _, record = self._find_record(conn, actor, record_id, write=True)
            was = record["muted_until"] if Q.muted(record, now) else None

            if was != until:
                conn.execute("UPDATE records SET muted_until = ? WHERE id = ?", (until, record["id"]))
                self._log(conn, record["id"], actor, "_muted", was, until, now)

            return {"record": {**record, "muted_until": until}}

    def start_action(self, actor: Actor, record_id: str, action_id: str, session: str | None = None) -> dict:
        """The person pressed one of a collection's buttons on a record: it is logged, the record waits on the bot (no
        amber) until the bot next writes it, and the answer carries the message the app sends the bot in a side chat."""
        if actor.kind != "person":
            raise DashboardError("a button is pressed by the person")

        with self._db(write=True) as conn:
            collection, record = self._find_record(conn, actor, record_id, write=True)
            action = next((item for item in collection["actions"] if item["id"] == str(action_id or "").strip().lower()), None)
            now = self._now()

            if action is None:
                raise NotFound(f"no button {action_id!r} on {collection['key']}")

            if record["closed_at"]:
                raise DashboardError("this record is done; open it again first")

            if Q.running(record, now):
                raise DashboardError(f"{record['running']['label']} is already under way for this record")

            if not _offered(action, record, collection["fields"], now):
                raise DashboardError(f"{action['label']} isn't offered for this record right now")

            job = {"action": action["id"], "label": action["label"], "session": session if SESSION.match(str(session or "")) else None, "at": now}
            conn.execute("UPDATE records SET running = ? WHERE id = ?", (_dump(job), record["id"]))
            self._log(conn, record["id"], actor, "_action", None, action["label"], now)
            record = {**record, "running": job}

            return {"record": record, "running": job, "message": action_message(collection, record, action, self._presentation(conn, collection, record))}

    def history(self, conn, record_id: str, limit: int = MAX_HISTORY) -> list[dict]:
        rows = conn.execute("SELECT * FROM changes WHERE record = ? ORDER BY id DESC LIMIT ?", (record_id, limit)).fetchall()

        return [
            {"at": row["at"], "by_kind": row["by_kind"], "by": row["by_name"], "field": row["field"], "old": None if row["old"] is None else json.loads(row["old"]), "new": None if row["new"] is None else json.loads(row["new"])}
            for row in rows
        ]

    def get_record(self, actor: Actor, record_id: str) -> dict:
        with self._db() as conn:
            collection, record = self._find_record(conn, actor, record_id, write=False)

            return {"record": record, "collection": collection, "history": self.history(conn, record["id"]), "show": self._presentation(conn, collection, record)}

    def _presentation(self, conn, collection: dict, record: dict) -> dict:
        """How the app heads a record: the title, subtitle and meta of the first view that shows its collection (its owner's
        dashboard first), a filled longtext field as the body, and whether any view's rule says it needs the person."""
        owner = Actor.bot(collection["owner"]).name
        views = [self._view(row) for row in conn.execute("SELECT * FROM views WHERE collection = ? ORDER BY dashboard = ? DESC, position", (collection["key"], owner))]
        fields = collection["fields"]
        live = [field for field in fields if not field.get("retired")]
        # The heading follows a view that shows rows (a number or chart has no title or meta to borrow).
        listed = next((view for view in views if view["config"].get("show")), None)
        show = dict(listed["config"]["show"]) if listed else {}

        if not show.get("title"):
            show["title"] = _default_title(fields)

        shown = {show.get(slot) for slot in ("title", "subtitle", "meta")}
        body = next((field["key"] for field in live if field["type"] == "longtext" and field["key"] not in shown and record["values"].get(field["key"])), None)
        flagged = next((view for view in views if Q.needs_you(record, view["config"], fields, self._now())), None)

        now = self._now()
        job = Q.running(record, now)
        offered = [] if record.get("closed_at") or job else [{"id": action["id"], "label": action["label"]} for action in collection.get("actions", []) if _offered(action, record, fields, now)]

        return {
            "title": show.get("title"),
            "subtitle": show.get("subtitle"),
            "meta": show.get("meta"),
            "body": body,
            "needs_you": flagged is not None,
            # The buttons this record offers now (the first is the white pill), and the pressed one it waits on.
            "actions": offered,
            "running": job,
            "view": (flagged or listed or (views[0] if views else {})).get("id"),
        }

    # -- queries --------------------------------------------------------------------------------------------------

    def _records(self, conn, collection_key: str) -> list[dict]:
        cutoff = self._now() - CLOSED_WINDOW_DAYS * 86_400
        rows = conn.execute("SELECT * FROM records WHERE collection = ? AND (closed_at IS NULL OR closed_at >= ?)", (collection_key, cutoff)).fetchall()

        return [self._record(row) for row in rows]

    def query(self, actor: Actor, ref, raw: dict) -> dict:
        """Records of a collection by filter, sorted, with an optional count/sum and grouping, for a bot to reason over."""
        raw = raw or {}

        with self._db() as conn:
            collection = self._find_collection(conn, actor, ref)
            fields = collection["fields"]

            try:
                spec = Q.clean_filter(raw.get("filter"), fields)
                order = Q.clean_sort(raw.get("sort"), fields)
                aggregate = Q.clean_aggregate(raw.get("aggregate"), fields) if raw.get("aggregate") else None
                group = Q._key(raw["group_by"], fields, "group_by") if raw.get("group_by") else None
            except (Q.QueryError, F.FieldError) as error:
                raise DashboardError(str(error)) from None

            now = self._now()
            records = [record for record in self._records(conn, collection["key"]) if Q.matches(spec, record, fields, now)]
            limit = max(1, min(int(raw.get("limit") or 50), Q.MAX_ROWS))
            result: dict = {"collection": collection["key"], "total": len(records)}

            if aggregate:
                result["aggregate"] = Q.aggregate(records, aggregate)

            if group:
                groups: dict = {}

                for record in records:
                    groups.setdefault(str(Q.value_of(record, group)), []).append(record)

                result["groups"] = {name: (Q.aggregate(items, aggregate) if aggregate else len(items)) for name, items in groups.items()}

            shown = Q.sort_records(records, order, fields)[:limit]

            if raw.get("include_history"):
                shown = [{**record, "history": self.history(conn, record["id"], 20)} for record in shown]

            result["records"] = shown

            return result

    # -- views ----------------------------------------------------------------------------------------------------

    @staticmethod
    def _view(row) -> dict:
        return {"id": row["id"], "dashboard": row["dashboard"], "collection": row["collection"], "title": row["title"], "kind": row["kind"], "config": json.loads(row["config"]), "position": row["position"], "updated_at": row["updated_at"]}

    def set_view(self, actor: Actor, raw: dict) -> dict:
        """Creates or replaces a view on the acting bot's own dashboard."""
        if actor.kind != "bot":
            raise DashboardError("views are changed by the bot")

        title = _text(raw.get("title"), "title", 40, required=True)
        vid = _slug(raw.get("id") or _slugify(title), "view id")
        kind = str(raw.get("kind") or "").strip().lower()

        with self._db(write=True) as conn:
            collection = None

            if kind not in ("summary", "stat"):
                collection = self._find_collection(conn, actor, raw.get("collection"))

            try:
                config = Q.clean_view_config(kind, raw.get("config"), collection["fields"] if collection else None)
            except (Q.QueryError, F.FieldError) as error:
                raise DashboardError(str(error)) from None

            key = f"{actor.name}/{vid}"
            existing = conn.execute("SELECT position FROM views WHERE key = ?", (key,)).fetchone()

            if existing is None and conn.execute("SELECT COUNT(*) FROM views WHERE dashboard = ?", (actor.name,)).fetchone()[0] >= MAX_VIEWS:
                raise DashboardError(f"a dashboard holds at most {MAX_VIEWS} views; remove one first")

            if "position" in raw:
                position = int(raw["position"])
            elif existing is not None:
                position = existing["position"]
            else:
                position = (conn.execute("SELECT MAX(position) FROM views WHERE dashboard = ?", (actor.name,)).fetchone()[0] or 0) + 1

            conn.execute(
                "INSERT INTO views (key, dashboard, id, collection, title, kind, config, position, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (key) DO UPDATE SET collection = excluded.collection, title = excluded.title, kind = excluded.kind, config = excluded.config, position = excluded.position, updated_at = excluded.updated_at",
                (key, actor.name, vid, collection["key"] if collection else None, title, kind, _dump(config), position, self._now()),
            )

            return self._view(conn.execute("SELECT * FROM views WHERE key = ?", (key,)).fetchone())

    def remove_view(self, actor: Actor, view_id) -> bool:
        with self._db(write=True) as conn:
            return bool(conn.execute("DELETE FROM views WHERE key = ?", (f"{actor.name}/{_slug(view_id, 'view id')}",)).rowcount)

    def reorder_views(self, actor: Actor, ids: list) -> list[dict]:
        with self._db(write=True) as conn:
            for position, vid in enumerate(ids or [], start=1):
                conn.execute("UPDATE views SET position = ? WHERE key = ?", (position, f"{actor.name}/{_slug(vid, 'view id')}"))

            return [self._view(row) for row in conn.execute("SELECT * FROM views WHERE dashboard = ? ORDER BY position, title", (actor.name,))]

    def list_views(self, dashboard: str) -> list[dict]:
        with self._db() as conn:
            return [self._view(row) for row in conn.execute("SELECT * FROM views WHERE dashboard = ? ORDER BY position, title", (dashboard,))]

    # -- what the app shows -----------------------------------------------------------------------------------

    def dashboard(self, profile: str) -> dict:
        """One bot's dashboard: each view computed, and the fields of the collections they show."""
        profile = Actor.bot(profile).name
        now = self._now()

        with self._db() as conn:
            views = [self._view(row) for row in conn.execute("SELECT * FROM views WHERE dashboard = ? ORDER BY position, title", (profile,))]
            collections: dict = {}
            records: dict = {}

            for view in views:
                key = view["collection"]

                if key and key not in collections:
                    row = conn.execute("SELECT * FROM collections WHERE key = ? AND retired_at IS NULL", (key,)).fetchone()
                    collections[key] = self._collection(row) if row else None
                    records[key] = self._records(conn, key) if row else []

            computed = []

            for view in views:
                collection = collections.get(view["collection"]) if view["collection"] else None

                if view["collection"] and collection is None:
                    continue

                computed.append(Q.compute_view({**view, "updated_at": max(view["updated_at"], max((record["updated_at"] for record in records.get(view["collection"], [])), default=0))}, collection["fields"] if collection else None, records.get(view["collection"], []), now))

            return {
                "profile": profile,
                "views": computed,
                "collections": {key: {"title": collection["title"], "owner": collection["owner"], "fields": collection["fields"]} for key, collection in collections.items() if collection},
                "now": now,
            }

    def summary(self) -> dict:
        """For each dashboard: open records that need the person (each counted once), how many views, the last change."""
        now = self._now()
        bots: dict = {}

        with self._db() as conn:
            dashboards = [row[0] for row in conn.execute("SELECT DISTINCT dashboard FROM views")]

        for profile in dashboards:
            board = self.dashboard(profile)
            flagged: set = set()
            updated = 0

            with self._db() as conn:
                for view in board["views"]:
                    updated = max(updated, view.get("updated") or 0)

                    if not view.get("needs_you") or not view.get("collection"):
                        continue

                    row = conn.execute("SELECT config FROM views WHERE key = ?", (f"{profile}/{view['id']}",)).fetchone()
                    config = json.loads(row["config"]) if row else {}
                    fields = board["collections"][view["collection"]]["fields"]

                    for record in self._records(conn, view["collection"]):
                        if Q.matches(config.get("filter"), record, fields, now) and Q.needs_you(record, config, fields, now):
                            flagged.add(record["id"])

            bots[profile] = {"needs_you": len(flagged), "views": len(board["views"]), "updated": updated}

        return bots

    def needs_you_records(self, profile: str) -> list[dict]:
        """The open records on a bot's dashboard that need the person, once each, with the view that flags them."""
        now = self._now()
        found: dict = {}

        with self._db() as conn:
            views = [self._view(row) for row in conn.execute("SELECT * FROM views WHERE dashboard = ? AND collection IS NOT NULL ORDER BY position", (Actor.bot(profile).name,))]
            # A record flagged by both a number and a list is named the way the list shows it (numbers have no rows).
            views.sort(key=lambda view: not view["config"].get("show"))

            for view in views:
                row = conn.execute("SELECT * FROM collections WHERE key = ? AND retired_at IS NULL", (view["collection"],)).fetchone()

                if not row or not view["config"].get("needs_you"):
                    continue

                collection = self._collection(row)
                shown = next((other["config"]["show"] for other in views if other["collection"] == view["collection"] and other["config"].get("show")), None)
                config = view["config"] if view["config"].get("show") else {**view["config"], "show": shown or {"title": _default_title(collection["fields"])}}

                for record in self._records(conn, collection["key"]):
                    if record["id"] not in found and Q.matches(view["config"].get("filter"), record, collection["fields"], now) and Q.needs_you(record, view["config"], collection["fields"], now):
                        found[record["id"]] = {"view": view["id"], "view_title": view["title"], "row": Q._row(record, config, collection["fields"], now)}

        return list(found.values())
