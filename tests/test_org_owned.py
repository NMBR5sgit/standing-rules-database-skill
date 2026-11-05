#!/usr/bin/env python3
"""Tests (Dashboard sprint, Commit 1): owner column + org-owned enforcement.

Covers: v2->v3 migration adds rules.owner; org-owned update/status hard-reject
with the exact error text; rejection lands in conflict_log; a
policy_bypass_attempt webhook fires; local-owned rules stay freely editable;
seed must not overwrite org-owned rows.
"""
import io
import json
import pathlib
import sqlite3
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


class OrgOwnedTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        rules_db.DB_PATH = pathlib.Path(self.tmp.name) / "rules.db"
        rules_db.REF_DIR = pathlib.Path(self.tmp.name) / "references"
        self.con = rules_db.connect()
        rules_db.init_db(self.con)
        self._saved_url = rules_db.os.environ.pop("STANDING_RULES_SYNC_URL", None)
        self._extra_cons = []

    def tearDown(self):
        for c in self._extra_cons:
            try:
                c.close()
            except Exception:
                pass
        if self._saved_url is not None:
            rules_db.os.environ["STANDING_RULES_SYNC_URL"] = self._saved_url
        else:
            rules_db.os.environ.pop("STANDING_RULES_SYNC_URL", None)
        self.con.close()
        self.tmp.cleanup()

    def _add(self, rule_id, owner="local"):
        args = _Args(id=rule_id, name="t", text="original text", summary=None,
                     tier=2, domain="testing", priority="MED", tags=None,
                     owner=owner)
        with redirect_stdout(io.StringIO()):
            rules_db.cmd_add(self.con, args)

    def test_migration_v2_to_v3_adds_owner_column(self):
        """A legacy v2 database (rules table without owner) gains the column
        on connect, and MAX(v) advances to 3 without regressing."""
        legacy = pathlib.Path(self.tmp.name) / "legacy.db"
        con2 = sqlite3.connect(legacy)
        con2.executescript("""
            CREATE TABLE rules(
              rule_id TEXT PRIMARY KEY, name TEXT NOT NULL, text TEXT NOT NULL,
              summary TEXT, tier INTEGER NOT NULL DEFAULT 2, domain TEXT,
              priority TEXT NOT NULL DEFAULT 'MED',
              status TEXT NOT NULL DEFAULT 'active', tags TEXT,
              created_at TEXT, updated_at TEXT, effective_from TEXT);
            CREATE TABLE rule_versions(version_id INTEGER PRIMARY KEY AUTOINCREMENT,
              rule_id TEXT NOT NULL, version_number INTEGER NOT NULL, name TEXT,
              text TEXT, priority TEXT, status TEXT, change_reason TEXT,
              changed_by TEXT, created_at TEXT);
            CREATE TABLE facts(fact_id INTEGER PRIMARY KEY AUTOINCREMENT,
              domain TEXT NOT NULL, text TEXT NOT NULL, created_at TEXT,
              updated_at TEXT, fact_key TEXT, fact_value TEXT);
            CREATE TABLE usage_log(log_id INTEGER PRIMARY KEY AUTOINCREMENT,
              task_desc TEXT, rule_ids TEXT, retrieval_method TEXT, reason TEXT,
              injected INTEGER DEFAULT 1, created_at TEXT);
            CREATE TABLE schema_version(v INTEGER NOT NULL);
            INSERT INTO schema_version(v) VALUES (2);
            INSERT INTO rules(rule_id,name,text,tier,domain,priority,status)
              VALUES('SR-950','legacy rule','legacy text',2,'legacy','MED','active');
        """)
        con2.commit()
        self._extra_cons.append(con2)
        rules_db.DB_PATH = legacy
        con3 = rules_db.connect()
        rules_db.init_db(con3)
        cols = {r["name"] for r in con3.execute("PRAGMA table_info(rules)")}
        self.assertIn("owner", cols)
        v = con3.execute("SELECT MAX(v) FROM schema_version").fetchone()[0]
        self.assertEqual(v, rules_db.SCHEMA_VERSION)
        self.assertGreaterEqual(v, 3)
        row = con3.execute(
            "SELECT owner FROM rules WHERE rule_id='SR-950'").fetchone()
        self.assertEqual(row["owner"], "local",
                         "migrated legacy rows must default to local")
        con3.close()
        # restore so tearDown closes the original connection cleanly
        rules_db.DB_PATH = pathlib.Path(self.tmp.name) / "rules.db"

    def test_owner_defaults_local(self):
        self._add("SR-900")
        row = con = self.con.execute(
            "SELECT owner FROM rules WHERE rule_id='SR-900'").fetchone()
        self.assertEqual(row["owner"], "local")

    def test_local_rule_remains_editable(self):
        self._add("SR-901")
        args = _Args(id="SR-901", text="new text", reason="fix", by="user",
                     priority=None)
        with redirect_stdout(io.StringIO()):
            rules_db.cmd_update(self.con, args)
        row = self.con.execute(
            "SELECT text FROM rules WHERE rule_id='SR-901'").fetchone()
        self.assertEqual(row["text"], "new text")

    def test_org_owned_update_hard_rejects(self):
        """update on an org-owned rule exits with the IT message and changes nothing."""
        self._add("SR-902", owner="org")
        args = _Args(id="SR-902", text="hijacked", reason="bypass", by="dev.jdoe",
                     priority=None)
        with self.assertRaises(SystemExit) as cm:
            with redirect_stdout(io.StringIO()):
                rules_db.cmd_update(self.con, args)
        self.assertEqual(cm.exception.code,
                         "ERROR: Cannot modify org-owned rule SR-902. Contact IT.")
        row = self.con.execute(
            "SELECT text FROM rules WHERE rule_id='SR-902'").fetchone()
        self.assertEqual(row["text"], "original text", "org rule text must not change")
        vs = self.con.execute(
            "SELECT COUNT(*) FROM rule_versions WHERE rule_id='SR-902'").fetchone()[0]
        self.assertEqual(vs, 0, "rejected update must not archive a version")

    def test_org_owned_status_hard_rejects_and_logs_conflict(self):
        self._add("SR-903", owner="org")
        args = _Args(id="SR-903", state="disabled", reason="dev.hates.it")
        with self.assertRaises(SystemExit):
            with redirect_stdout(io.StringIO()):
                rules_db.cmd_status(self.con, args)
        row = self.con.execute(
            "SELECT status FROM rules WHERE rule_id='SR-903'").fetchone()
        self.assertEqual(row["status"], "active")
        log = self.con.execute(
            "SELECT rule_id, action, attempted_by FROM conflict_log").fetchall()
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0]["rule_id"], "SR-903")
        self.assertEqual(log[0]["action"], "status")
        self.assertEqual(log[0]["attempted_by"], "dev.hates.it")

    def test_bypass_attempt_fires_webhook(self):
        """Rejection must notify the dashboard so IT sees the bypass attempt."""
        captured = {"done": threading.Event()}

        def fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data.decode("utf-8"))
            captured["done"].set()

        rules_db.os.environ["STANDING_RULES_SYNC_URL"] = "http://dashboard.invalid/hook"
        orig = rules_db.urllib.request.urlopen
        rules_db.urllib.request.urlopen = fake_urlopen
        try:
            self._add("SR-904", owner="org")
            args = _Args(id="SR-904", text="x", reason="try", by="dev.jane",
                         priority=None)
            with self.assertRaises(SystemExit):
                with redirect_stdout(io.StringIO()):
                    rules_db.cmd_update(self.con, args)
            sent = captured["done"].wait(timeout=5)
        finally:
            rules_db.urllib.request.urlopen = orig
        self.assertTrue(sent, "bypass webhook never fired")
        body = captured["body"]
        self.assertEqual(body["event"], "policy_bypass_attempt")
        self.assertEqual(body["rule_id"], "SR-904")
        self.assertEqual(body["data"]["action"], "update")
        self.assertEqual(body["data"]["attempted_by"], "dev.jane")
        self.assertTrue(body["timestamp"].endswith("Z"))

    def test_seed_does_not_overwrite_org_owned_rule(self):
        """seed/add paths must not silently clobber a dashboard-owned row."""
        self._add("SR-905", owner="org")
        seed = {"rules": [{"id": "SR-905", "name": "clobber", "text": "clobbered",
                           "tier": 2, "domain": "testing", "priority": "LOW"},
                          {"id": "SR-906", "name": "new", "text": "brand new",
                           "tier": 2, "domain": "testing", "priority": "MED"}],
                "facts": []}
        seed_path = pathlib.Path(self.tmp.name) / "seed.json"
        seed_path.write_text(json.dumps(seed), encoding="utf-8")
        with redirect_stdout(io.StringIO()) as out:
            rules_db.cmd_seed(self.con, str(seed_path))
        row = self.con.execute(
            "SELECT text FROM rules WHERE rule_id='SR-905'").fetchone()
        self.assertEqual(row["text"], "original text")
        row2 = self.con.execute(
            "SELECT 1 FROM rules WHERE rule_id='SR-906'").fetchone()
        self.assertIsNotNone(row2, "non-org seed rows must still apply")
        self.assertIn("1 org-owned rules left untouched", out.getvalue())


if __name__ == "__main__":
    unittest.main()
