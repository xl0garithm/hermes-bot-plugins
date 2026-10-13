"""The Hermes lifecycle hooks that decide when the phone should buzz.

Every callback returns at once and never raises: `pre_tool_call` fails *closed* on a timeout (a slow plugin would
block the bot's tool), and approval hooks run on the thread that is waiting for you. Sending is queued; the
network happens on the pusher's own threads.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from pathlib import Path

try:
    from . import notify
    from .store import DeviceStore
except ImportError:  # the tests import the modules flat
    import notify  # type: ignore[no-redef]
    from store import DeviceStore  # type: ignore[no-redef]

log = logging.getLogger("hermes-bot-mobile")

#: Turns we have seen start, so `post_llm_call` can tell a long one from a short one.
MAX_TRACKED_TURNS = 256

#: Hermes's persistent session ids (`20261010_011409_0dc184`): what the app keeps a chat by and opens it with.
SESSION_ID = re.compile(r"^\d{8}_\d{6}_[0-9a-f]+$")

#: A group room's id, as Hermes requires it.
ROOM_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

#: Turns that are not the person's own chat with a bot, by the session's surface: another bot's message to this one (it
#: runs as `hermes -p <bot> chat`), a group-room member at work, a subagent, and a routine's run (it has its own push).
#: That one of them finished is no news for the phone, and its link would open a chat the person never had.
NOT_A_CHAT = frozenset({"cli", "bot_room", "subagent", "cron"})


def turn_context() -> dict:
    """What Hermes knows about the turn the calling thread works for: `chat`, the persistent id of the chat it belongs to
    (the top-level one, also from a subagent's thread); `source`, its surface (`tui` for the app, `bot_room` for a room
    member); `room`, the group room a member works in. Empty where it cannot tell; never raises."""
    found = {"chat": "", "source": "", "room": ""}

    try:
        from gateway.session_context import get_session_env

        found["chat"] = str(get_session_env("HERMES_SESSION_ID", "") or "")
        found["source"] = str(get_session_env("HERMES_SESSION_SOURCE", "") or "").strip().lower()
    except Exception:
        pass

    try:
        # Private to Hermes, so read defensively: the live session a room member's turn runs in carries its room's task.
        from tui_gateway.server import _current_runtime_session_record

        task = (_current_runtime_session_record.get() or {}).get("_hosted_room_task") or {}
        room = str(task.get("room_id") or "")
        found["room"] = room if ROOM_ID.match(room) else ""
    except Exception:
        pass

    return found


def chat_id(*candidates) -> str:
    """The first of these that is a persistent session id, else ""."""
    for candidate in candidates:
        value = str(candidate or "")

        if SESSION_ID.match(value):
            return value

    return ""


def current_profile() -> str:
    """The name of the profile the calling thread is working for (`default` for the root home)."""
    from hermes_constants import get_hermes_home, named_profile_home

    home = get_hermes_home()
    named = named_profile_home(home)

    return named.name if named else "default"


def bot_name(profile: str) -> str:
    """The bot's own name as the app shows it: its `ui_meta` title, else "Hermes" / the title-cased profile name."""
    try:
        import yaml
        from hermes_constants import get_hermes_home

        data = yaml.safe_load(Path(get_hermes_home(), "profile.yaml").read_text(encoding="utf-8")) or {}
        title = ((data.get("ui_meta") or {}).get("hermes-bots") or {}).get("title")

        if isinstance(title, str) and title.strip():
            return title.strip()
    except Exception:
        pass

    return "Hermes" if profile == "default" else profile.replace("-", " ").replace("_", " ").title()


def pending_request_id(session_key: str, command: str) -> str:
    """The id Hermes gave this approval, so the same one is never pushed twice (None-safe: returns "" on any doubt)."""
    try:
        from tools.approval import list_gateway_approvals

        queue = list_gateway_approvals(session_key)

        for entry in reversed(queue):
            if entry.get("command") == command:
                return str(entry.get("request_id") or "")

        return str(queue[-1].get("request_id") or "") if queue else ""
    except Exception:
        return ""


def question_text(args) -> str:
    """The first question a `clarify` call asks (it may carry one `question` or a list under `questions`)."""
    if not isinstance(args, dict):
        return ""

    if isinstance(args.get("question"), str):
        return args["question"]

    questions = args.get("questions")

    if isinstance(questions, list) and questions:
        first = questions[0]

        if isinstance(first, dict):
            return str(first.get("question") or first.get("prompt") or "")

        return str(first)

    return ""


class PushHooks:
    def __init__(self, store: DeviceStore | None = None, pusher: notify.Pusher | None = None, profile=current_profile, name=bot_name, clock=time.monotonic, context=turn_context):
        self.store = store or DeviceStore()
        self.pusher = pusher or notify.Pusher(self.store)
        self.dedupe = notify.Dedupe()
        self._profile = profile
        self._name = name
        self._clock = clock
        self._context = context
        self._starts: dict[str, float] = {}
        self._lock = threading.Lock()

    # -- helpers -----------------------------------------------------------------------------------------------

    def _target(self, *ids) -> dict:
        """Where a push about the calling turn should land: its group room for a room member (Needs you when the room
        cannot be told: a member's own session is not a chat the app opens), else the chat it belongs to."""
        context = self._context()

        if context["room"]:
            return {"room": context["room"]}

        if context["source"] == "bot_room":
            return {"inbox": True}

        return {"chat": chat_id(context["chat"], *ids)}

    def _emit(self, kind: str, key: str, *, detail: str = "", seconds: float = 0, failed: bool = False, session: str = "", target: dict | None = None) -> None:
        profile = self._profile()
        level = self.store.level(profile)

        if level == "off":
            return

        # The phone is in front: what is on its screen needs no push, and a finished reply shows on Home already.
        seen = self.store.presence()

        if seen["foreground"] and (kind == "done" or (kind == "routine" and not failed) or (session and session in seen["sessions"])):
            return

        # `needs_you` keeps the bot quiet unless it is blocked on you, or a routine broke.
        quiet = kind == "done" or (kind == "routine" and not failed)

        if level == "needs_you" and quiet:
            return

        if not self.store.devices() or not self.dedupe.first(f"{kind}:{profile}:{key}"):
            return

        body = notify.lines(kind, detail=detail, seconds=seconds, failed=failed)
        target = target or {}
        url = notify.INBOX if target.get("inbox") else notify.link(profile, chat=target.get("chat", ""), room=target.get("room", ""), page=target.get("page", ""))
        self.pusher.send(kind, notify.build(kind, profile=profile, bot=self._name(profile), body=body, key=key, url=url))

    # -- hooks -------------------------------------------------------------------------------------------------

    def on_approval(self, **kw) -> None:
        # Only an approval waiting on a person who is away from the terminal: the gateway path (the app, Desktop). A
        # `coalesced` one repeats an approval already waiting, which puts nothing new in front of the person.
        if kw.get("surface") != "gateway" or kw.get("coalesced"):
            return

        session_key = str(kw.get("session_key") or "")
        command = str(kw.get("command") or "")
        key = pending_request_id(session_key, command) or f"{session_key}:{kw.get('pattern_key')}"
        # In the dashboard the session key is the chat's persistent id; in the messaging gateway it is not, and then
        # the chat comes from the turn's context.
        target = self._target(session_key, kw.get("session_id"))
        self._emit("approval", key, detail=notify.describe(kw.get("description", ""), command), session=target.get("chat") or session_key, target=target)

    def on_tool(self, **kw) -> None:
        if kw.get("tool_name") != "clarify":
            return

        key = str(kw.get("tool_call_id") or kw.get("session_id") or "")
        target = self._target(kw.get("session_id"))
        self._emit("question", key, detail=notify.tidy(question_text(kw.get("args"))), session=target.get("chat") or str(kw.get("session_id") or ""), target=target)

    def on_turn_start(self, **kw) -> None:
        session = str(kw.get("session_id") or "")

        with self._lock:
            if len(self._starts) >= MAX_TRACKED_TURNS:
                self._starts.pop(next(iter(self._starts)))

            self._starts[session] = self._clock()

    def on_turn_done(self, **kw) -> None:
        session = str(kw.get("session_id") or "")

        with self._lock:
            started = self._starts.pop(session, None)

        # Only the person's own chats: not another bot's message, a room member, a subagent or a routine's run.
        if started is None or str(kw.get("platform") or "").lower() in NOT_A_CHAT:
            return

        seconds = self._clock() - started

        if seconds >= notify.DONE_AFTER_SECONDS:
            target = self._target(session)

            # A room member, known by its context when the platform did not say: its turn finishing is the room's business.
            if not (target.get("room") or target.get("inbox")):
                self._emit("done", f"{session}:{int(started)}", seconds=seconds, session=target.get("chat") or session, target=target)

    def on_session_end(self, **kw) -> None:
        if kw.get("platform") != "cron" or kw.get("interrupted"):
            return

        session = str(kw.get("session_id") or "")
        self._emit("routine", session, failed=bool(kw.get("failed")), target={"page": "routines"})

    def callbacks(self) -> dict:
        """Hook name -> callback, each wrapped so that nothing it does can reach the bot."""
        def guard(callback):
            def run(**kw):
                try:
                    callback(**kw)
                except Exception:
                    log.exception("hermes-bot-mobile hook failed")

            return run

        return {
            "pre_approval_request": guard(self.on_approval),
            "pre_tool_call": guard(self.on_tool),
            "pre_llm_call": guard(self.on_turn_start),
            "post_llm_call": guard(self.on_turn_done),
            "on_session_end": guard(self.on_session_end),
        }
