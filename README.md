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
card per IPO. Details: [docs/codebase/live_alerts.md](docs/codebase/live_alerts.md).

Local dry-run (prints cards, does not send or write the audit log):

```powershell
python -m chittorgarh.live_dashboard
python scripts/live_scanner.py --dry-run --include-open
```

Weekday crons (UTC): `50 9 * * 1-5` = 3:20 PM IST scan;
`15 4 * * 1-5` = 9:45 AM IST listing-day verification.

Repo setup: Settings → Actions → General → Workflow permissions →
**Read and write**. Add secrets `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`
(create a bot with @BotFather, message it once, then call `getUpdates`).
Optional: `GMAIL_USER` and `GMAIL_APP_PASSWORD`.

**Commit the trained models** (`data/analysis/models/*.pkl`, ~1 MB) — without
them in the repo, GitHub Actions has no local training run to load Strategy 1
predictions from, and every alert silently degrades to `SKIP`.

How the rest of the code works is in `docs/codebase/`.
