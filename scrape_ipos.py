"""CLI for the Chittorgarh IPO historical dataset scraper."""

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
