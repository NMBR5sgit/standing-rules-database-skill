# Design: Fact Re-seed Idempotency

## Fact identity analysis

Facts have no stable ID. Unlike rules (which carry `SR-xxx` identifiers that
never change), the `facts` table uses an autoincrement `fact_id` with no
external identity. The seed template includes `id` and `tags` fields for facts,
but `cmd_seed` already ignores them — only `domain` and `text` are read from
each fact object.

Therefore the `(domain, text)` pair is the correct and only meaningful fact
identity. Two facts with the same domain and text are the same fact. Two facts
with the same text but different domains are distinct.

## Dedup check

Before each fact insert, run a parameterized existence check:

```python
con.execute("SELECT 1 FROM facts WHERE domain=? AND text=?",
            (f["domain"], f["text"])).fetchone()
```

If a row is returned, increment `facts_skipped` and continue. Otherwise insert
the fact and increment `facts_inserted`.

No schema change is needed. No index change is needed (the `facts` table is
small; a full scan per fact is trivially fast at this scale).

## Counters and summary output

Two counters replace the old `len(data.get("facts", []))` count:

- `facts_inserted` — facts actually written to the DB this run.
- `facts_skipped` — facts that already existed and were skipped.

The old summary line:
```
seeded 4 rules, 1 facts
```

becomes:
```
seeded 4 rules, inserted 1 facts, skipped 0 duplicate facts
```

On a re-seed of the same file:
```
seeded 4 rules, inserted 0 facts, skipped 1 duplicate facts
```

## Testing approach

- Framework: `unittest` (stdlib), consistent with the project's zero-dependency
  convention.
- Isolation: each test runs in a `tempfile.TemporaryDirectory` and monkeypatches
  `rules_db.DB_PATH` and `rules_db.REF_DIR` to avoid touching the real database.
- The test imports `rules_db` from `scripts/` via `sys.path.insert`.

### Test cases

| Test | What it verifies |
|---|---|
| `test_seed_twice_no_duplicate_facts` | Core regression: seed same file twice, fact count stays at 2, texts unchanged |
| `test_seed_same_text_different_domain_is_distinct` | Same text + different domain is not a duplicate (both insert) |
| `test_seed_partial_overlap_skips_existing_only` | Second seed with one existing + one new fact: existing skipped, new inserted, total = 2 |
| `test_seed_summary_reports_inserted_and_skipped` | First seed prints "inserted 1 facts, skipped 0"; second prints "inserted 0, skipped 1" |
| `test_seed_empty_facts_list` | Empty facts list prints "inserted 0 facts, skipped 0 duplicate facts" |

## What was not changed

- `facts` table schema — no new columns, no index changes.
- `cmd_seed` signature — still `(con, path)`.
- No new dependencies added.
- `upsert_rule` logic untouched — rules were already idempotent.
- No other `rules_db.py` functions modified.
