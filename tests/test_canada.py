"""Offline tests for the Canadian data source (Job Bank via Open Government).
Network calls are stubbed. Run: python3 -m unittest discover -s tests -v
"""
import io
import json
import pathlib
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from jobdata import canada as ca  # noqa: E402
from jobdata.net import FetchError  # noqa: E402
import canada_outlook  # noqa: E402

NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
OUTLOOK_HEAD = ["NOC_Code", "NOC Title", "Outlook", "Employment Trends", "Release Date", "Province",
                "Economic Region Code", "Economic Region Name", "LANG"]
TREND = "<P>The employment outlook will be {o} for x in {r} for the 2025-2027 period.</P><UL><LI>Retirements.</LI></UL>"
OUTLOOK_ROWS = [
    ["NOC_20012", "Computer and information systems managers", "Limited", TREND.format(o="Limited", r="Ontario"), "45999", "ON", "3500", "Ontario", "EN"],
    ["NOC_20012", "Computer and information systems managers", "very good", TREND.format(o="very good", r="Toronto"), "45999", "ON", "3530", "Toronto", "EN"],
    ["NOC_20012", "Computer and information systems managers", "Good", TREND.format(o="Good", r="BC"), "45999", "BC", "5900", "British Columbia", "EN"],
]
WAGES_CSV = (
    "﻿NOC_CNP,NOC_Title_eng,prov,ER_Code_Code_RE,ER_Name,Low_Wage_Salaire_Minium,Median_Wage_Salaire_Median,"
    "High_Wage_Salaire_Maximal,Data_Source_E,Reference_Period,Annual_Wage_Flag_Salaire_annuel\n"
    "NOC_20012,Computer and information systems managers,NAT,ER00,Canada,43.75,66.67,103.37,Labour Force Survey,2023-2024,0\n"
    "NOC_20012,Computer and information systems managers,ON,ER35,Ontario,45.07,66.67,107.69,Labour Force Survey,2023-2024,0\n"
    "NOC_20012,Computer and information systems managers,ON,ER3530,Toronto,45.54,67.69,120.19,Labour Force Survey,2023-2024,0\n"
    "NOC_00010,Legislators,NAT,ER00,Canada,32867,84000,184000,2021 Census,2021,1\n"
    "NOC_21211,Data scientists,NAT,ER00,Canada,30,46.15,69.74,Labour Force Survey,2023-2024,0\n"
)


def xlsx(rows, shared=False) -> bytes:
    """A minimal one-sheet workbook, with inline strings or a shared-strings table."""
    strings, cells = [], []
    for i, row in enumerate(rows, 1):
        cs = []
        for j, v in enumerate(row):
            ref = f"{chr(65 + j)}{i}"
            if shared:
                strings.append(v)
                cs.append(f'<c r="{ref}" t="s"><v>{len(strings) - 1}</v></c>')
            else:
                v = v.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                cs.append(f'<c r="{ref}" t="inlineStr"><is><t>{v}</t></is></c>')
        cells.append(f'<row r="{i}">{"".join(cs)}</row>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/worksheets/sheet1.xml", f'<worksheet xmlns="{NS}"><sheetData>{"".join(cells)}</sheetData></worksheet>')
        if shared:
            esc = [s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;") for s in strings]
            z.writestr("xl/sharedStrings.xml", f'<sst xmlns="{NS}">{"".join(f"<si><t>{s}</t></si>" for s in esc)}</sst>')
    return buf.getvalue()


def ckan(urls):
    return {"success": True, "result": {"resources": [
        {"format": f, "language": [lang], "url": u} for f, lang, u in urls]}}


PKG = {
    ca.OUTLOOK_DATASET: ckan([("CSV", "en", "https://x/download/20232025_outlook_n21_en_240118.xlsx"),
                              ("XLSX", "en", "https://x/resource/20aa-2114e02/download/20252027_outlook_n21_en_251208.xlsx"),
                              ("XLSX", "fr", "https://x/download/20252027_outlook_n21_fr_251208.xlsx")]),
    ca.WAGES_DATASET: ckan([("CSV", "en", "https://x/download/2a71-das-wage2024opendata-esdc-all-11dec2024-vf.csv"),
                            ("CSV", "en", "https://x/download/2a71-das-wage2025opendata-esdc-all-19nov2025-vf.csv")]),
}


def fake_json(url, **kw):
    return next(v for k, v in PKG.items() if k in url)


def fake_fetch(url, **kw):
    if "20252027_outlook_n21_en" in url:
        return xlsx([OUTLOOK_HEAD, *OUTLOOK_ROWS])
    if "wage2025" in url:
        return WAGES_CSV.encode("utf-8")
    raise AssertionError(f"fetched an old or unexpected file: {url}")


class TestCanadaParsing(unittest.TestCase):
    def test_noc_code_forms(self):
        self.assertEqual(ca.noc5("NOC_21211"), "21211")
        self.assertEqual(ca.noc5("21211"), "21211")
        with self.assertRaises(ValueError):
            ca.noc5("15-1252.00")

    def test_newest_file_wins_and_ids_are_not_years(self):
        k = ca._year_key
        self.assertGreater(k("a/20252027_outlook_n21_en_251208.xlsx"), k("a/20232025_outlook_n21_en_240118.xlsx"))
        self.assertEqual(k("ed-8d4582041fcd/download/20232025_outlook_n21_en_240118.xlsx")[0], 2025)
        self.assertEqual(k("a/2018-en-20190501.csv")[0], 2018)
        self.assertEqual(k("a/2a71-das-wage2025opendata-esdc-all-19nov2025-vf.csv")[0], 2025)

    def test_latest_resource_skips_french(self):
        with mock.patch.object(ca, "fetch_json", side_effect=fake_json):
            r = ca.latest_resource(ca.OUTLOOK_DATASET, ("XLSX", "CSV"))
        self.assertIn("20252027_outlook_n21_en", r["url"])

    def test_xlsx_inline_and_shared_strings(self):
        for shared in (False, True):
            rows = ca.read_xlsx(xlsx([["a", "b & c"], ["1", "<P>x</P>"]], shared=shared))
            self.assertEqual(rows, [["a", "b & c"], ["1", "<P>x</P>"]])

    def test_excel_date_and_html(self):
        self.assertEqual(ca.excel_date("45999"), "2025-12-08")
        self.assertIn("- Retirements.", ca.html_to_text(TREND.format(o="Good", r="x")))


class TestJobBank(unittest.TestCase):
    def setUp(self):
        p1 = mock.patch.object(ca, "fetch_json", side_effect=fake_json)
        p2 = mock.patch.object(ca, "fetch", side_effect=fake_fetch)
        p1.start(), p2.start()
        self.addCleanup(mock.patch.stopall)
        self.jb = ca.JobBank()

    def test_province_row_by_default_region_on_request(self):
        o = self.jb.outlook("20012", "ON")
        self.assertEqual((o["region"], o["outlook"], o["rank"], o["period"]), ("Ontario", "Limited", 4, "2025-2027"))
        t = self.jb.outlook("20012", "on", "toronto")
        self.assertEqual((t["region"], t["outlook"], t["rank"]), ("Toronto", "Very good", 1))  # case normalized

    def test_bad_region_and_province_are_refused(self):
        with self.assertRaises(ValueError) as e:
            self.jb.outlook("20012", "ON", "Nowhere")
        self.assertIn("Toronto", str(e.exception))  # lists the real regions
        with self.assertRaises(ValueError):
            self.jb.outlook("20012", "XX")

    def test_missing_outlook_says_so(self):
        o = self.jb.outlook("21211", "ON")
        self.assertIsNone(o["outlook"])
        self.assertIn("no outlook", o["note"])

    def test_wages_hourly_and_annual(self):
        w = self.jb.wages("20012", "ON", "Toronto")
        self.assertEqual((w["median"], w["unit"], w["median_yearly"]), (67.69, "hour", round(67.69 * ca.HOURS_PER_YEAR)))
        self.assertEqual(self.jb.wages("20012", "ON")["region"], "Ontario")  # province row, not a region
        leg = self.jb.wages("00010")
        self.assertEqual((leg["unit"], leg["median_yearly"]), ("year", 84000))

    def test_search_by_title_words(self):
        hits = self.jb.search("information systems manager")
        self.assertEqual(hits[0]["noc"], "20012")
        self.assertEqual(self.jb.search("zzzqqq"), [])

    def test_summary_line_names_its_sources(self):
        line = ca.summary_line(self.jb.outlook("20012", "ON", "Toronto"), self.jb.wages("20012", "ON", "Toronto"))
        self.assertIn("Very good in Toronto, ON for 2025-2027", line)
        self.assertIn("$67.69/hour", line)
        self.assertIn("Labour Force Survey 2023-2024", line)
        self.assertIn("2021 Census.", ca.summary_line({}, self.jb.wages("00010")))  # no "2021 Census 2021"


class TestCanadaStopsOffline(unittest.TestCase):
    def test_failed_pull_stops(self):
        with mock.patch.object(ca, "fetch_json", side_effect=FetchError("offline")), mock.patch("sys.stderr", new=io.StringIO()):
            with self.assertRaises(FetchError):
                ca.JobBank().outlook("20012", "ON")
            self.assertEqual(canada_outlook.main(["--noc", "20012"]), 3)

    def test_cli_report_and_json(self):
        with mock.patch.object(ca, "fetch_json", side_effect=fake_json), mock.patch.object(ca, "fetch", side_effect=fake_fetch):
            with tempfile.TemporaryDirectory() as d, mock.patch("sys.stdout", new=io.StringIO()):
                out = pathlib.Path(d) / "o.json"
                self.assertEqual(canada_outlook.main(["--noc", "20012", "--province", "ON", "--region", "Toronto",
                                                      "--json", str(out)]), 0)
                data = json.loads(out.read_text())
        self.assertEqual(data["occupations"][0]["outlook"]["outlook"], "Very good")
        self.assertIn("20252027", data["sources"]["outlook"])

    def test_resume_score_uses_job_bank_not_bls_with_noc(self):
        import resume_score as rs
        resume = (ROOT / "tests" / "fixtures" / "screen_resume.txt").read_text()
        on = mock.MagicMock()
        on.technology_skills.return_value = [{"skill": "Python", "hot": True, "in_demand": True, "example": "Python"}]
        on.title.return_value = "Business Intelligence Analysts"
        on.version = "30_2"
        with tempfile.TemporaryDirectory() as d:
            r = pathlib.Path(d) / "r.txt"
            r.write_text(resume)
            js = pathlib.Path(d) / "s.json"
            with mock.patch.object(rs.onet_mod, "Onet", return_value=on), \
                    mock.patch.object(rs.bls_mod, "Bls", side_effect=AssertionError("BLS used for a Canadian lane")), \
                    mock.patch.object(ca, "fetch_json", side_effect=fake_json), \
                    mock.patch.object(ca, "fetch", side_effect=fake_fetch), \
                    mock.patch("sys.stdout", new=io.StringIO()):
                rc = rs.main(["--resume", str(r), "--onet", "15-2051.01", "--noc", "20012", "--region", "Toronto", "--json", str(js)])
            self.assertIn(rc, (0, 1))
            out = json.loads(js.read_text())
        self.assertEqual(out["outlook_ca"]["outlook"], "Very good")
        self.assertNotIn("outlook", out)


if __name__ == "__main__":
    unittest.main()
