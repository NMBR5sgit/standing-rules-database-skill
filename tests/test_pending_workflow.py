#!/usr/bin/env python3
"""Tests (Dashboard sprint, Commit 2): pending status + propose verb.

Approval-workflow state machine (Enterprise PRD §2.2):
propose -> pending -> active (approved) | rejected.
No auto-promotion: a pending rule must NOT be enforced by query_rules.
"""
import io
import json
import pathlib
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout

SCRIPTS_DIR = pathlib.Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import rules_db  # noqa: E402


class _Args:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class PendingWorkflowTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        rules_db.DB_PATH = pathlib.Path(self.tmp.name) / "rules.db"
        rules_db.REF_DIR = pathlib.Path(self.tmp.name) / "references"
        self.con = rules_db.connect()
        rules_db.init_db(self.con)
        self._saved_url = rules_db.os.environ.pop("STANDING_RULES_SYNC_URL", None)

    def tearDown(self):
        if self._saved_url is not None:
            rules_db.os.environ["STANDING_RULES_SYNC_URL"] = self._saved_url
        else:
            rules_db.os.environ.pop("STANDING_RULES_SYNC_URL", None)
        self.con.close()
        self.tmp.cleanup()

    def _propose(self, rule_id="SR-910"):
        args = _Args(id=rule_id, name="proposed", text="team deploy rule",
                     summary=None, tier=2, domain="deployment",
                     priority="MED", tags="deploy team", owner="local",
                     status="pending")
        with redirect_stdout(io.StringIO()) as out:
            rules_db.cmd_add(self.con, args)
        return out.getvalue()

    def _status(self, rule_id, state, reason=""):
        args = _Args(id=rule_id, state=state, reason=reason)
        with redirect_stdout(io.StringIO()):
            rules_db.cmd_status(self.con, args)

    def _state(self, rule_id):
        return self.con.execute(
            "SELECT status FROM rules WHERE rule_id=?", (rule_id,)).fetchone()["status"]

    def test_propose_creates_pending_rule(self):
        out = self._propose()
        self.assertIn("proposed SR-910", out)
        self.assertEqual(self._state("SR-910"), "pending")

    def test_pending_rule_is_not_enforced(self):
        """No auto-promotion: query_rules must not return a pending rule."""
        self._propose("SR-911")
        rows = rules_db.query_rules(self.con, "deploy team")
        ids = [r["rule_id"] for r in rows]
        self.assertNotIn("SR-911", ids,
                         "pending rule was enforced before approval")

    def test_approve_activates_rule(self):
        """Admin approval (status active) puts the rule into enforcement."""
        self._propose("SR-912")
        self._status("SR-912", "active", "approved by IT lead")
        self.assertEqual(self._state("SR-912"), "active")
        rows = rules_db.query_rules(self.con, "deploy team")
        ids = [r["rule_id"] for r in rows]
        self.assertIn("SR-912", ids)

    def test_reject_state(self):
        self._propose("SR-913")
        self._status("SR-913", "rejected", "duplicate of SR-101")
        self.assertEqual(self._state("SR-913"), "rejected")
        rows = rules_db.query_rules(self.con, "deploy team")
        ids = [r["rule_id"] for r in rows]
        self.assertNotIn("SR-913", ids)

    def test_add_still_creates_active_rule(self):
        """The plain add verb must be unchanged: active immediately."""
        args = _Args(id="SR-914", name="direct", text="personal preference",
                     summary=None, tier=2, domain="testing", priority="LOW",
                     tags=None, owner="local", status="active")
        with redirect_stdout(io.StringIO()) as out:
            rules_db.cmd_add(self.con, args)
        self.assertIn("added SR-914", out.getvalue())
        self.assertEqual(self._state("SR-914"), "active")

    def test_propose_fires_rule_proposed_webhook(self):
        captured = {"done": threading.Event()}

        def fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data.decode("utf-8"))
            captured["done"].set()

        rules_db.os.environ["STANDING_RULES_SYNC_URL"] = "http://dashboard.invalid/hook"
        orig = rules_db.urllib.request.urlopen
        rules_db.urllib.request.urlopen = fake_urlopen
        try:
            self._propose("SR-915")
            sent = captured["done"].wait(timeout=5)
        finally:
            rules_db.urllib.request.urlopen = orig
        self.assertTrue(sent, "proposal webhook never fired")
        body = captured["body"]
        self.assertEqual(body["event"], "rule_proposed")
        self.assertEqual(body["rule_id"], "SR-915")
        self.assertEqual(body["data"]["status"], "pending")

    def test_pending_status_transition_fires_status_webhook(self):
        captured = {"done": threading.Event()}

        def fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data.decode("utf-8"))
            captured["done"].set()

        rules_db.os.environ["STANDING_RULES_SYNC_URL"] = "http://dashboard.invalid/hook"
        orig = rules_db.urllib.request.urlopen
        rules_db.urllib.request.urlopen = fake_urlopen
        try:
            self._propose("SR-916")
            captured["done"].wait(timeout=5)
            captured["done"].clear()
            self._status("SR-916", "active", "approved")
            sent = captured["done"].wait(timeout=5)
        finally:
            rules_db.urllib.request.urlopen = orig
        self.assertTrue(sent)
        body = captured["body"]
        self.assertEqual(body["event"], "rule_status_changed")
        self.assertEqual(body["data"]["status"], "active")


if __name__ == "__main__":
    unittest.main()
