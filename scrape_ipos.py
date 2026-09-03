"""CLI for the Chittorgarh IPO historical dataset scraper.

WHAT THIS FILE DOES
--------------------
Repo-root entrypoint for the `chittorgarh` package. It only parses flags and
delegates: `--smoke` → `smoke.run_smoke`, `--rebuild-xlsx` →
`export.rebuild_master_xlsx`, otherwise `pipeline.run_pipeline`. Nothing else
in the codebase imports this module; humans (and the README) run
`python scrape_ipos.py ...`. Live daily alerts and allotment checks are
separate CLIs (`scripts/live_scanner.py`, `scripts/check_allotment.py`).

KEY TERMS USED HERE
--------------------
- Mainline / SME: the two Chittorgarh tracker boards (`--exchange`).
  `mainline` is the primary NSE/BSE tier (stored as `mainboard` downstream);
  SME is the small-and-medium segment. Default is both.
- GMP: Grey Market Premium history from InvestorGain, fetched unless
  `--no-gmp`. Skip it only for faster parser debugging.
- Resume: `--resume` skips `ipo_id`s already in `ipos.csv`. Combine with
  `--retry-failed` to redo rows listed in `failed.csv`.
- Smoke: live end-to-end check of one known IPO (Lohia Corp, id 2574).
  Uses a temp folder; this CLI also deletes a leftover `data/smoke/` from
  older versions so it cannot be mistaken for the real dataset.
- `--headed`: show the Chromium window (tracker + GMP). Default is headless.

FUNCTIONS / CLASSES IN THIS FILE
---------------------------------
- `build_parser()`: argparse for year range, `--year`, `--exchange`,
  `--ipo-id` (filter after that year's tracker loads), `--out`, `--delay`,
  `--resume`, `--retry-failed`, `--no-gmp`, `--headed`, `--smoke`,
  `--rebuild-xlsx`.
- `_remove_stale_smoke_folder()`: delete `data/smoke/` (and empty `data/`)
  so an old smoke dump cannot mix with a real scrape.
- `main(argv)`: dispatch to smoke, xlsx rebuild, or the full pipeline.
  `--year` overrides `--from-year` / `--to-year`. Return code 0/1.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from chittorgarh.pipeline import run_pipeline
from chittorgarh.smoke import run_smoke

ROOT = Path(__file__).resolve().parent


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Scrape listed Mainboard and SME IPOs from Chittorgarh (2016–2026).")
    p.add_argument("--from-year", type=int, default=2016)
    p.add_argument("--to-year", type=int, default=2026)
    p.add_argument("--year", type=int, help="Single year (overrides from/to).")
    p.add_argument("--exchange", choices=["mainline", "sme", "both"], default="both")
    p.add_argument("--ipo-id", help="Scrape a single IPO id (after loading that year's tracker).")
    p.add_argument("--out", default="data", help="Folder for ipos.xlsx, ipos.csv, and HTML cache.")
    p.add_argument("--delay", type=float, default=1.5, help="Seconds between HTTP requests.")
    p.add_argument("--resume", action="store_true", help="Skip IPO ids already in ipos.csv.")
    p.add_argument("--retry-failed", action="store_true")
    p.add_argument("--no-gmp", action="store_true", help="Skip GMP tab (faster debug).")
    p.add_argument("--headed", action="store_true", help="Show the browser window.")
    p.add_argument("--smoke", action="store_true", help="End-to-end test for Lohia Corp (ipo_id=2574).")
    p.add_argument("--rebuild-xlsx", action="store_true", help="Rebuild ipos.xlsx from ipos.csv without scraping.")
    return p


def _remove_stale_smoke_folder() -> None:
    stale = ROOT / "data" / "smoke"
    if stale.is_dir():
        shutil.rmtree(stale, ignore_errors=True)
    data = ROOT / "data"
    if data.is_dir() and not any(data.iterdir()):
        data.rmdir()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _remove_stale_smoke_folder()
    if args.smoke:
        return run_smoke(headed=args.headed)
    out_dir = ROOT / args.out if not Path(args.out).is_absolute() else Path(args.out)
    if args.rebuild_xlsx:
        from chittorgarh.export import rebuild_master_xlsx
        rebuild_master_xlsx(out_dir)
        return 0

    from_year = to_year = args.year if args.year else None
    if from_year is None:
        from_year, to_year = args.from_year, args.to_year
    exchanges = ("mainline", "sme") if args.exchange == "both" else (args.exchange,)
    run_pipeline(
        out_dir=out_dir,
        from_year=from_year,
        to_year=to_year,
        exchanges=exchanges,
        delay=args.delay,
        resume=args.resume,
        retry_failed=args.retry_failed,
        ipo_id=args.ipo_id,
        fetch_gmp=not args.no_gmp,
        headless=not args.headed,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
