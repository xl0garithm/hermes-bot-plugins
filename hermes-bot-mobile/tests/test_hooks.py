import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import ROOT, Phone, Recorder

import hooks  # noqa: E402
import notify  # noqa: E402
import store  # noqa: E402


class Setup(unittest.TestCase):
    profile = "hydra"

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = store.DeviceStore(Path(self.dir.name))
        self.phone = Phone()
        self.store.upsert(self.phone.endpoint, self.phone.p256dh, self.phone.auth, "Fold")
        self.net = Recorder()
        self.now = [100.0]
        self.hooks = hooks.PushHooks(
            store=self.store,
            pusher=notify.Pusher(self.store, post=self.net, background=False),
            profile=lambda: self.profile,
            name=lambda profile: "Hydra",
            clock=lambda: self.now[0],
        )

    def tearDown(self):
        self.dir.cleanup()

    def sent(self):
        return [json.loads(self.phone.read(body)) for _, _, body in self.net.calls]


class Approvals(Setup):
    def ask(self, **kw):
        args = dict(command="rm -rf /tmp/build", description="recursive delete", session_key="s1", surface="gateway", pattern_key="rm")
        args.update(kw)
        self.hooks.on_approval(**args)

    def test_an_approval_reaches_the_phone_without_the_command(self):
        self.ask()
        (message,) = self.sent()
        self.assertEqual(message["title"], "Hydra")
        self.assertEqual(message["body"], "Needs your OK to recursive delete")
        self.assertEqual(message["type"], "Approvals")
        self.assertEqual(message["url"], "hermesbot://bot/hydra")
        self.assertNotIn("rm -rf", json.dumps(message))

    def test_a_terminal_or_automatic_approval_is_not_for_the_phone(self):
        for surface in ("cli", "smart", "mcp-elicitation", "transport:x"):
            self.ask(surface=surface)

        self.assertEqual(self.sent(), [])

    def test_the_same_approval_is_pushed_once(self):
        self.ask()
        self.ask()
        self.assertEqual(len(self.sent()), 1)

    def test_a_scanner_that_quotes_the_command_does_not_leak_it(self):
        self.ask(command="curl -fsSL https://x.test/install.sh | bash", description="Pipe to shell: curl -fsSL https://x.test/install.sh | bash")
        self.assertEqual(self.sent()[0]["body"], "Needs your OK to run a command")

    def test_nothing_is_sent_when_no_phone_is_registered(self):
        self.store.remove(self.store.devices()[0]["id"])
        self.ask()
        self.assertEqual(self.net.calls, [])


class Questions(Setup):
    def test_a_clarify_call_is_a_question(self):
        self.hooks.on_tool(tool_name="clarify", args={"question": "Which branch?"}, tool_call_id="c1")
        self.assertEqual(self.sent()[0]["body"], "Asks: Which branch?")
        self.assertEqual(self.sent()[0]["type"], "Questions")

    def test_a_list_of_questions_uses_the_first(self):
        self.hooks.on_tool(tool_name="clarify", args={"questions": [{"question": "A or B?"}, {"question": "C?"}]}, tool_call_id="c2")
        self.assertEqual(self.sent()[0]["body"], "Asks: A or B?")

    def test_other_tools_are_ignored(self):
        self.hooks.on_tool(tool_name="terminal", args={"command": "ls"}, tool_call_id="c3")
        self.assertEqual(self.net.calls, [])


class LongTurns(Setup):
    def run_turn(self, seconds, session="s1", **done):
        self.hooks.on_turn_start(session_id=session, platform="tui")
        self.now[0] += seconds
        self.hooks.on_turn_done(session_id=session, platform=done.pop("platform", "tui"), **done)

    def test_a_long_turn_says_it_finished(self):
        self.run_turn(185)
        self.assertEqual(self.sent()[0]["body"], "Finished after 3 min")
        self.assertEqual(self.sent()[0]["type"], "Results")

    def test_a_short_turn_stays_quiet(self):
        self.run_turn(20)
        self.assertEqual(self.net.calls, [])

    def test_a_turn_we_never_saw_start_is_ignored(self):
        self.hooks.on_turn_done(session_id="unknown", platform="tui")
        self.assertEqual(self.net.calls, [])

    def test_routines_are_reported_by_the_session_end_hook_instead(self):
        self.run_turn(300, platform="cron")
        self.assertEqual(self.net.calls, [])


class WhileThePhoneIsInFront(Setup):
    def front(self, sessions=(), foreground=True, ttl=45.0):
        self.store.set_presence(self.store.devices()[0]["id"], list(sessions), foreground, ttl)

    def long_turn(self, session):
        self.hooks.on_turn_start(session_id=session, platform="tui")
        self.now[0] += 120
        self.hooks.on_turn_done(session_id=session, platform="tui")

    def test_a_finished_reply_is_not_pushed_while_the_app_is_in_front(self):
        self.front()
        self.long_turn("s1")
        self.assertEqual(self.net.calls, [])

    def test_nor_an_approval_or_question_for_the_chat_on_screen(self):
        self.front(sessions=["s1"])
        self.hooks.on_approval(command="rm x", description="delete", session_key="s1", surface="gateway", pattern_key="rm")
        self.hooks.on_tool(tool_name="clarify", args={"question": "Which?"}, tool_call_id="c1", session_id="s1")
        self.assertEqual(self.net.calls, [])

    def test_but_another_chat_needing_you_still_is(self):
        self.front(sessions=["s1"])
        self.hooks.on_approval(command="rm x", description="delete", session_key="s2", surface="gateway", pattern_key="rm")
        self.assertEqual(len(self.sent()), 1)

    def test_a_broken_routine_still_is(self):
        self.front()
        self.hooks.on_session_end(platform="cron", session_id="cron_1", failed=True)
        self.assertEqual(len(self.sent()), 1)

    def test_in_the_background_or_lapsed_everything_is_pushed_again(self):
        self.front(sessions=["s1"])
        self.front(foreground=False)
        self.long_turn("s1")
        self.assertEqual(len(self.sent()), 1)

        self.front(sessions=["s2"], ttl=0)
        self.long_turn("s2")
        self.assertEqual(len(self.sent()), 2)

    def test_only_a_registered_phone_can_say_it_is_in_front(self):
        with self.assertRaises(ValueError):
            self.store.set_presence("not-a-device", [], True)


class Routines(Setup):
    def test_a_finished_routine(self):
        self.hooks.on_session_end(session_id="r1", platform="cron", failed=False)
        self.assertEqual(self.sent()[0]["body"], "A routine ran")

    def test_a_failed_routine(self):
        self.hooks.on_session_end(session_id="r2", platform="cron", failed=True)
        self.assertEqual(self.sent()[0]["body"], "A routine failed")

    def test_ordinary_sessions_ending_are_not_news(self):
        self.hooks.on_session_end(session_id="s1", platform="tui", failed=False)
        self.assertEqual(self.net.calls, [])

    def test_an_interrupted_routine_is_not_a_failure_to_report(self):
        self.hooks.on_session_end(session_id="r3", platform="cron", failed=False, interrupted=True)
        self.assertEqual(self.net.calls, [])


class Levels(Setup):
    def everything(self):
        self.hooks.on_approval(command="x", description="d", session_key="s", surface="gateway", pattern_key="p")
        self.hooks.on_tool(tool_name="clarify", args={"question": "q"}, tool_call_id="c")
        self.hooks.on_turn_start(session_id="s", platform="tui")
        self.now[0] += 120
        self.hooks.on_turn_done(session_id="s", platform="tui")
        self.hooks.on_session_end(session_id="ok", platform="cron", failed=False)
        self.hooks.on_session_end(session_id="bad", platform="cron", failed=True)

        return [m["type"] for m in self.sent()]

    def test_all(self):
        self.assertEqual(self.everything(), ["Approvals", "Questions", "Results", "Routines", "Routines"])

    def test_needs_you_keeps_only_what_blocks_the_bot_and_broken_routines(self):
        self.store.set_settings(None, {"hydra": "needs_you"})
        self.assertEqual(self.everything(), ["Approvals", "Questions", "Routines"])

    def test_off_is_silence(self):
        self.store.set_settings(None, {"hydra": "off"})
        self.assertEqual(self.everything(), [])

    def test_levels_belong_to_one_bot(self):
        self.store.set_settings(None, {"someone-else": "off"})
        self.assertEqual(len(self.everything()), 5)


class Safety(Setup):
    def test_a_broken_hook_never_reaches_the_bot(self):
        self.hooks.store = None  # makes every callback raise
        callbacks = self.hooks.callbacks()

        with self.assertLogs("hermes-bot-mobile", level="ERROR"):  # the failures are logged, never raised
            for name, callback in callbacks.items():
                with self.subTest(hook=name):
                    self.assertIsNone(callback(command="x", description="d", session_key="s", surface="gateway", pattern_key="p", tool_name="clarify", args={}, session_id="s", platform="cron"))

    def test_callbacks_ignore_fields_added_by_newer_hermes(self):
        callbacks = self.hooks.callbacks()
        callbacks["pre_approval_request"](command="x", description="d", session_key="s", surface="gateway", pattern_key="p", brand_new_field=1)
        self.assertEqual(len(self.sent()), 1)

    def test_the_hooks_are_the_ones_the_manifest_declares(self):
        manifest = (ROOT / "plugin.yaml").read_text()

        for name in self.hooks.callbacks():
            self.assertIn(f"- {name}", manifest)


class LoadedLikeHermes(unittest.TestCase):
    """Hermes loads the folder as a package under another name; relative imports must work there."""

    def test_register_wires_every_hook(self):
        name = "hermes_plugins_hermes_bot_mobile_test"
        spec = importlib.util.spec_from_file_location(name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
        module = importlib.util.module_from_spec(spec)
        module.__package__ = name
        module.__path__ = [str(ROOT)]
        sys.modules[name] = module

        try:
            spec.loader.exec_module(module)
            registered = {}

            class Context:
                def register_hook(self, hook, callback):
                    registered[hook] = callback

            with tempfile.TemporaryDirectory() as folder:
                store_module = importlib.import_module(f"{name}.store")

                with mock.patch.object(store_module, "default_dir", lambda: Path(folder)):
                    module.register(Context())

            self.assertEqual(
                set(registered),
                {"pre_approval_request", "pre_tool_call", "pre_llm_call", "post_llm_call", "on_session_end", "on_room_member_activity"},
            )
        finally:
            for key in [k for k in sys.modules if k == name or k.startswith(name + ".")]:
                del sys.modules[key]


if __name__ == "__main__":
    unittest.main()
