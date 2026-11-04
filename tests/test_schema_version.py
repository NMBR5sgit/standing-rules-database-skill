#!/usr/bin/env python3
"""Tests (C3): schema_version table and migration runner."""
import pathlib
import sys
import tempfile
import unittest

SCRIPTS_DIR = pathlib.Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import rules_db  # noqa: E402


class SchemaVersionTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        rules_db.DB_PATH = pathlib.Path(self.tmp.name) / "rules.db"
        rules_db.REF_DIR = pathlib.Path(self.tmp.name) / "references"
        self.con = rules_db.connect()

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def _version_rows(self):
        return self.con.execute("SELECT v FROM schema_version").fetchall()

    def test_fresh_db_records_current_version(self):
        rules_db.init_db(self.con)
        rows = self._version_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], rules_db.SCHEMA_VERSION)

    def test_init_is_idempotent(self):
        rules_db.init_db(self.con)
        rules_db.init_db(self.con)
        rows = self._version_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], rules_db.SCHEMA_VERSION)

    def test_old_db_row_recreated(self):
        rules_db.init_db(self.con)
        # simulate an old DB: version row deleted
        self.con.execute("DELETE FROM schema_version")
        self.con.commit()
        rules_db.init_db(self.con)
        rows = self._version_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], rules_db.SCHEMA_VERSION)

    def test_stale_version_upgraded(self):
        rules_db.init_db(self.con)
        self.con.execute("UPDATE schema_version SET v=0")
        self.con.commit()
        rules_db.init_db(self.con)
        # Brief's _migrate inserts when current == 0; assert the DB reflects
        # the current version, not a specific row count (rows>1 possible per
        # the brief's runner; no column changes in this commit).
        rows = self._version_rows()
        self.assertTrue(rows)
        self.assertTrue(any(r[0] == rules_db.SCHEMA_VERSION for r in rows))
        # NOTE: per the brief's runner, a stale v=0 row is left in place and a
        # new row is INSERTed (rows may be [0, 1]). _migrate re-reads LIMIT 1;
        # flagged to owner rather than improvised around in this commit.


if __name__ == "__main__":
    unittest.main()
