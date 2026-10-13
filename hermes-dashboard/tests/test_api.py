import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent


def load_api(folder):
    """Load plugin_api.py the way Hermes does: by path, under its own module name."""
    name = "hermes_dashboard_plugin_hermes-dashboard-test"
    spec = importlib.util.spec_from_file_location(name, ROOT / "dashboard" / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    board_module = module._board_module()

    return module, board_module, mock.patch.object(board_module, "default_dir", lambda: Path(folder))


class Api(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.module, self.board, patch = load_api(self.dir.name)
        patch.start()
        self.addCleanup(patch.stop)
        app = FastAPI()
        app.include_router(self.module.router, prefix="/api/plugins/hermes-dashboard")
        self.client = TestClient(app)
        self.base = "/api/plugins/hermes-dashboard"
        self.board.Board().set_panel(
            "concierge",
            {"id": "voicemails", "title": "Voicemails", "kind": "list", "items": [{"id": "vm-303", "title": "Guest in Cabin 3", "needs_you": True}, {"id": "vm-plow", "title": "Plow"}]},
        )

    def tearDown(self):
        self.dir.cleanup()

        for key in [k for k in sys.modules if k.startswith("_hermes_dashboard_package") or k.startswith("hermes_dashboard_plugin_hermes-dashboard-test")]:
            del sys.modules[key]

    def test_status_says_it_is_there(self):
        body = self.client.get(f"{self.base}/status").json()
        self.assertEqual(body["version"], "0.1.0")
        self.assertEqual(body["bots"], 1)

    def test_the_summary_counts_what_needs_the_person(self):
        body = self.client.get(f"{self.base}/bots").json()
        self.assertEqual(body["bots"]["concierge"]["needs_you"], 1)
        self.assertIsInstance(body["now"], int)

    def test_a_bots_panels(self):
        body = self.client.get(f"{self.base}/bots/concierge").json()
        self.assertEqual(body["profile"], "concierge")
        self.assertEqual([item["id"] for item in body["panels"][0]["items"]], ["vm-303", "vm-plow"])
        self.assertEqual(self.client.get(f"{self.base}/bots/assistant").json()["panels"], [])

    def test_marking_done_and_back(self):
        url = f"{self.base}/bots/concierge/panels/voicemails/items/vm-303"
        done = self.client.post(url, json={"done": True}).json()["panel"]
        self.assertIn("done_at", done["items"][0])
        self.assertEqual(self.client.get(f"{self.base}/bots").json()["bots"]["concierge"]["needs_you"], 0)
        undone = self.client.post(url, json={"done": False}).json()["panel"]
        self.assertNotIn("done_at", undone["items"][0])

    def test_marking_what_is_not_there(self):
        self.assertEqual(self.client.post(f"{self.base}/bots/concierge/panels/voicemails/items/nope", json={"done": True}).status_code, 404)
        self.assertEqual(self.client.post(f"{self.base}/bots/concierge/panels/BAD!/items/x", json={"done": True}).status_code, 400)

    def test_a_name_that_is_not_a_bot(self):
        # The router itself turns a path away (404) before it reaches the bot-name check (400): refused either way.
        self.assertIn(self.client.get(f"{self.base}/bots/..%2Fetc").status_code, (400, 404))
        self.assertEqual(self.client.get(f"{self.base}/bots/Not_A_Bot").status_code, 400)


if __name__ == "__main__":
    unittest.main()
