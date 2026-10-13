import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import board  # noqa: E402
from datetime import datetime, timezone  # noqa: E402

#: 2026-10-13 13:14 UTC, the same moment as 09:14 in New York.
VM_AT = int(datetime(2026, 10, 13, 13, 14, tzinfo=timezone.utc).timestamp())


class Setup(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.now = [1_760_000_000.0]
        self.board = board.Board(Path(self.dir.name), clock=lambda: self.now[0])

    def tearDown(self):
        self.dir.cleanup()


VOICEMAILS = {
    "id": "voicemails",
    "title": "Voicemails",
    "kind": "list",
    "note": "3 today",
    "items": [
        {"id": "vm-303", "title": "Guest in Cabin 3", "detail": "AC isn't cooling.", "needs_you": True, "at": "2026-10-13T09:14:00-04:00", "action": {"label": "Call back", "url": "tel:+15555550143"}},
        {"id": "vm-plow", "title": "Plow contractor", "detail": "Confirms Thursday."},
    ],
}


class Panels(Setup):
    def test_a_bot_writes_a_panel_and_reads_it_back(self):
        written = self.board.set_panel("concierge", VOICEMAILS)
        self.assertEqual(written["items"][0]["at"], VM_AT)
        self.assertEqual(written["updated"], 1_760_000_000)
        (panel,) = self.board.read("concierge")
        self.assertEqual([item["id"] for item in panel["items"]], ["vm-303", "vm-plow"])
        self.assertEqual(panel["items"][0]["action"], {"label": "Call back", "url": "tel:+15555550143"})

    def test_panels_keep_the_order_they_were_made_in_unless_told(self):
        self.board.set_panel("concierge", {"title": "Follow up", "items": []})
        self.board.set_panel("concierge", {"title": "This week", "kind": "metrics", "metrics": [{"label": "Reviews", "value": "12", "change": "+3"}]})
        self.board.set_panel("concierge", {"title": "Top", "kind": "summary", "summary": "**All quiet.**", "order": -1})
        self.assertEqual([panel["title"] for panel in self.board.read("concierge")], ["Top", "Follow up", "This week"])

    def test_an_item_without_an_id_keeps_one_from_its_title(self):
        first = self.board.set_panel("concierge", {"title": "Reminders", "kind": "checklist", "items": [{"title": "Restock firewood", "due": "2026-10-16"}]})
        again = self.board.set_panel("concierge", {"title": "Reminders", "kind": "checklist", "items": [{"title": "Restock firewood", "due": "2026-10-16"}]})
        self.assertEqual(first["id"], "reminders")
        self.assertEqual(first["items"][0]["id"], again["items"][0]["id"])

    def test_what_the_person_marked_done_stays_done_when_the_routine_rewrites_the_panel(self):
        self.board.set_panel("concierge", VOICEMAILS)
        self.board.mark("concierge", "voicemails", "vm-303", True)
        self.now[0] += 3600
        self.board.set_panel("concierge", VOICEMAILS)
        (panel,) = self.board.read("concierge")
        self.assertEqual(panel["items"][0]["done_at"], 1_760_000_000)
        self.assertNotIn("done_at", panel["items"][1])

    def test_marking_not_done_again(self):
        self.board.set_panel("concierge", VOICEMAILS)
        self.board.mark("concierge", "voicemails", "vm-303", True)
        panel = self.board.mark("concierge", "voicemails", "vm-303", False)
        self.assertNotIn("done_at", panel["items"][0])

    def test_marking_something_that_is_not_there(self):
        self.board.set_panel("concierge", VOICEMAILS)
        with self.assertRaises(KeyError):
            self.board.mark("concierge", "voicemails", "nope", True)
        with self.assertRaises(KeyError):
            self.board.mark("concierge", "nope", "vm-303", True)

    def test_done_marks_for_items_gone_a_month_are_forgotten(self):
        self.board.set_panel("concierge", VOICEMAILS)
        self.board.mark("concierge", "voicemails", "vm-303", True)
        self.now[0] += 31 * 86_400
        self.board.set_panel("concierge", {**VOICEMAILS, "items": VOICEMAILS["items"][1:]})
        data = json.loads((Path(self.dir.name) / "concierge.json").read_text())
        self.assertEqual(data["done"]["voicemails"], {})

    def test_items_are_added_replaced_and_removed_without_rewriting_the_panel(self):
        self.board.set_panel("concierge", VOICEMAILS)
        panel = self.board.update_items("concierge", "voicemails", upsert=[{"id": "vm-new", "title": "Lake house inquiry", "needs_you": True}, {"id": "vm-plow", "title": "Plow contractor", "detail": "Moved to Friday."}], remove=["vm-303"])
        self.assertEqual([(item["id"], item.get("detail")) for item in panel["items"]], [("vm-plow", "Moved to Friday."), ("vm-new", None)])

    def test_removing_a_panel(self):
        self.board.set_panel("concierge", VOICEMAILS)
        self.assertTrue(self.board.remove_panel("concierge", "voicemails"))
        self.assertFalse(self.board.remove_panel("concierge", "voicemails"))
        self.assertEqual(self.board.read("concierge"), [])

    def test_the_summary_counts_open_items_that_need_the_person(self):
        self.board.set_panel("concierge", VOICEMAILS)
        self.board.set_panel("concierge", {"title": "Follow up", "items": [{"title": "Early check-in request", "needs_you": True}]})
        self.board.set_panel("assistant", {"title": "Server", "kind": "metrics", "metrics": [{"label": "Disk", "value": "71%"}]})
        self.board.mark("concierge", "voicemails", "vm-303", True)
        summary = self.board.summary()
        self.assertEqual(summary["concierge"]["needs_you"], 1)
        self.assertEqual(summary["concierge"]["panels"], 2)
        self.assertEqual(summary["assistant"]["needs_you"], 0)

    def test_files_are_private(self):
        self.board.set_panel("concierge", VOICEMAILS)
        self.assertEqual(os.stat(Path(self.dir.name) / "concierge.json").st_mode & 0o777, 0o600)


class WhatABotMaySend(Setup):
    def refused(self, panel, words):
        with self.assertRaises(board.DashboardError) as caught:
            self.board.set_panel("concierge", panel)
        self.assertIn(words, str(caught.exception))

    def test_kinds_and_shapes(self):
        self.refused({"title": "X", "kind": "chart"}, "kind must be one of")
        self.refused({"title": "X", "kind": "metrics"}, "needs metrics")
        self.refused({"title": "X", "kind": "summary"}, "summary is required")
        self.refused({"kind": "list"}, "panel title is required")
        self.refused({"title": "X", "items": [{"detail": "no title"}]}, "title is required")
        self.refused({"title": "X", "items": [{"id": "a", "title": "A"}, {"id": "a", "title": "B"}]}, "share an id")

    def test_links_and_actions_only_to_safe_places(self):
        self.refused({"title": "X", "items": [{"title": "A", "action": {"label": "Go", "url": "javascript:alert(1)"}}]}, "tel:, sms:, mailto: or https://")
        self.refused({"title": "X", "items": [{"title": "A", "link": "http://example.com"}]}, "link must be")
        ok = self.board.set_panel("concierge", {"title": "X", "items": [{"title": "A", "link": "/api/plugins/hermes-drive/files/abc123defg/preview"}, {"title": "B", "link": "https://mail.google.com/mail/u/0/#inbox/1"}]})
        self.assertEqual(len(ok["items"]), 2)

    def test_ids_and_bot_names(self):
        self.refused({"id": "Bad Id!", "title": "X"}, "panel id must be")
        with self.assertRaises(board.DashboardError):
            self.board.read("../etc")

    def test_long_text_is_trimmed_and_control_characters_dropped(self):
        panel = self.board.set_panel("concierge", {"title": "X", "items": [{"title": "T" * 200, "detail": "a\x07b\nc"}]})
        self.assertEqual(len(panel["items"][0]["title"]), 80)
        self.assertEqual(panel["items"][0]["detail"], "ab c")

    def test_times(self):
        panel = self.board.set_panel("concierge", {"title": "X", "items": [{"title": "A", "at": 1_760_000_000_000}, {"title": "B", "at": "2026-10-13T13:14:00Z"}]})
        self.assertEqual([item["at"] for item in panel["items"]], [1_760_000_000, VM_AT])
        self.refused({"title": "X", "items": [{"title": "A", "at": "tomorrow"}]}, "ISO 8601")
        self.refused({"title": "X", "items": [{"title": "A", "due": "2026-02-30"}]}, "real date")

    def test_limits(self):
        for index in range(board.MAX_PANELS):
            self.board.set_panel("concierge", {"id": f"p{index}", "title": f"P{index}", "kind": "summary", "summary": "x"})
        self.refused({"id": "one-more", "title": "One more", "kind": "summary", "summary": "x"}, "at most")
        self.board.set_panel("concierge", {"id": "p0", "title": "P0 again", "kind": "summary", "summary": "y"})  # replacing is fine


if __name__ == "__main__":
    unittest.main()
