# Spec: Fact Re-seed Idempotency

## Problem

`cmd_seed` in `scripts/rules_db.py` upserts rules idempotently (safe to run
twice with the same file) but always blindly INSERTs facts. Re-running `seed`
with the same file duplicates every fact with no warning. This contradicts the
docstring's "idempotent upsert" claim and the quick-start flow, which encourages
re-running `seed` after editing.

## Requirements

1. **Exact match on domain and text.** Detect existing facts using an exact
   match on the `(domain, text)` pair. This must be verified as the correct fact
   identity: facts have no stable ID (unlike rules with `SR-xxx`), the `facts`
   table schema is `(fact_id AUTOINCREMENT, domain, text, created_at,
   updated_at)`, and the seed template's `id`/`tags` fields are already silently
   ignored by the code. The `(domain, text)` pair is the only meaningful identity.

2. **Skip duplicates.** If a fact with the same `(domain, text)` already exists,
   skip it rather than inserting a new row.

3. **Track counts.** Maintain counters for facts inserted and facts skipped
   across the seed loop.

4. **Updated seed summary.** The `cmd_seed` output must report rules seeded,
   facts inserted, and duplicate facts skipped (replacing the old format that
   only reported total rules and total facts).

5. **Regression test.** Add an automated test that seeds the same file twice and
   verifies no duplicate facts are created. Must also cover domain separation
   (same text, different domain is distinct), partial overlap (existing skipped,
   new inserted), summary output, and the empty-facts edge case.

6. **Run the complete existing test suite.** All tests must pass.

7. **Minimal change.** Only `cmd_seed` is modified. No schema changes, no new
   dependencies, no unrelated modifications to application code or existing tests.
