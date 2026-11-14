"""Offline tests for the job-alert email parser (scripts/job_alerts.py).
Fixtures in tests/fixtures/alerts/ are synthetic: made-up companies, jobs and links.
Run: python3 -m unittest discover -s tests -v
"""
import csv
import io
import json
import mailbox
import pathlib
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import job_alerts as ja  # noqa: E402

FIX = ROOT / "tests" / "fixtures" / "alerts"


def parse(name):
    return [j for m in ja.iter_messages([str(FIX / name)]) for j in ja.parse_message(m)]


def all_jobs():
    return [j for m in ja.iter_messages([str(FIX)]) for j in ja.parse_message(m)]


class LinksTest(unittest.TestCase):
    def test_linkedin_link_drops_tracking(self):
        self.assertEqual(ja.job_key("https://www.linkedin.com/comm/jobs/view/1000000001/?trackingId=a&refId=b"),
                         ("linkedin", "https://www.linkedin.com/jobs/view/1000000001/"))

    def test_linkedin_slug_link(self):
        self.assertEqual(ja.job_key("https://ca.linkedin.com/jobs/view/manager-data-at-x-1000000005?x=1")[1],
                         "https://www.linkedin.com/jobs/view/1000000005/")

    def test_indeed_link_keeps_only_job_id(self):
        self.assertEqual(ja.job_key("https://ca.indeed.com/rc/clk?jk=00000000000000A1&from=ja&tk=x"),
                         ("indeed", "https://ca.indeed.com/viewjob?jk=00000000000000a1"))

    def test_lookalike_hosts_are_not_job_links(self):
        self.assertIsNone(ja.job_key("https://indeed.com.example.net/rc/clk?jk=00000000000000a1"))
        self.assertIsNone(ja.job_key("https://ca.indeed.com/alerts"))
        self.assertIsNone(ja.job_key("https://www.linkedin.com/comm/jobs/search/"))


class FieldsTest(unittest.TestCase):
    def test_pay_formats(self):
        p = ja.parse_pay("$95K - $115K/yr")
        self.assertEqual((p["low"], p["high"], p["unit"]), (95000, 115000, "year"))
        p = ja.parse_pay("$95,000 - $115,000 a year")
        self.assertEqual((p["low"], p["high"], p["unit"]), (95000, 115000, "year"))
        p = ja.parse_pay("$48 - $55 an hour")
        self.assertEqual((p["unit"], p["high_yearly"]), ("hour", 55 * ja.HOURS_PER_YEAR))
        self.assertEqual(ja.parse_pay("CA$104,000 a year")["high"], 104000)
        self.assertIsNone(ja.parse_pay("Competitive salary"))

    def test_hostile_whitespace_is_fast(self):
        import time
        t = time.time()
        ja.parse_workplace("1 days" + " " * 5000 + "z")
        ja.parse_pay("$1" + " " * 5000 + "z")
        ja.parse_text("Title\nCo\n1 days" + " " * 5000 + "z\nhttps://ca.indeed.com/viewjob?jk=00000000000000d1", "indeed")
        self.assertLess(time.time() - t, 1.0)

    def test_workplace(self):
        self.assertEqual(ja.parse_workplace("Hybrid, 3 days in office"), ("hybrid", 3))
        self.assertEqual(ja.parse_workplace("two days a week in the office (hybrid)"), ("hybrid", 2))
        self.assertEqual(ja.parse_workplace("Remote"), ("remote", 0))
        self.assertEqual(ja.parse_workplace("On-site"), ("on-site", 5))
        self.assertEqual(ja.parse_workplace("Hybrid work"), ("hybrid", None))
        self.assertEqual(ja.parse_workplace("Ottawa, ON"), ("", None))


class ParseTest(unittest.TestCase):
    def test_linkedin_html(self):
        jobs = parse("linkedin_alert.eml")
        self.assertEqual([j["title"] for j in jobs],
                         ["Manager, Data Analytics", "Analytics Team Lead", "Senior Manager, Business Intelligence"])
        j = jobs[0]
        self.assertEqual((j["company"], j["location"], j["workplace"], j["office_days"]),
                         ("Northwind Data Ltd", "Ottawa, ON", "hybrid", 2))
        self.assertEqual(j["pay"]["high"], 115000)
        self.assertEqual(jobs[2]["workplace"], "remote")

    def test_footer_is_not_a_job_fact(self):
        for j in parse("linkedin_alert.eml"):
            self.assertNotIn("Synthetic footer", " ".join(str(v) for v in j.values()))

    def test_indeed_html(self):
        jobs = parse("indeed_alert.eml")
        self.assertEqual(len(jobs), 3)
        c = jobs[1]
        self.assertEqual((c["company"], c["office_days"], c["contract"], c["pay"]["unit"]),
                         ("Tailspin Services", 3, True, "hour"))
        self.assertEqual(jobs[2]["office_days"], 5)

    def test_plain_text_fallback(self):
        jobs = parse("indeed_alert_plain.eml")
        self.assertEqual([(j["title"], j["company"]) for j in jobs],
                         [("Analytics Lead", "Adventure Works Co"), ("Data Analyst", "Adventure Works Co")])
        self.assertEqual((jobs[0]["location"], jobs[0]["pay"]["high"]), ("Remote", 104000))
        self.assertEqual(jobs[1]["location"], "Regina, SK")

    def test_other_senders_ignored(self):
        self.assertEqual(parse("not_an_alert.eml"), [])

    def test_mbox_export(self):
        with tempfile.TemporaryDirectory() as d:
            box = mailbox.mbox(str(pathlib.Path(d) / "alerts.mbox"))
            for f in ("linkedin_alert.eml", "indeed_alert.eml"):
                box.add(mailbox.mboxMessage((FIX / f).read_bytes()))
            box.flush()
            box.close()
            jobs = [j for m in ja.iter_messages([str(pathlib.Path(d) / "alerts.mbox")]) for j in ja.parse_message(m)]
        self.assertEqual(len(jobs), 6)


class ReviewTest(unittest.TestCase):
    def test_merge_same_job_across_sources(self):
        merged = ja.merge(all_jobs())
        nw = [j for j in merged if j["company"] == "Northwind Data Ltd"]
        self.assertEqual(len(nw), 1)
        self.assertEqual(sorted(nw[0]["sources"]), ["indeed", "linkedin"])
        self.assertEqual(nw[0]["office_days"], 2)  # filled in from the LinkedIn copy
        self.assertEqual(len(merged), 7)

    def test_filters(self):
        flt = {"pay_floor": 90000, "keep_unknown_pay": True, "max_office_days": 2,
               "high_pay_threshold": 200000, "high_pay_max_office_days": 5,
               "keep_unknown_office_days": True, "exclude_title_words": ["analyst"],
               "preferred_locations": ["remote", "Ottawa"]}
        kept, skipped, repeats, total = ja.review(all_jobs(), flt, set())
        titles = [j["title"] for j in kept]
        self.assertEqual(titles[:2], ["Analytics Lead", "Senior Manager, Business Intelligence"])
        self.assertIn("Manager, Data Analytics", titles)
        reasons = {j["title"]: " ".join(j["notes"]) for j in skipped}
        self.assertIn("below floor", reasons["Analytics Team Lead"])
        self.assertIn("5 office days", reasons["Head of Analytics"])
        self.assertIn("exclude word", reasons["Data Analyst"])
        fab = next(j for j in kept if j["company"] == "Fabrikam Insurance")
        self.assertIn("pay not stated", fab["notes"])

    def test_high_pay_lifts_office_limit(self):
        job = ja._fields("indeed", "https://ca.indeed.com/viewjob?jk=00000000000000c1", "Director",
                         ["Example Co", "Example City", "$210,000 a year", "4 days in office"])
        flt = {"max_office_days": 2, "high_pay_threshold": 200000, "high_pay_max_office_days": 4}
        self.assertTrue(ja.check(job, flt)[0])
        self.assertFalse(ja.check(job, {"max_office_days": 2})[0])

    def test_unknown_pay_can_be_dropped(self):
        job = ja._fields("indeed", "https://ca.indeed.com/viewjob?jk=00000000000000c2", "Lead", ["Example Co"])
        self.assertFalse(ja.check(job, {"pay_floor": 1, "keep_unknown_pay": False})[0])

    def test_no_filters_keeps_everything(self):
        kept, skipped, _, total = ja.review(all_jobs(), {}, set())
        self.assertEqual((len(kept), len(skipped), total), (7, 0, 7))

    def test_unknown_filter_key_refused(self):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "f.json"
            p.write_text(json.dumps({"pay_flor": 1}))
            with self.assertRaises(ValueError):
                ja.load_filters(str(p))

    def test_template_loads_and_is_all_off(self):
        flt = ja.load_filters(str(ROOT / "templates" / "job-alert-filters.json"))
        for k in ("pay_floor", "max_office_days", "high_pay_threshold", "allowed_locations"):
            self.assertIsNone(flt[k])


class CliTest(unittest.TestCase):
    def run_cli(self, args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = ja.main(args)
        return code, out.getvalue(), err.getvalue()

    def test_seen_file_skips_repeats_next_day(self):
        with tempfile.TemporaryDirectory() as d:
            seen, md, out_csv = (str(pathlib.Path(d) / n) for n in ("seen.csv", "r.md", "r.csv"))
            code, _, _ = self.run_cli([str(FIX), "--seen", seen, "--update-seen", "--md", md, "--csv", out_csv])
            self.assertEqual(code, 0)
            self.assertIn("7 kept", pathlib.Path(md).read_text())
            with open(out_csv, newline="") as f:
                self.assertEqual(len(list(csv.DictReader(f))), 7)
            code, out, _ = self.run_cli([str(FIX), "--seen", seen])
            self.assertIn("0 kept, 0 skipped, 7 already seen", out)

    def test_refuses_to_write_inside_git_repo(self):
        with tempfile.TemporaryDirectory() as d:
            subprocess.run(["git", "init", "-q", d], check=True)
            code, _, err = self.run_cli([str(FIX), "--md", str(pathlib.Path(d) / "r.md")])
            self.assertEqual(code, 2)
            self.assertIn("outside any git repository", err)
            self.assertFalse((pathlib.Path(d) / "r.md").exists())

    def test_update_seen_needs_seen(self):
        self.assertEqual(self.run_cli([str(FIX), "--update-seen"])[0], 2)


if __name__ == "__main__":
    unittest.main()
