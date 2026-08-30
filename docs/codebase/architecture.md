# Architecture

CLI entry: `scrape_ipos.py`. Package: `chittorgarh/`.

## Flow

1. `tracker.scrape_tracker` opens the listed-IPO table for one exchange and year. That table is filled by JavaScript, so Playwright is required.
2. `pipeline.scrape_one` fetches each IPO detail URL with `http.HttpClient` (httpx, delay, HTML cache under `out/cache/{ipo_id}.html`).
3. `parse_ipo.parse_ipo_html` reads the static HTML (BeautifulSoup). Industry comes from the heading `Recently Listed IPOs in {Industry}`.
4. `gmp.scrape_gmp` opens the same detail page and clicks the GMP tab (also JavaScript).
5. `export.flatten_into_master` copies financial years, listing OHLC, objects of issue, and OFS sellers onto **one row**.
6. `export.append_master` writes `ipos.csv` after each IPO. `export.rebuild_master_xlsx` writes `ipos.xlsx` atomically (temp file, then replace) every 50 new rows and at the end of a run. CSV is the source of truth.

## Modules

| Module | Role |
| --- | --- |
| `scrape_ipos.py` | CLI flags (`--smoke`, `--from-year`, `--resume`, `--out`) |
| `pipeline.py` | Index → fetch → parse → persist; resume via `ipo_id` already in `ipos.csv` |
| `tracker.py` | Performance tracker index |
| `live_dashboard.py` | Current open/close IPO table (static HTML, no Playwright) |
| `parse_ipo.py` | Detail-page parser, SME-tolerant missing sections |
| `gmp.py` | Per-IPO GMP tab |
| `shards.py` | Round-robin worker split + CSV merge for GMP/price jobs |
| `export.py` | Master sheet columns, flatten, read/write, leftover-file cleanup |
| `analysis/` | Scoring pipeline (see [analysis.md](analysis.md)) and live audit ledger |
| `scripts/live_scanner.py` | Close-day discover → score → alert (see [live_alerts.md](live_alerts.md)) |
| `http.py` | Polite HTTP + disk cache |
| `browser.py` | Playwright Chromium that always closes |
| `normalize.py` | Dates, Indian commas, crore, price bands |
| `smoke.py` | Live check for Lohia Corp `ipo_id=2574` |

## Output rules

A full scrape writes only:

- `ipos.xlsx` / `ipos.csv` — master sheet
- `failed.csv` — errors
- `coverage.txt` — fill-rate summary
- `cache/{ipo_id}.html` — HTML cache

It does **not** write satellite CSVs (`financials.csv`, `kpis.csv`, and similar). `remove_legacy_outputs` deletes those if they are still on disk.

`ipos.xlsx` / `ipos.csv` is the **single master**. Every scrape **appends or updates** rows in that file (`append_master` replaces a row when `ipo_id` already exists, and adds it when it does not). A run never deletes the master to start over.

`--resume` only changes *what gets fetched*: it skips `ipo_id`s already in the master. Without `--resume`, those ids are fetched again and the same rows are overwritten in place.

`--smoke` writes to a temp folder and deletes it. It must not create `data/smoke/`.
