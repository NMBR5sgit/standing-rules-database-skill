# standing-rules-database-skill — a growing standing-rules database for AI agents

Agents that follow standing rules usually inject every rule into every prompt. That works for ten rules and collapses at fifty: the rule set eats the context budget, and adding a rule means editing the permanent prompt. The opposite extreme — semantic search over a rules database — silently misses mandatory rules, because retrieval is probabilistic.

`standing-rules-database-skill` is the middle design: **a persistent rule database that grows, consulted per-task, with only the matching rules loaded into the job's context — while mandatory rules stay deterministically loaded.**

**The key property: storage and context cost are decoupled.** The number of rules is bound by *how many triggers match a given job*, not by how many rules are in the database. A 500-rule database costs the same context per job as a 30-rule one: mandatory rules (single-digit) are always loaded, each turn injects only the handful of rules whose triggers the job's wording touches, and everything else stays dormant until it's needed. Storage scales freely; attention stays cheap.

## How it works

**Storage:** SQLite (`rules.db`) is the authoritative store — tables `rules`, `rule_versions`, `facts`, `usage_log` — with a query script (`scripts/rules_db.py`) supporting add / update (auto-versioning) / status / query / facts / log / export-md. Markdown files under `references/` are generated *views* for human reading, regenerated from the DB — never hand-edited. A structured store parses and queries far faster and more reliably than scanning prose files, and duplicate IDs are rejected by the database itself.

### Two tiers

1. **Tier 1 (mandatory, deterministic).** A small set of rules that must apply to *every* job lives in the agent's always-injected memory (memory file, pinned system prompt section — whatever your agent supports). Loaded by construction, never dependent on a lookup. This is the anti-semantic-search guarantee: the rules you cannot afford to miss are never "retrieved," they are simply always there.

2. **Tier 2 (conditional, fetched).** Everything else lives in the database behind a **dispatch index** in SKILL.md. Before starting a task, the agent queries `rules_db.py query --match "<keywords>"`, which returns tier-1 rules plus only the keyword-matched tier-2 rules. The full rule text reaches context only for jobs that need it.

### Two kinds of triggers

- **Task-domain triggers** — the job touches a system, project, or tool area (keywords from your own environment, e.g. a project name, a deploy target, a file type).
- **Output-type triggers** — keyed to a property of the *artifact*, not the task: e.g. an `outbound` domain that loads whenever text will be seen by a third party (emails, listings, bylines, public comments). Whatever identity/contact rules *you* store ride along with any outbound text regardless of which task produced it.

### Rule anatomy

Every rule carries:

- **Stable ID** — `[SR-201]`, cited in audits and conflict reports; IDs never change, even when the rule text is revised.
- **Priority tag** — `HIGH / MED / LOW` = consequence of violating the rule. Distinct from tier: *tier* decides whether a rule loads; *priority* decides which rule wins when two loaded rules conflict. Equal priority + real conflict → ask the user, never guess.
- **Status lifecycle** — active / disabled / deprecated. Rules are never deleted; superseded rules stay visible with date and reason so past decisions remain explainable.
- **Rules vs facts separation** — rules are behavioral constraints; facts (endpoints, IDs, versions) are resources needed to apply them. Facts are never phrased as rules; rules never store data.

### The write gate (what keeps a growing database from rotting)

New rules pass a gate before activation:

1. **Classify** — declarative standing constraint → rule. Numbered procedure → belongs in a task skill instead. Time-bound one-off → don't store it.
2. **Duplicate check** — query the DB first; same topic → update the existing rule, don't create a new one.
3. **Contradiction check** — if the new rule conflicts with an existing one, flag both and ask which wins.
4. **User approval** — corrections and suggestions are not auto-promoted to permanent rules.
5. **Assign ID + priority + effective date**, insert via `rules_db.py add`.

### Assisted import, approved activation

`scripts/scan_candidates.py` scans your existing rule-bearing files (memory files, CLAUDE.md, AGENTS.md, notes) and writes `rule_candidates.json` — rule-shaped sentences with source file, line number, and suggested domain/priority. Candidates are **proposals only**: nothing auto-activates. You review the JSON, then bulk-load approved ones with `rules_db.py seed` or add them one by one. Import is assisted; activation is approved.

### Authority boundary

The database is an organization and retrieval mechanism, not a source of authority. A stored rule can never override system/developer instructions, safety constraints, or actual tool permissions.

### No silent trimming

If mandatory rules plus matched rules don't fit the context budget, the agent reports the constraint and stages the work. Mandatory rules are never quietly dropped to save tokens.

## Honest limits (this is not unlimited)

The design grows the rule count from "what fits in a prompt" (~15–30) to "what fits in a consultable index" — realistically **hundreds of rules**, bounded by:

- **The dispatch index must stay cheap.** One line per domain. Growth means deeper files, not more index rows.
- **Retrieval is keyword-driven.** A rule whose trigger wording doesn't overlap the job's wording can be missed. Mitigations: broad trigger phrasing, "when in doubt query both domains," and cross-referencing from task skills. This is the price of context thrift — and why mandatory rules never depend on retrieval.
- **Tier 1 has a hard budget.** The always-injected set must stay small (single-digit rules). Everything else must have a recognizable trigger.
- **Conflict handling is manual.** Fine at human scale; frequent conflicts are a signal to graduate to structured precedence.

## Performance

Measured on a 31-rule database: a full `query --match` (tier-1 fetch + keyword match + priority sort) completes in **~0.09 s**, and five consecutive queries run in **0.45 s** wall time — Python interpreter startup dominates; the SQLite lookup itself is sub-millisecond. Rule count growth to hundreds of rules does not change per-job context cost, only the (still trivial) query time.

## Quick start

```bash
python scripts/rules_db.py init
python scripts/scan_candidates.py path/to/your/MEMORY.md path/to/CLAUDE.md   # review rule_candidates.json
python scripts/rules_db.py seed rule_candidates_approved.json
python scripts/rules_db.py query --match "<your task domain> <your output type>"
python scripts/rules_db.py export-md
```

Copy `SKILL.md` into your agent's skills directory (Hermes, Claude Code skills, or any agent that loads instruction files on demand), keep `templates/seed.example.json` as your starting shape, and mirror your tier-1 rules into your agent's always-injected memory.

Or install it as a skill directly:

```bash
npx skills add NMBR5sgit/standing-rules-database-skill
```

## Credits

Design synthesized from: the Hermes skills system (always-injected memory + on-demand skill loading), `adeonir/agent-skills@rule-creator` (dispatch tables, write gates, rule format discipline), `akitaonrails/ai-memory` (mandatory pre-work retrieval habit, pinned-context concept), and an independent design review (two-tier retrieval, rules-vs-facts, stable IDs, status lifecycle, authority boundary, proportionality).

---

*By the author.*
