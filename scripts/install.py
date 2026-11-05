#!/usr/bin/env python3
"""IT provisioner: one-line enterprise rollout.

Usage:
    python scripts/install.py --sync-url https://rules.example.com [--agent claude|cursor|hermes|aider]

Sets STANDING_RULES_SYNC_URL permanently for the machine user, initializes
the local rules DB, and runs the first sync pull. When --agent is omitted,
every installed agent (Hermes, Claude Code, Cursor, Aider) is detected and
reported; the persistent env var is user-global, so all of them pick it up.
Stdlib only — same zero-dependency contract as the engine itself.

STANDING_RULES_SYNC_TOKEN is honored when already present in the
environment, or supplied via --token (written persistently alongside the
URL so the next session can authenticate the pull).
"""
import argparse
import os
import pathlib
import shutil
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import rules_db  # noqa: E402  (same directory; stdlib-only engine)

AGENT_BINARIES = {"hermes": "hermes", "claude": "claude",
                  "cursor": "cursor", "aider": "aider"}


def detect_agents() -> list[str]:
    """Best-effort multi-agent detection from well-known CLI binaries on PATH.

    Returns EVERY installed agent, not the first one found — the persistent
    env var is user-global (setx / shell rc), so all detected agents pick it
    up after the next restart.
    """
    return [name for name, binary in AGENT_BINARIES.items()
            if shutil.which(binary)]


def append_rc_exports(home: pathlib.Path, pairs: list[tuple[str, str]]) -> pathlib.Path:
    """Append export lines to the user's shell rc file, skipping keys that
    are already exported. Returns the rc file actually written."""
    rc = home / ".zshrc" if ((home / ".zshrc").exists()
                             and not (home / ".bashrc").exists()) else home / ".bashrc"
    existing = rc.read_text(encoding="utf-8") if rc.exists() else ""
    with rc.open("a", encoding="utf-8") as fh:
        for key, value in pairs:
            if f"export {key}=" in existing:
                continue  # already provisioned; don't stack duplicates
            fh.write(f'\nexport {key}="{value}"\n')
    return rc


def persist_env(url: str, token: str | None = None) -> str:
    """Persist STANDING_RULES_SYNC_URL (+ optional token) for future sessions.

    Windows: setx (registry-backed user env var, visible on next restart).
    Unix/macOS: append an export line to the user's shell rc file.
    Returns a human-readable note about where it was stored.
    """
    pairs = [("STANDING_RULES_SYNC_URL", url)]
    if token:
        pairs.append(("STANDING_RULES_SYNC_TOKEN", token))
    if os.name == "nt":
        for key, value in pairs:
            os.system(f'setx {key} "{value}"')
        return "Windows user environment (setx)"

    rc = append_rc_exports(pathlib.Path.home(), pairs)
    return f"{rc.name} in {pathlib.Path.home()}"


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        prog="install.py",
        description="Provision this machine as a Standing Rules seat.")
    ap.add_argument("--sync-url", required=True,
                    help="License server base URL (e.g. https://rules.example.com)")
    ap.add_argument("--agent", choices=sorted(AGENT_BINARIES), default=None,
                    help="Agent this seat runs; auto-detected when omitted")
    ap.add_argument("--token", default=os.environ.get("STANDING_RULES_SYNC_TOKEN"),
                    help="Sync token from the dashboard tenant (or set "
                         "STANDING_RULES_SYNC_TOKEN in the environment)")
    args = ap.parse_args(argv)

    if args.agent:
        agents = [args.agent]
        print(f"install: agent={args.agent} (explicit --agent override)")
    else:
        agents = detect_agents()
        if agents:
            print(f"install: detected agents: {', '.join(agents)}")
        else:
            print("install: no agents detected (env var is agent-agnostic)")

    # Visible to this process too, before the persistent write takes effect
    os.environ["STANDING_RULES_SYNC_URL"] = args.sync_url
    if args.token:
        os.environ["STANDING_RULES_SYNC_TOKEN"] = args.token

    where = persist_env(args.sync_url, args.token)
    print(f"install: STANDING_RULES_SYNC_URL persisted to {where}")

    con = rules_db.connect()
    rules_db.init_db(con)
    print("install: local rules.db initialized")

    try:
        rules_db.cmd_sync(con, None)
    except SystemExit as exc:  # engine reports sync problems via sys.exit
        con.close()
        sys.exit(f"install: {exc}")
    con.close()

    if len(agents) > 1:
        print(f"Provisioning complete. Rules synced from {args.sync_url}. "
              f"Configured {len(agents)} agents ({', '.join(agents)}); each will "
              "pick up the environment variable on next restart.")
    else:
        print(f"Provisioning complete. Rules synced from {args.sync_url}. "
              "Agent will pick up the environment variable on next restart.")


if __name__ == "__main__":
    main()
