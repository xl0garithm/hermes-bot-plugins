import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent

FIELDS = [
    {"key": "guest", "label": "Guest", "type": "text", "required": True},
    {"key": "status", "type": "status", "stages": [{"name": "New"}, {"name": "Replied"}, {"name": "Done", "done": True}]},
    {"key": "channel", "type": "choice", "options": ["Airbnb", "Vrbo"]},
]


class Api(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        name = "hermes_dashboard_plugin_hermes-dashboard-test"
        spec = importlib.util.spec_from_file_location(name, ROOT / "dashboard" / "plugin_api.py")
        self.module = importlib.util.module_from_spec(spec)
        sys.modules[name] = self.module
        spec.loader.exec_module(self.module)
        S, T = self.module._package()
        self.S = S
        T._shared.clear()
        # The routes mirror the kanban board first: never this machine's real board, a stand-in with no cards.
        self.kanban = self.module._kanban()
        self.kanban._last[0] = 0.0
        board = mock.patch.object(self.kanban, "HermesBoard", lambda: types.SimpleNamespace(boards=lambda: [], open_cards=lambda board: [], card=lambda board, task: None))
        board.start()
        self.addCleanup(board.stop)
        patch = mock.patch.object(S, "default_dir", lambda: Path(self.dir.name))
        patch.start()
        self.addCleanup(patch.stop)
        store = T.open_store()
        bot = S.Actor.bot("concierge")
        store.create_collection(bot, {"id": "guest-emails", "title": "Guest emails", "fields": FIELDS})
        self.record = store.upsert(bot, "guest-emails", [{"source_key": "m1", "values": {"guest": "Dana", "channel": "Airbnb"}}])[0]["record"]
        store.set_view(bot, {"id": "inbox", "title": "Guest emails", "kind": "list", "collection": "guest-emails", "config": {"show": {"title": "guest", "subtitle": "channel"}, "needs_you": {"field": "status", "op": "eq", "value": "New"}}})
        app = FastAPI()
        app.include_router(self.module.router, prefix="/api/plugins/hermes-dashboard")
        self.client = TestClient(app)
        self.base = "/api/plugins/hermes-dashboard"

    def tearDown(self):
        self.dir.cleanup()

        for key in [k for k in sys.modules if k.startswith("_hermes_dashboard_package") or k.startswith("hermes_dashboard_plugin_hermes-dashboard-test")]:
            del sys.modules[key]

    def test_status_and_summary(self):
        manifest = (ROOT / "plugin.yaml").read_text(encoding="utf-8")
        self.assertIn(f"version: {self.client.get(f'{self.base}/status').json()['version']}\n", manifest)
        self.assertEqual(self.client.get(f"{self.base}/bots").json()["bots"]["concierge"]["needs_you"], 1)

    def test_a_blocked_kanban_card_needs_you_and_a_note_answers_it(self):
        card = {"board": "default", "id": "t_1", "title": "Write the arrivals rule", "assignee": "concierge", "status": "blocked", "reason": "Which date?", "latest": None, "updated": 1}
        answered = []

        def answer(board, task, text):
            answered.append((task, text))
            card.update(status="ready", reason=None)

            return "ready"

        fake = types.SimpleNamespace(boards=lambda: ["default"], open_cards=lambda board: [dict(card)], card=lambda board, task: dict(card), answer=answer)

        with mock.patch.object(self.kanban, "HermesBoard", lambda: fake), mock.patch.object(self.S.L, "plugin_installed", lambda root, profile: True):
            self.assertEqual(self.client.get(f"{self.base}/bots").json()["bots"]["concierge"]["needs_you"], 2)
            records = self.client.get(f"{self.base}/bots/concierge/needs-you").json()["records"]
            row = next(item["row"] for item in records if item["view"] == "background-work")
            body = self.client.post(f"{self.base}/records/{row['id']}/notes", json={"text": "The 14th."}).json()
            self.assertEqual(body["card"], {"card": "t_1", "posted": True, "status": "ready"})
            self.assertEqual(answered, [("t_1", "The 14th.")])
            self.assertEqual(self.client.get(f"{self.base}/bots").json()["bots"]["concierge"]["needs_you"], 1)

        # A note on any other record is only a note.
        self.assertNotIn("card", self.client.post(f"{self.base}/records/{self.record['id']}/notes", json={"text": "hi"}).json())

    def test_a_dashboard_computed(self):
        body = self.client.get(f"{self.base}/bots/concierge").json()
        self.assertEqual(body["views"][0]["rows"][0]["title"]["value"], "Dana")
        self.assertEqual(body["collections"]["concierge/guest-emails"]["fields"][0]["key"], "guest")
        self.assertEqual(self.client.get(f"{self.base}/bots/assistant").json()["views"], [])
        # Whether the bot has the plugin is asked of its own profile; this test root has none, so it can't be told.
        self.assertIn("installed", body)
        self.assertIsNone(body["installed"])

    def test_the_persons_actions_on_a_record(self):
        url = f"{self.base}/records/{self.record['id']}"
        self.assertEqual(self.client.patch(url, json={"values": {"status": "Replied"}}).json()["record"]["values"]["status"], "Replied")
        self.assertEqual(self.client.post(f"{url}/notes", json={"text": "Call her after 2"}).json()["note"], "Call her after 2")
        self.assertIsNotNone(self.client.post(f"{url}/close").json()["record"]["closed_at"])
        self.assertIsNone(self.client.post(f"{url}/reopen").json()["record"]["closed_at"])
        history = self.client.get(url).json()["history"]
        self.assertEqual([entry["field"] for entry in history][:3], ["status", "status", "_note"])
        self.assertEqual(self.client.get(f"{self.base}/bots/concierge/needs-you").json()["records"][0]["row"]["title"]["value"], "Dana")

    def test_pressing_a_button(self):
        store = self.module._package()[1].open_store()
        store.update_collection(self.S.Actor.bot("concierge"), "guest-emails", {"actions": [{"id": "send", "label": "Send reply", "prompt": "Send it."}]})
        url = f"{self.base}/records/{self.record['id']}/actions/send"
        body = self.client.post(url, json={"session": "20261013_094100_ab12"}).json()
        self.assertTrue(body["message"].startswith('[Dashboard button "Send reply"'))
        self.assertEqual(body["running"]["session"], "20261013_094100_ab12")
        self.assertEqual(self.client.post(url, json={}).status_code, 400)
        self.assertEqual(self.client.post(f"{self.base}/records/{self.record['id']}/actions/nope", json={}).status_code, 404)

    def test_muting_a_record(self):
        url = f"{self.base}/records/{self.record['id']}/mute"
        until = int(__import__("time").time()) + 3600
        self.assertEqual(self.client.post(url, json={"until": until}).json()["record"]["muted_until"], until)
        self.assertEqual(self.client.get(f"{self.base}/bots").json()["bots"]["concierge"]["needs_you"], 0)
        self.assertIsNone(self.client.post(url, json={"until": None}).json()["record"]["muted_until"])
        self.assertEqual(self.client.post(url, json={"until": 5}).status_code, 400)

    def test_unchanged_since_the_last_read(self):
        board = self.client.get(f"{self.base}/bots/concierge").json()
        same = self.client.get(f"{self.base}/bots/concierge", params={"since": board["version"]}).json()
        self.assertEqual((same["unchanged"], same["version"]), (True, board["version"]))
        self.client.patch(f"{self.base}/records/{self.record['id']}", json={"values": {"status": "Replied"}})
        changed = self.client.get(f"{self.base}/bots/concierge", params={"since": board["version"]}).json()
        self.assertNotIn("unchanged", changed)
        self.assertIn("views", changed)

    def test_bad_requests(self):
        self.assertEqual(self.client.get(f"{self.base}/records/nope").status_code, 404)
        self.assertEqual(self.client.patch(f"{self.base}/records/{self.record['id']}", json={"values": {"status": "Archived"}}).status_code, 400)
        self.assertEqual(self.client.get(f"{self.base}/bots/Not_A_Bot").status_code, 400)


if __name__ == "__main__":
    unittest.main()
