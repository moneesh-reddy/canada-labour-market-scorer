#!/usr/bin/env python3
"""Daily job-alert review: turn LinkedIn and Indeed job-alert EMAILS into one filtered, ranked list.

Reads alert emails you already received, from files on your own computer: single .eml files,
folders of them, or an .mbox export (for example Google Takeout). It never logs in to LinkedIn
or Indeed, never fetches their pages and never applies for you. Nothing leaves your machine.

  python3 job_alerts.py --filters ~/job-search/job-alert-filters.json \\
                        --seen ~/job-search/alerts-seen.csv --update-seen \\
                        --md ~/job-search/alerts-2026-11-14.md --csv ~/job-search/alerts-2026-11-14.csv \\
                        ~/job-search/inbox/

Steps: parse every alert -> merge the same job seen twice -> drop jobs already in --seen ->
apply your filters (pay, office days, location, title words) -> rank -> report.
Filters: copy templates/job-alert-filters.json and fill in your own values. Every filter is
off (null) until you set it. Keep the filters, the seen file and the reports outside any git
repository; they describe your job search.

Email layouts change without notice. The parser keys on job links (LinkedIn /jobs/view/<id>,
Indeed jk=<id>) and reads the text around each one, so a layout change can leave a field blank
but should not invent one. Pay and office days are only what the alert itself says.
"""

from __future__ import annotations

import argparse
import csv
import email
import email.policy
import html
import json
import mailbox
import pathlib
import re
import sys
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import screen_grade as sg  # noqa: E402

HOURS_PER_YEAR = 1950  # same yearly guide as the Canadian Job Bank tools

FILTER_KEYS = {
    "pay_floor", "keep_unknown_pay", "max_office_days", "high_pay_threshold",
    "high_pay_max_office_days", "keep_unknown_office_days", "preferred_locations",
    "allowed_locations", "remote_ok", "include_title_words", "exclude_title_words",
    "exclude_companies", "exclude_contract",
}

# ---------------------------------------------------------------- reading mail


def iter_messages(paths: list[str]):
    """Yield email.message.EmailMessage objects from .eml files, folders and .mbox files."""
    for raw in paths:
        p = pathlib.Path(raw).expanduser()
        if p.is_dir():
            for f in sorted(p.rglob("*")):
                if f.is_file() and f.suffix.lower() in (".eml", ".mbox"):
                    yield from iter_messages([str(f)])
        elif p.suffix.lower() == ".mbox":
            for m in mailbox.mbox(str(p)):
                yield email.message_from_bytes(m.as_bytes(), policy=email.policy.default)
        elif p.is_file():
            yield email.message_from_bytes(p.read_bytes(), policy=email.policy.default)
        else:
            raise FileNotFoundError(f"Not found: {p}")


def alert_source(msg) -> str | None:
    """'linkedin', 'indeed' or None, from the sender's domain."""
    sender = str(msg.get("From", "")).lower()
    m = re.search(r"@([a-z0-9.-]+)", sender)
    domain = m.group(1) if m else ""
    if domain == "linkedin.com" or domain.endswith(".linkedin.com"):
        return "linkedin"
    if domain == "indeed.com" or domain.endswith(".indeed.com"):
        return "indeed"
    return None


def bodies(msg) -> tuple[str, str]:
    """(html, plain) bodies of a message; either may be empty."""
    h = t = ""
    for part in msg.walk():
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
        try:
            content = part.get_content()
        except (LookupError, ValueError):
            raw = part.get_payload(decode=True) or b""
            content = raw.decode("utf-8", "replace")
        if not isinstance(content, str):
            continue
        if ctype == "text/html" and not h:
            h = content
        elif ctype == "text/plain" and not t:
            t = content
    return h, t


# ---------------------------------------------------------------- job links

_LI_JOB = re.compile(r"linkedin\.com/(?:comm/)?jobs/view/(?:[^/?#]*-)?(\d{6,})", re.I)


def job_key(url: str) -> tuple[str, str] | None:
    """(source, canonical link) for a job link, with tracking parameters dropped; else None."""
    url = html.unescape(url or "").strip()
    m = _LI_JOB.search(url)
    if m:
        return "linkedin", f"https://www.linkedin.com/jobs/view/{m.group(1)}/"
    u = urlparse(url)
    host = (u.hostname or "").lower()
    if host == "indeed.com" or host.endswith(".indeed.com"):
        jk = parse_qs(u.query).get("jk") or parse_qs(u.query).get("vjk")
        if jk and re.fullmatch(r"[0-9a-fA-F]{8,32}", jk[0]):
            return "indeed", f"https://{host}/viewjob?jk={jk[0].lower()}"
    return None


class _Tokens(HTMLParser):
    """Visible text chunks in order, each tagged with the job link it sits inside (if any)."""

    _SKIP = {"style", "script", "head", "title"}
    _BREAK = {"p", "div", "br", "tr", "td", "li", "table", "h1", "h2", "h3", "h4", "span"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[tuple[str, str | None]] = []  # (text, job link or None); "" marks a link start
        self._href: list[str | None] = []
        self._skip = 0
        self._buf = ""

    def _flush(self):
        text = " ".join(self._buf.split())
        if text:
            self.out.append((text, self._href[-1] if self._href else None))
        self._buf = ""

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip += 1
        elif tag == "a":
            self._flush()
            href = dict(attrs).get("href") or ""
            self._href.append(href)
            if job_key(href):
                self.out.append(("", href))
        elif tag in self._BREAK:
            self._flush()

    def handle_endtag(self, tag):
        if tag in self._SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag == "a":
            self._flush()
            if self._href:
                self._href.pop()
        elif tag in self._BREAK:
            self._flush()

    def handle_data(self, data):
        if not self._skip:
            self._buf += data


# Lines in alerts that are labels, not job facts.
_NOISE = re.compile(
    r"^(easy apply|apply now|actively recruiting|promoted|new|be an early applicant|view job|"
    r"see (all|more) jobs?|just posted|today|\d+ (hours?|days?) ago|responsive employer|"
    r"urgently hiring|\d+ (connections?|alumni|applicants?).*|.*school alum.*)$", re.I)


_FOOTER = re.compile(r"unsubscribe|you(?: are|'re) receiving|manage (?:your )?(?:job )?alerts|"
                     r"privacy policy|\u00a9|copyright|see all jobs", re.I)


def _job_blocks(tokens: list[tuple[str, str | None]]):
    """Group text after each job link until the next different job link."""
    blocks: dict[str, dict] = {}
    cur = None
    for text, href in tokens:
        key = job_key(href) if href else None
        if key and text == "":  # a job link starts: switch to that job's block
            cur = blocks.setdefault(key[1], {"source": key[0], "link": key[1], "title": "", "lines": []})
            continue
        if cur is None or not text:
            continue
        if key and key[1] == cur["link"]:
            if not cur["title"]:
                cur["title"] = text
            continue
        if _FOOTER.search(text):  # the email footer: no more job facts after this
            cur = None
            continue
        if href:  # text of some other link (unsubscribe, settings): not a job fact
            continue
        if len(cur["lines"]) < 6:  # the facts sit right under the title; footers come later
            cur["lines"].append(text)
    return list(blocks.values())


def parse_html(body: str, source: str) -> list[dict]:
    p = _Tokens()
    p.feed(body or "")
    p.close()
    jobs = []
    for b in _job_blocks(p.out):
        lines = [l for l in b["lines"] if not _NOISE.match(l)]
        if not b["title"] and lines:
            b["title"] = lines.pop(0)
        jobs.append(_fields(b["source"] or source, b["link"], b["title"], lines))
    return [j for j in jobs if j["title"]]


def parse_text(body: str, source: str) -> list[dict]:
    """Plain-text fallback: a job is the lines just above its link."""
    lines = [_squash(l) for l in (body or "").splitlines()]
    jobs, buf = [], []
    for line in lines:
        urls = re.findall(r"https?://\S+", line)
        key = next((job_key(u) for u in urls if job_key(u)), None)
        if key:
            text = [l for l in buf if l and not _NOISE.match(l) and not set(l) <= set("-=_* ")]
            if text:
                title, rest = text[-min(len(text), 4)], text[-min(len(text), 4) + 1:]
                jobs.append(_fields(key[0], key[1], title, rest))
            buf = []
        elif line:
            buf.append(line)
        else:
            buf.append("")
            buf = buf[-6:]
    return jobs


# ---------------------------------------------------------------- fields

_MONEY = r"(?:CA\$|C\$|CAD\s?\$?|US\$|\$)\s?(\d[\d,]*(?:\.\d+)?)\s?([kK])?"
_PAY = re.compile(_MONEY + r"(?:\s*(?:-|–|—|to)\s*" + _MONEY + r")?\s*(?:/|an?\s|per\s)?\s*(hour|hr|h|year|yr|annum|annually)?", re.I)
_DAYS_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5}
# Patterns below expect single spaces: every input goes through _squash first, which also
# keeps backtracking linear on hostile input (long runs of spaces).
_DAYS = re.compile(r"\b([1-5]|one|two|three|four|five) ?(?:days?|x)(?: (?:a|per) week| ?/ ?week| a week| per week)?"
                   r"(?: (?:in|at))?(?: the)? (?:office|on-?site)", re.I)


def _squash(text: str) -> str:
    return " ".join((text or "").split())


def parse_pay(text: str) -> dict | None:
    """{'low', 'high', 'unit', 'high_yearly'} from text like '$90K - $110K/yr' or '$45 - $55 an hour'."""
    m = _PAY.search(_squash(text)[:500])
    if not m:
        return None

    def num(v, k):
        n = float(v.replace(",", ""))
        return n * 1000 if k else n

    low = num(m.group(1), m.group(2))
    high = num(m.group(3), m.group(4)) if m.group(3) else low
    unit = (m.group(5) or "").lower()
    unit = "hour" if unit in ("hour", "hr", "h") else "year" if unit else ("hour" if high < 500 else "year")
    return {"low": low, "high": high, "unit": unit,
            "high_yearly": high * HOURS_PER_YEAR if unit == "hour" else high}


def parse_workplace(text: str) -> tuple[str, int | None]:
    """(workplace, office days per week) from alert text. Office days None when not stated."""
    t = _squash(text).lower()
    m = _DAYS.search(t)
    days = None
    if m:
        v = m.group(1)
        days = int(v) if v.isdigit() else _DAYS_WORDS[v]
    if re.search(r"\bremote\b", t) and not re.search(r"\bhybrid\b", t):
        return "remote", 0 if days is None else days
    if re.search(r"\bhybrid\b", t):
        return "hybrid", days
    if days is not None and days < 5:
        return "hybrid", days
    if re.search(r"\bon-?site\b|\bin[- ]office\b|\bin[- ]person\b", t):
        return "on-site", 5 if days is None else days
    return "", days


def _fields(source: str, link: str, title: str, lines: list[str]) -> dict:
    title = _squash(title)
    rest = [_squash(l)[:300] for l in lines]
    rest = [l for l in rest if l and l != title]
    pay = None
    for l in rest:
        pay = parse_pay(l)
        if pay:
            break
    joined = " | ".join(rest + [title])
    workplace, days = parse_workplace(joined)
    facts = [l for l in rest if not parse_pay(l) and not _DAYS.search(l)
             and l.strip("() ").lower() not in ("remote", "hybrid", "on-site", "onsite")]
    company = facts[0] if facts else ""
    location = facts[1] if len(facts) > 1 else ""
    # LinkedIn often writes "Company · City, ON (Hybrid)" on one line
    if "·" in company and not location:
        company, _, location = (x.strip() for x in company.partition("·"))
    location = re.sub(r"\s*\((remote|hybrid|on-?site)\)\s*$", "", location, flags=re.I).strip()
    if not location and workplace == "remote":
        location = "Remote"
    return {
        "source": source, "title": title, "company": company, "location": location,
        "workplace": workplace, "office_days": days,
        "pay": pay, "contract": bool(re.search(r"\b(contract|temporary|temp|fixed[- ]term)\b", joined, re.I)),
        "link": link,
    }


def parse_message(msg) -> list[dict]:
    source = alert_source(msg)
    if not source:
        return []
    h, t = bodies(msg)
    jobs = parse_html(h, source) if h else []
    return jobs or parse_text(t, source)


# ---------------------------------------------------------------- merge, dedupe, filter, rank


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def dedupe_key(job: dict) -> str:
    return f"{norm(job['company'])}|{norm(job['title'])}"


def merge(jobs: list[dict]) -> list[dict]:
    """One row per job: same link, or same company + title across LinkedIn and Indeed."""
    out: dict[str, dict] = {}
    by_link: dict[str, str] = {}
    for j in jobs:
        k = by_link.get(j["link"]) or (dedupe_key(j) if j["company"] else j["link"])
        if k in out:
            cur = out[k]
            for f, v in j.items():
                if cur.get(f) in ("", None, False) and v not in ("", None):
                    cur[f] = v
            if j["link"] not in cur["links"]:
                cur["links"].append(j["link"])
            if j["source"] not in cur["sources"]:
                cur["sources"].append(j["source"])
        else:
            out[k] = dict(j, links=[j["link"]], sources=[j["source"]])
        by_link[j["link"]] = k
    return list(out.values())


def load_seen(path: str | None) -> set[str]:
    if not path or not pathlib.Path(path).expanduser().exists():
        return set()
    with open(pathlib.Path(path).expanduser(), newline="", encoding="utf-8") as f:
        return {r["key"] for r in csv.DictReader(f) if r.get("key")}


def seen_keys(job: dict) -> list[str]:
    return job["links"] + ([dedupe_key(job)] if job["company"] else [])


def load_filters(path: str | None) -> dict:
    if not path:
        return {}
    data = json.loads(pathlib.Path(path).expanduser().read_text(encoding="utf-8"))
    flt = {k: v for k, v in data.items() if not k.startswith("_")}
    unknown = set(flt) - FILTER_KEYS
    if unknown:
        raise ValueError(f"Unknown filter keys: {', '.join(sorted(unknown))}. Valid: {', '.join(sorted(FILTER_KEYS))}")
    return flt


def _words(xs) -> list[str]:
    return [norm(x) for x in (xs or []) if norm(x)]


def _has(text: str, words: list[str]) -> bool:
    t = f" {norm(text)} "
    return any(f" {w} " in t for w in words)


def check(job: dict, flt: dict) -> tuple[bool, list[str]]:
    """(keep, reasons). Reasons explain a skip, or flag what the alert didn't say."""
    why, flags = [], []
    pay_top = job["pay"]["high_yearly"] if job["pay"] else None
    if flt.get("pay_floor") is not None:
        if pay_top is None:
            if flt.get("keep_unknown_pay", True):
                flags.append("pay not stated")
            else:
                why.append("pay not stated")
        elif pay_top < flt["pay_floor"]:
            why.append(f"pay tops out at {pay_top:,.0f}, below floor {flt['pay_floor']:,.0f}")
    if flt.get("max_office_days") is not None:
        limit = flt["max_office_days"]
        if (flt.get("high_pay_threshold") is not None and pay_top is not None
                and pay_top >= flt["high_pay_threshold"]):
            hp = flt.get("high_pay_max_office_days")
            limit = 5 if hp is None else hp
        d = job["office_days"]
        if d is None:
            if flt.get("keep_unknown_office_days", True):
                flags.append("office days not stated")
            else:
                why.append("office days not stated")
        elif d > limit:
            why.append(f"{d} office days a week, above {limit}")
    allowed = _words(flt.get("allowed_locations"))
    if allowed and not _has(job["location"], allowed):
        if not (flt.get("remote_ok", True) and job["workplace"] == "remote"):
            why.append(f"location '{job['location'] or 'not stated'}' not in allowed list")
    inc = _words(flt.get("include_title_words"))
    if inc and not _has(job["title"], inc):
        why.append("title has none of the include words")
    exc = _words(flt.get("exclude_title_words"))
    if exc and _has(job["title"], exc):
        why.append("title has an exclude word")
    if norm(job["company"]) in _words(flt.get("exclude_companies")):
        why.append("company excluded")
    if flt.get("exclude_contract") and job["contract"]:
        why.append("contract or temporary")
    return (not why), (why or flags)


def rank_key(job: dict, flt: dict):
    prefs = _words(flt.get("preferred_locations"))
    loc = next((i for i, p in enumerate(prefs) if _has(job["location"], [p])
                or (p == "remote" and job["workplace"] == "remote")), len(prefs))
    pay = job["pay"]["high_yearly"] if job["pay"] else -1
    return (loc, -pay, norm(job["company"]))


def review(jobs: list[dict], flt: dict, seen: set[str]):
    merged = merge(jobs)
    kept, skipped, repeats = [], [], []
    for j in merged:
        if any(k in seen for k in seen_keys(j)):
            repeats.append(j)
            continue
        ok, reasons = check(j, flt)
        j["notes"] = reasons
        (kept if ok else skipped).append(j)
    kept.sort(key=lambda j: rank_key(j, flt))
    return kept, skipped, repeats, len(merged)


# ---------------------------------------------------------------- output


def pay_text(p: dict | None) -> str:
    if not p:
        return "not stated"
    if p["unit"] == "hour":
        s = f"${p['low']:,.2f}" + (f"-${p['high']:,.2f}" if p["high"] != p["low"] else "")
        return f"{s}/h (~${p['high_yearly']:,.0f}/yr)"
    return f"${p['low']:,.0f}" + (f"-${p['high']:,.0f}" if p["high"] != p["low"] else "") + "/yr"


def days_text(j: dict) -> str:
    d = j["office_days"]
    w = j["workplace"] or "?"
    return f"{w}, {d} days" if d is not None and w != "remote" else (w if w != "?" else "not stated")


def _cell(v) -> str:
    return str(v).replace("|", "\\|").replace("[", "(").replace("]", ")").replace("\n", " ")


def report_md(kept, skipped, repeats, total, n_alerts) -> str:
    L = ["# Job alert review", "",
         f"{n_alerts} alert emails, {total} unique jobs: {len(kept)} kept, {len(skipped)} skipped, "
         f"{len(repeats)} already seen.", ""]
    if kept:
        L += ["| # | Role | Company | Location | Office | Pay | Seen on | Notes |", "|---|---|---|---|---|---|---|---|"]
        for i, j in enumerate(kept, 1):
            L.append(f"| {i} | [{_cell(j['title'])}]({j['links'][0]}) | {_cell(j['company'])} | {_cell(j['location'])} | "
                     f"{days_text(j)} | {pay_text(j['pay'])} | {', '.join(j['sources'])} | {_cell('; '.join(j['notes']))} |")
    else:
        L.append("Nothing passed the filters today.")
    if skipped:
        L += ["", "## Skipped", ""]
        L += [f"- {j['title']} ({j['company'] or 'company not stated'}): {'; '.join(j['notes'])}" for j in skipped]
    L += ["", "Pay and office days are only what the alert email says. Check the posting before you apply."]
    return "\n".join(L) + "\n"


CSV_COLS = ["title", "company", "location", "workplace", "office_days", "pay", "sources", "link", "notes"]


def write_csv(path: pathlib.Path, kept: list[dict]):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(CSV_COLS)
        for j in kept:
            w.writerow([j["title"], j["company"], j["location"], j["workplace"],
                        "" if j["office_days"] is None else j["office_days"], pay_text(j["pay"]),
                        " ".join(j["sources"]), j["links"][0], "; ".join(j["notes"])])


def update_seen(path: pathlib.Path, jobs: list[dict]):
    new = not path.exists()
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["key", "title", "company"])
        for j in jobs:
            for k in seen_keys(j):
                w.writerow([k, j["title"], j["company"]])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("inputs", nargs="+", help=".eml files, folders of .eml files, or .mbox exports")
    ap.add_argument("--filters", help="your copy of templates/job-alert-filters.json")
    ap.add_argument("--seen", help="CSV of jobs already reviewed (skipped as repeats)")
    ap.add_argument("--update-seen", action="store_true", help="add today's kept and skipped jobs to --seen")
    ap.add_argument("--md", help="write the Markdown report here (else print it)")
    ap.add_argument("--csv", help="write kept jobs as CSV here")
    ap.add_argument("--json", help="write kept jobs as JSON here")
    a = ap.parse_args(argv)

    outs = [pathlib.Path(x).expanduser().resolve() for x in (a.md, a.csv, a.json, a.seen) if x]
    if a.update_seen and not a.seen:
        print("REFUSED: --update-seen needs --seen", file=sys.stderr)
        return 2
    bad = [str(p) for p in outs if sg.in_git_repo(p)]
    if bad:
        print("REFUSED: these files describe your job search; keep them outside any git repository: "
              + ", ".join(bad), file=sys.stderr)
        return 2
    try:
        flt = load_filters(a.filters)
        msgs = list(iter_messages(a.inputs))
    except (OSError, ValueError) as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 2

    alerts = [m for m in msgs if alert_source(m)]
    jobs = [j for m in alerts for j in parse_message(m)]
    kept, skipped, repeats, total = review(jobs, flt, load_seen(a.seen))
    md = report_md(kept, skipped, repeats, total, len(alerts))
    if a.md:
        pathlib.Path(a.md).expanduser().write_text(md, encoding="utf-8")
    else:
        print(md)
    if a.csv:
        write_csv(pathlib.Path(a.csv).expanduser(), kept)
    if a.json:
        pathlib.Path(a.json).expanduser().write_text(json.dumps(kept, indent=2), encoding="utf-8")
    if a.update_seen:
        update_seen(pathlib.Path(a.seen).expanduser(), kept + skipped)
    if not alerts:
        print(f"No LinkedIn or Indeed alert emails found in {len(msgs)} messages.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
