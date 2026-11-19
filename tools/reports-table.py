#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""The public table of charging reports: REPORTS.md, made from data/reports.json. No network, no model.

  tools/reports-table.py add REPORT.txt --issue N [--note TEXT]   add one report's summary line (maintainers)
  tools/reports-table.py render                                    rewrite REPORTS.md from data/reports.json
  tools/reports-table.py check                                     exit 1 if REPORTS.md is out of date (CI)

Each entry is the report's own `charge-watch-summary` line plus the issue number: pattern, SteamOS build,
charger (as the person wrote it), what it advertises, and the sleep result. Nothing else from the report.
"""
import argparse, json, os, sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import charge_watch as cw  # noqa: E402

DATA = os.path.join(HERE, "data", "reports.json")
OUT = os.path.join(HERE, "REPORTS.md")
REPO = "https://github.com/frame-commons/charge-watch"


def load():
    with open(DATA) as f:
        return json.load(f)


def cell(s):
    return str(s if s not in (None, "") else "–").replace("|", "\\|").replace("\n", " ")


def render(entries):
    out = ["# Charging reports", "",
           "Made by `tools/reports-table.py` from `data/reports.json`; don't edit this file by hand. Each row is one "
           f"[charging report]({REPO}/issues?q=label%3Areport) someone chose to share. Make your own with "
           "`./charge_watch.py report` (see the README).", "",
           "## Patterns so far", "", "| Pattern | Reports | Builds | What it means |", "|---|---|---|---|"]
    for pid, (title, explain) in cw.FINDINGS.items():
        rows = [e for e in entries if e.get("pattern") == pid]
        if not rows and pid in ("unplugged",):
            continue
        builds = sorted({e.get("build") or "?" for e in rows})
        out.append(f"| **{cell(title)}** | {len(rows)} | {cell(', '.join(builds)) if rows else '–'} | {cell(explain)} |")
    out += ["", "## Every report", ""]
    if not entries:
        out.append("None yet.")
    else:
        out += ["| Issue | Pattern | SteamOS build | Charger and cable | Charger offers | Asleep on the charger | Note |",
                "|---|---|---|---|---|---|---|"]
        for e in sorted(entries, key=lambda e: e.get("issue") or 0):
            pdos = ", ".join(f"{v:g} V" for v, _ in e.get("pdos") or []) or None
            sl = e.get("sleep") or {}
            sleep = None
            if sl.get("minutes"):
                sleep = f"{sl.get('soc_delta', '?'):+} % in {sl['minutes']} min" if isinstance(sl.get("soc_delta"), int) \
                    else f"? in {sl['minutes']} min"
            title = cw.FINDINGS.get(e.get("pattern"), (e.get("pattern"),))[0]
            out.append(f"| [#{e['issue']}]({REPO}/issues/{e['issue']}) | {cell(title)} | {cell(e.get('build'))} | "
                       f"{cell(e.get('charger'))} | {cell(pdos)} | {cell(sleep)} | {cell(e.get('note'))} |")
    return "\n".join(out) + "\n"


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    a_ = sub.add_parser("add"); a_.add_argument("report"); a_.add_argument("--issue", type=int, required=True)
    a_.add_argument("--note")
    sub.add_parser("render"); sub.add_parser("check")
    a = p.parse_args(argv)
    entries = load()
    if a.cmd == "add":
        e = cw.summary_line(a.report)
        e = {k: e.get(k) for k in ("pattern", "build", "version", "charger", "pdos", "sleep")}
        e["issue"] = a.issue
        if a.note:
            e["note"] = a.note
        entries = [x for x in entries if x.get("issue") != a.issue] + [e]
        with open(DATA, "w") as f:
            json.dump(entries, f, indent=1); f.write("\n")
    text = render(entries)
    if a.cmd == "check":
        try:
            with open(OUT) as f:
                same = f.read() == text
        except OSError:
            same = False
        if not same:
            print("REPORTS.md is out of date: run tools/reports-table.py render", file=sys.stderr)
        return 0 if same else 1
    with open(OUT, "w") as f:
        f.write(text)
    print(f"wrote {OUT} ({len(entries)} report(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main())
