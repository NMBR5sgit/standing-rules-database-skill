#!/usr/bin/env python3
"""Regression test (C4): a query during a held write transaction must wait,
not fail.

Under SQLite defaults (busy_timeout=0) a concurrent query raises
`sqlite3.OperationalError: database is locked` and the agent receives zero
rules — a silent recall failure. With busy_timeout=5000 the query blocks up
to 5s and succeeds.
"""
import pathlib
import sys
import tempfile
import threading
import time
import types
import unittest

SCRIPTS_DIR = pathlib.Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import rules_db  # noqa: E402


class LockResilienceTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        rules_db.DB_PATH = pathlib.Path(self.tmp.name) / "rules.db"
        rules_db.REF_DIR = pathlib.Path(self.tmp.name) / "references"

    def tearDown(self):
        self.tmp.cleanup()

    def test_concurrent_query_waits_for_held_write_lock(self):
        con_a = rules_db.connect()
        rules_db.init_db(con_a)

        # A holds a write transaction.
        con_a.execute("BEGIN IMMEDIATE")
        con_a.execute(
            "INSERT INTO rules(rule_id,name,text,tier,domain,priority,status,created_at,updated_at)"
            " VALUES('SR-X1','x','x',2,'core','MED','active','now','now')")

        result = {}

        def query_b():
            # Open B's own connection inside B's thread (sqlite3 connections
            # are thread-bound; contention is cross-connection, cross-thread).
            # B attempts a WRITE while A holds BEGIN IMMEDIATE — the real
            # zero-rule failure mode: the second writer gets
            # 'database is locked' instantly at busy_timeout=0 and the
            # query path dies before returning anything.
            con_b = rules_db.connect()
            time.sleep(0.3)  # ensure A holds the lock first
            try:
                con_b.execute("BEGIN IMMEDIATE")
                con_b.execute(
                    "INSERT INTO rules(rule_id,name,text,tier,domain,priority,status,created_at,updated_at)"
                    " VALUES('SR-X2','y','y',2,'core','MED','active','now','now')")
                con_b.commit()
                args = types.SimpleNamespace(match="anything", json=False)
                rules_db.cmd_query(con_b, args)
                result["ok"] = True
            except Exception as e:  # noqa: BLE001
                result["error"] = e
            finally:
                con_b.close()

        t = threading.Thread(target=query_b)
        start = time.monotonic()
        t.start()
        time.sleep(1.0)          # A still holds the lock here; B must be waiting (not failed)
        con_a.rollback()         # release A while B is blocked
        t.join(timeout=10)
        con_a.close()
        elapsed = time.monotonic() - start

        self.assertNotIn("error", result,
                         f"query failed under contention: {result.get('error')!r}")
        self.assertTrue(result.get("ok"), "query thread did not complete")
        # busy_timeout is 5s; the whole thing must resolve well inside the
        # 10s join budget. Do not raise the DB timeout to make this pass.
        self.assertLess(elapsed, 10.0)


if __name__ == "__main__":
    unittest.main()
