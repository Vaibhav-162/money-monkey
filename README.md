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

If the Excel file looks corrupt, rebuild it from the CSV (do not delete `ipos.csv`):

```powershell
python scrape_ipos.py --rebuild-xlsx --out data
```

Every run writes into this same `data/ipos.csv` file. New IPOs are added; an IPO that is scraped again updates its existing row. `--resume` only skips work already done. Excel (`ipos.xlsx`) is rebuilt from that CSV at the end of a scrape (and every 50 new rows), not after every IPO.

Be polite: one request at a time, 1.5 second delay, no discussion pages.

## Later

New user-facing tools and commands will be added below this line.

How the code works (for developers and later context) is in `docs/codebase/`.
