#!/usr/bin/env python3
"""Keys scanner window — the local half of the standing-rules key panel.

Scan this machine for sync keys, see exactly where each one physically lives,
and delete a verified key from that place: one variable, one vault entry, or
one line in a file that keeps all its other settings. Masked findings can be
reported to the dashboard's Keys page, which can queue a delete back to this
window and revoke or rotate the key server-side.

Every button runs the tested `rules_db.py keys` verbs as a subprocess, so
what you click here is exactly what the CLI does. A full key is never shown
in this window and never leaves the machine.
"""
import json
import pathlib
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import messagebox, ttk

HERE = pathlib.Path(__file__).resolve().parent
ENGINE = HERE / "rules_db.py"


# ------------------------------------------------- data layer (no tkinter)

def run_keys(*args, timeout=120):
    """Run one `rules_db.py keys` verb. Returns (returncode, combined output)."""
    r = subprocess.run([sys.executable, str(ENGINE), "keys", *args],
                       capture_output=True, text=True, timeout=timeout)
    out = (r.stdout or "") + (r.stderr or "")
    return r.returncode, out.strip()


def scan():
    """Masked scan of this machine as a dict: {machine, base, candidates[]}."""
    code, out = run_keys("--json")
    if code != 0:
        raise ValueError(out or f"scan failed (exit {code})")
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        raise ValueError(f"scan returned no JSON: {out[:200]}")


def delete_key(source, force=False):
    args = ["--delete", source] + (["--force"] if force else [])
    return run_keys(*args)


def delete_warning(source, verdict):
    """Plain words for what a delete will actually touch — the whole point of
    this window is that it never empties your environment or drops a file
    that carries other information."""
    kind, _, target = source.partition(":")
    if kind == "env":
        what = (f"Remove the single environment variable {target} from this "
                f"machine. Every other variable stays exactly as it is.")
    elif kind == "vault":
        what = (f"Remove the single vault entry {target}. Every other secret "
                f"in your vault stays exactly as it is.")
    elif kind == "file":
        what = (f"Remove only the line(s) holding this key in {target}. "
                f"The file itself stays, with all its other settings; it is "
                f"deleted only if the key was its entire content.")
    else:
        what = f"Remove this key from {source}."
    note = ""
    if verdict == "revoked":
        note = ("\n\nThat tenant is revoked: the key still authenticates but "
                "no longer syncs any rules.")
    elif verdict == "reject":
        note = ("\n\nWarning: this key does not authenticate against the "
                "dashboard. Deleting an unverified key needs an explicit "
                "confirmation.")
    return f"{what}{note}\n\nDelete it?"


# ------------------------------------------------------------- window layer

class KeysWindow(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Standing-rules keys — this machine")
        self.geometry("1060x560")
        self.minsize(900, 420)
        self._q = queue.Queue()

        top = ttk.Frame(self, padding=(12, 10, 12, 4))
        top.pack(fill="x")
        ttk.Button(top, text="Scan this machine", command=self.on_scan).pack(side="left")
        ttk.Button(top, text="Report to dashboard", command=self.on_report).pack(side="left", padx=6)
        ttk.Button(top, text="Apply queued deletes", command=self.on_apply).pack(side="left")
        ttk.Button(top, text="Delete selected key here", command=self.on_delete).pack(side="right")
        self.status = ttk.Label(top, text="")
        self.status.pack(side="left", padx=12)

        self.tree = ttk.Treeview(
            self, columns=("masked", "verdict", "tenant", "source"),
            show="headings", selectmode="browse")
        for col, w in (("masked", 240), ("verdict", 90), ("tenant", 180), ("source", 420)):
            self.tree.heading(col, text=col.capitalize())
            self.tree.column(col, width=w, anchor="w")
        self.tree.pack(fill="both", expand=True, padx=12, pady=6)
        self.tree.bind("<Double-1>", lambda e: self.on_delete())

        self.log = tk.Text(self, height=9, wrap="word", font=("Consolas", 10))
        self.log.pack(fill="both", padx=12, pady=(0, 10))
        self._write("Scan first. Findings stay masked; the full key never leaves this machine.\n")
        self.after(150, self._poll)

    # ---- plumbing
    def _write(self, text):
        self.log.insert("end", text.rstrip() + "\n")
        self.log.see("end")

    def _spawn(self, fn):
        threading.Thread(target=lambda: self._q.put(fn()), daemon=True).start()

    def _poll(self):
        try:
            while True:
                kind, payload = self._q.get_nowait()
                if kind == "scan":
                    self._render_scan(payload)
                elif kind == "out":
                    code, text = payload
                    self._write(text if text else "(no output)")
                    if code != 0:
                        self._write(f"[exit {code}]")
                elif kind == "busy":
                    self.status.config(text=payload)
        except queue.Empty:
            pass
        self.after(150, self._poll)

    def _render_scan(self, data):
        self.tree.delete(*self.tree.get_children())
        for c in data.get("candidates") or []:
            self.tree.insert("", "end", values=(c["masked"], c["verdict"],
                                                c.get("tenant") or "?", c["source"]))
        n = len(data.get("candidates") or [])
        self.status.config(text=f"{n} key(s) on {data.get('machine')} → "
                               f"{data.get('base')}")

    # ---- actions
    def on_scan(self):
        self._spawn(self._do_scan)

    def _do_scan(self):
        self._q.put(("busy", "scanning…"))
        try:
            self._q.put(("scan", scan()))
        except ValueError as e:
            self._q.put(("out", (1, str(e))))

    def on_report(self):
        self._spawn(lambda: ("out", run_keys("--report")))

    def on_apply(self):
        self._spawn(lambda: ("out", run_keys("--apply")))
        self.after(2500, self.on_scan)      # show the machine as it now is

    def on_delete(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("Pick a key", "Select a key in the list first.")
            return
        masked, verdict, _tenant, source = self.tree.item(sel[0])["values"]
        if not messagebox.askyesno("Delete this key at its source?",
                                   delete_warning(source, verdict)):
            return
        force = verdict == "reject"
        self._spawn(lambda: ("out", delete_key(source, force=force)))
        self.after(2500, self.on_scan)


def main():
    if not ENGINE.exists():
        print(f"keys: cannot find the engine at {ENGINE}", file=sys.stderr)
        sys.exit(1)
    KeysWindow().mainloop()


if __name__ == "__main__":
    main()
