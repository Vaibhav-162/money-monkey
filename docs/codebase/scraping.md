# Scraping notes (Chittorgarh)

Index URL pattern:

`https://www.chittorgarh.com/ipo/ipo_perf_tracker.asp?exchange=mainline|sme&year=YYYY`

Detail example:

`https://www.chittorgarh.com/ipo/gaja-alternative-asset-management-ipo/2527/`

Golden smoke IPO: **Lohia Corp**, `ipo_id=2574`, listed 2026-07-30, `/ipo/lohia-corp-ipo/2574/`.

## What the site actually has

- Tracker main table is JavaScript. Static HTML often shows “No Record Found”. Use Playwright.
- Detail pages are HTML. BeautifulSoup is enough.
- Industry: heading `Recently Listed IPOs in {Industry}` (Lohia = Industrial Products).
- ROCE is a KPI row when the company discloses it (Lohia yes; some IPOs no).
- Financial grid is Assets / Total Income / PAT / EBITDA / Net Worth / Reserves / Borrowings. **CFO and FCF are not published.** Do not invent a proxy.
- GMP is **not on the Chittorgarh detail HTML**. The GMP tab links to InvestorGain (`https://www.investorgain.com/chr-gmp/{slug}/{id}`). Daily history is a JS table there. Older years (e.g. 2016) often have **no archive**. Empty archive → `gmp_missing` is correct.
- SME layouts differ: often no QIB, market maker, single exchange; some subscription rows are IPOMatrix-paywalled. Parser must tolerate missing sections.
- `robots.txt` allows `/ipo/` except discussions. Do not hit discussion pages.

## Browser

`browser.chromium_page` launches Chromium, yields one page, and always closes context + browser in `finally`. Tracker and the one-shot `scrape_gmp()` still use it.

`browser.chromium_session` launches one browser + context and yields the context. The GMP history re-scrape (`scripts/rescrape_gmp_history.py`) opens **one session per shard** and a new Page per IPO, then closes that page in `finally`. Do not leave Playwright browsers running in the background.

HTTP uses a 1.5s delay by default, one thread.

GMP history re-scrape and post-listing price fetch can run as process shards
(`--workers N`). Each worker writes its own files (`data/gmp_parts/shard_XX.csv`
or `data/prices/daily/{ipo_id}.parquet`); the parent merges afterward. Do not
point two workers at the same CSV.

## Resume

`--resume` skips `ipo_id` values already in `ipos.csv`. Cache hits skip the network for that detail HTML. GMP still opens Playwright unless `--no-gmp`.
