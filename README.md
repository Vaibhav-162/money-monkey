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

Weekday crons run on **`main` only** (UTC):

- `45 9 * * 1-5` = **3:15 PM IST** hedge tick. GitHub Actions `schedule`
  events on a public repo can still delay any of the three ticks by hours
  (known platform behavior, not a timezone bug); the hedge reduces — not
  eliminates — the chance of a very late first email.
- `0 10 * * 1-5` = **3:30 PM IST** primary close-day scan (still inside the
  bidding window; most broker UPI cutoffs are 4:00–4:30 PM IST).
- `30 10 * * 1-5` = **4:00 PM IST** catch-up. Whichever of the three ticks
  first writes an audit row for a given `(ipo_id, close_date)` sends the
  one email for that IPO that day; later ticks that day are silent for
  that key even if GMP/Sub moved a lot, so three crons cannot cause three
  emails for the same IPO. Failure alerts (Telegram+email) fire at
  most once per IST calendar day, persisted in `data/live_alert_state.json`
  which the workflow commits even when the scan step fails.
- External **3:30 PM IST** weekday POST (cron-job.org →
  `repository_dispatch` type `trigger-daily-ipo-alert`) is the on-time
  clock; GitHub's three `schedule` ticks stay as delayed backup. Same
  presence-only gate, so both clocks cannot send two emails for one IPO.
  That dispatch is a **live** send (not the UI dry-run). The PAT lives
  only in cron-job.org (fine-grained, this repo, Contents read/write) —
  never commit it. HTTP 204 means GitHub accepted the webhook; confirm
  Actions shows event `repository_dispatch`. URL:
  `https://api.github.com/repos/Vaibhav-162/money-monkey/dispatches`.
- `15 4 * * 1-5` = 9:45 AM IST listing-day verification.
- `30 6 * * 1-5` = 12:00 PM IST allotment-out check.

You do **not** need to click Run every weekday. If Actions shows no
**Scheduled** or **repository_dispatch** run of *Daily IPO close-day
alert* by ~3:35 PM IST, the external clock and GitHub both missed —
click **Run workflow** on **`main`** with dry_run **unchecked**
immediately (a next-day retry is too late to bid). SMTP 535 / quoted
Windows secrets are a separate email-login problem; they do not explain a
missing schedule.

**Manual "Run workflow" clicks default to a dry run.** Both
`daily_ipo_alert.yml` and `check_allotment.yml` expose a `dry_run`
checkbox (default **checked**) on `workflow_dispatch`: it prints results
but never writes the audit log or sends mail/Telegram, so ad-hoc testing
can never consume the one-alert-per-IPO-per-day slot that a real
scheduled tick would otherwise use later that day. **Uncheck the box**
when you actually need a real backup send (e.g. a dropped 3:30/4:00 PM
tick) — scheduled `cron` runs and cron-job.org `repository_dispatch`
always ignore this input and send for real.

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
