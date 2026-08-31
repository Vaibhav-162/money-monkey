"""Close-day IPO scan: discover, scrape live GMP/sub, score, alert, audit."""

from __future__ import annotations

import argparse
import sys
import traceback
from contextlib import nullcontext
from datetime import date
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from analysis.live_audit import (
    build_alert_record,
    rank_same_day_candidates,
    read_audit,
    records_needing_alert,
    scrape_timestamps,
    to_score_row,
    upsert_audit,
)
from analysis.market_regime import fetch_market_regime
from analysis.score import score_features
from chittorgarh.browser import chromium_session
from chittorgarh.http import HttpClient
from chittorgarh.live_dashboard import closing_on, scrape_all_open_ipos, today_ist
from chittorgarh.live_subscription import fetch_live_subscription
from chittorgarh.pipeline import scrape_one
from scripts.notify import dispatch, send_failure_alert


def _select_candidates(
    rows: list[dict[str, Any]],
    as_of: date,
    include_open: bool,
) -> list[dict[str, Any]]:
    chosen = closing_on(rows, as_of)
    if include_open:
        seen = {r["ipo_id"] for r in chosen}
        for row in rows:
            if row.get("status") == "open" and row["ipo_id"] not in seen:
                chosen.append(row)
                seen.add(row["ipo_id"])
    return chosen


def _score_one(
    client: HttpClient,
    discovery: dict[str, Any],
    *,
    fetch_gmp: bool,
    gmp_page: Any,
    model_dir: Optional[Path],
) -> dict[str, Any]:
    year = None
    if discovery.get("close_date"):
        year = int(str(discovery["close_date"])[:4])
    row = {**discovery, "listing_year": year or as_of_year_fallback(discovery)}
    try:
        master, _sats = scrape_one(
            client,
            row,
            fetch_gmp=fetch_gmp,
            use_cache=False,
            gmp_page=gmp_page,
        )
    except Exception as exc:
        return build_alert_record({}, None, discovery, error=f"scrape:{exc}")
    slug = discovery.get("slug") or master.get("slug")
    if slug and discovery.get("ipo_id"):
        try:
            live_sub = fetch_live_subscription(client, str(slug), str(discovery["ipo_id"]))
            for key, val in live_sub.items():
                if val is not None:
                    master[key] = val
        except Exception as exc:
            warnings = master.get("parse_warnings") or ""
            master["parse_warnings"] = f"{warnings}; live_sub_error:{exc}".strip("; ")
    try:
        scored = score_features(to_score_row(master, discovery.get("close_date")), model_dir=model_dir)
        rec = build_alert_record(master, scored, discovery)
        rec["quality_breakdown"] = scored.get("quality_breakdown")
        return rec
    except Exception as exc:
        return build_alert_record(master, None, discovery, error=f"score:{exc}")


def as_of_year_fallback(discovery: dict[str, Any]) -> int:
    raw = discovery.get("close_date") or discovery.get("open_date")
    text = str(raw)[:4] if raw else ""
    return int(text) if text.isdigit() else today_ist().year


def run_scan(
    *,
    as_of: Optional[date] = None,
    include_open: bool = False,
    fetch_gmp: bool = True,
    dry_run: bool = False,
    write_audit: bool = True,
    out_dir: Path | None = None,
    model_dir: Path | None = None,
    rows: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    day = as_of or today_ist()
    out_dir = Path(out_dir) if out_dir else ROOT / "data"
    discovered = rows if rows is not None else scrape_all_open_ipos(as_of=day)
    if not discovered:
        msg = (
            "Live IPO dashboard returned 0 rows for both mainboard and SME "
            "(as_of={}). The site structure may have changed; discovery is silent "
            "by design otherwise, so this is the one case worth a heads-up."
        ).format(day.isoformat())
        print(f"[scan] WARNING {msg}")
        if not dry_run:
            send_failure_alert(msg)

    candidates = _select_candidates(discovered, day, include_open)
    print(f"[scan] as_of={day.isoformat()} discovered={len(discovered)} candidates={len(candidates)}")
    if not candidates:
        print("[scan] no closing (or open) IPOs; nothing to score")
        return []

    records: list[dict[str, Any]] = []
    cache_dir = out_dir / "cache_live"
    http = HttpClient(cache_dir=cache_dir, delay=1.5)
    session_cm = chromium_session() if fetch_gmp else nullcontext(None)
    try:
        with session_cm as context:
            page = None
            try:
                page = context.new_page() if context is not None else None
                for i, cand in enumerate(candidates, 1):
                    print(f"[scan] [{i}/{len(candidates)}] {cand.get('company_name')} ({cand.get('ipo_id')})", flush=True)
                    records.append(
                        _score_one(http, cand, fetch_gmp=fetch_gmp, gmp_page=page, model_dir=model_dir)
                    )
            finally:
                if page is not None:
                    try:
                        page.close()
                    except Exception:
                        pass
    finally:
        http.close()

    regime = fetch_market_regime(cache_path=out_dir / "analysis" / "market_regime.json")
    stamps = scrape_timestamps()
    for rec in records:
        rec["market_regime"] = regime
        rec["scraped_at_utc"] = rec.get("scraped_at_utc") or stamps["scraped_at_utc"]
        rec["scraped_at_ist"] = rec.get("scraped_at_ist") or stamps["scraped_at_ist"]
    records = rank_same_day_candidates(records)
    print(f"[scan] market_regime={regime} ranked={sum(1 for r in records if r.get('rank_of_day') is not None)}")

    audit_path = out_dir / "live_audit_log.csv"
    to_alert = records
    if write_audit and not dry_run:
        prior = read_audit(audit_path)
        to_alert = records_needing_alert(records, prior)
        skipped = len(records) - len(to_alert)
        if skipped:
            print(f"[scan] skip {skipped} unchanged alert(s) (gmp_rs/sub_total_x match audit)")
        upsert_audit(audit_path, records)
        print(f"[scan] wrote {audit_path}")
    dispatch(to_alert, dry_run=dry_run)
    return records


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Score IPOs closing today and send alerts")
    parser.add_argument("--as-of", default=None, help="YYYY-MM-DD IST (default: today IST)")
    parser.add_argument("--include-open", action="store_true", help="Also score currently-open IPOs")
    parser.add_argument("--no-gmp", action="store_true", help="Skip InvestorGain Playwright GMP fetch")
    parser.add_argument("--dry-run", action="store_true", help="Print cards; do not send or write the audit log")
    parser.add_argument("--out", default="data")
    parser.add_argument("--model-dir", default=None)
    args = parser.parse_args(argv)
    as_of = date.fromisoformat(args.as_of) if args.as_of else None
    model_dir = Path(args.model_dir) if args.model_dir else None
    try:
        run_scan(
            as_of=as_of,
            include_open=args.include_open,
            fetch_gmp=not args.no_gmp,
            dry_run=args.dry_run,
            write_audit=not args.dry_run,
            out_dir=Path(args.out),
            model_dir=model_dir,
        )
        return 0
    except Exception:
        send_failure_alert(traceback.format_exc())
        raise


if __name__ == "__main__":
    raise SystemExit(main())
