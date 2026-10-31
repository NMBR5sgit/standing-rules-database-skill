#!/usr/bin/env python3
"""Regression test: seeding the same file twice must not duplicate facts.

Verifies that cmd_seed skips facts that already exist (matched by domain + text)
instead of blindly inserting them on every run.
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


class SeedFactsDedupTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        rules_db.DB_PATH = pathlib.Path(self.tmp.name) / "rules.db"
        rules_db.REF_DIR = pathlib.Path(self.tmp.name) / "references"
        self.con = rules_db.connect()
        rules_db.init_db(self.con)

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def _write_seed(self, facts, rules=None):
        rules = rules or []
        seed_path = pathlib.Path(self.tmp.name) / "seed.json"
        seed_path.write_text(
            json.dumps({"rules": rules, "facts": facts}), encoding="utf-8")
        return str(seed_path)

    # ---- requirement: same file seeded twice, no duplicate facts ----

    def test_seed_twice_no_duplicate_facts(self):
        seed_path = self._write_seed(
            facts=[
                {"domain": "test", "text": "Fact one."},
                {"domain": "test", "text": "Fact two."},
            ],
            rules=[
                {"id": "SR-101", "name": "test-rule", "text": "A test rule.",
                 "domain": "test", "priority": "MED"},
            ],
        )

        rules_db.cmd_seed(self.con, seed_path)
        rules_db.cmd_seed(self.con, seed_path)

        count = self.con.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
        self.assertEqual(count, 2, f"expected 2 facts after double-seed, got {count}")

        texts = sorted(r[0] for r in
                       self.con.execute("SELECT text FROM facts").fetchall())
        self.assertEqual(texts, ["Fact one.", "Fact two."])

    def test_seed_partial_overlap_skips_existing_only(self):
        """Only facts already present are skipped; new ones still insert."""
        first = self._write_seed(
            facts=[{"domain": "web", "text": "Existing fact."}])
        rules_db.cmd_seed(self.con, first)

        second = self._write_seed(
            facts=[
                {"domain": "web", "text": "Existing fact."},   # duplicate
                {"domain": "web", "text": "New fact."},          # new
            ])
        rules_db.cmd_seed(self.con, second)

        count = self.con.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
        self.assertEqual(count, 2, f"expected 2 facts after partial overlap, got {count}")

    def test_seed_same_text_different_domain_is_distinct(self):
        """Two facts with identical text but different domains are not duplicates."""
        first = self._write_seed(
            facts=[{"domain": "web", "text": "Shared wording."}])
        rules_db.cmd_seed(self.con, first)

        second = self._write_seed(
            facts=[{"domain": "messaging", "text": "Shared wording."}])
        rules_db.cmd_seed(self.con, second)

        count = self.con.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
        self.assertEqual(count, 2, f"expected 2 facts, got {count}")

        pairs = sorted(
            (r[0], r[1]) for r in
            self.con.execute("SELECT domain, text FROM facts").fetchall())
        self.assertEqual(
            pairs, [("messaging", "Shared wording."), ("web", "Shared wording.")])

    def test_seed_summary_reports_inserted_and_skipped(self):
        seed_path = self._write_seed(
            facts=[{"domain": "test", "text": "Only fact."}])

        buf1 = io.StringIO()
        with redirect_stdout(buf1):
            rules_db.cmd_seed(self.con, seed_path)
        out1 = buf1.getvalue()
        self.assertIn("inserted 1 facts", out1)
        self.assertIn("skipped 0 duplicate facts", out1)

        buf2 = io.StringIO()
        with redirect_stdout(buf2):
            rules_db.cmd_seed(self.con, seed_path)
        out2 = buf2.getvalue()
        self.assertIn("inserted 0 facts", out2)
        self.assertIn("skipped 1 duplicate facts", out2)

    def test_seed_empty_facts_list(self):
        seed_path = self._write_seed(facts=[])
        buf = io.StringIO()
        with redirect_stdout(buf):
            rules_db.cmd_seed(self.con, seed_path)
        self.assertIn("inserted 0 facts", buf.getvalue())
        self.assertIn("skipped 0 duplicate facts", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
