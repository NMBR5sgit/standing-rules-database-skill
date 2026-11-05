#!/usr/bin/env python3
"""Regression tests for the pre_llm_call auto-consult hook.

The hook must NOT re-implement matching: it imports query_rules from
scripts/rules_db.py (single source of truth). These tests prove the
deploy<->deployment collision works through the hook path, exactly like
the CLI tests do, and that the hook contains no local matcher copy.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

SKILL_DIR = os.path.expandvars(
    r"$LOCALAPPDATA/hermes/skills/standing-rules-database-skill")
HOOK = os.path.expandvars(
    r"$LOCALAPPDATA/hermes/agent-hooks/standing_rules_consult.py")


def _run_hook(home, user_message):
    """Run the hook against the fixture DB rooted at <home>/skills/... .
    HERMES_HOME must be the hermes-home root (parent of skills/), matching
    how the hook resolves DB = HERMES_HOME/skills/standing-rules-database-skill/rules.db."""
    payload = json.dumps({"extra": {"user_message": user_message}})
    env = dict(os.environ, HERMES_HOME=home)
    proc = subprocess.run(
        [sys.executable, HOOK],
        input=payload, capture_output=True, text=True, env=env, timeout=30)
    out = proc.stdout.strip()
    return json.loads(out) if out else None


@unittest.skipUnless(
    os.path.isdir(SKILL_DIR) and os.path.isfile(HOOK),
    "requires a Hermes install (SKILL_DIR/HOOK not present)")
class HookRecallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        home = self.tmp.name
        skills = os.path.join(home, "skills", "standing-rules-database-skill")
        scripts = os.path.join(skills, "scripts")
        os.makedirs(scripts)
        self.db = os.path.join(skills, "rules.db")
        # Use the live engine to build the fixture DB.
        engine = os.path.join(SKILL_DIR, "scripts", "rules_db.py")
        sys.path.insert(0, os.path.dirname(engine))
        import importlib
        self.rules_db = importlib.import_module("rules_db")
        import sqlite3
        con = sqlite3.connect(self.db)
        con.row_factory = sqlite3.Row
        self.rules_db.init_db(con)
        con.execute(
            "INSERT INTO rules (rule_id, name, text, tier, priority, status, domain, tags) "
            "VALUES ('SR-901','deploy hygiene','Always rebuild the deployment pipeline from a clean checkout.',"
            "2,'HIGH','active','deployments','deploy deployment pipeline')")
        con.execute(
            "INSERT INTO rules (rule_id, name, text, tier, priority, status, domain, tags) "
            "VALUES ('SR-902','deploy verb','Redeploy after any config change.',"
            "2,'MED','active','deployments','deploy')")
        con.execute(
            "INSERT INTO rules (rule_id, name, text, tier, priority, status, domain, tags) "
            "VALUES ('SR-903','inference','Keep inference batches under 32 tokens per request.',"
            "2,'MED','active','inference','inference model')")
        con.commit()
        con.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_deploy_query_matches_deployment_rule(self):
        """Hook path: a message containing 'deploy' must match the rule whose
        text says 'deployment' — the A1′ collision, via the hook."""
        res = _run_hook(self.tmp.name, "please deploy this")
        self.assertIsNotNone(res, "hook produced no context for 'deploy'")
        self.assertIn("SR-901", res["context"])

    def test_deployment_query_matches_deploy_rule(self):
        """Hook path: 'deployment' must match the rule whose text is 'Redeploy'."""
        res = _run_hook(self.tmp.name, "the deployment failed")
        self.assertIsNotNone(res, "hook produced no context for 'deployment'")
        self.assertIn("SR-902", res["context"])

    def test_hook_does_not_reimplement_matching(self):
        """The hook source must import the engine's query_rules and contain no
        local matcher/stem/token re-implementation."""
        with open(HOOK, encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("from rules_db import query_rules", src)
        self.assertIn("query_rules(con", src)
        # No local substring-LIKE recall left in the hook.
        self.assertNotIn("LIKE ?", src)
        # No local stem/prefix logic re-implemented.
        self.assertNotIn("startswith", src)
        self.assertNotIn("_stem", src)

    def test_hook_matches_engine_results(self):
        """For the same query, hook tier-2 matches == engine query_rules
        tier-2 matches (modulo the presentation cap)."""
        res = _run_hook(self.tmp.name, "deployment pipeline inference")
        import sqlite3
        con = sqlite3.connect(self.db)
        con.row_factory = sqlite3.Row
        engine_ids = {r["rule_id"] for r in self.rules_db.query_rules(con, "deployment pipeline inference")
                      if r["tier"] == 2}
        con.close()
        hook_ids = {rid for rid in ("SR-901", "SR-902", "SR-903")
                    if res and rid in res["context"]}
        self.assertEqual(engine_ids, hook_ids)


if __name__ == "__main__":
    unittest.main()
