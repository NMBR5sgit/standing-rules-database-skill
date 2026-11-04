#!/usr/bin/env python3
"""Tests (B2′): keyed facts — nullable fact_key/fact_value, key=value
rendering, --verbose fallback, v2 migration from an old facts schema."""
import io
import json
import pathlib
import sqlite3
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout

SCRIPTS_DIR = pathlib.Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import rules_db  # noqa: E402


def _capture(fn, *args):
    buf = io.StringIO()
    with redirect_stdout(buf):
        fn(*args)
    return buf.getvalue()


class KeyedFactsTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        rules_db.DB_PATH = pathlib.Path(self.tmp.name) / "rules.db"
        rules_db.REF_DIR = pathlib.Path(self.tmp.name) / "references"
        self.con = rules_db.connect()
        rules_db.init_db(self.con)

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def _seed(self, facts):
        path = pathlib.Path(self.tmp.name) / "seed.json"
        path.write_text(json.dumps({"rules": [], "facts": facts}), encoding="utf-8")
        _capture(rules_db.cmd_seed, self.con, path)

    def _facts(self, verbose=False):
        args = types.SimpleNamespace(domain=None, verbose=verbose)
        return _capture(rules_db.cmd_facts, self.con, args)

    def test_keyed_fact_renders_key_value(self):
        self._seed([{"domain": "web", "text": "Primary domain is example.com",
                     "key": "primary_domain", "value": "example.com"}])
        out = self._facts()
        self.assertIn("[web] primary_domain=example.com", out)

    def test_verbose_falls_back_to_text(self):
        self._seed([{"domain": "web", "text": "Primary domain is example.com",
                     "key": "primary_domain", "value": "example.com"}])
        out = self._facts(verbose=True)
        self.assertIn("[web] Primary domain is example.com", out)

    def test_unkeyed_fact_renders_text(self):
        self._seed([{"domain": "misc", "text": "Something true about the world"}])
        out = self._facts()
        self.assertIn("[misc] Something true about the world", out)

    def test_migration_from_old_schema_no_data_loss(self):
        # Simulate a pre-B2′ DB: no fact_key/fact_value columns, one fact row.
        self.con.execute("DROP TABLE facts")
        self.con.execute(
            "CREATE TABLE facts(fact_id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "domain TEXT NOT NULL,text TEXT NOT NULL,created_at TEXT,updated_at TEXT)")
        self.con.execute("INSERT INTO facts(domain,text,created_at,updated_at) "
                         "VALUES('legacy','an old fact','now','now')")
        self.con.execute("UPDATE schema_version SET v=1")
        self.con.commit()

        rules_db.init_db(self.con)  # must run the v1 -> v2 migration

        cols = {r["name"] for r in self.con.execute("PRAGMA table_info(facts)")}
        self.assertIn("fact_key", cols)
        self.assertIn("fact_value", cols)
        row = self.con.execute(
            "SELECT domain,text,fact_key,fact_value FROM facts").fetchone()
        self.assertEqual(row["domain"], "legacy")
        self.assertEqual(row["text"], "an old fact")
        self.assertIsNone(row["fact_key"])
        self.assertIsNone(row["fact_value"])
        out = self._facts()
        self.assertIn("[legacy] an old fact", out)

    def test_dedup_still_on_domain_text_with_keys(self):
        fact = {"domain": "web", "text": "Primary domain is example.com",
                "key": "primary_domain", "value": "example.com"}
        self._seed([fact])
        self._seed([fact])  # same (domain, text) again, keys present
        n = self.con.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
        self.assertEqual(n, 1)

    def test_dedup_keyed_vs_unkeyed_same_text(self):
        self._seed([{"domain": "web", "text": "Same sentence"}])
        self._seed([{"domain": "web", "text": "Same sentence",
                     "key": "k", "value": "v"}])
        n = self.con.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
        self.assertEqual(n, 1)  # identity remains (domain, text)

    def test_partial_key_only_renders_text(self):
        # key without value must fall back to text rendering
        self.con.execute("INSERT INTO facts(domain,text,fact_key,fact_value,created_at,updated_at) "
                         "VALUES('p','full sentence','lonelykey',NULL,'now','now')")
        self.con.commit()
        out = self._facts()
        self.assertIn("[p] full sentence", out)


if __name__ == "__main__":
    unittest.main()
