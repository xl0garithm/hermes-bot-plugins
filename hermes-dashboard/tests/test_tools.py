import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import board  # noqa: E402
import tools  # noqa: E402


class Tools(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.who = ["concierge"]
        self.tools = tools.DashboardTools(board=board.Board(Path(self.dir.name)), profile=lambda: self.who[0])

    def tearDown(self):
        self.dir.cleanup()

    def call(self, handler, args=None):
        return json.loads(handler(args or {}, task_id="t1"))

    def test_a_bot_builds_reads_and_changes_its_dashboard(self):
        saved = self.call(self.tools.set_panel, {"title": "Voicemails", "kind": "list", "items": [{"id": "a", "title": "Guest in 303", "needs_you": True}, {"id": "b", "title": "Plow"}]})
        self.assertEqual(saved["panel"]["id"], "voicemails")
        self.assertIn("2 open items, 1 need the person", saved["message"])

        updated = self.call(self.tools.update_items, {"panel_id": "voicemails", "upsert": [{"id": "c", "title": "Lake house inquiry"}], "remove": ["b"]})
        self.assertEqual([item["id"] for item in updated["panel"]["items"]], ["a", "c"])

        read = self.call(self.tools.read)
        self.assertEqual(len(read["panels"]), 1)

        self.assertTrue(self.call(self.tools.remove_panel, {"panel_id": "voicemails"})["removed"])
        self.assertEqual(self.call(self.tools.read)["message"], "No panels yet.")

    def test_each_bot_has_its_own_dashboard(self):
        self.call(self.tools.set_panel, {"title": "Voicemails", "kind": "list", "items": []})
        self.who[0] = "assistant"
        self.assertEqual(self.call(self.tools.read)["panels"], [])

    def test_mistakes_come_back_as_errors_the_bot_can_fix(self):
        self.assertIn("kind must be one of", self.call(self.tools.set_panel, {"title": "X", "kind": "chart"})["error"])
        self.assertIn("no panel", self.call(self.tools.update_items, {"panel_id": "nope", "upsert": [{"title": "A"}]})["error"])
        self.assertIn("tel:, sms:, mailto: or https://", self.call(self.tools.set_panel, {"title": "X", "kind": "list", "items": [{"title": "A", "action": {"label": "Go", "url": "file:///etc/passwd"}}]})["error"])

    def test_the_schemas_name_the_tools_the_manifest_declares(self):
        manifest = (ROOT / "plugin.yaml").read_text()
        for name, schema, _handler in self.tools.registrations():
            self.assertEqual(schema["name"], name)
            self.assertIn(f"- {name}", manifest)


class LoadedLikeHermes(unittest.TestCase):
    """Hermes loads the folder as a package under another name; registration must work there."""

    def test_register_adds_the_tools_the_skill_and_the_hint(self):
        name = "hermes_plugins_hermes_dashboard_test"
        spec = importlib.util.spec_from_file_location(name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
        module = importlib.util.module_from_spec(spec)
        module.__package__ = name
        module.__path__ = [str(ROOT)]
        sys.modules[name] = module

        try:
            spec.loader.exec_module(module)
            seen = {"tools": [], "skills": [], "sections": []}

            class Context:
                def register_tool(self, name, toolset, schema, handler, **kw):
                    seen["tools"].append((name, toolset))

                def register_skill(self, name, path, description):
                    seen["skills"].append((name, Path(path).is_file()))

                def register_system_prompt_section(self, name, text, max_chars=None):
                    seen["sections"].append(name)

            module.register(Context())
            self.assertEqual([tool for tool, _ in seen["tools"]], ["dashboard_read", "dashboard_set_panel", "dashboard_update_items", "dashboard_remove_panel"])
            self.assertEqual({toolset for _, toolset in seen["tools"]}, {"dashboard"})
            self.assertEqual(seen["skills"], [("dashboard", True)])
            self.assertEqual(seen["sections"], ["hermes-dashboard.hint"])
        finally:
            for key in [k for k in sys.modules if k == name or k.startswith(name + ".")]:
                del sys.modules[key]


if __name__ == "__main__":
    unittest.main()
