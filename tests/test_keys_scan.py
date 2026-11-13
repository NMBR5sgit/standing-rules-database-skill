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


FAKE_VAULT_SRC = '''import json, sys, pathlib
P = pathlib.Path(__file__).with_name("vault_store.json")
def main():
    a = sys.argv[1:]
    d = json.loads(P.read_text()) if P.exists() else {}
    if a[0] == "get":
        v = d.get(a[1])
        if not v:
            sys.exit(1)
        print(v)
    elif a[0] == "list":
        print("\\n".join(d.keys()))
    elif a[0] == "del":
        d.pop(a[1], None)
        P.write_text(json.dumps(d))
main()
'''


class _Args:
    """Stand-in for argparse.Namespace with the keys-verb defaults."""
    def __init__(self, **kw):
        self.file = []
        self.delete = None
        self.force = False
        self.no_vault = False
        for k, v in kw.items():
            setattr(self, k, v)


class KeysDeleteTest(unittest.TestCase):
    """Deleting a verified key removes only that key: one variable, one vault
    entry, or one line in a shared file — never the whole environment, and
    never a file that carries other information."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._env = dict(os.environ)
        os.environ["STANDING_RULES_SYNC_URL"] = "http://dash.invalid"
        os.environ.pop("STANDING_RULES_SYNC_TOKEN", None)
        vault = pathlib.Path(self.tmp.name) / "vault.py"
        vault.write_text(FAKE_VAULT_SRC, encoding="utf-8")
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

    def _vault_store(self, mapping):
        pathlib.Path(self.tmp.name, "vault_store.json").write_text(
            json.dumps(mapping), encoding="utf-8")

    def _accept(self, tokens):
        """Only the listed tokens authenticate; everything else gets 401."""
        def handler(req, timeout=None):
            hdrs = {k.lower(): v for k, v in (req.headers or {}).items()}
            tok = (hdrs.get("authorization") or "").replace("Bearer ", "")
            if tok in tokens:
                return _FakeResp(json.dumps(
                    {"rules": [], "tenant_name": "lee-weaver-personal",
                     "valid_until": "2030-01-01T00:00:00Z"}).encode())
            raise urllib.error.HTTPError(
                req.full_url, 401, "Unauthorized", {}, None)
        rules_db.urllib.request.urlopen = handler

    def test_file_scan_finds_a_key_without_printing_it(self):
        tok = "sr_live_" + "w" * 32
        cfg = pathlib.Path(self.tmp.name) / "config.txt"
        cfg.write_text("TOKEN=" + tok + "\nOTHER=1\n", encoding="utf-8")
        self._accept({tok})
        buf = io.StringIO()
        with redirect_stdout(buf):
            rules_db.cmd_keys(self.con, _Args(file=[str(cfg)]))
        out = buf.getvalue()
        self.assertIn("file:", out)
        self.assertIn("tenant=lee-weaver-personal", out)
        self.assertNotIn(tok, out)          # masked in the report
        self.assertIn("keys --delete", out) # tells the user the next step

    def test_delete_removes_only_the_key_line_in_a_shared_file(self):
        tok = "sr_live_" + "f" * 32
        cfg = pathlib.Path(self.tmp.name) / "config.txt"
        cfg.write_text("# machine settings\nAPI_URL=https://example.test\n"
                       "TOKEN=" + tok + "\nREGION=us\n", encoding="utf-8")
        self._accept({tok})
        self.assertIn(tok, cfg.read_text())                    # key present first
        with redirect_stdout(io.StringIO()):
            rules_db.cmd_keys(self.con, _Args(
                file=[str(cfg)], delete=f"file:{cfg}"))
        text = cfg.read_text()
        self.assertTrue(cfg.exists())
        self.assertIn("API_URL=https://example.test", text)   # neighbours kept
        self.assertIn("REGION=us", text)
        self.assertNotIn(tok, text)                           # only the key gone

    def test_delete_removes_the_file_only_when_the_key_is_all_it_has(self):
        tok = "sr_live_" + "o" * 32
        solo = pathlib.Path(self.tmp.name) / "key.txt"
        solo.write_text(tok + "\n", encoding="utf-8")
        self._accept({tok})
        self.assertTrue(solo.exists())
        with redirect_stdout(io.StringIO()):
            rules_db.cmd_keys(self.con, _Args(
                file=[str(solo)], delete=f"file:{solo}"))
        self.assertFalse(solo.exists())

    def test_delete_refuses_an_unverified_key_unless_forced(self):
        tok = "sr_live_" + "x" * 32
        cfg = pathlib.Path(self.tmp.name) / "config.txt"
        cfg.write_text("KEEP=1\nTOKEN=" + tok + "\n", encoding="utf-8")
        self._accept(set())  # nothing authenticates
        with self.assertRaises(SystemExit) as cm:
            with redirect_stdout(io.StringIO()):
                rules_db.cmd_keys(self.con, _Args(
                    file=[str(cfg)], delete=f"file:{cfg}"))
        self.assertIn("unverified", str(cm.exception))
        self.assertIn(tok, cfg.read_text())                   # nothing deleted
        with redirect_stdout(io.StringIO()):
            rules_db.cmd_keys(self.con, _Args(
                file=[str(cfg)], delete=f"file:{cfg}", force=True))
        self.assertNotIn(tok, cfg.read_text())
        self.assertIn("KEEP=1", cfg.read_text())

    def test_env_delete_removes_one_variable_and_leaves_the_rest(self):
        tok = "sr_live_" + "z" * 32
        os.environ["STANDING_RULES_SYNC_TOKEN"] = tok
        os.environ["SR_UNRELATED_KEEPME"] = "keepme"
        self._accept({tok})
        with redirect_stdout(io.StringIO()):
            rules_db.cmd_keys(self.con, _Args(
                no_vault=True, delete="env:STANDING_RULES_SYNC_TOKEN"))
        self.assertNotIn("STANDING_RULES_SYNC_TOKEN", os.environ)
        self.assertEqual(os.environ.get("SR_UNRELATED_KEEPME"), "keepme")

    def test_vault_delete_removes_one_entry_and_leaves_the_rest(self):
        tok = "sr_live_" + "y" * 32
        self._vault_store({"standing_rules_sync_token": tok,
                           "some_other_secret": "keepme-value"})
        self._accept({tok})
        with redirect_stdout(io.StringIO()):
            rules_db.cmd_keys(self.con, _Args(
                delete="vault:standing_rules_sync_token"))
        store = json.loads(pathlib.Path(self.tmp.name, "vault_store.json")
                           .read_text(encoding="utf-8"))
        self.assertNotIn("standing_rules_sync_token", store)
        self.assertEqual(store.get("some_other_secret"), "keepme-value")

    def test_a_revoked_tenants_key_is_reported_revoked_not_ok(self):
        tok = "sr_live_" + "r" * 32
        cfg = pathlib.Path(self.tmp.name) / "config.txt"
        cfg.write_text("MISC=1\nKEY=" + tok + "\n", encoding="utf-8")

        def handler(req, timeout=None):
            return _FakeResp(json.dumps({"status": "revoked",
                                         "action": "wipe_org_rules",
                                         "tenant_name": "key-scan-live-test"}).encode())
        rules_db.urllib.request.urlopen = handler
        buf = io.StringIO()
        with redirect_stdout(buf):
            rules_db.cmd_keys(self.con, _Args(file=[str(cfg)]))
        out = buf.getvalue()
        self.assertIn("REVOKED", out)          # not mistaken for a live key
        self.assertNotIn("OK     ", out)
        # a revoked key is still deletable from where it was found
        with redirect_stdout(io.StringIO()):
            rules_db.cmd_keys(self.con, _Args(
                file=[str(cfg)], delete=f"file:{cfg}"))
        text = cfg.read_text()
        self.assertNotIn(tok, text)
        self.assertIn("MISC=1", text)

    def test_delete_names_an_unknown_source_cleanly(self):
        os.environ["STANDING_RULES_SYNC_TOKEN"] = "sr_live_" + "n" * 32
        self._accept(set())
        with self.assertRaises(SystemExit) as cm:
            with redirect_stdout(io.StringIO()):
                rules_db.cmd_keys(self.con, _Args(
                    no_vault=True, delete="file:C:/nope/here.txt", force=True))
        self.assertIn("no candidate with source", str(cm.exception))


class FakeDash:
    """Minimal stand-in dashboard for the keys endpoints: /sync/pull,
    /keys/report, /keys/pending, /keys/ack."""

    def __init__(self, tokens, pending=()):
        self.tokens = set(tokens)
        self.pending = list(pending)
        self.reports = []          # (token, payload) actually uploaded
        self.acks = []             # payloads the machine acked
        self.pending_for = []      # machine ids that were served a queue
        self.calls = []

    def handler(self, req, timeout=None):
        import urllib.parse
        path = urllib.parse.urlparse(req.full_url).path
        hdrs = {k.lower(): v for k, v in (req.headers or {}).items()}
        tok = (hdrs.get("authorization") or "").replace("Bearer ", "")
        self.calls.append((path, tok))
        body = json.loads(req.data.decode()) if req.data else {}

        def deny(code=401):
            raise urllib.error.HTTPError(req.full_url, code, "Denied", {}, None)

        if path == "/sync/pull":
            if tok not in self.tokens:
                deny()
            return _FakeResp(json.dumps({"rules": [],
                                         "tenant_name": "lee-weaver-personal",
                                         "valid_until": "2030-01-01T00:00:00Z"}).encode())
        if path == "/keys/report":
            if tok not in self.tokens:
                deny()
            self.reports.append((tok, body))
            return _FakeResp(json.dumps({"status": "ok", "keys": len(
                body.get("candidates") or []), "scope": body.get("scope")}).encode())
        if path == "/keys/pending":
            if tok not in self.tokens:
                deny()
            self.pending_for.append(hdrs.get("x-client-machine"))
            return _FakeResp(json.dumps({"pending": self.pending}).encode())
        if path == "/keys/ack":
            if tok not in self.tokens:
                deny()
            self.acks.append(body)
            return _FakeResp(json.dumps({"status": "ok", "results": len(
                body.get("results") or [])}).encode())
        deny(404)


class KeysReportApplyTest(unittest.TestCase):
    """`keys --report` uploads masked sightings; `keys --apply` carries out
    the deletes the dashboard queued, and only those it can match."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._env = dict(os.environ)
        os.environ["STANDING_RULES_SYNC_URL"] = "http://dash.invalid"
        os.environ["STANDING_RULES_SYNC_TOKEN"] = "sr_live_" + "a" * 32
        vault = pathlib.Path(self.tmp.name) / "vault.py"
        vault.write_text(FAKE_VAULT_SRC, encoding="utf-8")
        os.environ["STANDING_RULES_VAULT_PATH"] = str(vault)
        self.dash_token = os.environ["STANDING_RULES_SYNC_TOKEN"]
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

    def _install(self, dash):
        rules_db.urllib.request.urlopen = dash.handler

    def _config_with(self, token):
        cfg = pathlib.Path(self.tmp.name) / "config.txt"
        cfg.write_text("# machine settings\nAPI_URL=https://example.test\n"
                       "TOKEN=" + token + "\nREGION=us\n", encoding="utf-8")
        return cfg

    def test_report_uploads_masked_sightings_and_never_the_full_key(self):
        tok = "sr_live_" + "b" * 32
        cfg = self._config_with(tok)
        dash = FakeDash({self.dash_token, tok})
        self._install(dash)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rules_db.cmd_keys(self.con, _Args(file=[str(cfg)], report=True))
        self.assertIn("masked sighting", buf.getvalue())
        assert len(dash.reports) == 1
        _token_used, payload = dash.reports[0]
        cands = payload["candidates"]
        self.assertEqual(len(cands), 2)       # env + the file
        by_source = {c["source"]: c for c in cands}
        file_cand = by_source[f"file:{cfg}"]
        self.assertEqual(file_cand["verdict"], "ok")
        self.assertEqual(file_cand["rules"], 0)
        self.assertIn("…", file_cand["masked"])
        # the privacy promise: no full key anywhere in the upload
        self.assertNotIn(tok, json.dumps(payload))
        self.assertNotIn(self.dash_token, json.dumps(payload))

    def test_report_labels_a_limited_scan_as_partial_so_absence_is_not_gone(self):
        dash = FakeDash({self.dash_token})
        self._install(dash)
        with redirect_stdout(io.StringIO()):
            rules_db.cmd_keys(self.con, _Args(report=True))
        self.assertEqual(dash.reports[0][1]["scope"], "full")
        cfg = self._config_with("sr_live_" + "c" * 32)
        with redirect_stdout(io.StringIO()):
            rules_db.cmd_keys(self.con, _Args(file=[str(cfg)], report=True))
        self.assertEqual(dash.reports[1][1]["scope"], "partial")
        with redirect_stdout(io.StringIO()):
            rules_db.cmd_keys(self.con, _Args(no_vault=True, report=True))
        self.assertEqual(dash.reports[2][1]["scope"], "partial")

    def test_apply_deletes_exactly_the_queued_key_and_acks_it(self):
        tok = "sr_live_" + "d" * 32
        cfg = self._config_with(tok)
        dash = FakeDash({self.dash_token, tok},
                        pending=[{"id": 7, "source": f"file:{cfg}",
                                  "masked": rules_db._mask(tok)}])
        self._install(dash)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rules_db.cmd_keys(self.con, _Args(file=[str(cfg)], apply=True))
        text = cfg.read_text()
        self.assertNotIn(tok, text)
        self.assertIn("API_URL=https://example.test", text)   # neighbours survive
        self.assertIn("REGION=us", text)
        self.assertEqual(dash.acks[0]["results"], [{"id": 7, "status": "deleted"}])
        self.assertIn("1 of 1 queued", buf.getvalue())

    def test_apply_only_removes_keys_the_dashboard_actually_saw(self):
        mine = "sr_live_" + "e" * 32
        cfg = self._config_with(mine)
        dash = FakeDash({self.dash_token}, pending=[
            {"id": 8, "source": f"file:{cfg}",
             "masked": rules_db._mask("sr_live_" + "f" * 32)},   # different key
            {"id": 9, "source": "env:GONE_TOKEN",
             "masked": rules_db._mask(mine)},                    # wrong source
        ])
        self._install(dash)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rules_db.cmd_keys(self.con, _Args(file=[str(cfg)], apply=True))
        self.assertIn(mine, cfg.read_text())      # nothing was deleted
        statuses = {r["id"]: r["status"] for r in dash.acks[0]["results"]}
        self.assertEqual(statuses, {8: "not_found", 9: "not_found"})

    def test_apply_with_an_empty_queue_touches_nothing(self):
        tok = "sr_live_" + "g" * 32
        cfg = self._config_with(tok)
        dash = FakeDash({self.dash_token, tok}, pending=[])
        self._install(dash)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rules_db.cmd_keys(self.con, _Args(file=[str(cfg)], apply=True))
        self.assertIn("no deletes queued", buf.getvalue())
        self.assertIn(tok, cfg.read_text())
        self.assertEqual(dash.acks, [])

    def test_a_string_that_authenticates_nowhere_is_not_uploaded_as_a_sighting(self):
        tok = "sr_live_" + "h" * 32
        cfg = self._config_with(tok)
        dash = FakeDash({self.dash_token})      # the file key is unknown to it
        self._install(dash)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rules_db.cmd_keys(self.con, _Args(file=[str(cfg)], report=True))
        self.assertIn("REJECT", buf.getvalue())               # shown locally...
        sources = [c["source"] for c in dash.reports[0][1]["candidates"]]
        self.assertNotIn(f"file:{cfg}", sources)             # ...but not reported
        self.assertIn("env:STANDING_RULES_SYNC_TOKEN", sources)

    def test_json_scan_is_machine_readable_and_still_masked(self):
        tok = "sr_live_" + "j" * 32
        cfg = self._config_with(tok)
        dash = FakeDash({self.dash_token, tok})
        self._install(dash)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rules_db.cmd_keys(self.con, _Args(file=[str(cfg)], json_mode=True))
        data = json.loads(buf.getvalue())      # stdout is pure JSON
        self.assertEqual(data["machine"], rules_db._machine_id())
        by_source = {c["source"]: c for c in data["candidates"]}
        self.assertEqual(by_source[f"file:{cfg}"]["verdict"], "ok")
        self.assertEqual(by_source[f"file:{cfg}"]["tenant"], "lee-weaver-personal")
        self.assertNotIn(tok, buf.getvalue())   # masked, even in JSON
        self.assertEqual(dash.reports, [])      # --json does not imply --report

    def test_report_reports_failure_with_the_servers_own_reason(self):
        os.environ["STANDING_RULES_SYNC_TOKEN"] = "sr_live_" + "i" * 32  # unknown
        dash = FakeDash(set())
        self._install(dash)
        with self.assertRaises(SystemExit) as cm:
            with redirect_stdout(io.StringIO()):
                rules_db.cmd_keys(self.con, _Args(report=True))
        self.assertIn("report failed", str(cm.exception))
        self.assertEqual(dash.reports, [])


if __name__ == "__main__":
    unittest.main()
