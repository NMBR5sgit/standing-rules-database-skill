#!/usr/bin/env python3
"""Tests (Dashboard Phase 1): pluggable sync webhook.

Local-first guarantee: with STANDING_RULES_SYNC_URL unset the engine makes
zero network calls and spawns zero threads; with a bad URL the write still
completes and the local DB is authoritative; with a configured URL the
payload is a well-formed POST.
"""
import io
import json
import pathlib
import sys
import tempfile
import threading
import unittest
import urllib.request
from contextlib import redirect_stdout

SCRIPTS_DIR = pathlib.Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import rules_db  # noqa: E402


class _Args:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class SyncHookTest(unittest.TestCase):

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

    def _add(self, rule_id="SR-900"):
        args = _Args(id=rule_id, name="sync test", text="a rule for sync tests",
                     summary=None, tier=2, domain="testing",
                     priority="MED", tags=None)
        with redirect_stdout(io.StringIO()):
            rules_db.cmd_add(self.con, args)

    def test_no_url_no_threads_no_network(self):
        """URL unset -> cmd_add completes with zero threads and zero network."""
        before = threading.active_count()
        called = []
        orig = rules_db.urllib.request.urlopen
        rules_db.urllib.request.urlopen = lambda *a, **k: called.append(a)
        try:
            self._add()
        finally:
            rules_db.urllib.request.urlopen = orig
        self.assertEqual(threading.active_count(), before,
                         "webhook thread spawned despite unset URL")
        self.assertEqual(called, [], "network call attempted despite unset URL")
        row = self.con.execute(
            "SELECT 1 FROM rules WHERE rule_id='SR-900'").fetchone()
        self.assertIsNotNone(row, "local write must still happen")

    def test_invalid_url_swallowed_and_local_write_survives(self):
        """Bad URL -> cmd_add completes instantly, exception swallowed, DB updated."""
        rules_db.os.environ["STANDING_RULES_SYNC_URL"] = "notaurl://bad"
        self._add()  # must not raise, must not block
        row = self.con.execute(
            "SELECT text FROM rules WHERE rule_id='SR-900'").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["text"], "a rule for sync tests")
        # Daemon thread (if any) must never keep the process alive:
        for t in threading.enumerate():
            if t is not threading.current_thread():
                self.assertTrue(t.daemon, "sync thread must be daemon")

    def test_payload_shape_via_mocked_urlopen(self):
        """Configured URL -> POST with correct JSON payload shape."""
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["method"] = req.get_method()
            captured["content_type"] = req.get_header("Content-type")
            captured["timeout"] = timeout
            captured["body"] = json.loads(req.data.decode("utf-8"))
            captured["done"].set()

        captured["done"] = threading.Event()
        rules_db.os.environ["STANDING_RULES_SYNC_URL"] = "http://dashboard.invalid/webhook"
        orig = rules_db.urllib.request.urlopen
        rules_db.urllib.request.urlopen = fake_urlopen
        try:
            self._add("SR-901")
            sent = captured["done"].wait(timeout=5)
        finally:
            rules_db.urllib.request.urlopen = orig
        self.assertTrue(sent, "webhook POST never issued for configured URL")
        self.assertEqual(captured["url"], "http://dashboard.invalid/webhook/events", "SYNC_URL is the base; the CLI appends /events")
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["content_type"], "application/json")
        self.assertEqual(captured["timeout"], 2)
        body = captured["body"]
        self.assertEqual(body["event"], "rule_added")
        self.assertEqual(body["rule_id"], "SR-901")
        self.assertTrue(body["timestamp"].endswith("Z"))
        self.assertTrue(body["source_machine"])
        self.assertEqual(body["data"]["rule_id"], "SR-901")
        self.assertEqual(body["data"]["text"], "a rule for sync tests")


if __name__ == "__main__":
    unittest.main()
