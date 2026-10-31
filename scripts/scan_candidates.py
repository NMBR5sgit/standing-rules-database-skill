#!/usr/bin/env python3
"""Bootstrap helper: scan existing rule sources and propose seed candidates.

Scans markdown/text files (agent memory files, CLAUDE.md, AGENTS.md, notes)
for lines that read like standing rules, and emits a JSON candidate list.
Nothing is written to the database: candidates go through human review,
then `rules_db.py seed` (or individual `add`) activates them.

Usage:
  python scan_candidates.py <file-or-dir> [...] [--out candidates.json]
"""
import json, re, sys, pathlib, argparse

RULE_SIGNAL = re.compile(
    r"(standing rule|always|never|must|only|prefer|do not|don't|intentional|forbidden)",
    re.IGNORECASE)
SKIP_START = re.compile(r"^\s*(#|```|\||\-\s*\[SR-|\s*$)")


def looks_like_rule(line: str) -> bool:
    s = line.strip().lstrip("-•* ").strip()
    if len(s) < 15 or SKIP_START.match(line):
        return False
    if not RULE_SIGNAL.search(s):
        return False
    # procedural: numbered steps or explicit ordering
    if re.match(r"^\d+[).]", s) or re.search(r"\bfirst\b.*\bthen\b", s, re.IGNORECASE):
        return False
    return True


def scan_file(p: pathlib.Path):
    out = []
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        print(f"skip {p}: {e}", file=sys.stderr)
        return out
    domain = p.stem.lower().replace(" ", "-")[:24]
    for i, line in enumerate(text.splitlines(), 1):
        if looks_like_rule(line):
            s = line.strip().lstrip("-•* ").strip()
            out.append({
                "candidate_id": f"CAND-{len(out)+1:03d}",
                "source": str(p), "line": i,
                "text": s,
                "suggested_domain": domain,
                "suggested_priority": "HIGH" if re.search(r"\b(never|always|must)\b", s, re.IGNORECASE) else "MED",
            })
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--out", default="rule_candidates.json")
    a = ap.parse_args()
    candidates = []
    for raw in a.paths:
        p = pathlib.Path(raw)
        if p.is_dir():
            for f in sorted(p.rglob("*.md")):
                candidates.extend(scan_file(f))
        elif p.is_file():
            candidates.extend(scan_file(p))
        else:
            print(f"not found: {p}", file=sys.stderr)
    # dedupe by normalized text
    seen, uniq = set(), []
    for c in candidates:
        k = re.sub(r"\W+", " ", c["text"].lower()).strip()
        if k not in seen:
            seen.add(k)
            uniq.append(c)
    pathlib.Path(a.out).write_text(json.dumps({"candidates": uniq, "review_required": True},
                                              ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{len(uniq)} candidate rules written to {a.out}")
    print("REVIEW REQUIRED: candidates are proposals, not active rules. "
          "Edit/curate the JSON, then activate via rules_db.py seed/add after user approval.")


if __name__ == "__main__":
    main()
