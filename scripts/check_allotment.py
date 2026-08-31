"""Notify when Chittorgarh shows allotment is out. No PAN scraping."""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from analysis.live_audit import read_audit, write_audit
from chittorgarh.http import HttpClient
from chittorgarh.live_dashboard import today_ist
from chittorgarh.parse_ipo import parse_ipo_html
from scripts.notify import format_allotment_card, format_email_digest, send_email, send_telegram


def _flag(value: Any) -> bool:
    return str(value).strip().lower() in {"true", "1", "yes"}


def _parse_day(value: Any) -> Optional[date]:
    if value is None or value == "":
        return None
    text = str(value)[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def is_allotment_due(
    row: dict[str, Any],
    as_of: date,
    *,
    min_days_after_close: int = 1,
    max_days_after_close: int = 4,
) -> bool:
    if _flag(row.get("allotment_notified")):
        return False
    close = _parse_day(row.get("close_date"))
    if close is None:
        return False
    age = (as_of - close).days
    return min_days_after_close <= age <= max_days_after_close


def allotment_is_out(master: dict[str, Any], row: dict[str, Any], as_of: date) -> bool:
    if master.get("allotment_published"):
        return True
    expected = _parse_day(master.get("allotment_date") or row.get("allotment_date"))
    return expected is not None and as_of >= expected


def inspect_row(client: HttpClient, row: dict[str, Any], as_of: date) -> dict[str, Any]:
    updated = dict(row)
    url = row.get("url")
    if not url:
        return updated
    html = client.get_text(url, cache_name=None, use_cache=False)
    parsed = parse_ipo_html(html, url=url, exchange_type=row.get("board"))
    master = parsed["master"]
    if master.get("registrar") and not updated.get("registrar"):
        updated["registrar"] = master["registrar"]
    if master.get("allotment_date") and not updated.get("allotment_date"):
        updated["allotment_date"] = master["allotment_date"]
    if allotment_is_out(master, updated, as_of):
        updated["allotment_notified"] = True
        updated["allotment_notified_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        updated["_just_notified"] = True
    return updated


def dispatch_allotment(record: dict[str, Any], *, dry_run: bool = False) -> None:
    card = format_allotment_card(record)
    company = record.get("company_name") or record.get("ipo_id") or "IPO"
    if dry_run:
        print("--- allotment card ---")
        print(card)
        print("--------------------")
        return
    try:
        send_telegram(card)
    except Exception as exc:
        print(f"[allotment] Telegram error for {company}: {exc}")
    try:
        send_email(f"Allotment out: {company}", format_email_digest([card]))
    except Exception as exc:
        print(f"[allotment] Email error for {company}: {exc}")


def run_check(
    *,
    out_dir: Path | None = None,
    audit_path: Path | None = None,
    rows: Optional[pd.DataFrame] = None,
    client: Optional[HttpClient] = None,
    as_of: Optional[date] = None,
    dry_run: bool = False,
) -> list[dict[str, Any]]:
    out_dir = Path(out_dir) if out_dir else ROOT / "data"
    path = Path(audit_path) if audit_path else out_dir / "live_audit_log.csv"
    day = as_of or today_ist()
    frame = rows if rows is not None else read_audit(path)
    if frame.empty:
        print("[allotment] audit log empty")
        return []

    own = client is None
    http = client or HttpClient(cache_dir=out_dir / "cache_live", delay=1.5)
    updated_rows: list[dict[str, Any]] = []
    newly: list[dict[str, Any]] = []
    try:
        for rec in frame.to_dict(orient="records"):
            if not is_allotment_due(rec, day):
                updated_rows.append(rec)
                continue
            try:
                inspected = inspect_row(http, rec, day)
            except Exception as exc:
                rec = dict(rec)
                rec["error"] = f"allotment:{exc}"
                updated_rows.append(rec)
                continue
            if inspected.pop("_just_notified", False):
                newly.append(inspected)
                dispatch_allotment(inspected, dry_run=dry_run)
            updated_rows.append(inspected)
    finally:
        if own:
            http.close()

    if not dry_run:
        write_audit(path, pd.DataFrame(updated_rows))
    print(f"[allotment] newly_out={len(newly)} of {len(frame)}")
    return newly


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Notify when allotment results go live")
    parser.add_argument("--out", default="data")
    parser.add_argument("--as-of", default=None, help="YYYY-MM-DD IST")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    as_of = date.fromisoformat(args.as_of) if args.as_of else None
    run_check(out_dir=Path(args.out), as_of=as_of, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
