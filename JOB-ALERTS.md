# Daily job-alert review (LinkedIn and Indeed)

The kit scores you against the market. This adds the other half of a daily routine: the jobs that land in your inbox. LinkedIn and Indeed both email job alerts. `scripts/job_alerts.py` reads those emails from files on your own computer, merges the same job seen on both sites, drops what you've already reviewed, applies your pay, office-day, location and title filters, and gives you one ranked list.

## What it does and doesn't do

- It reads alert emails **you already received**, as files: `.eml` files, a folder of them, or an `.mbox` export (Google Takeout and most mail apps can export one).
- It never logs in to LinkedIn or Indeed, never opens their pages, and never applies, saves, follows or messages anyone. There's no scraping and no browser automation, because both sites' terms forbid automated access to their pages. Alert emails are messages sent to you, so reading your own copy is fine.
- Nothing leaves your machine. There's no network call at all.
- Pay and office days come only from the alert text. If an alert doesn't say, the report says "not stated" rather than guessing. Check the posting yourself before you apply.

## One-time setup

1. **Create the alerts.** On LinkedIn, run a job search with your role, location and filters (for example "Remote" or "Hybrid", "Past 24 hours") and turn on the alert, set to daily email. On Indeed, run the same search and choose "Get new jobs for this search by email". Two to four searches with different titles is usually enough.
2. **Make a work folder outside any git repository**, for example `~/job-search/`. The filters, the seen list and the reports describe your job search, so the tool refuses to write them inside a repo.
3. **Copy the filters template** and fill in your own values:

   ```
   cp $KIT/templates/job-alert-filters.json ~/job-search/job-alert-filters.json
   ```

   Every filter is off (`null`) until you set it:

   | Key | What it does |
   |---|---|
   | `pay_floor` | Skip a job when the top of its stated range is below this (yearly; hourly pay is converted at 1,950 hours). |
   | `keep_unknown_pay` | Keep jobs whose alert states no pay. They're flagged in the report. |
   | `max_office_days` | The most office days a week you accept. Remote counts as 0 and on-site as 5. |
   | `high_pay_threshold`, `high_pay_max_office_days` | Accept more office days when the pay is high enough. |
   | `keep_unknown_office_days` | Keep jobs that don't say how many office days. Flagged. |
   | `preferred_locations` | Ranked list; results are sorted by it. Use `"remote"` to rank remote jobs. |
   | `allowed_locations`, `remote_ok` | If set, skip jobs elsewhere (remote jobs stay when `remote_ok` is true). |
   | `include_title_words`, `exclude_title_words` | Whole-word title matches. |
   | `exclude_companies`, `exclude_contract` | Skip named companies, or contract and temporary roles. |

## The daily run

1. **Get the alerts onto disk.** Save the day's alert emails as `.eml` into `~/job-search/inbox/` (most mail apps: select, then drag to a folder or "Save as"), or export a label as `.mbox`. In Gmail, a search like `from:linkedin.com OR from:indeed.com newer_than:1d` finds them. If you use an AI assistant that has a connector to *your own* mailbox, it can save them for you; keep it read-only.
2. **Run the review:**

   ```
   cd ~/job-search
   python3 $KIT/scripts/job_alerts.py inbox/ \
       --filters job-alert-filters.json \
       --seen alerts-seen.csv --update-seen \
       --md alerts-$(date +%F).md --csv alerts-$(date +%F).csv
   ```

   `--update-seen` adds today's jobs to `alerts-seen.csv`, so tomorrow's run shows only new ones. Jobs are matched by link and by company plus title, so the same job in both a LinkedIn and an Indeed alert appears once, with both sources listed.
3. **Read the report.** Kept jobs come first, ranked by your preferred locations and then by pay. Every skipped job is listed with its reason, so you can loosen a filter if it's dropping jobs you want.
4. **Feed the rest of the kit.** For each job you'll pursue, save the posting text (from the employer's own page) and run the Screen Grade (`screen_grade.py`) against your resume. Log each application in the funnel tracker:

   ```
   python3 $KIT/scripts/funnel.py add --file funnel.csv --company "<company>" --role "<role>" --source job-board
   ```

5. **Clear the inbox folder** once you've reviewed the day.

## When a field comes out blank

Alert layouts change without notice. The parser keys on the job links (LinkedIn `/jobs/view/<id>`, Indeed `jk=<id>`) and reads the few lines after each one, so a layout change usually blanks a field rather than inventing one. If titles or companies start coming out wrong, save one alert as a synthetic fixture (replace every company, title, link and address with made-up values first) and add a test to `tests/test_job_alerts.py`.

## Privacy

- Keep `job-alert-filters.json`, `alerts-seen.csv`, the reports and your inbox folder outside any git repository. The tool refuses to write its outputs inside one, and `.gitignore` covers the default names in case you copy them somewhere by mistake.
- Never commit a real alert email. It holds your address and tracking links tied to your account. The test fixtures are made up.
