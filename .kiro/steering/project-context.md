# Project Context

## What this project is

`standing-rules-database-skill` is an installable agent skill that provides a
scalable standing-rules database for AI agents. The core thesis: decouple
storage from context cost. Instead of injecting all rules into every prompt
(collapses at scale) or relying on probabilistic semantic search (silently
misses mandatory rules), this project uses a two-tier system backed by SQLite.

## Architecture

**Authoritative store:** SQLite (`rules.db` in the skill root). Tables: `rules`,
`rule_versions`, `facts`, `usage_log`. All writes go through `scripts/rules_db.py`.

**Generated views:** Markdown files under `references/` are produced by
`export-md`. They are never hand-edited; regenerated from the DB after any change.

**Two-tier retrieval:**
- Tier 1 (mandatory): a small set of rules always injected by the agent's
  permanent memory. Never dependent on a lookup — the anti-semantic-search
  guarantee. Must stay single-digit in count.
- Tier 2 (conditional): everything else, fetched per-task via keyword match.
  Only rules whose trigger wording overlaps the job's wording reach context.

**Rule anatomy:** Stable ID (`SR-xxx`), priority tag (HIGH/MED/LOW), status
lifecycle (active/disabled/deprecated — never deleted), domain, tags.

**Rules vs facts:** Rules are behavioral constraints; facts (endpoints, IDs,
versions) are resources needed to apply them. Kept in separate tables.

**Write gate:** New rules pass through classify, duplicate-check,
contradiction-check, user approval, then ID/priority assignment before insertion.
Enforced socially via SKILL.md and README.md, not in code.

**Authority boundary:** A stored rule never grants permission. System/developer
instructions, safety constraints, and actual tool approvals always outrank any
rule, whatever its priority.

## Tech stack

- **Language:** Python 3 (stdlib only — no external dependencies)
- **Database:** SQLite (WAL journal mode)
- **Tests:** unittest (stdlib)
- **No build system:** scripts run directly via `python scripts/rules_db.py`

## File structure

```
scripts/
  rules_db.py          Query engine: init, seed, add, update, status, query,
                       facts, log, export-md (~230 lines)
  scan_candidates.py   Bootstrap helper: regex-scans rule-bearing files,
                       writes rule_candidates.json (proposals, never auto-activates)
templates/
  seed.example.json    Example seed file showing rule and fact shape
tests/
  test_seed_facts.py   Regression tests for fact re-seed idempotency
SKILL.md               Agent-facing dispatch file (loaded on demand by agents)
README.md              Human-facing design rationale and quick-start
```

## Key paths and constants

- `DB_PATH` is resolved from `__file__` in `rules_db.py` — always the skill
  root's `rules.db`, regardless of current working directory.
- `REF_DIR` is `references/` under the skill root.
- Git ignores: `rules.db`, WAL files, `rule_candidates.json`, `references/`,
  `__pycache__/`.
