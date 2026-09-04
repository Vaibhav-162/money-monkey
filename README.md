# IPO scraper

Downloads listed Mainboard and SME IPO data from Chittorgarh.com and saves **one Excel sheet**.

CFO and FCF are not on those pages, so they are not scraped.

## First-time setup

```powershell
pip install -r requirements.txt
python -m playwright install chromium
```

## Commands

Quick check (Lohia Corp). This does **not** save files in the project folder:

```powershell
python scrape_ipos.py --smoke
```

Full scrape, years 2016 to 2026:

```powershell
python scrape_ipos.py --from-year 2016 --to-year 2026 --delay 1.5 --out data
```

If it stops halfway, continue:

```powershell
python scrape_ipos.py --from-year 2016 --to-year 2026 --delay 1.5 --out data --resume
```

## After the full scrape

Open `data/ipos.xlsx` in Excel.

- Row 1 = group titles (Identity, Financials, Listing day, and so on)
- Row 2 = column names
- Row 3 onward = one IPO per row

`data/ipos.csv` is the same table. `data/failed.csv` lists pages that failed. `data/cache/` stores HTML so a resume is faster.

## Scoring pipeline (local)

After the master sheet exists:

```powershell
pip install -r requirements.txt
python run_analysis.py --out data
```

This trains **separate** Mainboard and SME models. It does not hit the network.

### You run these two network jobs yourself

Stop any sequential GMP scrape that is still running (Ctrl+C). `--resume` keeps
rows already in `data/gmp_history.csv`.

Leak-free GMP (InvestorGain daily history, 2020+, 4 parallel Chromium workers):

```powershell
python scripts/rescrape_gmp_history.py --out data --resume --workers 4 --delay 1.5
```

If a worker run is interrupted, merge whatever finished and/or resume:

```powershell
python scripts/rescrape_gmp_history.py --out data --merge
python scripts/rescrape_gmp_history.py --out data --resume --workers 4 --delay 1.5
```

Post-listing prices (spot-check first, then 4 parallel Yahoo/jugaad workers):

```powershell
python scripts/fetch_prices.py --spot-check
python scripts/fetch_prices.py --out data --workers 4
```

If the price workers finish but `returns.csv` is missing:

```powershell
python scripts/fetch_prices.py --out data --merge
```

If InvestorGain or Yahoo starts failing, drop `--workers` (try 2). If the
machine has RAM to spare and the sites stay clean, bump toward 6–8. Each GMP
worker launches its own headless Chromium (~150–300MB).

Then run `python run_analysis.py --out data` again so Strategy 1 uses `gmp_at_close` and Strategy 2 can use 6-month excess returns.

Score a close-day row:

```python
from analysis.score import score_features
score_features({"exchange_type": "sme", "issue_price": 100, "sub_total_x": 40, "gmp_at_close": 25, "issue_size_cr": 80})
```


If the Excel file looks corrupt, rebuild it from the CSV (do not delete `ipos.csv`):

```powershell
python scrape_ipos.py --rebuild-xlsx --out data
```

Every run writes into this same `data/ipos.csv` file. New IPOs are added; an IPO that is scraped again updates its existing row. `--resume` only skips work already done. Excel (`ipos.xlsx`) is rebuilt from that CSV at the end of a scrape (and every 50 new rows), not after every IPO.

Be polite: one request at a time, 1.5 second delay, no discussion pages.

## Live close-day alerts (GitHub Actions, no server)

The bot discovers IPOs closing today on Chittorgarh, scrapes live GMP and
total subscription, runs `score_features()`, and sends one Telegram/email
card per IPO. The Quality Checklist's first scored row is labeled "Total
Subscription (>20x)" (combined Chittorgarh total, not a retail-only
breakdown); a 5th informational, unscored "QIB Demand" line shows live
`sub_qib_x` and does not affect the /4 score. Fresh-capital-only issues
(no Offer for Sale row on Chittorgarh) now show PASS at 0% OFS instead of
NOT DISCLOSED; mixed issues that omit the OFS row for another reason still
show NOT DISCLOSED. Details: [docs/codebase/live_alerts.md](docs/codebase/live_alerts.md).

Local dry-run (prints cards, does not send or write the audit log):

```powershell
python -m chittorgarh.live_dashboard
python scripts/live_scanner.py --dry-run --include-open
```

None of the three live workflows has a GitHub Actions `schedule` trigger.
Public-repo `schedule` ticks were unreliable (multi-hour delays, dropped
ticks), so they were removed. Each workflow fires automatically **only**
when cron-job.org POSTs `repository_dispatch` (and `workflow_dispatch` is
the only manual fallback). If cron-job.org misses a POST, **nothing
runs** until a human notices and clicks **Run workflow** — there is no
in-window GitHub backup (the old 3:15 PM hedge and 4:00 PM catch-up
ticks are gone). Configure **all three** as separate cron-job.org jobs,
not just the daily alert, and turn on cron-job.org's own
failure-notification email. Dispatches run workflows on **`main` only**.

Weekday clocks (same IST times as the old GitHub crons; only the trigger
changed):

- **3:30 PM IST (10:00 UTC)** — *Daily IPO close-day alert*,
  `event_type` `trigger-daily-ipo-alert`. Close-day scan while the
  bidding window is still open (most broker UPI cutoffs are 4:00–4:30 PM
  IST). Presence-only gate: the first real write of an audit row for a
  given `(ipo_id, close_date)` sends the one email for that IPO that
  day; a later run that day (duplicate POST or a manual live retry) is
  silent for that key even if GMP/Sub moved a lot. Failure alerts
  (Telegram+email) fire at most once per IST calendar day, persisted in
  `data/live_alert_state.json` which the workflow commits even when the
  scan step fails. The dispatch is a **live** send (not the UI dry-run).
- **9:45 AM IST (04:15 UTC)** — *Verify IPO listing outcomes*,
  `event_type` `trigger-verify-outcomes`. Fills listing-day actuals; does
  not send alerts.
- **12:00 PM IST (06:30 UTC)** — *Check IPO allotment status*,
  `event_type` `trigger-check-allotment`. Allotment-out check. Also a
  **live** send (not the UI dry-run).

The PAT lives only in cron-job.org (fine-grained, this repo, Contents
read/write) — never commit it. Reuse the same PAT for all three jobs.
HTTP 204 means GitHub accepted the webhook; confirm Actions shows event
`repository_dispatch`. URL:
`https://api.github.com/repos/Vaibhav-162/money-monkey/dispatches`.
Each job is `POST` with headers `Accept: application/vnd.github+json`
and `Authorization: Bearer <PAT>`, and JSON body
`{"event_type": "<type above>"}`.

You do **not** need to click Run every weekday **if** all three
cron-job.org jobs are healthy. If Actions shows no `repository_dispatch`
run of *Daily IPO close-day alert* by ~3:35 PM IST, the external clock
missed — click **Run workflow** on **`main`** with dry_run **unchecked**
immediately (a next-day retry is too late to bid). Same idea for the
morning verify (~9:50 AM IST) and noon allotment (~12:05 PM IST) jobs,
except verify has no dry_run checkbox. SMTP 535 / quoted Windows secrets
are a separate email-login problem; they do not explain a missing run.

**Manual "Run workflow" clicks default to a dry run.** Both
`daily_ipo_alert.yml` and `check_allotment.yml` expose a `dry_run`
checkbox (default **checked**) on `workflow_dispatch`: it prints results
but never writes the audit log or sends mail/Telegram, so ad-hoc testing
can never consume the one-alert-per-IPO-per-day slot that a real
cron-job.org tick would otherwise use later that day. **Uncheck the box**
when you actually need a real backup send (e.g. a missed 3:30 PM POST)
— cron-job.org `repository_dispatch` always ignores this input and sends
for real. `verify_outcomes.yml` has a bare `workflow_dispatch` (no
inputs) because that job never sends alerts.

Repo setup: Settings → Actions → General → Workflow permissions →
**Read and write**. Add secrets `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`
(create a bot with @BotFather, message it once, then call `getUpdates`).
Optional: `GMAIL_USER` and `GMAIL_APP_PASSWORD` (one shared sender).
Optional: `ALERT_EMAIL_TO` (comma-separated close-day digest recipients;
if set, To is exactly that list; if unset, falls back to `GMAIL_USER`
alone). Optional: `PAN_PROFILES` JSON (`label` / `pan` / `email`) for
per-person allotment emails — that secret is used only by the noon
allotment checker; putting family emails in `PAN_PROFILES` does **not**
add them to the close-day digest (use `ALERT_EMAIL_TO` for that). Lookup
is best-effort on KFintech and MUFG Intime only
(captcha OCR). Personalized PAN emails are only sent for a confirmed
Allotted or Not allotted result; a captcha miss, unmatched company,
unsupported registrar, or "no application found" result stays silent
(no email, and Telegram is skipped too if nobody in that batch was emailed).
PANs and personal emails never belong in the repo — GitHub Secrets
only. Details: [docs/codebase/live_alerts.md](docs/codebase/live_alerts.md).

On Windows CMD, do **not** quote values (`set GMAIL_USER=you@gmail.com`).
Quoted `set VAR="value"` stores the quotes, which Gmail rejects as 535.
App passwords may be pasted with spaces; the notifier strips them.

**Commit the trained models** (`data/analysis/models/*.pkl`, ~1 MB) — without
them in the repo, GitHub Actions has no local training run to load Strategy 1
predictions from, and every alert silently degrades to `SKIP`.

How the rest of the code works is in `docs/codebase/`.
