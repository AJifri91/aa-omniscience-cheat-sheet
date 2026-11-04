# AA-Omniscience Benchmark Cheat Sheet

A one-page table of model results on Artificial Analysis's [AA-Omniscience](https://artificialanalysis.ai/evaluations/omniscience) benchmark, kept up to date automatically.

**Live site:** https://ajifri91.github.io/aa-omniscience-cheat-sheet/

This is an independent project, not affiliated with Artificial Analysis (AA).

## What the table shows

AA-Omniscience asks 6,000 expert-level questions without tools or context. Correct answers score +1, incorrect answers −1, and partial answers or abstentions 0.

| Column | Meaning |
|---|---|
| c, i, p+a | Correct, incorrect, and partial-plus-abstained answers, as percentages of all questions |
| OI (Omniscience Index) | `c − i`. Higher is better; range −100 to +100 |
| HR (Hallucination Rate) | `100 × i ÷ (i + p + a)`: how often a non-correct answer is a confident wrong one. Lower is better |
| Standard % | `(OI ÷ 2) + 50`. A custom 0–100 rescaling of OI, **not** accuracy and not an official AA metric |
| Profile | This site's own one-line summary of the numbers, not an AA rating |

## How the data stays current

No AI is involved: it is plain Python running on a schedule.

```
GitHub Actions, 4× a day (.github/workflows/update-data.yml)
└─ scripts/update_data.py
   ├─ aa_source.py     reads AA's page → finds the feed manifest → decrypts the feed → validated records
   ├─ metrics          derives c, i, p+a, OI, HR, Standard %, Profile (with a consistency check)
   ├─ admission rules  which models are shown (config.json)
   ├─ sanity guard     blocks suspicious mass changes (config.json)
   └─ writes data.json (what the site shows) and state.json (admission history)
      → committed only when something changed → GitHub Pages serves index.html + data.json
```

| File | Purpose |
|---|---|
| `index.html` | The site. Loads `data.json` in the browser. |
| `data.json` | Published table data. `updatedAt` is when the data last *changed*, not when it was last checked. |
| `state.json` | Admission history: every release seen, tracked and admitted. Never edit by hand. |
| `config.json` | Admission thresholds, grandfathered releases and sanity-guard limits. |
| `scripts/aa_source.py` | **The only code that knows how AA serves its data.** Fix this file when AA changes its site. |
| `scripts/update_data.py` | Metrics, admission rules, guard, state handling and the command line. |
| `scripts/fake_aa.py` | A small imitation of AA's page and feed layout, used by the tests. |
| `scripts/test_*.py` | Unit tests (run in CI and before every scheduled update). |

## Which models are shown

- A **new release** must reach **35%** accuracy, judged on AA's whole-percent display (rounded half up).
- Releases already listed when that rule was introduced (`grandfatheredReleases` in `config.json`) keep the older **27%** minimum.
- Only the **newest eligible release** per creator and product line is shown, and a new release replaces an older one only after it qualifies itself.
- Every scored release AA publishes is remembered in `state.json`, so a model that qualifies later (for example after a higher-effort run) is picked up.

## Safety nets

The updater **fails closed**: if anything looks wrong, it changes nothing and the site keeps the last good data. Then:

- **An issue opens automatically** ("Data updater is failing") with the error, the failed step and what to do. It updates on repeat failures and closes itself after the next successful run.
- **AA's raw responses are saved** as a workflow artifact (`aa-snapshot-<run id>`, kept 14 days), so the failure can be reproduced offline.
- **The sanity guard** stops a run that would remove more than 3, add more than 5, or re-score more than 3 models at once (limits in `config.json`). That pattern usually means the parser broke rather than the benchmark changing.
- **The site shows the updater's status badge** beside "Data last changed", so quiet weeks at AA are not mistaken for a broken updater, and a broken updater is never mistaken for a quiet week.
- **The schedule is kept alive.** GitHub disables scheduled workflows after 60 days without repository activity, so the workflow adds an empty commit if 50 days pass without one.
- **History cannot reset silently.** If `state.json` or `data.json` is missing or unreadable, the run stops and says how to restore it, rather than starting over and dropping admitted models.

## When AA changes its site

1. Open the failure issue. Errors name what broke, for example `models[12] ('gpt-x'): omniscienceBreakdown.accuracy must be a number from 0 to 1, got '58%'` or `No model-feed manifest was found in the page`.
2. Download the `aa-snapshot-…` artifact from the failed run and unzip it.
3. Reproduce the failure locally, without contacting AA:
   ```bash
   python scripts/update_data.py --from-snapshot path/to/aa-snapshot --dry-run
   ```
4. Fix `scripts/aa_source.py`. Mirror AA's new layout in `scripts/fake_aa.py` and add a test in `scripts/test_aa_source.py`, so the change stays covered.
5. Open a pull request. CI runs the unit tests and a **live dry run against AA**, which shows exactly what would be published, before anything merges.
6. Merge, then run *Update AA-Omniscience data* from the Actions tab (or wait for the next scheduled run).

If the error is the **sanity guard** and the change is genuine (for example AA re-scored every model), run the workflow manually with **Allow large changes** ticked instead.

## Running locally

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s scripts -p 'test_*.py'      # offline
python scripts/update_data.py --dry-run                     # contacts AA; writes nothing
python scripts/update_data.py --help                        # all options
```

To preview the site, serve the folder (for example `python -m http.server`) and open `http://localhost:8000`.

## About the data source

There is no official free API for these results that this project knows of. The updater reads the same files AA's own page loads in the browser: the page ships the feed's decryption key to every visitor, and the updater decrypts the feed the same way the page does. That makes it a reasonable personal tracker, but not an officially supported data source, so:

- Keep the request rate low (it fetches two files, four times a day) and respect AA's terms of use; stop if AA asks.
- Saved snapshots contain only what a browser downloads; the feed stays encrypted as served.
- The most robust long-term fix is official access. AA offers paid [data access](https://artificialanalysis.ai/data-access) (API and CSV); its listed metrics do not currently mention AA-Omniscience, so ask AA whether it can be included.
