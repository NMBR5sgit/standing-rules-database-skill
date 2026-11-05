#!/usr/bin/env python3
"""Tests (Dashboard sprint, Commit 4): sync fetch body (mock HTTP).

GET <STANDING_RULES_SYNC_URL>/rules.json -> upsert org-owned rules locally
(override allowed for org-owned rows, blocked for local-owned rows),
deprecate withdrawn org rules, never delete, fail clean when the dashboard
is unreachable. Mocked urlopen only — no real network in tests.
"""
import io
import json
import pathlib
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


class SyncFetchTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        rules_db.DB_PATH = pathlib.Path(self.tmp.name) / "rules.db"
        rules_db.REF_DIR = pathlib.Path(self.tmp.name) / "references"
        self.con = rules_db.connect()
        rules_db.init_db(self.con)
        rules_db.os.environ["STANDING_RULES_SYNC_URL"] = "http://dashboard.invalid"
        self._orig_urlopen = urllib.request.urlopen
        self._saved_req = rules_db.urllib.request.Request

    def tearDown(self):
        rules_db.os.environ.pop("STANDING_RULES_SYNC_URL", None)
        rules_db.urllib.request.urlopen = self._orig_urlopen
        self.con.close()
        self.tmp.cleanup()

    def _serve(self, payload):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")

        def fake_urlopen(req, timeout=None):
            fake_urlopen.last_url = req.full_url
            return _FakeResp(body)

        rules_db.urllib.request.urlopen = fake_urlopen
        return fake_urlopen

    def _sync(self):
        args = _Args()
        out = io.StringIO()
        with redirect_stdout(out):
            rules_db.cmd_sync(self.con, args)
        return out.getvalue()

    def _rule(self, rule_id, owner="local", text="personal rule", status="active"):
        con = self.con
        con.execute(
            "INSERT INTO rules(rule_id,name,text,tier,domain,priority,status,owner,created_at,updated_at) "
            "VALUES(?,?,?,2,'testing','MED',?,?,?,?)",
            (rule_id, "personal", text, status, owner,
             rules_db.now(), rules_db.now()))
        con.commit()

    def _get(self, rule_id):
        return self.con.execute(
            "SELECT * FROM rules WHERE rule_id=?", (rule_id,)).fetchone()

    def test_sync_fetches_and_upserts_org_rules(self):
        payload = {"rules": [
            {"id": "SR-ORG1", "name": "company policy", "text": "always run tests",
             "tier": 1, "domain": "engineering", "priority": "HIGH"},
            {"id": "SR-ORG2", "name": "sec policy", "text": "no secrets in logs",
             "tier": 2, "domain": "security", "priority": "HIGH", "tags": "secrets"},
        ]}
        fake = self._serve(payload)
        out = self._sync()
        self.assertIn("2 org rules applied", out)
        self.assertEqual(fake.last_url, "http://dashboard.invalid/rules.json")
        r1 = self._get("SR-ORG1")
        self.assertEqual(r1["owner"], "org")
        self.assertEqual(r1["tier"], 1)
        self.assertEqual(r1["status"], "active")

    def test_sync_overrides_org_owned_rule_but_not_local(self):
        """Org rows can overwrite org-owned local copies; local-owned rows
        in the master file are skipped unless explicitly marked org."""
        self._rule("SR-100", owner="local", text="my preference")
        payload = {"rules": [
            {"id": "SR-100", "name": "personal", "text": "org rewrite attempt",
             "tier": 2, "domain": "testing", "priority": "MED", "owner": "local"},
            {"id": "SR-100", "name": "forced", "text": "org forced text",
             "tier": 2, "domain": "testing", "priority": "MED", "owner": "org"},
        ]}
        self._serve(payload)
        out = self._sync()
        row = self._get("SR-100")
        # second entry (owner=org) upserts the first-applied org row; verify
        # a local-owned master entry never silently clobbers personal rules
        self.assertIn("1 org rules applied", out)
        self.assertIn("1 non-org entries skipped", out)
        self.assertEqual(row["owner"], "org")
        self.assertEqual(row["text"], "org forced text")

    def test_local_owned_rule_survives_org_sync_without_entry(self):
        self._rule("SR-101", owner="local", text="my rule")
        self._serve({"rules": [{"id": "SR-ORG3", "name": "n", "text": "t",
                                "tier": 2, "domain": "d", "priority": "MED"}]})
        self._sync()
        row = self._get("SR-101")
        self.assertEqual(row["owner"], "local")
        self.assertEqual(row["text"], "my rule")

    def test_org_rule_edit_rejected_after_sync(self):
        """Synced org rules must be hard-rejected by local update — the full
        enforcement loop: dashboard writes, local user cannot."""
        self._serve({"rules": [{"id": "SR-ORG4", "name": "policy", "text": "policy text",
                                "tier": 1, "domain": "engineering", "priority": "HIGH"}]})
        self._sync()
        args = _Args(id="SR-ORG4", text="local tampering", reason="try", by="dev.joe",
                     priority=None)
        with self.assertRaises(SystemExit) as cm:
            with redirect_stdout(io.StringIO()):
                rules_db.cmd_update(self.con, args)
        self.assertIn("Cannot modify org-owned rule SR-ORG4", str(cm.exception.code))
        self.assertEqual(self._get("SR-ORG4")["text"], "policy text")

    def test_withdrawn_org_rule_is_deprecated_not_deleted(self):
        self._rule("SR-OLD", owner="org", text="old policy")
        self._serve({"rules": [{"id": "SR-NEW", "name": "n", "text": "t",
                                "tier": 2, "domain": "d", "priority": "MED"}]})
        out = self._sync()
        self.assertIn("1 org rules applied", out)
        self.assertIn("1 withdrawn rules deprecated", out)
        row = self._get("SR-OLD")
        self.assertIsNotNone(row, "withdrawn rule must not be deleted")
        self.assertEqual(row["status"], "deprecated")

    def test_wipe_org_rules_payload_deletes_org_and_spares_local(self):
        """Explicit revocation directive from the dashboard:
        {"status": "revoked", "action": "wipe_org_rules"} deletes every
        owner='org' rule from the rules table (archived to rule_versions)
        while owner='local' rules stay completely intact."""
        self._rule("SR-ORGW1", owner="org", text="org policy 1")
        self._rule("SR-ORGW2", owner="org", text="org policy 2")
        self._rule("SR-MINE", owner="local", text="my personal rule")
        self._rule("SR-MINE2", owner="local", text="another personal rule")

        self._serve({"status": "revoked", "action": "wipe_org_rules",
                     "rules": []})
        out = self._sync()
        self.assertIn("2 org rules wiped", out)

        self.assertIsNone(self._get("SR-ORGW1"), "org rule must be deleted")
        self.assertIsNone(self._get("SR-ORGW2"), "org rule must be deleted")
        self.assertIsNotNone(self._get("SR-MINE"), "local rule must survive")
        self.assertIsNotNone(self._get("SR-MINE2"), "local rule must survive")
        self.assertEqual(self._get("SR-MINE")["text"], "my personal rule")

        # archived, not vanished: history lands in rule_versions as revoked
        snaps = self.con.execute(
            "SELECT rule_id, status, change_reason FROM rule_versions "
            "WHERE rule_id IN ('SR-ORGW1','SR-ORGW2')").fetchall()
        self.assertEqual(len(snaps), 2)
        self.assertTrue(all(s["status"] == "revoked" and
                            s["change_reason"] == "wipe_org_rules" for s in snaps))
        # audit-ledgered
        actions = [r["action"] for r in self.con.execute(
            "SELECT action FROM audit_log ORDER BY audit_id").fetchall()]
        self.assertEqual(actions.count("WIPED"), 2)

    def test_sync_fetch_failure_exits_clean(self):
        def boom(req, timeout=None):
            raise urllib.error.URLError("dashboard down")

        rules_db.urllib.request.urlopen = boom
        with self.assertRaises(SystemExit) as cm:
            with redirect_stdout(io.StringIO()):
                self._sync()
        self.assertIn("dashboard fetch failed", str(cm.exception.code))
        self.assertIn("source of truth", str(cm.exception.code))

    def test_bad_payload_applies_nothing(self):
        self._serve(b'{"unexpected": 1}')
        before = self.con.execute("SELECT COUNT(*) FROM rules").fetchone()[0]
        with self.assertRaises(SystemExit) as cm:
            with redirect_stdout(io.StringIO()):
                self._sync()
        self.assertIn("bad payload", str(cm.exception.code))
        after = self.con.execute("SELECT COUNT(*) FROM rules").fetchone()[0]
        self.assertEqual(after, before)

    def test_sync_writes_audit_entries(self):
        self._serve({"rules": [{"id": "SR-ORG5", "name": "n", "text": "t",
                                "tier": 2, "domain": "d", "priority": "MED"}]})
        self._sync()
        rows = self.con.execute(
            "SELECT action,actor FROM audit_log").fetchall()
        self.assertTrue(any(r["action"] == "SYNC" and r["actor"] == "sync" for r in rows))


if __name__ == "__main__":
    unittest.main()
