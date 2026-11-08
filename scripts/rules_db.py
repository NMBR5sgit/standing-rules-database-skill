#!/usr/bin/env python3
"""standing-rules query engine.

SQLite (rules.db in the skill dir) is the AUTHORITATIVE store.
The markdown files under references/ are generated views (export-md).

Commands:
  init                       create schema
  seed <seed.json>           bulk-load rules + facts (idempotent upsert)
  add --id --name --text [--summary --tier --domain --priority --tags --owner]  gate first!
  propose --id --name --text ...   add a rule in 'pending' (approval queue)
  update <id> --text --reason [--by]   auto-versions the old text
  status <id> <active|disabled|deprecated|pending|rejected> --reason
  query --match "kw kw ..." [--json]   tier-1 always + tier-2 keyword matches
  facts [--domain X]                   facts for a domain
  log --task --rule-ids --method --reason   audit entry
  export-md                  regenerate references/*.md from the DB
  export-index               regenerate references/_index.txt
  audit --format <csv|json> [--out]   export the audit ledger
  lockdown                   show the kill-switch (lockdown) state
"""
import argparse, json, re, sqlite3, sys, datetime, pathlib
import os
import threading
import urllib.request
import urllib.error

SKILL_DIR = pathlib.Path(__file__).resolve().parent.parent
DB_PATH = SKILL_DIR / "rules.db"
REF_DIR = SKILL_DIR / "references"


def now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M")


_TOKEN_RE = re.compile(r"[a-z0-9]+")
_NON_ASCII_RE = re.compile(r"[^\x00-\x7f]")


def _tokens(s: str) -> set:
    """Lowercase ASCII alphanumeric tokens. CJK/other scripts are dropped;
    queries containing them fall back to substring matching in cmd_query."""
    return set(_TOKEN_RE.findall(s.lower()))


def _stem(t: str) -> str:
    """Conservative plural strip: a trailing 's' on a word longer than 3 chars.
    Deliberately minimal — aggressive stemming introduces recall errors by
    collapsing distinct words. The prefix path in _match subsumes -ing/-ed/-es
    collisions; do not add a fuller stemmer."""
    return t[:-1] if len(t) > 3 and t.endswith("s") else t


def _stems(s: str) -> set:
    return {_stem(t) for t in _tokens(s)}


def rule_matches(query: str, row) -> bool:
    """Shared recall semantics for a rules-table row. THE single source of
    truth for matching — the CLI (cmd_query) and the auto-consult hook both
    call this. Do not re-implement matching elsewhere; local copies drift.
    Non-ASCII queries fall back to substring matching (no script-specific
    tokenizer without a dependency); ASCII queries use token-set matching."""
    if not query:
        return False
    fields = ("rule_id", "name", "text", "summary", "tags", "domain")
    hay = " ".join(str(row[c] or "") for c in fields if row[c] is not None)
    if _NON_ASCII_RE.search(query):
        kws = [k.lower() for k in query.split() if k]
        hay_l = hay.lower()
        return any(k in hay_l for k in kws)
    return _match(query, hay)


LOCKDOWN_RULE_ID = "SR-KILL"
LOCKDOWN_RULE_TEXT = "DENY ALL TASKS. Report to IT immediately."


def get_lockdown(con):
    """O(1) lookup: the lockdown lives in engine_state, not the rules table,
    so no rule command can delete or edit it (user decision, Phase 2)."""
    row = con.execute(
        "SELECT state_value FROM engine_state WHERE state_key='lockdown'").fetchone()
    if not row:
        return None
    try:
        state = json.loads(row["state_value"])
    except (ValueError, TypeError):
        return None
    return state if state.get("active") else None


def set_lockdown(con, payload: dict):
    """Apply the dashboard's Kill Switch payload (PRD §5) directly to
    engine_state. Never touches the rules table."""
    active = bool(payload.get("active"))
    state = {
        "active": active,
        "rule_text": payload.get("rule_text") or LOCKDOWN_RULE_TEXT,
        "reason": payload.get("reason", ""),
        "issued_by": payload.get("issued_by", "dashboard"),
        "issued_at": payload.get("issued_at") or now(),
    }
    con.execute(
        "INSERT INTO engine_state(state_key,state_value) VALUES('lockdown',?) "
        "ON CONFLICT(state_key) DO UPDATE SET state_value=excluded.state_value",
        (json.dumps(state, ensure_ascii=False),))
    _audit(con, state["issued_by"], "LOCKDOWN_ON" if active else "LOCKDOWN_OFF",
           None, None, state)
    return state


def query_rules(con, match_str: str) -> list:
    """Returns tier-1 rules plus tier-2 rules matching match_str.
    This is the single source of truth for query semantics.
    Both cmd_query and external consult hooks must use this.
    Tier-2 matches are ordered by priority (HIGH, MED, LOW).
    When the dashboard's Kill Switch is engaged (engine_state), the lockdown
    rule is returned FIRST and no tier-2 rules match — total freeze."""
    lockdown = get_lockdown(con)
    if lockdown:
        return [{"rule_id": LOCKDOWN_RULE_ID, "name": "KILL SWITCH",
                 "text": lockdown["rule_text"], "summary": None, "tier": 1,
                 "domain": "lockdown", "priority": "HIGH", "status": "active",
                 "owner": "org", "tags": "lockdown",
                 "created_at": lockdown["issued_at"], "updated_at": None,
                 "effective_from": None}]
    t1 = con.execute(
        "SELECT * FROM rules WHERE tier=1 AND status='active' ORDER BY rule_id"
    ).fetchall()
    matched = []
    if match_str:
        rows = con.execute(
            "SELECT * FROM rules WHERE tier=2 AND status='active'").fetchall()
        matched = [r for r in rows if rule_matches(match_str, r)]
        rank = {"HIGH": 0, "MED": 1, "LOW": 2}
        matched.sort(key=lambda r: rank.get(str(r["priority"] or "MED"), 1))
    return list(t1) + matched


def _match(query: str, hay: str) -> bool:
    """Token-set matcher: exact stem overlap, plus whole-token prefix
    containment (min length 3). Word-internal substrings (mail->email) are
    deliberately NOT matched — the old behavior was a false positive."""
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


def connect():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=5000")
    con.execute("PRAGMA synchronous=NORMAL")
    return con


SCHEMA_VERSION = 6   # v5: engine_state; v6: query_telemetry (batched analytics)


def init_db(con):
    con.executescript("""
    CREATE TABLE IF NOT EXISTS rules(
      rule_id TEXT PRIMARY KEY,
      name TEXT NOT NULL,
      text TEXT NOT NULL,
      summary TEXT,
      tier INTEGER NOT NULL DEFAULT 2,          -- 1 = mandatory/always, 2 = conditional
      domain TEXT,
      priority TEXT NOT NULL DEFAULT 'MED',     -- HIGH | MED | LOW
      status TEXT NOT NULL DEFAULT 'active',    -- active | disabled | deprecated
      owner TEXT NOT NULL DEFAULT 'local',      -- local | org (org-owned = immutable locally)
      tags TEXT,
      created_at TEXT, updated_at TEXT, effective_from TEXT
    );
    CREATE TABLE IF NOT EXISTS rule_versions(
      version_id INTEGER PRIMARY KEY AUTOINCREMENT,
      rule_id TEXT NOT NULL,
      version_number INTEGER NOT NULL,
      name TEXT, text TEXT, priority TEXT, status TEXT,
      change_reason TEXT, changed_by TEXT, created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS facts(
      fact_id INTEGER PRIMARY KEY AUTOINCREMENT,
      domain TEXT NOT NULL, text TEXT NOT NULL,
      created_at TEXT, updated_at TEXT
    );
    CREATE TABLE IF NOT EXISTS usage_log(
      log_id INTEGER PRIMARY KEY AUTOINCREMENT,
      task_desc TEXT, rule_ids TEXT, retrieval_method TEXT,
      reason TEXT, injected INTEGER DEFAULT 1, created_at TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_rules_domain ON rules(domain);
    CREATE INDEX IF NOT EXISTS idx_rules_status ON rules(status);
    CREATE TABLE IF NOT EXISTS query_telemetry(
      q_id INTEGER PRIMARY KEY AUTOINCREMENT,
      ts TEXT NOT NULL, match_str TEXT NOT NULL,
      matched_ids TEXT NOT NULL, match_count INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS schema_version(v INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS conflict_log(
      conflict_id INTEGER PRIMARY KEY AUTOINCREMENT,
      rule_id TEXT, action TEXT, attempted_by TEXT,
      detail TEXT, created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS audit_log(
      audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
      ts TEXT NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL,
      rule_id TEXT, before_state TEXT, after_state TEXT
    );
    CREATE TABLE IF NOT EXISTS engine_state(
      state_key TEXT PRIMARY KEY,
      state_value TEXT NOT NULL
    );
    """)
    con.commit()
    _migrate(con)


def _migrate(con):
    # MAX(v) rather than LIMIT 1: a stale row (e.g. v=0 left behind by a manual
    # UPDATE or a partially applied migration) must not cause the effective
    # version to regress. The highest recorded version is the current one.
    row = con.execute("SELECT MAX(v) FROM schema_version").fetchone()
    current = row[0] if row and row[0] is not None else 0
    if current >= SCHEMA_VERSION:
        return
    # Future migrations go here, guarded by PRAGMA table_info checks.
    if current < 2:
        cols = {r["name"] for r in con.execute("PRAGMA table_info(facts)")}
        if "fact_key" not in cols:
            con.execute("ALTER TABLE facts ADD COLUMN fact_key TEXT")
        if "fact_value" not in cols:
            con.execute("ALTER TABLE facts ADD COLUMN fact_value TEXT")
    if current < 3:
        cols = {r["name"] for r in con.execute("PRAGMA table_info(rules)")}
        if "owner" not in cols:
            con.execute("ALTER TABLE rules ADD COLUMN owner TEXT NOT NULL "
                        "DEFAULT 'local'")
    # Persist the new version. Always UPDATE if a row exists, INSERT otherwise.
    if con.execute("SELECT 1 FROM schema_version LIMIT 1").fetchone():
        con.execute("UPDATE schema_version SET v=?", (SCHEMA_VERSION,))
    else:
        con.execute("INSERT INTO schema_version(v) VALUES (?)", (SCHEMA_VERSION,))
    con.commit()


def _sync_base_url() -> str:
    """STANDING_RULES_SYNC_URL is the server BASE url (e.g.
    https://api.example.com). The sync pull endpoint is appended internally.
    A URL that already ends in a known endpoint path is stripped so we never
    build double paths (https://api.example.com/sync/pull works too)."""
    url = os.environ.get("STANDING_RULES_SYNC_URL", "").rstrip("/")
    for suffix in ("/sync/pull", "/rules.json"):
        if url.endswith(suffix):
            url = url[: -len(suffix)].rstrip("/")
    return url


# Explicit UA on every outbound request: Cloudflare-protected license servers
# block the default "Python-urllib" UA with error 1010 (HTTP 403), which
# looks like a token failure. A named client UA passes the WAF.
USER_AGENT = ("StandingRulesEngine/1.0 "
              "(+https://github.com/NMBR5sgit/standing-rules-database-skill)")


def _fire_sync_webhook(payload: dict, wait: bool = False):
    """Fire-and-forget webhook to the Enterprise Dashboard if configured.
    This must NEVER block the CLI or raise an exception to the user.
    When STANDING_RULES_SYNC_URL is unset (the default for local-only users),
    this returns immediately: zero threads, zero network, zero overhead.
    The local DB remains the source of truth; a dropped packet is acceptable.
    The dashboard receives the same Bearer sync_token when configured.

    wait=True drains the sender for up to 3s before returning. Callers that
    sys.exit() straight after firing (policy-bypass tattletale) must use it:
    a daemon thread dies at interpreter shutdown, so the packet would never
    leave. The urlopen timeout is 2s, so the drain is still bounded."""
    base = _sync_base_url()
    if not base:
        return
    headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
    token = os.environ.get("STANDING_RULES_SYNC_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    def _send():
        try:
            req = urllib.request.Request(
                base.rstrip("/") + "/events",
                data=json.dumps(payload).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            # 2-second timeout max. If the dashboard is slow, we drop the packet.
            urllib.request.urlopen(req, timeout=2)
        except Exception:
            pass  # Fail silently. The local DB is the source of truth.

    # Run in a daemon thread so the CLI exits immediately
    t = threading.Thread(target=_send)
    t.daemon = True
    t.start()
    if wait:
        t.join(timeout=3)  # caller is about to sys.exit(); let the packet leave


def _write_event(event: str, rule_id, con, extra=None):
    """Build the webhook payload after a successful local write."""
    data = None
    if rule_id is not None:
        row = con.execute("SELECT * FROM rules WHERE rule_id=?", (rule_id,)).fetchone()
        data = dict(row) if row else None
    if extra:
        data = {**(data or {}), **extra}
    return {
        "event": event,
        "rule_id": rule_id,
        "data": data,
        "timestamp": datetime.datetime.now(datetime.timezone.utc)
                        .isoformat(timespec="seconds").replace("+00:00", "Z"),
        "source_machine": _source_machine(),
    }


def _reject_if_lockdown(con, action):
    """Kill Switch engaged: the local engine freezes. No rule writes except
    `sync` (which is how IT lifts the lockdown)."""
    if get_lockdown(con):
        sys.exit("ERROR: LOCKDOWN active — all rule changes are frozen. "
                 "Contact IT.")


def _reject_if_org_owned(con, rule_id, action, attempted_by):
    """Hard enforcement (Enterprise PRD §2.3): org-owned rules are immutable
    on the local machine — the dashboard is their source of truth.
    The rejection is NOT silent: it lands in conflict_log and fires a
    webhook so IT sees who is trying to bypass policy. Then the CLI exits."""
    row = con.execute("SELECT owner FROM rules WHERE rule_id=?", (rule_id,)).fetchone()
    if row and row["owner"] == "org":
        con.execute(
            "INSERT INTO conflict_log(rule_id,action,attempted_by,detail,created_at) "
            "VALUES(?,?,?,?,?)",
            (rule_id, action, attempted_by,
             "local CLI attempted to modify an org-owned rule", now()))
        con.commit()
        _fire_sync_webhook({
            "event": "policy_bypass_attempt",
            "rule_id": rule_id,
            "data": {"action": action, "attempted_by": attempted_by},
            "timestamp": datetime.datetime.now(datetime.timezone.utc)
                            .isoformat(timespec="seconds").replace("+00:00", "Z"),
            "source_machine": _source_machine(),
        }, wait=True)  # we sys.exit() next; daemon threads die at exit
        sys.exit(f"ERROR: Cannot modify org-owned rule {rule_id}. Contact IT.")


def _source_machine():
    try:
        return os.environ.get("COMPUTERNAME") or os.uname().nodename
    except AttributeError:
        return "unknown"


def _state_blob(row):
    """Compact before/after snapshot for the audit ledger (PRD §3.1)."""
    if row is None:
        return None
    d = dict(row)
    keys = ("text", "priority", "status", "tier", "owner", "domain")
    return json.dumps({k: d[k] for k in keys if d.get(k) is not None},
                      ensure_ascii=False)


def _audit(con, actor, action, rule_id, before, after):
    """Immutable ledger entry: [ts][actor][action][rule_id][before][after]."""
    con.execute(
        "INSERT INTO audit_log(ts,actor,action,rule_id,before_state,after_state) "
        "VALUES(?,?,?,?,?,?)",
        (now(), actor, action, rule_id, _state_blob(before), _state_blob(after)))


def upsert_rule(con, r):
    cur = con.execute("SELECT owner FROM rules WHERE rule_id=?", (r["id"],))
    row = cur.fetchone()
    if row:
        if row[0] == "org" and not r.get("_org_write"):
            # Org-owned rows are immutable locally; seed/add must not
            # silently overwrite a rule the dashboard owns.
            return False
        con.execute(
            "UPDATE rules SET name=?,text=?,summary=?,tier=?,domain=?,priority=?,tags=?,owner=?,updated_at=? WHERE rule_id=?",
            (r["name"], r["text"], r.get("summary"), r.get("tier", 2), r.get("domain"),
             r.get("priority", "MED"), r.get("tags"), r.get("owner", "local"), now(), r["id"]))
    else:
        con.execute(
            "INSERT INTO rules(rule_id,name,text,summary,tier,domain,priority,status,tags,owner,created_at,updated_at,effective_from) VALUES(?,?,?,?,?,?,?,'active',?,?,?,?,?)",
            (r["id"], r["name"], r["text"], r.get("summary"), r.get("tier", 2), r.get("domain"),
             r.get("priority", "MED"), r.get("tags"), r.get("owner", "local"), now(), now(), r.get("effective_from", now())))
    return True


def cmd_seed(con, path):
    _reject_if_lockdown(con, "seed")
    data = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    rules_count = len(data.get("rules", []))
    rules_skipped = 0
    for r in data.get("rules", []):
        before = con.execute("SELECT * FROM rules WHERE rule_id=?", (r["id"],)).fetchone()
        if upsert_rule(con, r):
            after = con.execute("SELECT * FROM rules WHERE rule_id=?", (r["id"],)).fetchone()
            _audit(con, "seed", "SEED", r["id"], before, after)
        else:
            rules_skipped += 1
    facts_inserted = 0
    facts_skipped = 0
    for f in data.get("facts", []):
        exists = con.execute(
            "SELECT 1 FROM facts WHERE domain=? AND text=?",
            (f["domain"], f["text"])).fetchone()
        if exists:
            facts_skipped += 1
        else:
            key = f.get("key")
            value = f.get("value")
            con.execute(
                "INSERT INTO facts(domain,text,fact_key,fact_value,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?)",
                (f["domain"], f["text"], key, value, now(), now()),
            )
            facts_inserted += 1
    con.commit()
    print(f"seeded {rules_count} rules, inserted {facts_inserted} facts, "
          f"skipped {facts_skipped} duplicate facts"
          + (f", {rules_skipped} org-owned rules left untouched" if rules_skipped else ""))
    # Seed is bulk: one aggregate event instead of one webhook per rule
    # (a 5000-candidate seed must not spawn a thread storm).
    _fire_sync_webhook(_write_event(
        "seed_applied", None, con,
        extra={"rule_ids": [r["id"] for r in data.get("rules", [])],
               "rules_count": rules_count,
               "facts_inserted": facts_inserted}))


def cmd_add(con, a):
    _reject_if_lockdown(con, "add")
    dup = con.execute("SELECT rule_id FROM rules WHERE rule_id=?", (a.id,)).fetchone()
    if dup:
        sys.exit(f"DUPLICATE: {a.id} already exists — use update instead")
    proposed = getattr(a, "status", "active") == "pending"
    upsert_rule(con, {"id": a.id, "name": a.name, "text": a.text, "summary": a.summary,
                      "tier": a.tier, "domain": a.domain, "priority": a.priority,
                      "tags": a.tags, "owner": getattr(a, "owner", "local")})
    if proposed:
        con.execute("UPDATE rules SET status='pending' WHERE rule_id=?", (a.id,))
    _audit(con, getattr(a, "by", "user"), "PROPOSED" if proposed else "ADDED",
           a.id, None, con.execute("SELECT * FROM rules WHERE rule_id=?", (a.id,)).fetchone())
    con.commit()
    event = "rule_proposed" if proposed else "rule_added"
    print(f"{'proposed' if proposed else 'added'} {a.id}")
    _fire_sync_webhook(_write_event(event, a.id, con))


def cmd_update(con, a):
    row = con.execute("SELECT * FROM rules WHERE rule_id=?", (a.id,)).fetchone()
    if not row:
        sys.exit(f"not found: {a.id}")
    _reject_if_lockdown(con, "update")
    _reject_if_org_owned(con, a.id, "update", a.by)
    v = con.execute("SELECT COALESCE(MAX(version_number),0)+1 FROM rule_versions WHERE rule_id=?",
                    (a.id,)).fetchone()[0]
    con.execute("INSERT INTO rule_versions(rule_id,version_number,name,text,priority,status,change_reason,changed_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (a.id, v, row["name"], row["text"], row["priority"], row["status"], a.reason, a.by, now()))
    con.execute("UPDATE rules SET text=?, priority=?, updated_at=? WHERE rule_id=?",
                (a.text, a.priority or row["priority"], now(), a.id))
    _audit(con, a.by, "UPDATED", a.id, row,
           con.execute("SELECT * FROM rules WHERE rule_id=?", (a.id,)).fetchone())
    con.commit()
    print(f"updated {a.id}; old text archived as version {v}")
    _fire_sync_webhook(_write_event("rule_updated", a.id, con))


def cmd_status(con, a):
    _reject_if_lockdown(con, "status")
    _reject_if_org_owned(con, a.id, "status", a.reason or "user")
    before = con.execute("SELECT * FROM rules WHERE rule_id=?", (a.id,)).fetchone()
    con.execute("UPDATE rules SET status=?, updated_at=? WHERE rule_id=?", (a.state, now(), a.id))
    _audit(con, a.reason or "user", "STATUS", a.id, before,
           con.execute("SELECT * FROM rules WHERE rule_id=?", (a.id,)).fetchone())
    con.commit()
    print(f"{a.id} -> {a.state} ({a.reason})")
    _fire_sync_webhook(_write_event("rule_status_changed", a.id, con))


def cmd_query(con, a):
    rows = query_rules(con, a.match)
    _record_query_telemetry(con, a.match, rows)
    t1 = [r for r in rows if r["tier"] == 1]
    matched = [r for r in rows if r["tier"] != 1]
    if a.json:
        out = {"mandatory": [dict(r) for r in t1], "matched": [dict(r) for r in matched],
               "queried_at": now()}
        print(json.dumps(out, ensure_ascii=False))
        return
    print(f"== MANDATORY tier-1 ({len(t1)}) ==")
    for r in t1:
        print(f"[{r['rule_id']} \u00b7 {r['priority']}] {r['text']}")
    print(f"== MATCHED ({len(matched)}) ==")
    for r in matched:
        print(f"[{r['rule_id']} \u00b7 {r['priority']} \u00b7 {r['domain']}] {r['text']}")


def cmd_facts(con, a):
    q = "SELECT * FROM facts" + (" WHERE domain=?" if a.domain else "") + " ORDER BY domain, fact_id"
    rows = con.execute(q, (a.domain,) if a.domain else ()).fetchall()
    for r in rows:
        if not a.verbose and r["fact_key"] and r["fact_value"]:
            print(f"[{r['domain']}] {r['fact_key']}={r['fact_value']}")
        else:
            print(f"[{r['domain']}] {r['text']}")


def cmd_log(con, a):
    con.execute("INSERT INTO usage_log(task_desc,rule_ids,retrieval_method,reason,injected,created_at) VALUES(?,?,?,?,?,?)",
                (a.task, a.rule_ids, a.method, a.reason, 0 if a.not_injected else 1, now()))
    con.commit()
    print("logged")


def cmd_export_md(con):
    REF_DIR.mkdir(exist_ok=True)
    domains = [d[0] for d in con.execute(
        "SELECT DISTINCT domain FROM rules WHERE domain IS NOT NULL AND domain!='core' ORDER BY domain")]
    for d in domains:
        rules = con.execute("SELECT * FROM rules WHERE domain=? ORDER BY CASE priority WHEN 'HIGH' THEN 0 WHEN 'MED' THEN 1 ELSE 2 END, rule_id", (d,)).fetchall()
        facts = con.execute("SELECT * FROM facts WHERE domain=? ORDER BY fact_id", (d,)).fetchall()
        lines = [f"# {d}\n", "## Rules"]
        for r in rules:
            st = "" if r["status"] == "active" else f" ({r['status']}: {r['updated_at']})"
            lines.append(f"- [{r['rule_id']} \u00b7 {r['priority']}] {r['text']}{st}")
        if not rules:
            lines.append("- (none yet)")
        lines += ["", "## Facts"]
        for f in facts:
            lines.append(f"- {f['text']}")
        if not facts:
            lines.append("- (none yet)")
        (REF_DIR / f"{d}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"exported {len(domains)} domain views")


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
        # Also include words from the domain itself so
        # "web-delivery" contributes "web" and "delivery".
        for word in re.split(r"[^a-z0-9]+", d.lower()):
            if word:
                tags.add(word)
        lines.append(f"{d}: {' '.join(sorted(tags))}")
    (REF_DIR / "_index.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"exported index for {len(rows)} domains")


def cmd_audit(con, a):
    """Export the audit ledger (PRD §3.1) as CSV or JSON for the dashboard."""
    rows = con.execute(
        "SELECT ts,actor,action,rule_id,before_state,after_state "
        "FROM audit_log ORDER BY audit_id").fetchall()
    fmt = a.format
    if a.out:
        out_path = pathlib.Path(a.out)
    else:
        out_path = REF_DIR / f"audit_log.{fmt}"
        REF_DIR.mkdir(exist_ok=True)
    if fmt == "csv":
        buf = ["ts,actor,action,rule_id,before_state,after_state"]
        for r in rows:
            cols = []
            for v in (r["ts"], r["actor"], r["action"], r["rule_id"],
                      r["before_state"], r["after_state"]):
                s = "" if v is None else str(v)
                cols.append('"' + s.replace('"', '""') + '"')
            buf.append(",".join(cols))
        out_path.write_text("\n".join(buf) + "\n", encoding="utf-8")
    else:
        payload = [dict(r) for r in rows]
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                            encoding="utf-8")
    print(f"audit: exported {len(rows)} entries -> {out_path}")


TELEMETRY_BATCH_SIZE = 20


def _record_query_telemetry(con, match_str, rows):
    """Batched query analytics (PRD §2.4 'which rules are actually being used').
    URL-gated: with STANDING_RULES_SYNC_URL unset this is a no-op — zero
    writes, zero threads, the hot query path stays untouched. With it set,
    each query appends one local row; every TELEMETRY_BATCH_SIZE queries are
    flushed to the dashboard as ONE webhook event (no per-query packets)."""
    if not os.environ.get("STANDING_RULES_SYNC_URL"):
        return
    ids = ",".join(r["rule_id"] for r in rows)
    con.execute(
        "INSERT INTO query_telemetry(ts,match_str,matched_ids,match_count) "
        "VALUES(?,?,?,?)", (now(), match_str, ids, len(rows)))
    con.commit()
    pending = con.execute("SELECT COUNT(*) FROM query_telemetry").fetchone()[0]
    if pending < TELEMETRY_BATCH_SIZE:
        return
    batch = con.execute(
        "SELECT q_id,ts,match_str,matched_ids,match_count FROM query_telemetry "
        "ORDER BY q_id LIMIT ?", (TELEMETRY_BATCH_SIZE,)).fetchall()
    con.execute(
        "DELETE FROM query_telemetry WHERE q_id<=?", (batch[-1]["q_id"],))
    con.commit()
    _fire_sync_webhook({
        "event": "query_telemetry_batch",
        "rule_id": None,
        "data": {"queries": [dict(b) for b in batch], "batch_size": len(batch)},
        "timestamp": datetime.datetime.now(datetime.timezone.utc)
                        .isoformat(timespec="seconds").replace("+00:00", "Z"),
        "source_machine": _source_machine(),
    })


def cmd_sync(con, a):
    """Pull the organization's master rules from the license server and
    upsert them locally. The local DB stays the source of truth; the server
    is a sync target, not an API the agent depends on.
    STANDING_RULES_SYNC_URL is the base URL; /sync/pull is appended
    internally. STANDING_RULES_SYNC_TOKEN authenticates the pull."""
    url = os.environ.get("STANDING_RULES_SYNC_URL")
    if not url:
        sys.exit("ERROR: STANDING_RULES_SYNC_URL is not set. Sync is disabled.")
    token = os.environ.get("STANDING_RULES_SYNC_TOKEN")
    if not token:
        sys.exit("ERROR: SYNC_URL is set, but SYNC_TOKEN is missing.")
    fetch_url = _sync_base_url() + "/sync/pull"
    # Optional corporate domain for key-sharing detection (paid tenants only).
    # Normalize: lowercase + strip trailing dot (legal in DNS, but the dashboard
    # stores the domain without it). Defense in depth: both sides normalize.
    corporate_domain = os.environ.get("STANDING_RULES_CORPORATE_DOMAIN")
    if corporate_domain:
        corporate_domain = corporate_domain.lower().strip()
        if corporate_domain.endswith('.'):
            corporate_domain = corporate_domain[:-1]
    # Machine ID for seat enforcement: stable across runs on this machine
    # Uses hostname + user hash as a stable identifier (no PII)
    import hashlib
    import platform
    machine_id = f"{platform.node()}-{hashlib.sha256(platform.node().encode()).hexdigest()[:8]}"
    sync_headers = {"Authorization": f"Bearer {token}",
                    "User-Agent": USER_AGENT,
                    "X-Client-Machine": machine_id}
    if corporate_domain:
        sync_headers["X-Client-Domain"] = corporate_domain
    try:
        req = urllib.request.Request(
            fetch_url, method="GET",
            headers=sync_headers)
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 401:
            sys.exit("sync: license server rejected the sync token (401 "
                     "Unauthorized). Check STANDING_RULES_SYNC_TOKEN; "
                     "local rules.db remains the source of truth")
        if e.code == 403:
            # Seat limit exceeded — contact admin to add seats
            sys.exit(f"sync: seat limit exceeded (HTTP 403: {e.reason}); "
                     "contact your admin to add seats. "
                     "Local rules.db remains the source of truth")
        sys.exit(f"sync: dashboard fetch failed (HTTP {e.code}: {e.reason}); "
                 "local rules.db remains the source of truth")
    except Exception as e:
        sys.exit(f"sync: dashboard fetch failed ({type(e).__name__}: {e}); "
                 "local rules.db remains the source of truth")
    directives = {"wipe_org_rules"}
    is_directive = isinstance(body, dict) and body.get("action") in directives
    if not isinstance(body, dict) or (not isinstance(body.get("rules"), list)
                                      and not is_directive):
        sys.exit("sync: bad payload — expected {\"rules\": [...]} or a "
                 "directive action (e.g. wipe_org_rules); nothing applied")

    applied = 0
    skipped_local = 0
    org_ids = set()
    for r in body.get("rules", []):
        rid = r.get("id") or r.get("rule_id")
        if not rid:
            continue
        owner = r.get("owner", "org")
        if owner != "org":
            # The org master file must not clobber locally-owned rules
            # unless it explicitly marks them org-owned.
            skipped_local += 1
            continue
        org_ids.add(rid)
        before = con.execute("SELECT * FROM rules WHERE rule_id=?", (rid,)).fetchone()
        upsert_rule(con, {**r, "id": rid, "owner": "org", "_org_write": True})
        after = con.execute("SELECT * FROM rules WHERE rule_id=?", (rid,)).fetchone()
        _audit(con, "sync", "SYNC", rid, before, after)
        applied += 1

    # Revocation handler (PRD §3.3): the dashboard can send an explicit
    # {"status":"revoked","action":"wipe_org_rules"} directive — archive all
    # org-owned rules to rule_versions, then DELETE them. owner='local'
    # rules are never touched by a wipe.
    wipe_applied = body.get("action") == "wipe_org_rules"
    wipe_reason = body.get("reason", "wipe_org_rules")
    delinquency_stage = body.get("delinquency_stage")
    wiped = 0
    if wipe_applied:
        for row in con.execute("SELECT * FROM rules WHERE owner='org'").fetchall():
            v = con.execute(
                "SELECT COALESCE(MAX(version_number),0)+1 FROM rule_versions "
                "WHERE rule_id=?", (row["rule_id"],)).fetchone()[0]
            con.execute(
                "INSERT INTO rule_versions(rule_id,version_number,name,text,"
                "priority,status,change_reason,changed_by,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (row["rule_id"], v, row["name"], row["text"], row["priority"],
                 "revoked", wipe_reason, "sync", now()))
            con.execute("DELETE FROM rules WHERE rule_id=?", (row["rule_id"],))
            _audit(con, "sync", "WIPED", row["rule_id"], row, None)
            wiped += 1

    # Rules the org withdrew: deprecate the local copy (never delete —
    # version history stays queryable). Skipped during a wipe (already gone).
    # ALSO SKIPPED when the dashboard signals "scope_filtered": in that case,
    # rules not in the payload are merely scoped out (not visible from this
    # subdomain), NOT withdrawn by IT. Deprecating them would corrupt the
    # audit trail and churn on every subdomain switch.
    scope_filtered = body.get("scope_filtered", False)
    withdrawn = 0
    if not wipe_applied and not scope_filtered:
        for row in con.execute(
                "SELECT rule_id FROM rules WHERE owner='org' AND status='active'").fetchall():
            if row["rule_id"] not in org_ids:
                before = con.execute("SELECT * FROM rules WHERE rule_id=?",
                                     (row["rule_id"],)).fetchone()
                con.execute("UPDATE rules SET status='deprecated', updated_at=? WHERE rule_id=?",
                            (now(), row["rule_id"]))
                after = con.execute("SELECT * FROM rules WHERE rule_id=?",
                                    (row["rule_id"],)).fetchone()
                _audit(con, "sync", "WITHDRAWN", row["rule_id"], before, after)
                withdrawn += 1

    lockdown = body.get("lockdown")
    lockdown_note = ""
    if isinstance(lockdown, dict):
        state = set_lockdown(con, lockdown)
        lockdown_note = (f", lockdown={'ENGAGED' if state['active'] else 'lifted'}"
                         f" ({state['rule_text']})")

    con.commit()
    msg = f"sync: {applied} org rules applied"
    if wiped:
        msg += f", {wiped} org rules wiped"
        if delinquency_stage:
            msg += f" (delinquency: {delinquency_stage})"
    if withdrawn:
        msg += f", {withdrawn} withdrawn rules deprecated"
    if skipped_local:
        msg += f", {skipped_local} non-org entries skipped"
    msg += lockdown_note
    if body.get("warning"):
        msg += f" | WARNING: {body['warning']}"
    if body.get("delinquency_stage") and not wiped:
        msg += f" | DELINQUENCY: {body['delinquency_stage']}"
    print(msg)


def cmd_lockdown(con, a):
    """Inspect the kill-switch state locally (IT verification). Read-only."""
    state = get_lockdown(con)
    if state is None:
        print("lockdown: off (normal operation)")
    else:
        print(f"lockdown: ENGAGED — {state['rule_text']} "
              f"(issued {state['issued_at']} by {state['issued_by']}: {state['reason']})")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    s = sub.add_parser("seed"); s.add_argument("path")
    a = sub.add_parser("add"); a.add_argument("--id", required=True); a.add_argument("--name", required=True)
    a.add_argument("--text", required=True); a.add_argument("--summary"); a.add_argument("--tier", type=int, default=2)
    a.add_argument("--domain"); a.add_argument("--priority", default="MED"); a.add_argument("--tags")
    a.add_argument("--owner", choices=["local", "org"], default="local")
    a.set_defaults(status="active")
    pr = sub.add_parser("propose")  # approval-queue entry (Enterprise PRD §2.2)
    pr.add_argument("--id", required=True); pr.add_argument("--name", required=True)
    pr.add_argument("--text", required=True); pr.add_argument("--summary"); pr.add_argument("--tier", type=int, default=2)
    pr.add_argument("--domain"); pr.add_argument("--priority", default="MED"); pr.add_argument("--tags")
    pr.add_argument("--owner", choices=["local", "org"], default="local")
    pr.set_defaults(status="pending")
    u = sub.add_parser("update"); u.add_argument("id"); u.add_argument("--text", required=True)
    u.add_argument("--reason", required=True); u.add_argument("--by", default="user")
    u.add_argument("--priority")
    st = sub.add_parser("status"); st.add_argument("id"); st.add_argument("state",
        choices=["active", "disabled", "deprecated", "pending", "rejected"]); st.add_argument("--reason", default="")
    q = sub.add_parser("query"); q.add_argument("--match", default=""); q.add_argument("--json", action="store_true")
    f = sub.add_parser("facts"); f.add_argument("--domain"); f.add_argument("--verbose", action="store_true")
    l = sub.add_parser("log"); l.add_argument("--task", required=True); l.add_argument("--rule-ids", required=True)
    l.add_argument("--method", default="keyword"); l.add_argument("--reason", default="")
    l.add_argument("--not-injected", action="store_true")
    sub.add_parser("export-md")
    sub.add_parser("export-index")
    au = sub.add_parser("audit"); au.add_argument("--format", choices=["csv", "json"], default="csv"); au.add_argument("--out")
    sub.add_parser("lockdown")  # read the kill-switch state (PRD §5)
    s = sub.add_parser("sync")  # enterprise dashboard pull (skeleton; Phase 3)
    args = p.parse_args()
    con = connect()
    init_db(con)
    {"init": lambda: print("schema ready"),
     "seed": lambda: cmd_seed(con, args.path),
     "add": lambda: cmd_add(con, args),
     "propose": lambda: cmd_add(con, args),
     "update": lambda: cmd_update(con, args),
     "status": lambda: cmd_status(con, args),
     "query": lambda: cmd_query(con, args),
     "facts": lambda: cmd_facts(con, args),
     "log": lambda: cmd_log(con, args),
     "export-md": lambda: cmd_export_md(con),
     "export-index": lambda: cmd_export_index(con),
     "audit": lambda: cmd_audit(con, args),
     "lockdown": lambda: cmd_lockdown(con, args),
     "sync": lambda: cmd_sync(con, args)}[args.cmd]()


if __name__ == "__main__":
    main()
