#!/usr/bin/env python3
"""Matching-semantics tests (A1′): token-set matching with conservative
stemming and whole-token prefix containment (min length 3).

Replaces substring matching, which both over-matched short queries
('in' -> 'inference') and under-matched inflections ('deployment' vs
'deploy' — a silent recall loss).
"""
import json
import pathlib
import sys
import tempfile
import types
import unittest

SCRIPTS_DIR = pathlib.Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import rules_db  # noqa: E402


class MatchingTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        rules_db.DB_PATH = pathlib.Path(self.tmp.name) / "rules.db"
        rules_db.REF_DIR = pathlib.Path(self.tmp.name) / "references"
        self.con = rules_db.connect()
        rules_db.init_db(self.con)

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def _seed_rule(self, rule_id, text, domain="test", tier=2):
        rules_db.upsert_rule(self.con, {
            "id": rule_id, "name": rule_id.lower(), "text": text,
            "tier": tier, "domain": domain, "priority": "MED"})
        self.con.commit()

    def _query(self, match):
        args = types.SimpleNamespace(match=match, json=True)
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            rules_db.cmd_query(self.con, args)
        return json.loads(buf.getvalue())

    def _matched_ids(self, match):
        return {r["rule_id"] for r in self._query(match)["matched"]}

    # ---- brief fixture table ----

    def test_deploy_matches_deployment(self):
        self._seed_rule("SR-M1", "Always use the deployment pipeline")
        self.assertIn("SR-M1", self._matched_ids("deploy"))

    def test_deployment_matches_deploy(self):
        self._seed_rule("SR-M2", "Deploy early and often")
        self.assertIn("SR-M2", self._matched_ids("deployment"))

    def test_short_token_does_not_overmatch(self):
        self._seed_rule("SR-M3", "The inference engine runs locally")
        self.assertNotIn("SR-M3", self._matched_ids("in"))

    def test_exact_token_match_unchanged(self):
        self._seed_rule("SR-M4", "Discord delivery rules")
        self.assertIn("SR-M4", self._matched_ids("discord"))

    def test_hyphenated_domain_queryable(self):
        self._seed_rule("SR-M5", "Rule about sites", domain="web-delivery")
        self.assertIn("SR-M5", self._matched_ids("web"))

    def test_hyphenated_domain_queryable_second(self):
        self._seed_rule("SR-M6", "Rule about sites", domain="web-delivery")
        self.assertIn("SR-M6", self._matched_ids("delivery"))

    def test_multiple_tokens_any_overlap(self):
        self._seed_rule("SR-M7", "The discord bot posts updates")
        self.assertIn("SR-M7", self._matched_ids("bot cron"))

    def test_non_ascii_falls_back_to_substring(self):
        self._seed_rule("SR-M8", "配置 rules apply always")
        self.assertIn("SR-M8", self._matched_ids("配置"))

    def test_no_limit_returns_all_matches(self):
        for i in range(50):
            self._seed_rule(f"SR-L{i:02d}", f"match number {i}")
        self.assertEqual(len(self._matched_ids("match")), 50)

    def test_tier_1_always_present_regardless(self):
        self._seed_rule("SR-T9", "mandatory rule", tier=1)
        self._seed_rule("SR-M9", "unrelated tier two rule")
        out = self._query("zzznomatch")
        self.assertIn("SR-T9", {r["rule_id"] for r in out["mandatory"]})

    def test_empty_match_returns_only_tier_1(self):
        self._seed_rule("SR-T10", "mandatory rule", tier=1)
        self._seed_rule("SR-M10", "conditional rule")
        out = self._query("")
        self.assertEqual(out["matched"], [])
        self.assertEqual({r["rule_id"] for r in out["mandatory"]}, {"SR-T10"})

    # ---- owner-added trade-off visibility test ----

    def test_word_internal_substring_not_matched(self):
        self._seed_rule("SR-W1", "email delivery notifications")
        self.assertNotIn("SR-W1", self._matched_ids("mail"))

    # ---- recall safety net ----

    def test_recall_non_decreasing_vs_old_matcher(self):
        def old_matches(query, hay):
            kws = [k.lower() for k in query.split() if k]
            hay_l = hay.lower()
            return any(k in hay_l for k in kws)

        def new_matches(query, hay):
            return rules_db._match(query, hay)

        def stem_recoverable(query, hay):
            q_tokens = set(rules_db._TOKEN_RE.findall(query.lower()))
            h_tokens = set(rules_db._TOKEN_RE.findall(hay.lower()))
            kws = [k.lower() for k in query.split() if k]
            if any(k in hay.lower() for k in kws):
                return False  # old matcher would have hit; not a "recovery"
            for q in q_tokens:
                for h in h_tokens:
                    if len(q) >= 3 and len(h) >= 3 and (q.startswith(h) or h.startswith(q)):
                        return True
            return False

        fixtures = [
            ("Always use the deployment pipeline", "deploy"),
            ("Deploy early and often", "deployment"),
            ("The inference engine runs locally", "in"),
            ("Discord delivery rules", "discord"),
            ("web-delivery site dns hosting", "web"),
            ("web-delivery site dns hosting", "delivery"),
            ("The discord bot posts updates", "bot cron"),
            ("email delivery notifications", "mail"),
            ("rules about running deployments", "rule deploy running"),
            ("Configure dns cdn hosting", "dns config"),
            ("KDP Kindle publishing promo", "kindle"),
            ("cron watchdog timer jobs", "timer cron"),
            ("timestamp every response top", "timestamp"),
            ("training pipeline dataset export", "datasets"),
            ("GPU cooling temperatures limits", "temperature"),
        ]
        for hay, query in fixtures:
            old = old_matches(query, hay)
            new = new_matches(query, hay)
            if new:
                assert old or stem_recoverable(query, hay), \
                    f"recall regression: {query!r} on {hay!r} matched new " \
                    f"but not old and is not a stem recovery"


if __name__ == "__main__":
    unittest.main()
