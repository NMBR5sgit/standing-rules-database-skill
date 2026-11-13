#!/usr/bin/env python3
"""Tests for the keys scanner window's data layer and its delete wording.

The window is a thin driver over the tested `rules_db.py keys` verbs, so what
matters here is (a) it talks to the real engine, (b) its confirmation text
promises exactly the surgical semantics the engine implements — never
"clear the environment", never "delete the file" for a shared file, and
(c) the UI can actually be built on this machine.
"""
import pathlib
import sys
import unittest

SCRIPTS_DIR = pathlib.Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import keys_gui  # noqa: E402


class EngineWiringTests(unittest.TestCase):
    def test_the_window_drives_the_real_engine(self):
        self.assertTrue(keys_gui.ENGINE.exists(),
                        "keys_gui.py must sit next to rules_db.py")
        self.assertIn("def cmd_keys", keys_gui.ENGINE.read_text(encoding="utf-8"))

    def test_run_keys_returns_exit_code_and_output(self):
        code, out = keys_gui.run_keys("--help")
        self.assertEqual(code, 0)
        self.assertIn("--delete", out)      # the verbs the buttons rely on
        self.assertIn("--apply", out)

    def test_scan_rejects_a_broken_engine_instead_of_guessing(self):
        real = keys_gui.run_keys
        try:
            keys_gui.run_keys = lambda *a, **k: (1, "STANDING_RULES_SYNC_URL is not set")
            with self.assertRaises(ValueError) as cm:
                keys_gui.scan()
            self.assertIn("SYNC_URL", str(cm.exception))
            keys_gui.run_keys = lambda *a, **k: (0, "not json at all")
            with self.assertRaises(ValueError):
                keys_gui.scan()
        finally:
            keys_gui.run_keys = real

    def test_scan_parses_the_json_scan(self):
        real = keys_gui.run_keys
        try:
            keys_gui.run_keys = lambda *a, **k: (0, (
                '{"machine": "BOX-1", "base": "http://dash", "candidates": '
                '[{"source": "file:C:/a/b.txt", "masked": "sr_live_…wxyz", '
                '"verdict": "ok", "rules": 4, "tenant": "lee-weaver-personal"}]}'))
            data = keys_gui.scan()
        finally:
            keys_gui.run_keys = real
        self.assertEqual(data["candidates"][0]["source"], "file:C:/a/b.txt")


class DeleteWordingTests(unittest.TestCase):
    """The promise shown in the confirm dialog must match what the engine
    actually does: one variable, one vault entry, or one line in a file."""

    def test_env_delete_promises_only_that_one_variable(self):
        w = keys_gui.delete_warning("env:STANDING_RULES_SYNC_TOKEN", "ok")
        self.assertIn("single environment variable STANDING_RULES_SYNC_TOKEN", w)
        self.assertIn("Every other variable stays", w)
        self.assertNotIn("clear the environment", w.lower())

    def test_vault_delete_promises_only_that_one_entry(self):
        w = keys_gui.delete_warning("vault:standing_rules_sync_token", "ok")
        self.assertIn("single vault entry standing_rules_sync_token", w)
        self.assertIn("Every other secret", w)

    def test_file_delete_promises_to_keep_a_shared_file(self):
        w = keys_gui.delete_warning("file:C:/somewhere/config.txt", "ok")
        self.assertIn("only the line(s) holding this key", w)
        self.assertIn("C:/somewhere/config.txt", w)
        self.assertIn("The file itself stays, with all its other settings", w)
        self.assertIn("deleted only if the key was its entire content", w)

    def test_an_unverified_key_is_flagged_as_needing_confirmation(self):
        w = keys_gui.delete_warning("env:SOME_TOKEN", "reject")
        self.assertIn("does not authenticate", w)
        self.assertIn("explicit confirmation", w)

    def test_a_revoked_tenant_is_flagged(self):
        w = keys_gui.delete_warning("vault:standing_rules_sync_token", "revoked")
        self.assertIn("revoked", w)
        self.assertIn("no longer syncs any rules", w)


class WindowBuildTests(unittest.TestCase):
    """Prove the widgets can be constructed on this machine (no window is
    shown to the user; the window is created, refreshed once, destroyed)."""

    def test_window_constructs_and_renders_a_scan(self):
        try:
            import tkinter as tk
        except ImportError:
            self.skipTest("tkinter unavailable")
        try:
            win = keys_gui.KeysWindow()
        except tk.TclError as e:
            self.skipTest(f"no display available: {e}")
        try:
            win._render_scan({"machine": "BOX-1", "base": "http://dash",
                              "candidates": [{"masked": "sr_live_…wxyz",
                                              "verdict": "ok", "rules": 1,
                                              "tenant": "T", "source": "env:X"}]})
            rows = [win.tree.item(i)["values"]
                    for i in win.tree.get_children()]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0][3], "env:X")
            win.update_idletasks()
        finally:
            win.destroy()


if __name__ == "__main__":
    unittest.main()
