# Live IPO alerts

Close-day scoring of IPOs that are still in subscription, using the same
`score_features()` payload as the historical pipeline. This is a statistical
alerter, not investment advice.

## What the bot scrapes

| Source | URL | Fields |
| --- | --- | --- |
| Chittorgarh mainboard dashboard | `https://www.chittorgarh.com/ipo/` | who is open / close date / detail URL |
| Chittorgarh SME dashboard | `https://www.chittorgarh.com/ipo/ipo_dashboard.asp?a=sme` | same |
| Chittorgarh detail page | `/ipo/{slug}/{id}/` | issue price, lot, size, OFS, ROE, D/E (no live Total x while open) |
| Chittorgarh subscription | `/ipo_subscription/{slug}/{id}/` | live `sub_total_x`, `sub_qib_x` (QIB often missing on SME) |
| InvestorGain GMP tab | via existing `chittorgarh.gmp` | live `gmp_rs` / `gmp_pct` and `sub_ig_x` (IG Subscription column; never sauda) |

Discovery is static HTML (`chittorgarh/live_dashboard.py`). Detail HTML is
`HttpClient` with cache disabled. GMP still needs Playwright.

## Daily flow

Schedules run on the **default branch (`main`) only**. A workflow that
exists only on a feature branch will not fire cron.

1. **15:30 IST (10:00 UTC) weekdays** — `.github/workflows/daily_ipo_alert.yml`
   runs `python scripts/live_scanner.py --out data`. This is the primary
   alert, timed so QIB/HNI books are largely in and you still have 30–60
   minutes before typical broker UPI cutoffs (4:00–4:30 PM IST) and the
   5:00 PM exchange close. Cards are **live snapshots**, not the final
   close-day print — GMP/Sub can still move in the last hour.
2. **16:00 IST (10:30 UTC) weekdays** — the same workflow's catch-up tick.
   If the 3:30 run already wrote matching `gmp_rs` and `sub_total_x` for
   that `(ipo_id, close_date)`, Telegram/email are skipped (no spam). If
   GitHub dropped the first tick, or numbers moved, the catch-up alerts.
3. IPOs with `close_date == today (IST)` are scraped and passed through
   `to_score_row()` → `score_features()`. Strategy 1 `p_allot` still uses
   Chittorgarh `sub_total_x` (the training source). InvestorGain
   `sub_ig_x` is display-only.
4. One Telegram message per IPO and one HTML email digest for the run.
   Missing bot/SMTP secrets are a no-op. If a given IPO's scrape or
   scoring step raised, its card says `SCAN ERROR` instead of a decision
   — it is never rendered as an ordinary `SKIP`.
5. A row is upserted into `data/live_audit_log.csv` on `(ipo_id, close_date)`.
   Each card and row stamps `scraped_at` (UTC + IST) and GMP as-of, and
   labels Sub as Chittorgarh vs InvestorGain when both are present.
6. If the scan raises, `send_failure_alert()` tries Telegram/email with the
   traceback. The same alert fires if the dashboard discovery itself returns
   zero rows for *both* boards (a near-certain sign the site HTML changed),
   even though zero *candidates closing today* is a normal, silent no-op.

If Actions has no **Scheduled** run of *Daily IPO close-day alert* after
3:30 PM IST, GitHub dropped the tick — click **Run workflow** on **`main`**
immediately. A missed schedule is unrelated to Gmail SMTP 535 (quoted
Windows `set VAR="value"` secrets).

7. **09:45 IST (04:15 UTC) weekdays** — `.github/workflows/verify_outcomes.yml`
   re-fetches the same detail URL. Once listing OHLC is published, it fills
   `actual_listing_open`, `actual_open_return_pct`, `actual_is_clean_pop` and
   writes `data/analysis/live_performance.json`.

   `actual_is_clean_pop` is computed from the **listing OPEN price** return
   (>= 15% and low held above issue price), not the close-day tracker gain
   used historically in `analysis/targets.py`. A live re-fetch of the bare
   detail URL never has the tracker context that populates
   `listing_day_gain_pct`, so anchoring to it would leave every row's outcome
   `None` forever while still flipping `verified=True`. The open-price basis
   also matches the EV framework's own "exit at listing open" assumption.
7. After scoring, the scanner stamps one Nifty-50 **market regime**
   (`BULLISH` / `BEARISH` / `NEUTRAL`) on every card and ranks same-day
   `apply_s1` names by `ev_retail / (price_band_high * lot_size)`, with
   `sub_qib_x` as the tie-breaker. A lone applicant is left unranked.
   QIB is a display/ranking overlay — it is not added to the LightGBM
   feature vector.
8. **12:00 IST (06:30 UTC) weekdays** — `.github/workflows/check_allotment.yml`
   re-fetches detail pages for IPOs that closed 1–4 IST days ago. The
   primary "allotment out" signal is a Basis of Allotment link/heading
   (confirmed on listed pages such as Lohia Corp). If that is absent, the
   expected timetable `allotment_date` is the fallback. Cards name the
   registrar and link to a public portal — never a PAN scrape.

Both workflows `git pull --rebase` before pushing, using `GITHUB_TOKEN` and
`permissions: contents: write`.

## Same-day ranking and staggered exits

- `capital_required = price_band_high * lot_size` (None if either is missing).
- `ev_capital_ratio = ev_retail / capital_required`. Missing ratio or QIB
  sorts last, not as zero.
- Strategy 2 card copy uses `quality_score` plus `market_regime`:
  score >= 3 is a 50/50 partial hold with a 10% trailing stop; score 2
  follows the Nifty 5-session flag; score <= 1 is a full listing-day flip.
- Regime is cached at `data/analysis/market_regime.json` (gitignored) for
  6 hours. A Yahoo failure reuses the cache or falls back to `NEUTRAL`.

## Local dry-run

```powershell
python -m chittorgarh.live_dashboard
python scripts/live_scanner.py --dry-run --include-open --no-gmp
python scripts/live_scanner.py --dry-run --include-open
```

`--include-open` scores currently-open issues even when today is not a close
day (useful on weekends). `--dry-run` prints cards and does not write the
audit log or send messages.

## Secrets (GitHub repo Settings → Secrets and variables → Actions)

| Secret | Required | How |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | recommended | Create a bot with [@BotFather](https://t.me/BotFather) |
| `TELEGRAM_CHAT_ID` | recommended | Message the bot, then `https://api.telegram.org/bot<token>/getUpdates` |
| `GMAIL_USER` | optional | Gmail address |
| `GMAIL_APP_PASSWORD` | optional | Google app password (not the account password) |

Local Windows CMD: `set GMAIL_USER=you@gmail.com` — do **not** wrap the
value in quotes. `set VAR="value"` stores the quote characters, and Gmail
returns SMTP 535. Google shows app passwords as four groups of four
letters; the notifier deletes those spaces before login. PowerShell is
fine with `$env:GMAIL_USER = "you@gmail.com"` (quotes are syntax, not part
of the value). After one SMTP auth failure the dispatcher skips remaining
emails instead of retrying the same bad login for every IPO.

Also enable **Read and write permissions** for Actions
(Settings → Actions → General → Workflow permissions) so the audit CSV can
be committed.

## Fragility

- Chittorgarh/InvestorGain HTML changes will break discovery or GMP. The
  failure Telegram is the watchdog, not a fix.
- GMP is often missing for thin SME books. The card must say
  `GMP: not available`, never a fabricated 0.
- GitHub cron can slip several minutes or drop a tick with no retry,
  especially the first weekdays after a workflow is added to `main`. The
  4:00 PM IST catch-up is the in-window backup; after that, click Run
  workflow on `main`. Do not wait until after 5:00 PM IST — the bid window
  is closed.
- 3:30/4:00 PM numbers are mid-afternoon snapshots. Chittorgarh Total x and
  InvestorGain Sub can disagree until both vendors catch up near the close.
  The card labels both sources and the fetch time so a snapshot is not
  mistaken for the 6 PM print.
- `data/` stays gitignored except `live_audit_log.csv`,
  `analysis/live_performance.json`, and `analysis/models/*.pkl`.
- The trained S1/S2 model bundles (`data/analysis/models/*.pkl`, ~1 MB total)
  **must be committed** — GitHub Actions checks out a fresh clone with no
  local training artifacts. Without them, `score_features()` still runs but
  every alert silently degrades to Strategy 1 `SKIP` / no `p_pop`, since
  there is no bundle to load. Re-run `python run_analysis.py` (or whatever
  regenerates the models) and commit the `.pkl` files whenever they change.
- Both commit steps guard `git add` with a file-existence check so a day with
  zero closing IPOs (audit CSV untouched) never fails the workflow, and both
  checkouts use `fetch-depth: 0` so `git pull --rebase` never fights a
  shallow-clone history.
- Allotment "published" is inferred from page HTML plus the timetable date.
  The date can slip a day; the Basis of Allotment heading is the stronger
  signal. Registrar portals are a static map with a Chittorgarh fallback.
