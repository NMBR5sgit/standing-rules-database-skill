# Standing Rules Database — Token & Recall Improvement Brief

**For:** Hermes (implementing agent)
**From:** Project owner
**Date:** 2026-11-04
**Scope:** All proposed improvements (A1′, A2, A3, B1′, B2′, C3, C4, C5). Read this
whole document, then scan the current code, then either implement or ask questions.

---

## How to use this brief

1. Read the whole document first. Do not start editing mid-read.
2. Scan `scripts/rules_db.py`, `scripts/scan_candidates.py`, `SKILL.md`,
   `templates/seed.example.json`, and `tests/`.
3. For each proposed change, verify that the "Current" description matches the
   code you actually see. If anything does not match, **stop and ask** — the
   code may have drifted since this brief was written.
4. For each change, verify the "Proposed" description is implementable as
   written against the current code. If it is not, **stop and ask** — do not
   improvise.
5. If everything matches, implement in the order given in the "Implementation
   order" section at the end. Commit per change (see "Commit discipline").
6. Run the full test suite after every commit:
   `python -m unittest discover -s tests -p "test_*.py" -v`

**Hard constraints that override everything in this document:**

- **No recall loss.** Every change must be recall-neutral or recall-increasing.
  Any change that could cause a matched rule to be dropped from a query result
  is unacceptable, even if it saves tokens. If you believe a change requires a
  recall trade-off, stop and ask — it does not.
- **Stdlib only.** No external dependencies. No `nltk`, no `whoosh`, no `jieba`.
- **Existing 5 tests must keep passing.** `tests/test_seed_facts.py` — 5 tests —
  must remain green throughout.
- **Do not modify the incident-related files** (`SECURITY.md`, anything in
  `.kiro/`) as part of this work. The incident is closed; this is separate.
- **Do not commit anything until you have run the tests and they pass.**

---

## Background — why these changes

The repo's thesis is that storage cost and context cost are decoupled: a large
rule database does not cost more context per job than a small one, because only
matching rules are loaded. That thesis is correct. But the current implementation
has four concrete problems that leak tokens or, worse, silently drop rules:

1. **Substring keyword matching is both over- and under-inclusive.** It
   over-matches short queries (`"in"` matches `"inference"`) and — critically —
   under-matches inflections (`"deployment"` does not match `"deploy"`). The
   under-matching direction is a **silent recall bug**: the agent asks for rules
   about deployment, the rule exists, and it is never loaded.
2. **`connect()` uses SQLite defaults for `busy_timeout` (0) and `synchronous`.**
   If a cron job and an interactive agent query the DB concurrently, the second
   query raises `database is locked` and the agent receives **zero rules** — a
   total, silent recall failure.
3. **Tier-1 rules — the highest-cost tokens in the system, paid every turn — are
   currently documented in verbose prose.** Users copy the prose shape and pay
   for it forever.
4. **The dispatch index in `SKILL.md` is a hand-maintained markdown table that
   can silently drift from the DB**, and the facts table uses free-text sentences
   where `key=value` would be shorter with no information loss.

Items 1 and 2 are **correctness fixes under the "no recall loss" constraint** and
should ship first. Items 3 and onward are token reductions with zero recall
impact. The later items (B1′, C5) require care and instrumentation and are gated
accordingly.

---

## C4 — `busy_timeout` and `synchronous` (SHIP FIRST)

**Current** (`scripts/rules_db.py`, `connect()`):

```python
def connect():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    return con
```

**Problem:** `busy_timeout` defaults to 0, so any lock contention raises
`database is locked` immediately. Under WAL, a write transaction held by one
connection blocks concurrent writes; a query during that window fails rather
than waiting. The failure is a silent zero-rule result for the agent.

**Proposed:**

```python
def connect():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=5000")
    con.execute("PRAGMA synchronous=NORMAL")
    return con
```

- `busy_timeout=5000` — a colliding query waits up to 5 seconds rather than
  failing. 5s is longer than any single query this code performs, so in
  practice it eliminates the failure mode entirely.
- `synchronous=NORMAL` — safe under WAL. Durability across process crashes is
  preserved; only the last checkpoint before an OS-level crash is at risk,
  which is irrelevant for a rules DB. Faster writes.

**Test:** new file `tests/test_lock_resilience.py`.

- Open two connections via `rules_db.connect()`.
- On connection A, `BEGIN IMMEDIATE` and do an insert, holding the transaction.
- On connection B (in a thread with a short delay so A holds the lock first),
  run `rules_db.cmd_query(con_b, args)` where `args.match = "anything"`.
- Assert the query **succeeds** (returns `mandatory` and `matched` without
  raising) within ~5 seconds. Under the current code, this raises
  `sqlite3.OperationalError: database is locked`.
- Clean up: rollback both, close both.

**Edge case:** If the test machine is so slow that 5s is insufficient, the
assertion is still correct — the test will fail loudly rather than silently
passing. Do not increase the timeout to make the test pass; increase it only if
you find a legitimate real-world contention window that exceeds it.

---

## A1′ — Token-set matching with conservative stemming

**Current** (`scripts/rules_db.py`, `cmd_query`):

```python
if a.match:
    kws = [k.lower() for k in a.match.split() if k]
    rows = con.execute("SELECT * FROM rules WHERE tier=2 AND status='active'").fetchall()
    for r in rows:
        hay = " ".join(str(r[c] or "") for c in ("rule_id", "name", "text", "summary", "tags", "domain")).lower()
        if any(k in hay for k in kws):
            matched.append(r)
```

**Problems (both directions):**

- **Over-match:** `--match "in"` matches every rule whose text contains the
  substring `"in"` — including `"inference"`, `"inline"`, `"initial"`. These
  enter context and dilute attention.
- **Under-match (the serious bug):** `--match "deployment"` does not match a
  rule whose text says `"deploy"`. The rule exists in the DB, the agent asked
  a legitimate question about it, and the rule is **silently dropped**. This
  violates the "no recall loss" constraint and must be fixed.

**Proposed:**

> **Correction (applied during implementation):** The original spec below uses
> `_stem` + intersection, which cannot collide `deploy` and `deployment` — the
> exact inflection bug this change exists to fix. The shipped matcher uses
> `_match` with **whole-token prefix containment (min 3 chars)** in addition to
> conservative `-s` stemming, per Hermes's amendment. See the "Shipped matcher"
> block at the end of this section.

Add module-level helpers:

```python
import re

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_NON_ASCII_RE = re.compile(r"[^\x00-\x7f]")

def _tokens(s: str) -> set:
    """Lowercase ASCII alphanumeric tokens. CJK/other scripts are dropped;
    queries containing them fall back to substring matching in cmd_query."""
    return set(_TOKEN_RE.findall(s.lower()))

def _stem(t: str) -> str:
    """Conservative plural strip: a trailing 's' on a word longer than 3 chars.
    Deliberately minimal — aggressive stemming introduces recall errors by
    collapsing distinct words. Do not add -ing, -ed, -es without a test that
    proves it helps on a real fixture set."""
    return t[:-1] if len(t) > 3 and t.endswith("s") else t

def _stems(s: str) -> set:
    return {_stem(t) for t in _tokens(s)}
```

Replace the matching loop in `cmd_query`:

```python
if a.match:
    # Non-ASCII queries (CJK, Cyrillic, etc.) cannot be tokenized correctly
    # without a script-specific tokenizer, which would violate stdlib-only.
    # Fall back to the previous substring behavior for those queries so that
    # nothing is silently dropped. ASCII queries use token-set matching.
    if _NON_ASCII_RE.search(a.match):
        kws = [k.lower() for k in a.match.split() if k]
        rows = con.execute("SELECT * FROM rules WHERE tier=2 AND status='active'").fetchall()
        for r in rows:
            hay = " ".join(str(r[c] or "") for c in ("rule_id", "name", "text", "summary", "tags", "domain")).lower()
            if any(k in hay for k in kws):
                matched.append(r)
    else:
        query_stems = _stems(a.match)
        rows = con.execute("SELECT * FROM rules WHERE tier=2 AND status='active'").fetchall()
        for r in rows:
            hay = " ".join(str(r[c] or "") for c in ("rule_id", "name", "text", "summary", "tags", "domain"))
            if query_stems & _stems(hay):
                matched.append(r)
```

**Do not add `--limit`, `--min-score`, `--top-k`, or any thresholding.** Every
overlapping rule still enters context. The change is about *which rules
overlap* (correctness), not *how many are returned* (which would be a recall
trade-off).

**Tests:** new file `tests/test_matching.py`. Each test seeds a small DB with
the rules named below and asserts the query result.

| Test | Fixture rule | Query | Assert |
|---|---|---|---|
| `test_deploy_matches_deployment` | text contains "deployment" | `"deploy"` | rule is in `matched` (current code misses this) |
| `test_deployment_matches_deploy` | text contains "deploy" | `"deployment"` | rule is in `matched` (current code misses this) |
| `test_short_token_does_not_overmatch` | text contains "inference" | `"in"` | rule is **not** in `matched` (current code wrongly matches) |
| `test_exact_token_match_unchanged` | text contains "discord delivery" | `"discord"` | rule is in `matched` |
| `test_hyphenated_domain_queryable` | domain = `"web-delivery"` | `"web"` | rule is in `matched` |
| `test_hyphenated_domain_queryable_second` | domain = `"web-delivery"` | `"delivery"` | rule is in `matched` |
| `test_multiple_tokens_any_overlap` | text contains "discord bot" | `"bot cron"` | rule is in `matched` (any-overlap semantics preserved) |
| `test_non_ascii_falls_back_to_substring` | text contains `"配置"` | `"配置"` | rule is in `matched` (fallback preserves current behavior) |
| `test_no_limit_returns_all_matches` | 50 rules all containing "match" | `"match"` | **all 50** are in `matched` — proves no silent cap |
| `test_tier_1_always_present_regardless` | one tier-1 rule | any non-matching query | tier-1 rule is in `mandatory` |
| `test_empty_match_returns_only_tier_1` | one tier-1, one tier-2 | `""` | `matched` is empty; `mandatory` has the tier-1 |

**Recall-non-decreasing fixture assertion:** add one more test
`test_recall_non_decreasing_vs_old_matcher` that defines both matchers
(old substring, new token-set) as local functions, runs a fixed fixture list of
(rule, query) pairs through both, and asserts:

```python
for rule, query in fixtures:
    old = old_matches(rule, query)
    new = new_matches(rule, query)
    if new:
        assert old or stem_recoverable(rule, query), \
            f"recall regression: {query!r} on {rule!r} matched new but not old and is not a stem recovery"
```

This is the safety net: it proves the new matcher never *loses* a match that
the old matcher would have found, except where the new match is a legitimate
stem recovery (in which case recall went *up*, not down).

**Non-goals for this change:**

- Do not introduce stemming beyond the trailing-`s` strip.
- Do not attempt to tokenize CJK; the fallback is the answer.
- Do not add fuzzy matching, edit distance, or semantic similarity.
- Do not change `facts` retrieval, `get` by ID, or `log`.

### Shipped matcher (authoritative — what actually landed)

```python
def _match(query: str, hay: str) -> bool:
    q_tokens = set(_TOKEN_RE.findall(query.lower()))
    h_tokens = set(_TOKEN_RE.findall(hay.lower()))
    q_exact = {_stem(t) for t in q_tokens}
    h_exact = {_stem(t) for t in h_tokens}
    if q_exact & h_exact:
        return True
    q_pfx = {t for t in q_tokens if len(t) >= 3}
    h_pfx = {t for t in h_tokens if len(t) >= 3}
    for q in q_pfx:
        for h in h_pfx:
            if q.startswith(h) or h.startswith(q):
                return True
    return False
```

And `stem_recoverable` for the recall test:

```python
def stem_recoverable(query: str, hay: str) -> bool:
    q_tokens = set(_TOKEN_RE.findall(query.lower()))
    h_tokens = set(_TOKEN_RE.findall(hay.lower()))
    kws = [k.lower() for k in query.split() if k]
    if any(k in hay.lower() for k in kws):
        return False
    for q in q_tokens:
        for h in h_tokens:
            if len(q) >= 3 and len(h) >= 3 and (q.startswith(h) or h.startswith(q)):
                return True
    return False
```

Word-internal substring recall (`mail`→`email`) is intentionally sacrificed:
the old behavior was a false positive, not real recall. The over-match fix
(`in` no longer matches `inference`) requires accepting this trade-off. This is
asserted by `test_word_internal_substring_not_matched` in `tests/test_matching.py`.

---

## A3 — Terse Tier-1 shape in `SKILL.md`

**Current** (`SKILL.md`, section "Tier 1 — HARD rules"):

```
Tier-1 is YOURS — the few rules that must apply to every job no matter what, imported from your own system at bootstrap. Keep them single-digit and mirror them into your agent's always-injected memory. Shape of a tier-1 entry: `[SR-T1x] <rule text>`, e.g. a required output-format rule, or a contact-identity rule for outbound text.
```

That last sentence ("e.g. a required output-format rule...") is prose that costs
tokens every time the skill is loaded, and teaches the user to write verbose
rules.

**Proposed replacement** (replace that paragraph entirely):

```
Tier-1 is YOURS — the few rules that must apply to every job, imported from your
own system at bootstrap. Keep them single-digit; mirror them into your agent's
always-injected memory. Write them terse — every turn pays for them:

  [SR-T1x] <one imperative sentence>

Form examples (not shipped rules — replace with your own):
  [SR-T1x] Public identity = legal business name, never nickname.
  [SR-T1x] Business contact = A@B only; never C.
  [SR-T1x] Every response: timestamp on top.
  [SR-T1x] Prices: check live, never from memory.
```

**Why terse:** Tier-1 is paid on every turn of every job — the most expensive
tokens in the system. The example phrasing above is ~50–60% fewer tokens than
verbose phrasing for the same rule. Full prose lives in the DB `summary` field;
only the terse form goes into always-injected memory.

**Zero recall impact:** the rule still loads; only its rendering is tighter.

**Test:** new file `tests/test_skill_doc.py` (or add to `test_matching.py` if
you prefer fewer files — but a separate file is cleaner):

- Read `SKILL.md` as text.
- Extract the section between `## Tier 1 — HARD rules` and the next `## `.
- Assert: no line in that section exceeds 200 characters (prevents drift back
  to paragraph form).
- Assert: the literal string `[SR-T1x] <one imperative sentence>` appears.
- Assert: the string `Public identity = legal business name` appears (proves
  the examples were not accidentally removed).
- Assert: the string `e.g. a required output-format rule` does NOT appear
  (proves the old verbose phrasing was removed).

---

## A2 — Generated compact dispatch index

**Current:** `SKILL.md` contains a hand-maintained markdown dispatch table. It
can drift from the DB's actual domains and tags. Every drift is a potential
recall loss (a domain exists in the DB but is not in the index, so the agent
never queries for it).

**Proposed:** add a new `export-index` subcommand to `rules_db.py` that writes
a compact, machine-readable index to `references/_index.txt`:

```
outbound: email listing byline comment outreach reply
web-delivery: site dns cdn hosting deploy
messaging: chat dm bot cron channel delivery
...
```

Generated as follows:

```python
def cmd_export_index(con):
    REF_DIR.mkdir(exist_ok=True)
    rows = con.execute(
        "SELECT DISTINCT domain FROM rules "
        "WHERE domain IS NOT NULL AND domain != 'core' ORDER BY domain"
    ).fetchall()
    lines = []
    for (d,) in rows:
        # Collect tags from all active rules in this domain.
        tag_rows = con.execute(
            "SELECT tags FROM rules WHERE domain=? AND status='active'", (d,)
        ).fetchall()
        tags = set()
        for (t,) in tag_rows:
            if t:
                tags.update(t.lower().split())
        # Optional: also include words from the domain itself so
        # "web-delivery" contributes "web" and "delivery".
        for word in re.split(r"[^a-z0-9]+", d.lower()):
            if word:
                tags.add(word)
        lines.append(f"{d}: {' '.join(sorted(tags))}")
    (REF_DIR / "_index.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"exported index for {len(rows)} domains")
```

Register `sub.add_parser("export-index")` in `main()`.

Add a line to `SKILL.md` pointing to it: *"The compact index at
`references/_index.txt` is generated by `export-index`; regenerate after any DB
change so the index does not drift."*

**Do not remove the existing markdown table from `SKILL.md`** in this change.
The table is human-readable documentation; the compact index is the machine
contract. They serve different purposes. A later change can decide whether to
remove the table.

**Test:** `tests/test_export_index.py`:

- Seed 3 domains with rules and tags.
- Run `cmd_export_index`.
- Read `references/_index.txt`.
- Assert: 3 lines; each begins with `domain:`; the domain's own words appear;
  the tags from the rules appear.
- Assert idempotent: running twice produces the same file content.

**Not required for this change to be useful:** auto-regenerating the index from
`cmd_add` / `cmd_seed` / `cmd_update`. The README and SKILL.md already say
"regenerate after any change." If you want to wire that in, ask first — it
changes the write path and deserves its own review.

---

## C3 — Schema version table and migration runner

**Current:** `init_db` uses `CREATE TABLE IF NOT EXISTS` — idempotent, but if a
future change adds a column, existing `rules.db` files silently keep the old
schema and `SELECT new_column` throws at runtime. That is a silent failure in
the query path, which is a recall failure.

**Proposed:** add a `schema_version` table and a migration runner called from
`init_db`. No column changes yet — this is the enabler for B2′ later.

```python
SCHEMA_VERSION = 1

def init_db(con):
    con.executescript("""
    -- existing CREATE TABLE statements unchanged
    CREATE TABLE IF NOT EXISTS schema_version(v INTEGER NOT NULL);
    """)
    con.commit()
    _migrate(con)

def _migrate(con):
    row = con.execute("SELECT v FROM schema_version LIMIT 1").fetchone()
    current = row[0] if row else 0
    if current >= SCHEMA_VERSION:
        return
    # Future migrations go here, guarded by PRAGMA table_info checks:
    # if current < 2:
    #     cols = {r["name"] for r in con.execute("PRAGMA table_info(facts)")}
    #     if "fact_key" not in cols:
    #         con.execute("ALTER TABLE facts ADD COLUMN fact_key TEXT")
    if current == 0:
        con.execute("INSERT INTO schema_version(v) VALUES (?)", (SCHEMA_VERSION,))
    else:
        con.execute("UPDATE schema_version SET v=?", (SCHEMA_VERSION,))
    con.commit()
```

**Test:** `tests/test_schema_version.py`:

- Fresh DB: after `init_db`, `schema_version` has one row with `v = SCHEMA_VERSION`.
- `init_db` run twice: `schema_version` still has one row (idempotent).
- Simulate an old DB: delete the row, re-run `init_db`, assert the row is
  recreated.

**Do not add any column changes in this commit.** C3 is the runner only. B2′
(which needs `fact_key`) lands in a later commit that uses this runner.

---

## B2′ — Keyed facts (requires C3 shipped first)

**Current:** `facts` table is `(fact_id, domain, text, created_at, updated_at)`.
`cmd_facts` prints `[domain] text` for each row. Facts are free-text sentences.

**Proposed:** add nullable `fact_key` and `fact_value` columns via the C3
migration runner (version 2). Keep `text` as the fallback. `cmd_facts` prints:

- If `fact_key` and `fact_value` are both non-null: `[domain] key=value`
- Else: `[domain] text` (current behavior)

Add `--verbose` flag to `cmd_facts` to force printing `text` when key/value
exist (useful when the sentence has context the key/value loses).

**Do not add `--key`/`--value` args to `cmd_add`** — `cmd_add` is for rules.
Facts go through `seed` (bulk) or a future `fact-add`. For this change, only
`seed` needs to accept optional `key`/`value` fields in the JSON, and only
`cmd_facts` needs to render them.

**Migration (version 2):**

```python
if current < 2:
    cols = {r["name"] for r in con.execute("PRAGMA table_info(facts)")}
    if "fact_key" not in cols:
        con.execute("ALTER TABLE facts ADD COLUMN fact_key TEXT")
    if "fact_value" not in cols:
        con.execute("ALTER TABLE facts ADD COLUMN fact_value TEXT")
```

**Seeding (in `cmd_seed`, the facts loop):**

```python
key = f.get("key")
value = f.get("value")
con.execute(
    "INSERT INTO facts(domain,text,fact_key,fact_value,created_at,updated_at) "
    "VALUES(?,?,?,?,?,?)",
    (f["domain"], f["text"], key, value, now(), now()),
)
```

The dedup check remains on `(domain, text)`. Do **not** switch the dedup key to
`(domain, key)` — that would change identity semantics and requires its own
review. Keep `(domain, text)` as the identity; `key`/`value` are optional
display shortcuts.

**Rendering (in `cmd_facts`):**

```python
for r in rows:
    if not a.verbose and r["fact_key"] and r["fact_value"]:
        print(f"[{r['domain']}] {r['fact_key']}={r['fact_value']}")
    else:
        print(f"[{r['domain']}] {r['text']}")
```

**Tests:** `tests/test_keyed_facts.py`:

- Seed a fact with `{"domain": "web", "text": "Primary domain is example.com", "key": "primary_domain", "value": "example.com"}`.
- `cmd_facts` default: prints `[web] primary_domain=example.com`.
- `cmd_facts --verbose`: prints `[web] Primary domain is example.com`.
- Seed a fact with no key/value: prints text as before.
- Run `init_db` on an old DB (no fact_key column) and assert the column is added
  and no data is lost.
- Dedup still works: seeding the same `(domain, text)` twice does not duplicate,
  whether or not key/value are present.

**Recall safety:** `cmd_facts` output is used by the agent to read facts before
acting. The key/value rendering is strictly shorter; `--verbose` is available
when the full sentence is needed. No fact is ever hidden — every fact is still
printed by `cmd_facts`, just in a shorter form. This is token reduction with no
recall impact.

---

## C5 — FTS5 with permanent Python fallback (LATER — do not ship in first pass)

**Current:** `cmd_query` loads all tier-2 rows and filters in Python. At
hundreds of rules this is fine; at thousands it is not. A1′ improves the
matching quality but not the scaling.

**Proposed (later):** maintain an FTS5 virtual table mirroring the searchable
columns of `rules`, with the `porter` tokenizer. `cmd_query` uses FTS5 when
available; falls back to the A1′ Python matcher when FTS5 is unavailable (some
SQLite builds omit it).

**Do not implement this in the first pass.** A1′ is the recall-safe fix and is
smaller; FTS5 subsumes it but introduces a capability-check surface and a
reindex path. Ship A1′ first, watch it in real queries, then decide whether FTS5
is worth the added complexity.

If you do implement it later, the hard constraint is: **the fallback path must
be permanent, not temporary.** A user on a SQLite build without FTS5 must get
recall at least as good as A1′, not a degraded experience. The fallback is the
contract; FTS5 is an optimization on top of it.

---

## B1′ — Signature + full text, gated on instrumentation (LATER, GATED)

**Concept:** `query` returns one-line signatures for MED/LOW-priority matches,
full text for HIGH-priority matches. The agent fetches full text via a new
`get <rule_id>` subcommand for any signature rule it decides to apply.

**Why gated:** this is the *only* proposed change that can reduce recall, and it
does so via **agent behavior** rather than code — if the agent skips the `get`
call before acting, the rule is seen but not fully loaded. That is unacceptable
under the "no recall loss" constraint unless we can prove the agent reliably
calls `get`.

**Precondition before implementation:**

1. A1′, A2, A3, C3, B2′, C4 are shipped and stable in production use.
2. `get` subcommand is implemented and logged in `usage_log` (extend
   `usage_log` with a `kind` column: `"query"` | `"get"`).
3. At least two weeks of real usage data showing the agent calls `get` on
   signature rules reliably (>95% of the time).
4. A rollback plan: if `get` calls drop below 95%, the signature optimization
   is disabled by feature flag and full text returns by default.

**Do not implement B1′ before those preconditions.** If the user asks for it
before then, respond: *"B1′ is gated on `get` instrumentation and real usage
data. A1′/A2/A3/C3/B2′/C4 should ship first; B1′ is a later review."*

---

## Implementation order and commit discipline

**Ship in this order, one commit per change:**

1. **C4** — `busy_timeout` + `synchronous` in `connect()`, plus
   `tests/test_lock_resilience.py`. Commit message:
   `Fix: prevent zero-rule query failure on lock contention (busy_timeout)`.
2. **A1′** — token-set matching, helpers, fallback, plus
   `tests/test_matching.py` (including the recall-non-decreasing fixture
   assertion). Commit message:
   `Fix: keyword matching misses deploy/deployment; switch to token-set with conservative stem`.
3. **A3** — `SKILL.md` Tier-1 section rewrite, plus `tests/test_skill_doc.py`.
   Commit message:
   `Docs: terse tier-1 shape in SKILL.md; add snapshot test`.
4. **A2** — `export-index` subcommand, `references/_index.txt` generation,
   `SKILL.md` pointer line, plus `tests/test_export_index.py`. Commit message:
   `Add: generated compact dispatch index to prevent drift`.
5. **C3** — `schema_version` table and migration runner, plus
   `tests/test_schema_version.py`. Commit message:
   `Add: schema version and migration runner`.
6. **B2′** — keyed facts (migration v2, seed accepts `key`/`value`,
   `cmd_facts` renders `key=value`, `--verbose`), plus
   `tests/test_keyed_facts.py`. Commit message:
   `Add: optional key/value rendering for facts (requires C3)`.

After each commit:

- Run `python -m unittest discover -s tests -p "test_*.py" -v` and paste the
  full output.
- If any test fails, do not commit. Fix and re-run.
- The 5 existing tests in `tests/test_seed_facts.py` must remain green
  throughout.

**Do not batch commits.** One change, one commit, one test run. If you find
yourself wanting to combine C4 and A1′, stop — they are independent and the
smaller commit is easier to review and revert.

**What NOT to do:**

- Do not add `--limit`, `--min-score`, `--top-k`, or any recall-reducing flag
  to `cmd_query`.
- Do not change `upsert_rule`, `cmd_add`, `cmd_update`, `cmd_status`,
  `cmd_log`, or `cmd_export_md` in any of these commits, except B2′'s changes
  to `cmd_seed` and `cmd_facts`.
- Do not modify `templates/seed.example.json` — it is intentionally empty.
- Do not modify `README.md` unless a commit's text explicitly says to.
- Do not touch `.kiro/` or `SECURITY.md`.
- Do not add any dependency outside the Python stdlib.
- Do not add stemming beyond the trailing-`s` strip.
- Do not change the dedup key for facts from `(domain, text)`.

---

## Verification checklist before you say "done"

For each of the six commits, confirm:

- [ ] The commit modifies only the files named in its section above.
- [ ] `python -m unittest discover -s tests -p "test_*.py" -v` passes with
      zero failures and zero errors.
- [ ] The 5 original tests in `tests/test_seed_facts.py` still pass.
- [ ] No new dependency in `import` statements outside the stdlib.
- [ ] No change reduces the number of rules returned by any existing query
      fixture (verified by `test_recall_non_decreasing_vs_old_matcher` for A1′,
      and by manual reasoning for the rest).

After all six commits:

- [ ] `python scripts/rules_db.py init && python scripts/rules_db.py query --match "test"` runs without error on a fresh DB.
- [ ] `python scripts/rules_db.py export-md && python scripts/rules_db.py export-index` both succeed and produce files.
- [ ] `grep -rIl "<my-personal-email>\|<old-pseudonym>\|<private-domain>" . --exclude-dir=.git` returns nothing.
  (Replace the bracketed placeholders with your own private identifiers when running this check locally. Do not commit the literal identifiers.)

---

## If anything does not match

If the current code does not match the "Current" snippets in this document,
**stop and ask before editing.** The brief was written against a specific
snapshot. If the code has drifted, the drift may be relevant to the change, and
guessing could introduce a regression.

If a proposed change cannot be implemented as written (e.g. a test would need a
thread and the repo has no threading conventions), **stop and ask before
improvising.** Do not invent a solution that is not described here; the recall
and safety properties depend on the specifics.

If you find yourself thinking "I'll just skip this test because it's hard to
write," **stop.** The tests are the contract. If a test is genuinely
infeasible, the correct move is to tell the user which test is infeasible and
why, so the change can be scoped down — not to silently drop it.

---

## Questions to ask the owner before starting (if any apply)

1. Any of the "Current" snippets not matching the code? Describe the drift.
2. Is C4's 5-second `busy_timeout` acceptable, or should it be tuned (e.g. 2s
   if the workload has tighter latency budgets)?
3. Is A1′'s non-ASCII fallback acceptable, or should CJK queries be handled
   differently (would require a dependency, so this is a scope question, not an
   implementation one)?
4. Does `SKILL.md`'s Tier-1 section rewrite preserve the "shape not content"
   contract, i.e. does the owner agree the examples are generic and do not
   reintroduce personal rules? (They are generic — the examples above are the
   same shape as the ones already in the file, but the owner should confirm.)

---

## Implementation Notes (Post-Ship)

During implementation, two adjustments were made that are now part of the shipped architecture:

1. **Hook Sync:** The automatic pre-LLM consult hook (`standing_rules_consult.py`) was originally executing its own `LIKE %kw%` SQL query. It has been refactored to import `query_rules` from `scripts/rules_db.py`, guaranteeing a single source of truth for recall semantics across the CLI and the agent runtime. Regression tests: `tests/test_hook_consult.py` (deploy ↔ deployment through the hook path, and a source check that the hook contains no local matcher copy).
2. **Migration Ordering:** The `schema_version` runner was adjusted to read `MAX(v)` instead of `LIMIT 1` to prevent stale rows from regressing the version state. Regression test: `test_stale_zero_row_does_not_regress_version`.

---

*End of brief.*