#!/usr/bin/env python3
"""Tests (Dashboard sprint, Commit 3): audit_log ledger + audit export.

PRD §3.1: every state change writes [ts][actor][action][rule_id][before][after]
to an immutable ledger; `audit` exports it as CSV or JSON for the dashboard.
"""
import io
import json
import pathlib
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

SCRIPTS_DIR = pathlib.Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import rules_db  # noqa: E402


class _Args:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class AuditLedgerTest(unittest.TestCase):

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

    def _add(self, rule_id):
        args = _Args(id=rule_id, name="n", text="v1 text", summary=None,
                     tier=2, domain="testing", priority="MED", tags=None,
                     owner="local", status="active", by="dev.a")
        with redirect_stdout(io.StringIO()):
            rules_db.cmd_add(self.con, args)

    def _update(self, rule_id):
        args = _Args(id=rule_id, text="v2 text", reason="policy change",
                     by="dev.b", priority=None)
        with redirect_stdout(io.StringIO()):
            rules_db.cmd_update(self.con, args)

    def _status(self, rule_id, state):
        args = _Args(id=rule_id, state=state, reason="retire")
        with redirect_stdout(io.StringIO()):
            rules_db.cmd_status(self.con, args)

    def _rows(self):
        return self.con.execute(
            "SELECT ts,actor,action,rule_id,before_state,after_state "
            "FROM audit_log ORDER BY audit_id").fetchall()

    def test_add_update_status_each_write_ledger_entry(self):
        self._add("SR-920")
        self._update("SR-920")
        self._status("SR-920", "deprecated")
        rows = self._rows()
        actions = [r["action"] for r in rows]
        self.assertEqual(actions, ["ADDED", "UPDATED", "STATUS"])
        self.assertEqual([r["actor"] for r in rows], ["dev.a", "dev.b", "retire"])
        self.assertTrue(all(r["ts"] for r in rows),
                        "every ledger entry must carry a timestamp")

    def test_ledger_captures_before_and_after_states(self):
        self._add("SR-921")
        self._update("SR-921")
        upd = [r for r in self._rows() if r["action"] == "UPDATED"][0]
        before = json.loads(upd["before_state"])
        after = json.loads(upd["after_state"])
        self.assertEqual(before["text"], "v1 text")
        self.assertEqual(after["text"], "v2 text")
        added = [r for r in self._rows() if r["action"] == "ADDED"][0]
        self.assertIsNone(added["before_state"], "add has no before state")
        self.assertEqual(json.loads(added["after_state"])["status"], "active")

    def test_seed_writes_ledger_and_skips_are_not_ledgered(self):
        self._add("SR-922", )
        self.con.execute("UPDATE rules SET owner='org' WHERE rule_id='SR-922'")
        seed = {"rules": [{"id": "SR-922", "name": "clobber", "text": "x",
                           "tier": 2, "domain": "testing", "priority": "MED"},
                          {"id": "SR-923", "name": "fresh", "text": "new",
                           "tier": 2, "domain": "testing", "priority": "MED"}],
                "facts": []}
        seed_path = pathlib.Path(self.tmp.name) / "seed.json"
        seed_path.write_text(json.dumps(seed), encoding="utf-8")
        with redirect_stdout(io.StringIO()):
            rules_db.cmd_seed(self.con, str(seed_path))
        rows = self._rows()
        seeded = [r for r in rows if r["action"] == "SEED"]
        ids = [r["rule_id"] for r in seeded]
        self.assertIn("SR-923", ids)
        self.assertNotIn("SR-922", ids, "skipped org-owned row must not be ledgered")

    def test_audit_export_csv(self):
        self._add("SR-924")
        self._update("SR-924")
        out = pathlib.Path(self.tmp.name) / "audit.csv"
        args = _Args(format="csv", out=str(out))
        with redirect_stdout(io.StringIO()):
            rules_db.cmd_audit(self.con, args)
        text = out.read_text(encoding="utf-8")
        lines = text.strip().splitlines()
        self.assertEqual(lines[0], "ts,actor,action,rule_id,before_state,after_state")
        self.assertEqual(len(lines), 3)  # header + 2 entries
        self.assertIn('"ADDED"', lines[1])
        # before_state JSON contains commas+quotes -> must be CSV-quoted/escaped
        self.assertIn('""text""', lines[2])

    def test_audit_export_json(self):
        self._add("SR-925")
        out = pathlib.Path(self.tmp.name) / "audit.json"
        args = _Args(format="json", out=str(out))
        with redirect_stdout(io.StringIO()):
            rules_db.cmd_audit(self.con, args)
        payload = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(len(payload), 1)
        entry = payload[0]
        self.assertEqual(entry["action"], "ADDED")
        self.assertEqual(entry["rule_id"], "SR-925")
        self.assertEqual(json.loads(entry["after_state"])["text"], "v1 text")

    def test_ledger_is_append_only(self):
        """Audit rows are never rewritten: two updates -> two distinct rows."""
        self._add("SR-926")
        self._update("SR-926")
        first_count = len(self._rows())
        self._update("SR-926")
        rows = self._rows()
        self.assertEqual(len(rows), first_count + 1)
        self.assertEqual(rows[0]["action"], "ADDED")  # old rows intact


if __name__ == "__main__":
    unittest.main()
