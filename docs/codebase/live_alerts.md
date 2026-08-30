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
| Chittorgarh subscription | `/ipo_subscription/{slug}/{id}/` | live `sub_total_x` (and category x) |
| InvestorGain GMP tab | via existing `chittorgarh.gmp` | live `gmp_rs` / `gmp_pct` (may be missing) |

Discovery is static HTML (`chittorgarh/live_dashboard.py`). Detail HTML is
`HttpClient` with cache disabled. GMP still needs Playwright.

## Daily flow

1. **15:20 IST (09:50 UTC) weekdays** — `.github/workflows/daily_ipo_alert.yml`
   runs `python scripts/live_scanner.py --out data`.
2. IPOs with `close_date == today (IST)` are scraped and passed through
   `to_score_row()` → `score_features()`.
3. One Telegram/email card per IPO. Missing bot/SMTP secrets are a no-op.
   If a given IPO's scrape or scoring step raised, its card says
   `SCAN ERROR` instead of a decision — it is never rendered as an ordinary
   `SKIP`, so a broken pipeline can't be mistaken for a real "don't apply"
   signal.
4. A row is upserted into `data/live_audit_log.csv` on `(ipo_id, close_date)`.
5. If the scan raises, `send_failure_alert()` tries Telegram/email with the
   traceback. The same alert fires if the dashboard discovery itself returns
   zero rows for *both* boards (a near-certain sign the site HTML changed),
   even though zero *candidates closing today* is a normal, silent no-op.
6. **09:45 IST (04:15 UTC) weekdays** — `.github/workflows/verify_outcomes.yml`
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

Both workflows `git pull --rebase` before pushing, using `GITHUB_TOKEN` and
`permissions: contents: write`.

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

Also enable **Read and write permissions** for Actions
(Settings → Actions → General → Workflow permissions) so the audit CSV can
be committed.

## Fragility

- Chittorgarh/InvestorGain HTML changes will break discovery or GMP. The
  failure Telegram is the watchdog, not a fix.
- GMP is often missing for thin SME books. The card must say
  `GMP: not available`, never a fabricated 0.
- GitHub cron can slip several minutes. The 15:20 IST start is the buffer.
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
