# standing-rules-database-skill — a growing standing-rules database for AI agents

Agents that follow standing rules usually inject every rule into every prompt. That works for ten rules and collapses at fifty: the rule set eats the context budget, and adding a rule means editing the permanent prompt. The opposite extreme — semantic search over a rules database — silently misses mandatory rules, because retrieval is probabilistic.

`standing-rules-database-skill` is the middle design: **a persistent rule database that grows, consulted per-task, with only the matching rules loaded into the job's context — while mandatory rules stay deterministically loaded.**

**The skill ships mechanism only — it contains no rules of the author's.** Every rule in your database comes from your own system: scanned from your memory files at bootstrap, approved by you, stored locally. The author's rules never ship with the package.

**The key property: storage and context cost are decoupled.** The number of rules is bound by *how many triggers match a given job*, not by how many rules are in the database. A 500-rule database costs the same context per job as a 30-rule one: mandatory rules (single-digit) are always loaded, each turn injects only the handful of rules whose triggers the job's wording touches, and everything else stays dormant until it's needed. Storage scales freely; attention stays cheap.

## How it works

**Storage:** SQLite (`rules.db`) is the authoritative store — tables `rules`, `rule_versions`, `facts`, `usage_log`, `schema_version` — with a query script (`scripts/rules_db.py`) supporting add / update (auto-versioning) / status / query / facts / log / export-md / export-index. Markdown files under `references/` are generated *views* for human reading, regenerated from the DB — never hand-edited. A structured store parses and queries far faster and more reliably than scanning prose files, and duplicate IDs are rejected by the database itself.

### Two tiers

1. **Tier 1 (mandatory, deterministic).** A small set of rules that must apply to *every* job lives in the agent's always-injected memory (memory file, pinned system prompt section — whatever your agent supports). Loaded by construction, never dependent on a lookup. This is the anti-semantic-search guarantee: the rules you cannot afford to miss are never "retrieved," they are simply always there.

2. **Tier 2 (conditional, fetched).** Everything else lives in the database behind a **dispatch index** in SKILL.md. Before starting a task, the agent queries `rules_db.py query --match "<keywords>"`, which returns tier-1 rules plus only the keyword-matched tier-2 rules. The full rule text reaches context only for jobs that need it.

**Single source of truth:** The matching logic is centralized in a single `query_rules` function. Both the manual CLI (`rules_db.py query`) and the automatic per-turn agent consult hook (`standing_rules_consult.py`) call this exact same function. This guarantees that the CLI and the live agent runtime never drift in their recall semantics.

### Two kinds of triggers

- **Task-domain triggers** — the job touches a system, project, or tool area (e.g. "scheduled delivery", "model swaps", "app packaging"). The domains are yours, derived from your own systems at bootstrap.
- **Output-type triggers** — keyed to a property of the *artifact*, not the task: e.g. an `outbound` domain that loads whenever text will be seen by a third party (emails, listings, bylines, public comments). Whatever identity/contact rules *you* store ride along with any outbound text, regardless of which task produced it.

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
- **Retrieval is keyword-driven.** The matcher uses token-set intersection and whole-token prefix containment (e.g. `deploy` matches `deployment`), but it does not understand semantics. A rule whose trigger wording doesn't overlap the job's wording can be missed. Mitigations: broad trigger phrasing, "when in doubt query both domains," and cross-referencing from task skills. This is the price of context thrift — and why mandatory rules never depend on retrieval.
- **Tier 1 has a hard budget.** The always-injected set must stay small (single-digit rules). Everything else must have a recognizable trigger.
- **Conflict handling is manual.** Fine at human scale; frequent conflicts are a signal to graduate to structured precedence.

## Measured Performance

Real-world measurements on a 38-rule database (7 tier-1, 31 tier-2) show the structural advantage of this design:

- **Broad queries** (e.g., "outbound email") load ~51% of the database, saving roughly half the context space.
- **Focused queries** (e.g., "deployment pipeline" or "gpu server") load ~147 words out of 925 total. Because the ~120-word tier-1 floor is mandatory, the *tier-2 payload* is only ~27 words (under 3% of the database), yielding an ~85% context reduction on the query-specific rules.
- **Empty/no-match queries** load only tier-1, yielding an **~87% context reduction**.

Query latency for the matching loop is **~0.39 ms** (average over 100 warm `query_rules` runs per query on the live database; per-query averages ranged 0.37–0.42 ms). Python interpreter startup dominates the CLI execution time. Rule count growth to hundreds of rules does not change per-job context cost, only the (still trivial) query time.

## Enterprise Sync (Optional)

The local engine runs independently. However, if you are using a centralized dashboard, you can set `STANDING_RULES_SYNC_URL` to connect it. The agent's query time remains ~0.39ms because reads are always local — the dashboard is a **sync target**, never an API the agent depends on.

`STANDING_RULES_SYNC_URL` is always the server **base URL** (e.g. `https://api.yourdomain.com`). The CLI appends the endpoint paths (`/sync/pull` for pulls, `/events` for webhooks) internally; a URL that already ends in `/sync/pull` is normalized so you never get double paths. When the URL is set, `STANDING_RULES_SYNC_TOKEN` is also required — the CLI sends it as `Authorization: Bearer <token>` on every pull and webhook, and a rejected token exits with a clear 401 message instead of a stack trace.

With the variable unset (the default), the engine makes zero network calls and spawns zero threads — writes complete exactly as in the local-only version. With it set, the following local-core features activate:

- **Write telemetry** — successful writes (`add`, `update`, `status`, `seed`, `propose`) fire a fire-and-forget webhook POST to the dashboard; capped at 2 seconds, fails silently, always after the local commit. The local `rules.db` remains the source of truth.
- **Org-owned rules** — rules carry an `owner` column (`local` | `org`). Any local attempt to `update` or `status`-change an org-owned rule hard-rejects with `ERROR: Cannot modify org-owned rule SR-XXX. Contact IT.`, logs the attempt to `conflict_log`, and fires a `policy_bypass_attempt` webhook so IT sees it.
- **Approval queue** — `propose` creates a rule in `pending` state; nothing about a pending rule is enforced until IT approves it to `active` (or rejects it). No auto-promotion.
- **Sync pull with revocation** — `rules_db.py sync` fetches `<base>/sync/pull` with the Bearer sync token, upserts org-owned rules locally (never clobbering local-owned rows), and revokes org rules missing from the master payload: they are snapshotted into `rule_versions` and `DELETE`d from `rules`. A `{"status":"revoked","action":"wipe_org_rules"}` payload wipes every org-owned rule and spares local ones. An unreachable server or a bad token exits clean with the local DB untouched.
- **Kill Switch lockdown** — the sync payload can engage a lockdown stored in a dedicated `engine_state` table (O(1) check, immune to rule CRUD). While engaged, queries return only the lockdown Tier-1 rule and every local write verb freezes; `sync` is the only way IT lifts it. Inspect state with `rules_db.py lockdown`.
- **Audit ledger** — every state change appends an immutable `audit_log` row with before/after snapshots; `rules_db.py audit --format csv|json` exports it for the dashboard.
- **Query analytics** — with the URL set, each query appends one local `query_telemetry` row; every 20 queries flush as a single batched webhook. Unset means zero rows, zero threads, zero network — the hot query path is untouched.

All webhook and sync paths are stdlib-only (`urllib`), fail-open, and require no server, queue, or third-party dependency.

## Quick start

The skill ships with **no rules of its own** — an empty database. At install, `scan_candidates.py` reads YOUR existing rule files (memory files, CLAUDE.md, AGENTS.md, notes) and proposes candidates; you approve what enters the database. Nothing from the author's system is pre-loaded.

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

## Using without Hermes

The core engine (`scripts/rules_db.py`) is standard-library Python and works with any agent that can run shell commands. The two-tier method — tier-1 mirrored into always-injected memory plus a SKILL.md dispatch index, queried before each task — is agent-agnostic. The Hermes auto-consult hook (`standing_rules_consult.py`) is optional and Hermes-specific; on other agents you follow the SKILL.md "consult before task" instruction manually, with identical `query_rules` semantics. Note that `SKILL.md` frontmatter uses a `hermes:` metadata key, so porting to Claude Code means adapting the frontmatter wrapper only — the mechanism is unchanged.

### Agent Compatibility

Because the core engine is standard-library Python, it is not tied to any specific agent framework. It works with any local agent or tool that can execute shell commands and read instruction files (like `SKILL.md`).

**Works out of the box:**
- Claude Code (CLI)
- Cursor / Windsurf (via terminal access)
- Aider
- Custom Python scripts, LangChain, AutoGen (by importing `query_rules` or calling the CLI)
- Standard cron jobs or shell scripts

**Does not work with:**
- Pure web-chat interfaces (ChatGPT, Claude Web) where local code cannot be executed. (In these cases, you would have to manually copy-paste the rule text).

## Credits

Design synthesized from: the Hermes skills system (always-injected memory + on-demand skill loading), `adeonir/agent-skills@rule-creator` (dispatch tables, write gates, rule format discipline), `akitaonrails/ai-memory` (mandatory pre-work retrieval habit, pinned-context concept), and an independent design review (two-tier retrieval, rules-vs-facts, stable IDs, status lifecycle, authority boundary, proportionality).

---

## Enterprise & Team Use

The core engine is MIT-licensed and free for everyone, including commercial use.
As organizations scale their AI agents, they often need centralized management: a shared team database, a web UI for IT to approve rules, and audit logs. A paid enterprise tier for these team-management features is on the roadmap.
If you are interested in early access or have questions about deploying this across a team, contact me at `98245427+NMBR5sgit@users.noreply.github.com` or [open an issue](https://github.com/NMBR5sgit/standing-rules-database-skill/issues).


*By Lee Weaver.*
