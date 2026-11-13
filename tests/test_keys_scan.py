#!/usr/bin/env python3
"""Tests: vault token fallback and the `keys` scan command.

The engine resolves the sync token as: STANDING_RULES_SYNC_TOKEN env first,
then the central secrets vault (overridable via STANDING_RULES_VAULT_PATH).
`rules_db.py keys` scans env + vault for candidate keys, probes each against
the dashboard, and reports masked tokens with tenant attribution — never a
full key, never a write.
"""
import io
import json
import os
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


class _FakeResp:
    def __init__(self, body):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class VaultFallbackTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._env = dict(os.environ)
        os.environ["STANDING_RULES_SYNC_URL"] = "http://dash.invalid"
        os.environ.pop("STANDING_RULES_SYNC_TOKEN", None)
        self._real_urlopen = rules_db.urllib.request.urlopen

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        rules_db.urllib.request.urlopen = self._real_urlopen
        self.tmp.cleanup()

    def _fake_vault(self, token):
        """A stand-in vault.py that answers `get <name>` with `token`."""
        p = pathlib.Path(self.tmp.name) / "vault.py"
        p.write_text(f"print({token!r})\n", encoding="utf-8")
        os.environ["STANDING_RULES_VAULT_PATH"] = str(p)
        return p

    def test_env_token_wins_over_vault(self):
        self._fake_vault("sr_live_vaulttoken")
        os.environ["STANDING_RULES_SYNC_TOKEN"] = "sr_live_envtoken"
        self.assertEqual(rules_db._sync_token(), "sr_live_envtoken")

    def test_vault_fallback_when_env_missing(self):
        self._fake_vault("sr_live_vaulttoken")
        self.assertEqual(rules_db._sync_token(), "sr_live_vaulttoken")

    def test_missing_vault_degrades_to_none(self):
        os.environ["STANDING_RULES_VAULT_PATH"] = \
            str(pathlib.Path(self.tmp.name) / "nope.py")
        self.assertIsNone(rules_db._sync_token())

    def test_sync_succeeds_with_vault_token(self):
        self._fake_vault("sr_live_vaulttoken")
        body = {"rules": [], "valid_until": "2030-01-01T00:00:00Z",
                "tenant_name": "lee-weaver-personal"}
        seen = {}

        def fake_urlopen(req, timeout=None):
            hdrs = {k.lower(): v for k, v in (req.headers or {}).items()}
            seen["auth"] = hdrs.get("authorization")
            seen["machine"] = hdrs.get("x-client-machine")
            return _FakeResp(json.dumps(body).encode())

        rules_db.urllib.request.urlopen = fake_urlopen
        self._db_path = rules_db.DB_PATH
        rules_db.DB_PATH = pathlib.Path(self.tmp.name) / "rules.db"
        con = rules_db.connect()
        rules_db.init_db(con)
        try:
            with redirect_stdout(io.StringIO()):
                rules_db.cmd_sync(con, None)
            self.assertEqual(seen.get("auth"), "Bearer sr_live_vaulttoken")
            # seat identity must be the hashed machine id, not raw COMPUTERNAME
            self.assertIn("-", seen.get("machine") or "")
            self.assertEqual(seen.get("machine"), rules_db._machine_id())
        finally:
            con.close()
            rules_db.DB_PATH = self._db_path


class KeysScanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._env = dict(os.environ)
        os.environ["STANDING_RULES_SYNC_URL"] = "http://dash.invalid"
        os.environ["STANDING_RULES_SYNC_TOKEN"] = "sr_live_envtoken1234"
        vault = pathlib.Path(self.tmp.name) / "vault.py"
        vault.write_text("import sys\n"
                         "print('sr_live_vaultkey9999' if sys.argv[1:2] == "
                         "['get'] else 'standing_rules_sync_token')\n",
                         encoding="utf-8")
        os.environ["STANDING_RULES_VAULT_PATH"] = str(vault)
        self._real_urlopen = rules_db.urllib.request.urlopen
        self._db_path = rules_db.DB_PATH
        rules_db.DB_PATH = pathlib.Path(self.tmp.name) / "rules.db"
        self.con = rules_db.connect()
        rules_db.init_db(self.con)

    def tearDown(self):
        self.con.close()
        rules_db.DB_PATH = self._db_path
        os.environ.clear()
        os.environ.update(self._env)
        rules_db.urllib.request.urlopen = self._real_urlopen
        self.tmp.cleanup()

    def test_keys_reports_masked_candidates_with_tenant(self):
        body = {"rules": [{"rule_id": "SR-1", "tier": 1}],
                "tenant_id": "t-1", "tenant_name": "lee-weaver-personal",
                "valid_until": "2030-01-01T00:00:00Z"}
        rules_db.urllib.request.urlopen = lambda req, timeout=None: \
            _FakeResp(json.dumps(body).encode())
        buf = io.StringIO()
        with redirect_stdout(buf):
            rules_db.cmd_keys(self.con, None)
        out = buf.getvalue()
        self.assertIn("lee-weaver-personal", out)
        self.assertIn("env:STANDING_RULES_SYNC_TOKEN", out)
        self.assertIn("vault:standing_rules_sync_token", out)
        self.assertIn("OK", out)
        # masked only — the full 22-char token must never leak
        self.assertNotIn("sr_live_envtoken1234", out)
        self.assertNotIn("sr_live_vaultkey9999", out)
        self.assertIn("sr_live…1234", out)

    def test_keys_rejects_bad_token_without_crash(self):
        def deny(req, timeout=None):
            raise urllib.error.HTTPError(
                req.full_url, 401, "Unauthorized", {}, None)
        rules_db.urllib.request.urlopen = deny
        buf = io.StringIO()
        with redirect_stdout(buf):
            rules_db.cmd_keys(self.con, None)   # must NOT sys.exit
        out = buf.getvalue()
        self.assertIn("REJECT", out)
        self.assertIn("401", out)

    def test_keys_without_url_is_clean_error(self):
        os.environ.pop("STANDING_RULES_SYNC_URL", None)
        with self.assertRaises(SystemExit) as cm:
            rules_db.cmd_keys(self.con, None)
        self.assertIn("STANDING_RULES_SYNC_URL", str(cm.exception.code))


if __name__ == "__main__":
    unittest.main()
