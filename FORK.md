# What this fork adds

This is a fork of [Vets-Who-Code/ai-era-job-kit](https://github.com/Vets-Who-Code/ai-era-job-kit), a standard-library Python toolkit that scores resumes, LinkedIn and GitHub profiles against live public labour-market data. The original kit and its method belong to Vets Who Code. This file lists what the fork changes, and why.

## Canadian labour-market data

The kit's market data was US-only: O*NET for occupations and skills, BLS for outlook and wages. For someone job-hunting in Canada, the BLS line answered the wrong question. This fork adds Job Bank (Employment and Social Development Canada) as a second source, keyed by NOC 2021 occupation codes.

| Piece | What it does |
|---|---|
| `scripts/jobdata/canada.py` | Finds the newest Job Bank outlook and wage releases through the Open Government CKAN API, downloads them, and answers lookups: NOC title search, the 3-year outlook for a province or economic region, and low, median and high wages. |
| `scripts/canada_outlook.py` | CLI. Compares several NOC codes for one province or region, with Job Bank's reasons for each outlook, as Markdown or JSON. |
| `resume_score.py --noc --province --region` | Step 1 of the scoring chain reports Job Bank's outlook and pay in place of BLS. The skill list still comes from O*NET. |
| `tests/test_canada.py` | 14 offline tests with stubbed network calls. |

Design choices:

- **Same rules as the rest of the kit.** The data is public, needs no key, and is pulled live on every run. If a pull fails, the tool stops with `STOPPED:` and never falls back to a remembered number.
- **No hard-coded file URLs.** Each run asks the CKAN API for the dataset's resources and picks the newest English file by the period in its file name. A new Job Bank release is picked up without a code change. The year is read only from the file name, because ids elsewhere in the URL (such as `…-2114e02/`) can look like years. That bug showed up in testing and has a test now.
- **Stays dependency-free.** Job Bank publishes the outlooks as `.xlsx`. Those are read with `zipfile` and `ElementTree`, handling both inline and shared strings, so there's still nothing to `pip install`.
- **Honest units.** Job Bank wages are hourly unless the occupation is flagged annual. Reports show the hourly figure and a yearly guide at 1,950 hours, and say so. Each line names its source and period (for example "Labour Force Survey 2023-2024").
- **Clear refusals.** An unknown province or region is refused with the list of valid regions. A missing outlook is reported as missing rather than guessed.

Known limits: the NOC search matches title words only (for example "information systems", not "BI"). Job Bank has no national outlook row, so a province is required. Skills remain US-sourced (O*NET).

## Fixes

| Change | Why | Proof |
|---|---|---|
| The privacy guard fails closed when `git` isn't installed (`scripts/screen_grade.py`) | Resumes must never be written inside a git repo. The check shelled out to `git` and answered "not a repo" if git was missing, which let the save through. It now looks for a `.git` folder itself. | `test_git_guard_fails_closed_without_git` |
| The LinkedIn headline tip no longer defaults to "Software Engineer" (`scripts/linkedin_score.py`) | With no target role set, analysts and managers were told to title themselves software engineers. The tip now asks for `target_role` in `portfolio.json`. | |
| `test_bad_input_is_refused_before_any_pull` is hermetic | It passed only when the kit sat inside a git checkout. It now creates a throwaway repo. | The suite passes inside and outside a repo |

## Run the tests

```
python3 -m unittest discover -s tests -v
```

86 tests, all offline.
