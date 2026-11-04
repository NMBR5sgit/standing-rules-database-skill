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
        # schema_version is append-tolerant by design; rows may be [0, 2]
        # after a stale-row scenario. The invariant is MAX(v) == SCHEMA_VERSION.
        row = self.con.execute("SELECT MAX(v) FROM schema_version").fetchone()
        self.assertEqual(row[0], rules_db.SCHEMA_VERSION)

    def test_stale_zero_row_does_not_regress_version(self):
        """A stale v=0 row (manual UPDATE, half-applied migration) must not
        cause the effective version to regress on the next init_db."""
        rules_db.init_db(self.con)
        self.con.execute("INSERT INTO schema_version(v) VALUES (0)")
        self.con.commit()
        rules_db.init_db(self.con)
        row = self.con.execute("SELECT MAX(v) FROM schema_version").fetchone()
        self.assertEqual(row[0], rules_db.SCHEMA_VERSION)
        # And the effective version read by _migrate is now current:
        # (MAX(v) above proves the invariant; _migrate reads MAX(v) itself.)


if __name__ == "__main__":
    unittest.main()
