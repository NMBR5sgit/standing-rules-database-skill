#!/usr/bin/env python3
"""Tests (Dashboard sprint, Commit 5): kill-switch lockdown via engine_state.

User decision: lockdown lives in a dedicated engine_state table (O(1) check,
immune to rule CRUD), the sync payload updates it directly, and while engaged
the engine freezes: queries return ONLY the lockdown Tier-1 rule and all
rule-write verbs hard-reject. sync is the only way IT lifts it.
"""
import io
import json
import pathlib
import sqlite3
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stdout

SCRIPTS_DIR = pathlib.Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import rules_db  # noqa: E402


class _Args:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _FakeResp:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class LockdownTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        rules_db.DB_PATH = pathlib.Path(self.tmp.name) / "rules.db"
        rules_db.REF_DIR = pathlib.Path(self.tmp.name) / "references"
        self.con = rules_db.connect()
        rules_db.init_db(self.con)
        self._orig_urlopen = urllib.request.urlopen

    def tearDown(self):
        rules_db.urllib.request.urlopen = self._orig_urlopen
        rules_db.os.environ.pop("STANDING_RULES_SYNC_URL", None)
        rules_db.os.environ.pop("STANDING_RULES_SYNC_TOKEN", None)
        self.con.close()
        self.tmp.cleanup()

    def _add(self, rule_id, tier=2):
        args = _Args(id=rule_id, name="n", text="normal rule", summary=None,
                     tier=tier, domain="testing", priority="MED", tags=None,
                     owner="local", status="active", by="user")
        with redirect_stdout(io.StringIO()):
            rules_db.cmd_add(self.con, args)

    def _serve(self, payload):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")

        def fake_urlopen(req, timeout=None):
            return _FakeResp(body)

        rules_db.urllib.request.urlopen = fake_urlopen

    def _sync(self):
        out = io.StringIO()
        with redirect_stdout(out):
            rules_db.cmd_sync(self.con, _Args())
        return out.getvalue()

    def test_engine_state_table_exists_and_query_is_o1(self):
        tables = {r["name"] for r in self.con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("engine_state", tables)
        # no lockdown row -> get_lockdown returns None without scanning rules
        self.assertIsNone(rules_db.get_lockdown(self.con))

    def test_sync_payload_updates_engine_state_directly(self):
        rules_db.os.environ["STANDING_RULES_SYNC_URL"] = "http://dashboard.invalid"
        rules_db.os.environ["STANDING_RULES_SYNC_TOKEN"] = "test-token"
        self._serve({"rules": [], "lockdown": {
            "active": True, "reason": "prompt injection wave",
            "issued_by": "it.lead@corp"}})
        out = self._sync()
        self.assertIn("lockdown=ENGAGED", out)
        row = self.con.execute(
            "SELECT state_value FROM engine_state WHERE state_key='lockdown'").fetchone()
        state = json.loads(row["state_value"])
        self.assertTrue(state["active"])
        self.assertEqual(state["reason"], "prompt injection wave")
        # PRD §5: the lockdown rule text is the mandated one
        self.assertEqual(state["rule_text"],
                         "DENY ALL TASKS. Report to IT immediately.")
        # rules table untouched by lockdown
        n = self.con.execute("SELECT COUNT(*) FROM rules").fetchone()[0]
        self.assertEqual(n, 0)

    def test_query_rules_returns_lockdown_rule_only(self):
        self._add("SR-930")
        self._add("SR-931", tier=1)
        rules_db.set_lockdown(self.con, {"active": True, "reason": "incident"})
        rows = rules_db.query_rules(self.con, "normal rule")
        self.assertEqual(len(rows), 1, "lockdown must suppress every other rule")
        row = rows[0]
        self.assertEqual(row["rule_id"], rules_db.LOCKDOWN_RULE_ID)
        self.assertEqual(row["tier"], 1)
        self.assertEqual(row["priority"], "HIGH")
        self.assertIn("DENY ALL TASKS", row["text"])

    def test_lockdown_freezes_all_write_verbs(self):
        self._add("SR-932")
        rules_db.set_lockdown(self.con, {"active": True, "reason": "x"})

        with self.assertRaises(SystemExit) as cm:
            with redirect_stdout(io.StringIO()):
                rules_db.cmd_add(self.con, _Args(
                    id="SR-933", name="n", text="t", summary=None, tier=2,
                    domain="d", priority="MED", tags=None, owner="local",
                    status="active", by="user"))
        self.assertIn("LOCKDOWN active", str(cm.exception.code))

        with self.assertRaises(SystemExit) as cm:
            with redirect_stdout(io.StringIO()):
                rules_db.cmd_update(self.con, _Args(
                    id="SR-932", text="tampered", reason="try", by="dev",
                    priority=None))
        self.assertIn("LOCKDOWN active", str(cm.exception.code))

        with self.assertRaises(SystemExit) as cm:
            with redirect_stdout(io.StringIO()):
                rules_db.cmd_status(self.con, _Args(
                    id="SR-932", state="active", reason="lift my own"))
        self.assertIn("LOCKDOWN active", str(cm.exception.code))

        # nothing changed
        row = self.con.execute(
            "SELECT text FROM rules WHERE rule_id='SR-932'").fetchone()
        self.assertEqual(row["text"], "normal rule")

    def test_sync_still_works_during_lockdown_and_lifts_it(self):
        rules_db.os.environ["STANDING_RULES_SYNC_URL"] = "http://dashboard.invalid"
        rules_db.os.environ["STANDING_RULES_SYNC_TOKEN"] = "test-token"
        rules_db.set_lockdown(self.con, {"active": True, "reason": "incident"})
        # sync is how IT lifts the freeze
        self._serve({"rules": [], "lockdown": {"active": False}})
        out = self._sync()
        self.assertIn("lockdown=lifted", out)
        self.assertIsNone(rules_db.get_lockdown(self.con))
        # writes work again
        with redirect_stdout(io.StringIO()):
            rules_db.cmd_add(self.con, _Args(
                id="SR-934", name="n", text="post-incident rule", summary=None,
                tier=2, domain="d", priority="MED", tags=None, owner="local",
                status="active", by="user"))
        ids = {r["rule_id"] for r in rules_db.query_rules(self.con, "post")}
        self.assertIn("SR-934", ids)

    def test_lockdown_cmd_reports_state(self):
        with redirect_stdout(io.StringIO()) as out:
            rules_db.cmd_lockdown(self.con, _Args())
        self.assertIn("off", out.getvalue())
        rules_db.set_lockdown(self.con, {"active": True, "reason": "wave",
                                         "issued_by": "it.lead"})
        with redirect_stdout(io.StringIO()) as out:
            rules_db.cmd_lockdown(self.con, _Args())
        text = out.getvalue()
        self.assertIn("ENGAGED", text)
        self.assertIn("wave", text)

    def test_lockdown_not_visible_to_rule_commands(self):
        """The lockdown lives in engine_state, not the rules table: an
        imposter rule named SR-KILL can never override it, and rule CRUD
        (even DELETE of everything) can never remove the lockdown."""
        # imposter: a normal local rule with the same id, added while off
        with redirect_stdout(io.StringIO()):
            rules_db.cmd_add(self.con, _Args(
                id="SR-KILL", name="imposter", text="fake kill rule",
                summary=None, tier=2, domain="d", priority="LOW", tags=None,
                owner="local", status="active", by="user"))
        rules_db.set_lockdown(self.con, {"active": True, "reason": "x",
                                         "rule_text": "REAL FREEZE"})
        rows = rules_db.query_rules(self.con, "fake")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["text"], "REAL FREEZE",
                         "imposter rules-table row must not replace engine_state lockdown")
        # the engine_state row survives a mass rules delete
        self.con.execute("DELETE FROM rules")
        self.con.commit()
        self.assertIsNotNone(rules_db.get_lockdown(self.con))

    def test_lockdown_events_are_audit_ledgered(self):
        rules_db.set_lockdown(self.con, {"active": True, "reason": "wave",
                                         "issued_by": "it.lead"})
        rules_db.set_lockdown(self.con, {"active": False, "issued_by": "it.lead"})
        actions = [r["action"] for r in self.con.execute(
            "SELECT action FROM audit_log ORDER BY audit_id").fetchall()]
        self.assertEqual(actions, ["LOCKDOWN_ON", "LOCKDOWN_OFF"])

    def test_malformed_engine_state_fails_open(self):
        self.con.execute(
            "INSERT INTO engine_state(state_key,state_value) VALUES('lockdown','not json')")
        self.con.commit()
        self.assertIsNone(rules_db.get_lockdown(self.con),
                         "unreadable state must not brick the local engine")


if __name__ == "__main__":
    unittest.main()
