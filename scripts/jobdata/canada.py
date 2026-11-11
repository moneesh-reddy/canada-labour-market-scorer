"""Canadian labour-market data from Job Bank (ESDC), pulled live with no key.

The rest of the kit reads US sources (O*NET, BLS). This module gives the Canadian
equivalent for the outlook and wage lines, keyed by NOC 2021 five-digit codes:

1. Job Bank 3-year employment outlooks, by occupation, province and economic region.
   https://open.canada.ca/data/en/dataset/b0e112e9-cf53-4e79-8838-23cd98debe5b
2. Job Bank wages (low, median, high), by occupation, province and economic region.
   https://open.canada.ca/data/en/dataset/adad580f-76b0-4502-bd05-20c125de9116

Both datasets are found through the Open Government CKAN API on every run, so a new
release is picked up without a code change. The outlook file is .xlsx and is read with
the standard library (zipfile and ElementTree), keeping the kit free of dependencies.
If a pull fails, FetchError is raised and callers stop. Nothing is cached on disk.

Skills still come from O*NET: Canada has no keyless equivalent of O*NET Technology
Skills with demand flags. Pair an O*NET code (skills) with a NOC code (outlook and pay).
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import os
import re
import zipfile
import xml.etree.ElementTree as ET
from html.parser import HTMLParser

from .net import FetchError, fetch, fetch_json

CKAN = os.environ.get("CA_CKAN_URL", "https://open.canada.ca/data/api/3/action/package_show?id={id}")
OUTLOOK_DATASET = "b0e112e9-cf53-4e79-8838-23cd98debe5b"
WAGES_DATASET = "adad580f-76b0-4502-bd05-20c125de9116"
JOB_BANK_REPORT = "https://www.jobbank.gc.ca/marketreport/summary-occupation/{noc}/ca"

# Job Bank's outlook scale, best to worst. "Undetermined" means too few workers to rate.
OUTLOOK_SCALE = ["Very good", "Good", "Moderate", "Limited", "Very limited"]
PROVINCES = {"NL", "PE", "NS", "NB", "QC", "ON", "MB", "SK", "AB", "BC", "YT", "YK", "NT", "NU"}
_X = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
HOURS_PER_YEAR = 1950  # 37.5 h x 52 weeks, used only to show an hourly median as a yearly figure


def noc5(code: str) -> str:
    """'21211', 'NOC_21211' or 'NOC 21211' -> '21211'."""
    m = re.search(r"(\d{5})", str(code))
    if not m:
        raise ValueError(f"Not a NOC 2021 code (five digits): {code}")
    return m.group(1)


def _year_key(url: str) -> tuple:
    """Sort key: the period a file covers, read from its file name.

    '20252027_outlook_n21_en_251208.xlsx' -> 2027 (the end of the range);
    'wage2025opendata-esdc-all-19nov2025-vf.csv' -> 2025 (the first year).
    Only the file name is read: ids elsewhere in the URL can look like years.
    """
    base = url.rsplit("/", 1)[-1]
    rng = re.search(r"(20\d{2})[-_]?(20\d{2})(?!\d)", base)
    if rng and int(rng.group(2)) - int(rng.group(1)) in (1, 2, 3):
        return (int(rng.group(2)), base)
    first = re.search(r"(?<!\d)(20\d{2})(?!\d)|(20\d{2})(?=opendata)", base)
    year = next((int(g) for g in (first.groups() if first else ()) if g), 0)
    return (year, base)


def latest_resource(dataset_id: str, fmt: tuple[str, ...], lang: str = "en") -> dict:
    """The newest resource of a CKAN dataset in one of the formats, in English where marked."""
    pkg = fetch_json(CKAN.format(id=dataset_id))
    if not pkg.get("success"):
        raise FetchError(f"Open Government API refused dataset {dataset_id}")
    res = []
    for r in pkg["result"].get("resources", []):
        f = (r.get("format") or "").upper()
        url = r.get("url") or ""
        if f not in fmt and not url.lower().endswith(tuple(x.lower() for x in fmt)):
            continue
        langs = r.get("language") or []
        if langs and lang not in langs:
            continue
        if re.search(r"_fr[_.]|-fr-|_fra", url.lower()):
            continue
        res.append(r)
    if not res:
        raise FetchError(f"No {'/'.join(fmt)} resource found in dataset {dataset_id}")
    return max(res, key=lambda r: _year_key(r.get("url", "")))


def read_xlsx(data: bytes) -> list[list[str]]:
    """Rows of the first worksheet, as strings. Handles inline and shared strings."""
    z = zipfile.ZipFile(io.BytesIO(data))
    shared: list[str] = []
    if "xl/sharedStrings.xml" in z.namelist():
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")).iter(_X + "si"):
            shared.append("".join(t.text or "" for t in si.iter(_X + "t")))
    sheet = sorted(n for n in z.namelist() if re.match(r"xl/worksheets/sheet\d+\.xml$", n))[0]
    rows = []
    for row in ET.fromstring(z.read(sheet)).iter(_X + "row"):
        vals: dict[int, str] = {}
        for c in row.iter(_X + "c"):
            col = _col_index(c.get("r", ""))
            t = c.get("t")
            if t == "inlineStr":
                v = "".join(x.text or "" for x in c.iter(_X + "t"))
            else:
                e = c.find(_X + "v")
                v = e.text if e is not None and e.text is not None else ""
                if t == "s" and v:
                    v = shared[int(v)]
            vals[col if col >= 0 else len(vals)] = v
        if vals:
            rows.append([vals.get(i, "") for i in range(max(vals) + 1)])
    return rows


def _col_index(ref: str) -> int:
    letters = re.match(r"([A-Z]+)", ref)
    if not letters:
        return -1
    n = 0
    for ch in letters.group(1):
        n = n * 26 + ord(ch) - 64
    return n - 1


def excel_date(v: str) -> str:
    """Excel serial day (e.g. 45999) -> ISO date. Text dates pass through."""
    try:
        return (dt.date(1899, 12, 30) + dt.timedelta(days=int(float(v)))).isoformat()
    except ValueError:
        return v


class _Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in ("li",):
            self.parts.append("\n- ")
        elif tag in ("p", "ul"):
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)


def html_to_text(s: str) -> str:
    p = _Text()
    p.feed(s or "")
    return re.sub(r"\n{2,}", "\n", "".join(p.parts)).strip()


def _num(s: str) -> float | None:
    try:
        return float(str(s).replace(",", "").replace("$", ""))
    except ValueError:
        return None


class JobBank:
    """Live Job Bank outlooks and wages. One download per dataset per run."""

    def __init__(self):
        self._outlook: list[dict] | None = None
        self._wages: list[dict] | None = None
        self.sources: dict[str, str] = {}

    # ---- loading ----
    def outlook_rows(self) -> list[dict]:
        if self._outlook is None:
            r = latest_resource(OUTLOOK_DATASET, ("XLSX", "CSV"))
            url = r["url"]
            data = fetch(url, binary=True, timeout=120)
            rows = read_xlsx(data) if data[:2] == b"PK" else list(csv.reader(io.StringIO(data.decode("utf-8-sig"))))
            if not rows:
                raise FetchError(f"Empty outlook file: {url}")
            head = [h.strip() for h in rows[0]]
            self._outlook = [dict(zip(head, r)) for r in rows[1:] if any(r)]
            self.sources["outlook"] = url
        return self._outlook

    def wage_rows(self) -> list[dict]:
        if self._wages is None:
            r = latest_resource(WAGES_DATASET, ("CSV",))
            url = r["url"]
            text = fetch(url, binary=True, timeout=180).decode("utf-8-sig", errors="replace")
            self._wages = list(csv.DictReader(io.StringIO(text)))
            if not self._wages:
                raise FetchError(f"Empty wages file: {url}")
            self.sources["wages"] = url
        return self._wages

    # ---- lookups ----
    def search(self, query: str, limit: int = 10) -> list[dict]:
        """NOC occupations whose title shares the most words with the query."""
        words = {w for w in re.findall(r"[a-z]+", query.lower()) if len(w) > 2}
        titles: dict[str, str] = {}
        for r in self.wage_rows():
            titles.setdefault(noc5(r["NOC_CNP"]), r.get("NOC_Title_eng", ""))
        hits = []
        for code, title in titles.items():
            tw = set(re.findall(r"[a-z]+", title.lower()))
            score = sum(1 for w in words if w in tw or w.rstrip("s") in {t.rstrip("s") for t in tw})
            if score:
                hits.append({"noc": code, "title": title, "match": round(score / max(len(words), 1), 2),
                             "url": JOB_BANK_REPORT.format(noc=code)})
        hits.sort(key=lambda h: (-h["match"], len(h["title"]), h["noc"]))
        return hits[:limit]

    def outlook(self, noc: str, province: str, region: str | None = None) -> dict:
        """Job Bank's 3-year outlook for one occupation in a province, or a region of it."""
        code, prov = noc5(noc), province.upper()
        if prov not in PROVINCES:
            raise ValueError(f"Unknown province code {province}. Use two letters, e.g. ON, BC, QC.")
        rows = [r for r in self.outlook_rows() if noc5(r.get("NOC_Code", "")) == code and r.get("Province", "").upper() == prov]
        if not rows:
            return {"noc": code, "province": prov, "outlook": None, "note": "Job Bank has no outlook for this occupation here."}
        pick = None
        if region:
            want = region.lower()
            pick = next((r for r in rows if want in r.get("Economic Region Name", "").lower()), None)
            if pick is None:
                names = sorted({r.get("Economic Region Name", "") for r in rows})
                raise ValueError(f"No region matching {region!r} in {prov}. Regions: {', '.join(names)}")
        else:
            pick = next((r for r in rows if r.get("Economic Region Code", "").endswith("00")), rows[0])
        out = pick.get("Outlook", "").strip()
        out = out[:1].upper() + out[1:].lower()  # older files write "good", newer ones "Good"
        trends = html_to_text(pick.get("Employment Trends", ""))
        period = re.search(r"(20\d{2}-20\d{2}) period", trends)
        return {"noc": code, "title": pick.get("NOC Title", ""), "province": prov,
                "period": period.group(1) if period else None,
                "region": pick.get("Economic Region Name", ""), "outlook": out,
                "rank": (OUTLOOK_SCALE.index(out) + 1) if out in OUTLOOK_SCALE else None,
                "release": excel_date(pick.get("Release Date", "")),
                "trends": trends,
                "source": self.sources.get("outlook"), "url": JOB_BANK_REPORT.format(noc=code)}

    def wages(self, noc: str, province: str = "NAT", region: str | None = None) -> dict:
        """Low, median and high wage. Hourly unless Job Bank marks the occupation as annual."""
        code, prov = noc5(noc), province.upper()
        rows = [r for r in self.wage_rows() if noc5(r["NOC_CNP"]) == code and r.get("prov", "").upper() == prov]
        if region:
            rows = [r for r in rows if region.lower() in r.get("ER_Name", "").lower()]
        else:
            rows = [r for r in rows if r.get("ER_Code_Code_RE", "") in ("ER00",) or re.fullmatch(r"ER\d{2}", r.get("ER_Code_Code_RE", ""))]
        rows = [r for r in rows if _num(r.get("Median_Wage_Salaire_Median", "")) is not None]
        if not rows:
            return {"noc": code, "province": prov, "median": None, "note": "Job Bank has no wage for this occupation here."}
        r = rows[0]
        annual = r.get("Annual_Wage_Flag_Salaire_annuel", "0").strip() == "1"
        med = _num(r["Median_Wage_Salaire_Median"])
        return {"noc": code, "title": r.get("NOC_Title_eng", ""), "province": prov, "region": r.get("ER_Name", ""),
                "low": _num(r.get("Low_Wage_Salaire_Minium", "")), "median": med,
                "high": _num(r.get("High_Wage_Salaire_Maximal", "")), "unit": "year" if annual else "hour",
                "median_yearly": med if annual else round(med * HOURS_PER_YEAR),
                "period": r.get("Reference_Period", ""), "data_source": r.get("Data_Source_E", ""),
                "source": self.sources.get("wages")}


def _src(w: dict) -> str:
    """'Labour Force Survey 2023-2024', or just '2021 Census' when the period is already in the name."""
    src, per = w.get("data_source", ""), w.get("period", "")
    return src if per and per in src else f"{src} {per}".strip()


def summary_line(o: dict, w: dict) -> str:
    """One report line, e.g. 'Job Bank outlook (live): Good in Toronto, ON (released 2025-12-08). Median wage $46.33/hour.'"""
    parts = []
    if o.get("outlook"):
        where = o["region"] if o.get("region") and o["region"].lower() != o.get("province", "").lower() else o.get("province")
        parts.append(f"Job Bank outlook (live, NOC {o['noc']}): {o['outlook']} in {where}, {o['province']}"
                     f" for {o.get('period') or 'the next 3 years'} (released {o.get('release')}; scale: Very good, Good, Moderate, Limited, Very limited).")
    elif o:
        parts.append(f"Job Bank outlook (live, NOC {o.get('noc')}): {o.get('note', 'none')}")
    if w.get("median") is not None:
        if w["unit"] == "hour":
            parts.append(f"Median wage ${w['median']:,.2f}/hour (about ${w['median_yearly']:,.0f} a year at {HOURS_PER_YEAR} hours), "
                         f"{w.get('region')}, {_src(w)}.")
        else:
            parts.append(f"Median wage ${w['median']:,.0f} a year, {w.get('region')}, {_src(w)}.")
    return " ".join(parts)
