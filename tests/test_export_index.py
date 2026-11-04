#!/usr/bin/env python3
"""Tests (A2): generated compact dispatch index (references/_index.txt)."""
import pathlib
import sys
import tempfile
import unittest

SCRIPTS_DIR = pathlib.Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import rules_db  # noqa: E402


class ExportIndexTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        rules_db.DB_PATH = pathlib.Path(self.tmp.name) / "rules.db"
        rules_db.REF_DIR = pathlib.Path(self.tmp.name) / "references"
        self.con = rules_db.connect()
        rules_db.init_db(self.con)
        for i, (d, tags) in enumerate([
            ("outbound", "email listing byline"),
            ("web-delivery", "site dns cdn"),
            ("messaging", "chat dm bot cron"),
        ]):
            rules_db.upsert_rule(self.con, {
                "id": f"SR-I{i}", "name": f"rule {i}", "text": f"text {i}",
                "tier": 2, "domain": d, "priority": "MED", "tags": tags})
        self.con.commit()

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def _index(self):
        import io
        from contextlib import redirect_stdout
        with redirect_stdout(io.StringIO()):
            rules_db.cmd_export_index(self.con)
        return (rules_db.REF_DIR / "_index.txt").read_text(encoding="utf-8")

    def test_index_shape(self):
        content = self._index()
        lines = [l for l in content.splitlines() if l]
        self.assertEqual(len(lines), 3)
        self.assertTrue(all(l.startswith(d + ": ") for l, d in
                            zip(lines, ["messaging", "outbound", "web-delivery"])))
        for line in lines:
            dom = line.split(":", 1)[0]
            words = line.split(":", 1)[1]
            for w in dom.replace("-", " ").split():
                self.assertIn(w, words, f"domain word {w!r} missing from index line")
        web_line = next(l for l in lines if l.startswith("web-delivery:"))
        for tag in ("site", "dns", "cdn"):
            self.assertIn(tag, web_line)

    def test_index_idempotent(self):
        first = self._index()
        second = self._index()
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
