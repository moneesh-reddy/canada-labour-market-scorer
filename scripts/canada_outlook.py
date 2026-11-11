#!/usr/bin/env python3
"""Canadian outlook and pay for the occupations you target, live from Job Bank (ESDC).

The Canadian counterpart to market_snapshot.py's BLS columns. Occupations are NOC 2021
five-digit codes. Data: Job Bank 3-year employment outlooks and Job Bank wages, found
through the Open Government API on every run. No key, nothing cached, and it stops
with STOPPED: if a pull fails.

Examples
  python3 canada_outlook.py --find "business intelligence"          # look up NOC codes
  python3 canada_outlook.py --noc 20012 21223 --province ON --region Toronto
  python3 canada_outlook.py --noc 20012 --province BC --json outlook.json --md outlook.md
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from jobdata import canada as ca  # noqa: E402
from jobdata.net import FetchError  # noqa: E402


def report_md(rows: list[dict], province: str, region: str | None, sources: dict) -> str:
    where = f"{region}, {province}" if region else province
    L = [f"# Canadian outlook and pay ({where})", "",
         "| Occupation | NOC | Outlook | Period | Median wage | Yearly (approx.) | Wage period |",
         "|---|---|---|---|---|---|---|"]
    def rank(r):
        return r["outlook"].get("rank") or 9
    for r in sorted(rows, key=rank):
        o, w = r["outlook"], r["wages"]
        if w.get("median") is None:
            pay, yearly = "n/a", "n/a"
        elif w["unit"] == "hour":
            pay, yearly = f"${w['median']:,.2f}/h", f"${w['median_yearly']:,.0f}"
        else:
            pay = yearly = f"${w['median']:,.0f}/yr"
        L.append(f"| {o.get('title') or w.get('title') or ''} | {o['noc']} | {o.get('outlook') or 'n/a'} | "
                 f"{o.get('period') or ''} | {pay} | {yearly} | {w.get('period', '')} |")
    L += ["", f"Outlook scale: {', '.join(ca.OUTLOOK_SCALE)}. Hourly wages are shown yearly at {ca.HOURS_PER_YEAR} hours, as a guide only.",
          "", "Why each outlook was given (Job Bank's own words):", ""]
    for r in rows:
        o = r["outlook"]
        if o.get("trends"):
            L += [f"## {o.get('title')} (NOC {o['noc']})", "", o["trends"], "", f"Job Bank report: {o['url']}", ""]
    L += ["Sources (live):", *[f"- {k}: {v}" for k, v in sources.items()]]
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--find", help="search NOC occupation titles and exit")
    ap.add_argument("--noc", nargs="*", default=[], help="NOC 2021 codes, e.g. 20012 21211")
    ap.add_argument("--province", default="ON", help="two-letter province code (default ON)")
    ap.add_argument("--region", help="economic region within the province, e.g. Toronto, Ottawa, Lower Mainland")
    ap.add_argument("--json")
    ap.add_argument("--md")
    a = ap.parse_args(argv)
    try:
        jb = ca.JobBank()
        if a.find:
            hits = jb.search(a.find)
            for h in hits:
                print(f"{h['noc']}  {h['title']}  (match {h['match']})  {h['url']}")
            if not hits:
                print(f"No NOC title matches {a.find!r}. Try fewer or broader words.", file=sys.stderr)
            return 0
        if not a.noc:
            ap.error("give --noc or --find")
        rows = [{"outlook": jb.outlook(n, a.province, a.region), "wages": jb.wages(n, a.province, a.region)} for n in a.noc]
    except ValueError as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 2
    except FetchError as e:
        print(f"STOPPED: {e}", file=sys.stderr)
        return 3
    md = report_md(rows, a.province.upper(), a.region, jb.sources)
    if a.json:
        pathlib.Path(a.json).write_text(json.dumps({"province": a.province.upper(), "region": a.region,
                                                    "occupations": rows, "sources": jb.sources}, indent=2))
    if a.md:
        pathlib.Path(a.md).write_text(md)
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
