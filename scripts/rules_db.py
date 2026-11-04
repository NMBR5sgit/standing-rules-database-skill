#!/usr/bin/env python3
"""standing-rules query engine.

SQLite (rules.db in the skill dir) is the AUTHORITATIVE store.
The markdown files under references/ are generated views (export-md).

Commands:
  init                       create schema
  seed <seed.json>           bulk-load rules + facts (idempotent upsert)
  add --id --name --text [--summary --tier --domain --priority --tags]  gate first!
  update <id> --text --reason [--by]   auto-versions the old text
  status <id> <disabled|deprecated|active> --reason
  query --match "kw kw ..." [--json]   tier-1 always + tier-2 keyword matches
  facts [--domain X]                   facts for a domain
  log --task --rule-ids --method --reason   audit entry
  export-md                  regenerate references/*.md from the DB
"""
import argparse, json, sqlite3, sys, datetime, pathlib

SKILL_DIR = pathlib.Path(__file__).resolve().parent.parent
DB_PATH = SKILL_DIR / "rules.db"
REF_DIR = SKILL_DIR / "references"


def now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M")


def connect():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=5000")
    con.execute("PRAGMA synchronous=NORMAL")
    return con


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
    """)
    con.commit()


def upsert_rule(con, r):
    cur = con.execute("SELECT 1 FROM rules WHERE rule_id=?", (r["id"],))
    if cur.fetchone():
        con.execute(
            "UPDATE rules SET name=?,text=?,summary=?,tier=?,domain=?,priority=?,tags=?,updated_at=? WHERE rule_id=?",
            (r["name"], r["text"], r.get("summary"), r.get("tier", 2), r.get("domain"),
             r.get("priority", "MED"), r.get("tags"), now(), r["id"]))
    else:
        con.execute(
            "INSERT INTO rules(rule_id,name,text,summary,tier,domain,priority,status,tags,created_at,updated_at,effective_from) VALUES(?,?,?,?,?,?,?,'active',?,?,?,?)",
            (r["id"], r["name"], r["text"], r.get("summary"), r.get("tier", 2), r.get("domain"),
             r.get("priority", "MED"), r.get("tags"), now(), now(), r.get("effective_from", now())))


def cmd_seed(con, path):
    data = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    rules_count = len(data.get("rules", []))
    for r in data.get("rules", []):
        upsert_rule(con, r)
    facts_inserted = 0
    facts_skipped = 0
    for f in data.get("facts", []):
        exists = con.execute(
            "SELECT 1 FROM facts WHERE domain=? AND text=?",
            (f["domain"], f["text"])).fetchone()
        if exists:
            facts_skipped += 1
        else:
            con.execute("INSERT INTO facts(domain,text,created_at,updated_at) VALUES(?,?,?,?)",
                        (f["domain"], f["text"], now(), now()))
            facts_inserted += 1
    con.commit()
    print(f"seeded {rules_count} rules, inserted {facts_inserted} facts, "
          f"skipped {facts_skipped} duplicate facts")


def cmd_add(con, a):
    dup = con.execute("SELECT rule_id FROM rules WHERE rule_id=?", (a.id,)).fetchone()
    if dup:
        sys.exit(f"DUPLICATE: {a.id} already exists — use update instead")
    upsert_rule(con, {"id": a.id, "name": a.name, "text": a.text, "summary": a.summary,
                      "tier": a.tier, "domain": a.domain, "priority": a.priority, "tags": a.tags})
    con.commit()
    print(f"added {a.id}")


def cmd_update(con, a):
    row = con.execute("SELECT * FROM rules WHERE rule_id=?", (a.id,)).fetchone()
    if not row:
        sys.exit(f"not found: {a.id}")
    v = con.execute("SELECT COALESCE(MAX(version_number),0)+1 FROM rule_versions WHERE rule_id=?",
                    (a.id,)).fetchone()[0]
    con.execute("INSERT INTO rule_versions(rule_id,version_number,name,text,priority,status,change_reason,changed_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (a.id, v, row["name"], row["text"], row["priority"], row["status"], a.reason, a.by, now()))
    con.execute("UPDATE rules SET text=?, priority=?, updated_at=? WHERE rule_id=?",
                (a.text, a.priority or row["priority"], now(), a.id))
    con.commit()
    print(f"updated {a.id}; old text archived as version {v}")


def cmd_status(con, a):
    con.execute("UPDATE rules SET status=?, updated_at=? WHERE rule_id=?", (a.state, now(), a.id))
    con.commit()
    print(f"{a.id} -> {a.state} ({a.reason})")


def cmd_query(con, a):
    t1 = con.execute("SELECT * FROM rules WHERE tier=1 AND status='active' ORDER BY rule_id").fetchall()
    matched = []
    if a.match:
        kws = [k.lower() for k in a.match.split() if k]
        rows = con.execute("SELECT * FROM rules WHERE tier=2 AND status='active'").fetchall()
        for r in rows:
            hay = " ".join(str(r[c] or "") for c in ("rule_id", "name", "text", "summary", "tags", "domain")).lower()
            if any(k in hay for k in kws):
                matched.append(r)
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


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    s = sub.add_parser("seed"); s.add_argument("path")
    a = sub.add_parser("add"); a.add_argument("--id", required=True); a.add_argument("--name", required=True)
    a.add_argument("--text", required=True); a.add_argument("--summary"); a.add_argument("--tier", type=int, default=2)
    a.add_argument("--domain"); a.add_argument("--priority", default="MED"); a.add_argument("--tags")
    u = sub.add_parser("update"); u.add_argument("id"); u.add_argument("--text", required=True)
    u.add_argument("--reason", required=True); u.add_argument("--by", default="user")
    u.add_argument("--priority")
    st = sub.add_parser("status"); st.add_argument("id"); st.add_argument("state",
        choices=["active", "disabled", "deprecated"]); st.add_argument("--reason", default="")
    q = sub.add_parser("query"); q.add_argument("--match", default=""); q.add_argument("--json", action="store_true")
    f = sub.add_parser("facts"); f.add_argument("--domain")
    l = sub.add_parser("log"); l.add_argument("--task", required=True); l.add_argument("--rule-ids", required=True)
    l.add_argument("--method", default="keyword"); l.add_argument("--reason", default="")
    l.add_argument("--not-injected", action="store_true")
    sub.add_parser("export-md")
    args = p.parse_args()
    con = connect()
    init_db(con)
    {"init": lambda: print("schema ready"),
     "seed": lambda: cmd_seed(con, args.path),
     "add": lambda: cmd_add(con, args),
     "update": lambda: cmd_update(con, args),
     "status": lambda: cmd_status(con, args),
     "query": lambda: cmd_query(con, args),
     "facts": lambda: cmd_facts(con, args),
     "log": lambda: cmd_log(con, args),
     "export-md": lambda: cmd_export_md(con)}[args.cmd]()


if __name__ == "__main__":
    main()
