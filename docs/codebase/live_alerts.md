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

1. **15:15 IST (09:45 UTC) hedge, then 15:30 IST (10:00 UTC) primary** —
   `.github/workflows/daily_ipo_alert.yml` runs
   `python scripts/live_scanner.py --out data` on both ticks (`45 9` then
   `0 10`, weekdays). The 3:15 PM IST hedge exists because GitHub Actions
   `schedule` runs on public repos can be delayed several hours,
   especially right at the top of an hour; it reduces — not eliminates —
   the chance of a very late first email. The 3:30 PM IST primary is timed
   so QIB/HNI books are largely in and you still have 30–60 minutes before
   typical broker UPI cutoffs (4:00–4:30 PM IST) and the 5:00 PM exchange
   close. Cards are **live snapshots**, not the final close-day print —
   GMP/Sub can still move in the last hour.
2. **16:00 IST (10:30 UTC) weekdays** — the same workflow's catch-up tick
   (`30 10`). Whichever of the three ticks first writes an audit row for a
   given `(ipo_id, close_date)` sends the one email for that IPO that day;
   later ticks that day are silent for that key even if GMP/Sub moved a
   lot, so three crons cannot cause three emails for the same IPO. Failure
   alerts (Telegram+email) fire at most once per IST calendar day,
   persisted in `data/live_alert_state.json`, which the workflow commits
   even when the scan step fails.

   An external weekday **15:30 IST** POST from cron-job.org fires the same
   workflow via `repository_dispatch` type `trigger-daily-ipo-alert` so
   the first card is not waiting in GitHub's public-repo schedule queue.
   The three `schedule` ticks remain as delayed backup. The presence-only
   gate still means one email per `(ipo_id, close_date)` that day. A
   dispatch is a live send (not `workflow_dispatch` dry-run). The PAT is
   Contents read/write on this repo only and must never be committed.
   HTTP 204 means GitHub accepted the webhook; confirm Actions shows
   event `repository_dispatch`. Endpoint:
   `https://api.github.com/repos/Vaibhav-162/money-monkey/dispatches`.
3. IPOs with `close_date == today (IST)` are scraped and passed through
   `to_score_row()` → `score_features()`. Strategy 1 `p_allot` still uses
   Chittorgarh `sub_total_x` (the training source). InvestorGain
   `sub_ig_x` is display-only.
4. One Telegram message per IPO and one HTML email digest for the run.
   Missing bot/SMTP secrets are a no-op. If a given IPO's scrape or
   scoring step raised, its card says `SCAN ERROR` instead of a decision
   — it is never rendered as an ordinary `SKIP`. Each card's Quality
   Checklist has four scored rows (/4); the first is labeled "Total
   Subscription (>20x)" (combined Chittorgarh total, not a retail-only
   breakdown). A 5th informational, unscored "QIB Demand" line reflects
   live `sub_qib_x` and does not affect the score. Genuinely
   fresh-capital-only issues (no Offer for Sale row on Chittorgarh) show
   PASS at 0% OFS instead of NOT DISCLOSED; mixed issues that omit the OFS
   row for another reason still show NOT DISCLOSED.
5. A row is upserted into `data/live_audit_log.csv` on `(ipo_id, close_date)`.
   Each card and row stamps `scraped_at` (UTC + IST) and GMP as-of, and
   labels Sub as Chittorgarh vs InvestorGain when both are present.
6. If the scan raises, `send_failure_alert()` tries Telegram/email with the
   traceback. The same alert fires if the dashboard discovery itself returns
   zero rows for *both* boards (a near-certain sign the site HTML changed),
   even though zero *candidates closing today* is a normal, silent no-op.

If Actions has no **Scheduled** or **repository_dispatch** run of
*Daily IPO close-day alert* by ~3:35 PM IST, both clocks missed — click
**Run workflow** on **`main`** with dry_run unchecked immediately. A
missed schedule is unrelated to Gmail SMTP 535 (quoted Windows
`set VAR="value"` secrets).

**`workflow_dispatch` defaults to a dry run.** Both `daily_ipo_alert.yml`
and `check_allotment.yml` add a `dry_run` boolean input (default `true`)
that is passed through as `--dry-run` when the trigger is
`workflow_dispatch`; `schedule`-triggered runs always ignore the input
and run for real. A cron-job.org `repository_dispatch`
(`trigger-daily-ipo-alert`) is also live — it does not use `inputs.dry_run`.
This exists because `records_needing_alert` is a pure
presence check on `(ipo_id, close_date)` — the *first* run of the day to
see a candidate, test or not, upserts the audit row and is the one that
sends. Before this input existed, an off-hours manual test (e.g. run
just after midnight IST) could create that row hours before the real
3:15/3:30/4:00 PM ticks ran, silently burning the day's one alert for
that IPO before the bidding window even opened. To force a real send
from the UI (e.g. backfilling a dropped scheduled tick), untick the
`dry_run` checkbox before clicking **Run workflow**; via `gh`, use
`gh workflow run daily_ipo_alert.yml -f dry_run=false`.

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
   expected timetable `allotment_date` is the fallback.

   If the `PAN_PROFILES` secret is set, the job then tries an automated
   PAN lookup on **KFintech** and **MUFG Intime / Link Intime** (Playwright
   + local Tesseract OCR for the registrar captcha). Each profile is emailed
   Allotted / Not allotted at their own address from the shared `GMAIL_USER`
   sender. Personalized PAN emails are only sent for a confirmed Allotted
   or Not allotted result; a captcha miss, unmatched company, unsupported
   registrar, or "no application found" result stays silent (no email, and
   Telegram is skipped too if nobody in that batch was emailed). Telegram
   gets counts only. Full PANs never go to git, the audit CSV, GitHub logs,
   or Telegram. Unset `PAN_PROFILES` keeps the original single generic
   "allotment out" card.

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
| `ALERT_EMAIL_TO` | optional | Comma-separated close-day digest recipients. If set, To is exactly this list; if unset, falls back to `GMAIL_USER` alone. Unrelated to `PAN_PROFILES`. |
| `PAN_PROFILES` | optional | JSON array of `{label, pan, email}` for personalized allotment results |

`PAN_PROFILES` example (GitHub secret value, not a repo file):

```json
[
  {"label": "Dad", "pan": "ABCDE1234F", "email": "dad@example.com"},
  {"label": "Me", "pan": "PQRST5678G", "email": "me@example.com"}
]
```

One shared `GMAIL_USER` sends each person's result to that profile's
`email`. `PAN_PROFILES` is used only by the noon allotment checker;
putting family emails there does **not** add them to the close-day digest
— use `ALERT_EMAIL_TO` for that. PANs and personal emails must stay in
GitHub Secrets — never commit them, never write them to
`data/live_audit_log.csv`. Registrar captchas mean lookup is best-effort
(KFintech and MUFG Intime only in v1); a miss still emails a manual
portal link.

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
- GitHub cron can slip several minutes, delay by hours, or drop a tick
  with no retry on a public repo (known platform behavior, especially
  right at the top of an hour, and the first weekdays after a workflow is
  added to `main`). The on-time clock is the 3:30 PM IST cron-job.org
  `repository_dispatch`; the 3:15 PM IST hedge and 4:00 PM IST catch-up
  stay as in-window GitHub backups. If both miss, click Run workflow on
  `main` with dry_run unchecked. Do not wait until after 5:00 PM IST —
  the bid window is closed.
- 3:15/3:30/4:00 PM numbers are mid-afternoon snapshots. Chittorgarh Total x and
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
  Automated PAN lookup (KFintech / MUFG Intime) solves a captcha with
  Tesseract and can miss; the job then emails a manual link instead of
  failing silent.
