# Tasks: Fact Re-seed Idempotency

All tasks complete. Work was done in two commits on `main`:

- Fix fact duplication on re-seed: skip existing facts by domain+text
- Fix author name: set byline to the author's real name

## Tasks

- [x] **Implement fact dedup in `cmd_seed`** — Add existence check on
      `(domain, text)` before each insert. Track `facts_inserted` and
      `facts_skipped` counters. Update summary output.
- [x] **Create regression test file** — `tests/test_seed_facts.py` with
      `SeedFactsDedupTest` class using `unittest`, temp directory isolation,
      and monkeypatched `DB_PATH`.
- [x] **Add domain-separation test** — `test_seed_same_text_different_domain_is_distinct`
      verifies same text with different domains are treated as distinct facts.
- [x] **Run all tests** — 5/5 pass (`python -m unittest tests.test_seed_facts -v`).
- [x] **Commit** — Two commits on `main`, pushed to `origin/main`.

## Files changed

| File | Change |
|---|---|
| `scripts/rules_db.py` | `cmd_seed` modified: dedup check, counters, updated summary |
| `tests/test_seed_facts.py` | New file: 5 test cases |
| `README.md` | Author byline corrected |
| `LICENSE` | Copyright holder corrected |
