# Auto-consult hook — the contract (works with any agent)

Tier-2 rules only matter if they reliably reach the model. A SKILL.md line that says
"consult before any task" depends on the model remembering — under pressure it doesn't.
The **auto-consult hook** is what makes retrieval deterministic: a tiny script your
agent framework runs once per user turn, before the model call, which queries the
rules database and injects the matched rules into that turn's context.

This guide is deliberately framework-agnostic. Every capable agent framework has the
same four primitives under different names: a **turn-start trigger**, a way to **read
the user message**, a **context-injection channel**, and a **consent gate** for
user-supplied commands. Find those four in your framework and the hook writes itself.

Have your agent do it (it has the framework docs and can test), or follow it yourself.

## The contract (what a correct hook does)

1. **Trigger — once per user turn, before the model call.** Known names:
   Hermes `pre_llm_call`; Claude Code `UserPromptSubmit` (in `~/.claude/settings.json`);
   Gemini CLI / Codex-style: whatever the "before model" or "on prompt submit" event is.
   No trigger available → degraded mode (last section).

2. **Input — the user message text.** Usually JSON on stdin (Hermes:
   `extra.user_message`; Claude Code: `prompt`). Parse leniently; multimodal messages
   carry text inside part lists.

3. **Query — call the engine, never re-implement it.** Add `scripts/` to `sys.path`
   and import `query_rules` from `rules_db.py`. A hook with its own matcher copy
   drifts from the CLI and the two disagree about which rules apply. The repo's
   `tests/test_hook_consult.py` exists to keep that coupling honest.

4. **Selection — tier-2 only.** Tier-1 rules already live in always-injected memory;
   re-injecting them every turn duplicates them and breaks prompt caching. Cap
   presentation (e.g. ≤10 rules / ~8000 chars) and say so in the header line when the
   cap bites.

5. **Output — the framework's injected-context channel.**
   Hermes: `{"context": "..."}` on stdout.
   Claude Code: `{"hookSpecificOutput":{"hookEventName":"UserPromptSubmit","additionalContext":"..."}}`.
   Frameworks without a JSON channel: exit 2 + stderr where the framework feeds
   stderr back to the model; failing that, write a `rules-<turn>.md` file and have
   AGENTS.md/CLAUDE.md tell the agent to read it.

6. **Fault visibility — mandatory. A silently dead hook is worse than no hook**,
   because the whole two-tier design assumes rules are arriving. Three layers:
   - **(a) In-script self-report.** Never `except: pass`. Every error is appended to
     a rotating log next to the hook AND injected through the same context channel as
     `[standing-rules-hook FAULT] <what broke>` — so the breakage sits in the
     conversation every single turn until someone fixes it. Always exit 0: faults
     ride context, not exit codes. (Guard hooks that block irreversible actions are a
     different contract — those go fail-closed. An observer hook must never block a
     turn.)
   - **(b) Heartbeat.** Every invocation refreshes `standing_rules_hook.heartbeat`
     next to the script.
   - **(c) Dead-process detector.** A hook that *never runs* (killed, unregistered,
     lost after a restart, interpreter off PATH) cannot self-report. The CLI covers
     it: `rules_db.py query` warns on stderr when every known heartbeat is >36h
     stale. This layer exists because of a real incident: config.yaml looked perfect
     while the hook had actually been dead since the app's last restart.

7. **Consent gate — the most common silent killer.** A hook entry in a config file is
   NOT a running hook. Most frameworks require per-command approval before executing
   user-supplied hook commands (Hermes: the exact `(event, command)` pair must appear
   in `shell-hooks-allowlist.json` in the agent home, written by a first-run TTY
   approval; launch-time flags like `--accept-hooks` frequently do NOT survive
   restarts). Editing or reformatting the hook command string breaks the match and
   the hook silently stops firing. After any restart, upgrade, or hook edit,
   re-verify.

8. **Verification checklist — after install AND after every agent restart/upgrade:**
   - `echo '{"<sample payload>"}' | python <hook>` → context JSON with rules, or
     empty output + exit 0 when nothing matches.
   - `echo 'not-json' | python <hook>` → must print a FAULT line — never silence.
   - DB unreachable (env override → nonexistent path, with fallbacks disabled) →
     must print FAULT.
   - Heartbeat file exists and updates; the CLI staleness warning is gone.
   - Framework validator if it has one — e.g. `hermes hooks doctor` checks the
     allowlist and smoke-runs the script.
   - After a real restart: the heartbeat's mtime advanced on its own (live sessions
     refreshed it), which proves the trigger fires in production, not just in tests.

## Where files go (site-local, never shipped)

The hook script is a per-install artifact — deliberately NOT part of the skill
package — because it pins your paths and your framework dialect. Put it where your
framework expects user hooks (Hermes: `<agent-home>/agent-hooks/`; Claude Code:
`~/.claude/hooks/`), register it in the framework's hook config, and pass consent.
Keep the log and heartbeat files next to the script; the CLI's staleness check looks
for them there (add your install path to `HOOK_HEARTBEATS` in `scripts/rules_db.py`
if you use a new location).

## Skeleton (adapt names to your dialect)

```python
#!/usr/bin/env python3
import datetime, json, os, sqlite3, sys, traceback

HERE  = os.path.dirname(os.path.abspath(__file__))
SKILL = os.environ.get("STANDING_RULES_HOME") or "/path/to/your/skill/install"

def emit(text):                       # <- your framework's context channel
    print(json.dumps({"context": text}))          # Hermes form
    # Claude Code form instead:
    # print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
    #                                           "additionalContext": text}}))

def fault(msg, exc=None):                         # layer (a): visible failure
    with open(os.path.join(HERE, "standing_rules_hook.log"), "a", encoding="utf-8") as fh:
        fh.write(f"{datetime.datetime.now().astimezone().isoformat()} FAULT {msg}\n")
        if exc: fh.write(traceback.format_exc())
    emit(f"[standing-rules-hook FAULT] {msg} — rule auto-injection is NOT working.")
    sys.exit(0)                                    # faults ride context, not exit codes

try:
    payload = json.loads(sys.stdin.read())         # <- your framework's payload shape
    with open(os.path.join(HERE, "standing_rules_hook.heartbeat"), "w") as fh:
        fh.write(datetime.datetime.now().astimezone().isoformat())   # layer (b)
    msg = (payload.get("extra", {}).get("user_message")
           or payload.get("prompt") or "")
    if not msg: raise ValueError("no user message in payload")

    sys.path.insert(0, os.path.join(SKILL, "scripts"))
    from rules_db import query_rules               # single source of truth
    con = sqlite3.connect(os.path.join(SKILL, "rules.db"), timeout=2)
    con.row_factory = sqlite3.Row
    rows = [r for r in query_rules(con, msg) if r["tier"] == 2][:10]
    if rows:
        head = f"[standing-rules auto-consult · matched {len(rows)} rule(s)]"
        body = "\n".join(f"[{r['rule_id']} · {r['priority']}] {r['text']}" for r in rows)
        emit(head + "\n" + body)
except SystemExit:
    raise
except Exception as exc:
    fault(repr(exc), exc)
```

## Degraded mode (no hook trigger available)

Put near the top of the agent's always-loaded instructions (AGENTS.md / CLAUDE.md /
system prompt): *"Before starting any task, run
`python <skill>/scripts/rules_db.py query --match '<task domain> <output type>'` and
apply the returned rules."* This works anywhere shell commands run, but it is
model-dependent — a real trigger is strictly better. Watch for the tell: tasks that
start ignoring known rules are tasks where nobody consulted.

## Is it working right now?

`python scripts/rules_db.py query --match "ping"` — a `!! WARNING` about a
missing/stale heartbeat on stderr means the injection pipeline is DOWN. No warning
plus a recent heartbeat file plus the FAULT-self-test above passing = healthy.
