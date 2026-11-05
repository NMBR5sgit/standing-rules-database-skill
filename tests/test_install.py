#!/usr/bin/env python3
"""Tests: IT provisioner one-liner (scripts/install.py).

Covers: env persistence (setx on Windows, rc-file export on Unix/macOS),
DB init, first sync via mocked urlopen, agent auto-detect, duplicate-export
guard, clean failure when the sync token is missing, and the exact
completion line. No real network, no real setx, never touches the real
~/.bashrc or the Windows registry.

Platform branches are exercised in the platform-native way: the rc-export
helper is platform-neutral (takes the home dir as a parameter), the setx
branch runs natively on Windows with os.system mocked, and the full flow
runs on whatever platform the suite is executing on.
"""
import io
import json
import os
import pathlib
import sys
import tempfile
import unittest
import urllib.request
from contextlib import redirect_stdout

SCRIPTS_DIR = pathlib.Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import rules_db  # noqa: E402
import install  # noqa: E402


class _FakeResp:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class InstallProvisionerTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._saved_db_path = rules_db.DB_PATH
        self._saved_ref_dir = rules_db.REF_DIR
        rules_db.DB_PATH = pathlib.Path(self.tmp.name) / "rules.db"
        rules_db.REF_DIR = pathlib.Path(self.tmp.name) / "references"
        self._saved_env = {k: os.environ.get(k) for k in
                           ("STANDING_RULES_SYNC_URL", "STANDING_RULES_SYNC_TOKEN")}
        os.environ.pop("STANDING_RULES_SYNC_TOKEN", None)
        self._orig_urlopen = urllib.request.urlopen
        self._orig_system = os.system
        self._orig_append = install.append_rc_exports

    def tearDown(self):
        urllib.request.urlopen = self._orig_urlopen
        os.system = self._orig_system
        install.append_rc_exports = self._orig_append
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        rules_db.DB_PATH = self._saved_db_path
        rules_db.REF_DIR = self._saved_ref_dir
        self.tmp.cleanup()

    def _mock_http(self, body: dict):
        def fake_urlopen(req, timeout=None):
            assert req.headers.get("Authorization") == "Bearer tok-123"
            return _FakeResp(json.dumps(body).encode("utf-8"))
        urllib.request.urlopen = fake_urlopen

    def _sandbox_persist(self):
        """Route the persistent-env step into the temp dir: rc helper gets a
        sandboxed home on POSIX; setx is neutralized on Windows."""
        fake_home = pathlib.Path(self.tmp.name) / "home"
        fake_home.mkdir(exist_ok=True)
        install.append_rc_exports = lambda home, pairs: self._orig_append(fake_home, pairs)
        os.system = lambda cmd: 0
        return fake_home

    def test_rc_export_helper_appends_and_dedupes(self):
        home = pathlib.Path(self.tmp.name) / "home_rc"
        home.mkdir()
        rc = home / ".bashrc"
        rc.write_text("# existing\n", encoding="utf-8")
        pairs = [("STANDING_RULES_SYNC_URL", "https://rules.example.com"),
                 ("STANDING_RULES_SYNC_TOKEN", "tok-123")]
        install.append_rc_exports(home, pairs)
        text = rc.read_text(encoding="utf-8")
        self.assertIn('export STANDING_RULES_SYNC_URL="https://rules.example.com"', text)
        self.assertIn('export STANDING_RULES_SYNC_TOKEN="tok-123"', text)
        # re-provisioning must not stack duplicate exports
        install.append_rc_exports(home, pairs)
        self.assertEqual(rc.read_text(encoding="utf-8").count(
            "export STANDING_RULES_SYNC_URL"), 1)

    def test_rc_export_prefers_zshrc_when_no_bashrc(self):
        home = pathlib.Path(self.tmp.name) / "home_zsh"
        home.mkdir()
        (home / ".zshrc").write_text("", encoding="utf-8")
        rc = install.append_rc_exports(home, [("STANDING_RULES_SYNC_URL", "u")])
        self.assertEqual(rc.name, ".zshrc")
        self.assertIn('export STANDING_RULES_SYNC_URL="u"',
                      (home / ".zshrc").read_text(encoding="utf-8"))

    def test_windows_persist_uses_setx(self):
        if os.name != "nt":
            self.skipTest("setx branch is native-Windows")
        calls = []
        os.system = lambda cmd: calls.append(cmd) or 0
        where = install.persist_env("https://rules.example.com", "tok-123")
        self.assertIn('setx STANDING_RULES_SYNC_URL "https://rules.example.com"', calls)
        self.assertIn('setx STANDING_RULES_SYNC_TOKEN "tok-123"', calls)
        self.assertIn("setx", where)

    def test_full_provision_flow(self):
        self._sandbox_persist()
        self._mock_http({"rules": [{"id": "SR-900", "name": "Sign commits",
                                    "text": "Sign all commits.", "tier": 2,
                                    "domain": "git", "owner": "org"}]})
        buf = io.StringIO()
        with redirect_stdout(buf):
            install.main(["--sync-url", "http://dashboard.invalid",
                          "--agent", "hermes", "--token", "tok-123"])
        out = buf.getvalue()
        self.assertIn("Provisioning complete. Rules synced from http://dashboard.invalid.", out)
        self.assertIn("Agent will pick up the environment variable on next restart.", out)
        self.assertIn("local rules.db initialized", out)
        con = rules_db.connect()
        try:
            row = con.execute("SELECT * FROM rules WHERE rule_id='SR-900'").fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["owner"], "org")
        finally:
            con.close()

    def test_missing_token_is_clean_failure(self):
        self._sandbox_persist()
        buf = io.StringIO()
        with redirect_stdout(buf):
            with self.assertRaises(SystemExit) as cm:
                install.main(["--sync-url", "http://dashboard.invalid",
                              "--agent", "cursor"])
        self.assertIn("SYNC_TOKEN", str(cm.exception))

    def test_agent_autodetect_and_flag(self):
        real_which = install.shutil.which
        install.shutil.which = lambda name: "/usr/bin/claude" if name == "claude" else None
        try:
            self.assertEqual(install.detect_agent(), "claude")
            install.shutil.which = lambda name: None
            self.assertIsNone(install.detect_agent())
        finally:
            install.shutil.which = real_which


if __name__ == "__main__":
    unittest.main()
