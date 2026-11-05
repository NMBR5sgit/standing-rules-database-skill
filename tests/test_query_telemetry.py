#!/usr/bin/env python3
"""Tests (Dashboard sprint, Commit 6): batched query telemetry.

Hard constraints proven here:
- URL unset  -> hot query path is UNTOUCHED: no query_telemetry rows,
  zero threads, zero network.
- URL set    -> one local row per query; dashboard receives ONE batched
  webhook every TELEMETRY_BATCH_SIZE queries (not per-query packets).
- query_rules() itself remains pure: telemetry lives in cmd_query so the
  benchmark hot path never pays for it.
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


class _FakeResp:
    def read(self):
        return b"{}"

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class QueryTelemetryTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        rules_db.DB_PATH = pathlib.Path(self.tmp.name) / "rules.db"
        rules_db.REF_DIR = pathlib.Path(self.tmp.name) / "references"
        self.con = rules_db.connect()
        rules_db.init_db(self.con)
        self._orig_urlopen = urllib.request.urlopen
        self._orig_threads = threading.Thread
        self.posts = []
        self._thread_counts = []

        args = _Args(id="SR-940", name="n", text="telemetry probe rule",
                     summary=None, tier=2, domain="testing", priority="MED",
                     tags=None, owner="local", status="active", by="user")
        with redirect_stdout(io.StringIO()):
            rules_db.cmd_add(self.con, args)

    def tearDown(self):
        rules_db.urllib.request.urlopen = self._orig_urlopen
        rules_db.threading.Thread = self._orig_threads
        rules_db.os.environ.pop("STANDING_RULES_SYNC_URL", None)
        self.con.close()
        self.tmp.cleanup()

    def _mock_posts(self):
        def fake_urlopen(req, timeout=None):
            self.posts.append(json.loads(req.data.decode("utf-8")))
            return _FakeResp()

        rules_db.urllib.request.urlopen = fake_urlopen

        counts = self._thread_counts

        class _CountingThread(self._orig_threads):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                counts.append(1)

        def fake_thread(*a, **kw):
            return _CountingThread(*a, **kw)

        rules_db.threading.Thread = fake_thread

    def _query(self, match):
        out = io.StringIO()
        with redirect_stdout(out):
            rules_db.cmd_query(self.con, _Args(
                match=match, json=False, verbose=False))
        return out.getvalue()

    def test_unset_url_leaves_hot_path_untouched(self):
        rules_db.os.environ.pop("STANDING_RULES_SYNC_URL", None)
        self._mock_posts()
        for _ in range(30):
            self._query("telemetry")
        n = self.con.execute("SELECT COUNT(*) FROM query_telemetry").fetchone()[0]
        self.assertEqual(n, 0, "no rows without a sync URL")
        self.assertEqual(self.posts, [], "no network without a sync URL")
        self.assertEqual(len(self._thread_counts), 0, "no threads without a sync URL")

    def test_each_query_records_one_local_row(self):
        rules_db.os.environ["STANDING_RULES_SYNC_URL"] = "http://dashboard.invalid"
        self._mock_posts()
        for i in range(5):
            self._query("telemetry")
        rows = self.con.execute(
            "SELECT match_str, matched_ids, match_count FROM query_telemetry "
            "ORDER BY q_id").fetchall()
        self.assertEqual(len(rows), 5)
        self.assertTrue(all(r["match_str"] == "telemetry" for r in rows))
        self.assertTrue(all("SR-940" in r["matched_ids"] for r in rows))
        self.assertEqual(self.posts, [], "below batch size: no webhook yet")

    def test_batch_flushes_one_webhook_at_threshold(self):
        rules_db.os.environ["STANDING_RULES_SYNC_URL"] = "http://dashboard.invalid"
        self._mock_posts()
        for _ in range(rules_db.TELEMETRY_BATCH_SIZE + 3):
            self._query("telemetry")
        self.assertEqual(len(self.posts), 1, "exactly ONE webhook for the batch")
        batch = self.posts[0]["data"]["queries"]
        self.assertEqual(len(batch), rules_db.TELEMETRY_BATCH_SIZE)
        self.assertEqual(self.posts[0]["event"], "query_telemetry_batch")
        remaining = self.con.execute(
            "SELECT COUNT(*) FROM query_telemetry").fetchone()[0]
        self.assertEqual(remaining, 3, "flushed rows leave only the remainder")

    def test_query_rules_stays_pure(self):
        """query_rules (the benchmarked hot path, used by the agent hook)
        records nothing by itself."""
        rules_db.os.environ["STANDING_RULES_SYNC_URL"] = "http://dashboard.invalid"
        self._mock_posts()
        rules_db.query_rules(self.con, "telemetry")
        n = self.con.execute("SELECT COUNT(*) FROM query_telemetry").fetchone()[0]
        self.assertEqual(n, 0)


if __name__ == "__main__":
    unittest.main()
