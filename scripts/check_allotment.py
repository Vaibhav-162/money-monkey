"""Notify when Chittorgarh shows allotment is out.

With PAN_PROFILES set, try an automated registrar lookup per person and email
that person only. Full PANs never go to git, logs, Telegram, or the audit CSV.
"""

from __future__ import annotations

import argparse
import sys
from contextlib import nullcontext
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from analysis.live_audit import AUDIT_COLUMNS, read_audit, write_audit
from chittorgarh.browser import chromium_session
from chittorgarh.http import HttpClient
from chittorgarh.live_dashboard import today_ist
from chittorgarh.parse_ipo import parse_ipo_html
from chittorgarh.registrar_allotment import checker_for_registrar, load_pan_profiles, mask_pan
from scripts.notify import (
    format_allotment_card,
    format_allotment_result_email,
    format_allotment_telegram_summary,
    format_email_digest,
    send_email,
    send_telegram,
)

EMAIL_STATUSES = {"allotted", "not_allotted"}


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


def _lookup_status(checker, page, company: str, pan: str) -> dict[str, Any]:
    try:
        return checker(page, company, pan)
    except Exception as exc:
        print(f"[allotment] checker error for {mask_pan(pan)}: {exc}")
        return {"status": "captcha_failed", "shares": None}


def dispatch_pan_results(
    record: dict[str, Any],
    profiles: list[dict[str, str]],
    *,
    page: Any = None,
    dry_run: bool = False,
) -> dict[str, int]:
    """Email allotted/not-allotted only; Telegram counts if anyone was emailed."""
    company = str(record.get("company_name") or record.get("ipo_id") or "IPO")
    registrar = record.get("registrar") or ""
    checker = checker_for_registrar(registrar)
    n_emailed = 0
    n_skipped = 0
    for profile in profiles:
        result: dict[str, Any] | None = None
        if checker is not None and page is not None:
            result = _lookup_status(checker, page, company, profile["pan"])
        status = (result or {}).get("status")
        if status not in EMAIL_STATUSES:
            n_skipped += 1
            continue
        n_emailed += 1
        body = format_allotment_result_email(
            label=profile.get("label") or "investor",
            company=company,
            registrar=registrar,
            status=status,
            shares=(result or {}).get("shares"),
        )
        subject = f"Allotment {status.replace('_', ' ')}: {company}"
        if dry_run:
            print(f"--- allotment email ({profile.get('label')}) ---")
            print(body)
            print("--------------------")
            continue
        try:
            send_email(subject, format_email_digest([body]), to_addr=profile["email"])
        except Exception as exc:
            print(f"[allotment] Email error for {profile.get('label')}: {exc}")

    if n_emailed > 0:
        summary = format_allotment_telegram_summary(
            record, n_profiles=len(profiles), n_emailed=n_emailed, n_skipped=n_skipped
        )
        if dry_run:
            print("--- allotment telegram summary ---")
            print(summary)
            print("--------------------")
        else:
            try:
                send_telegram(summary)
            except Exception as exc:
                print(f"[allotment] Telegram error for {company}: {exc}")
    elif dry_run:
        print("--- allotment telegram skipped (no emails sent) ---")
    return {"n_emailed": n_emailed, "n_skipped": n_skipped, "n_profiles": len(profiles)}


def run_check(
    *,
    out_dir: Path | None = None,
    audit_path: Path | None = None,
    rows: Optional[pd.DataFrame] = None,
    client: Optional[HttpClient] = None,
    as_of: Optional[date] = None,
    dry_run: bool = False,
    profiles: Optional[list[dict[str, str]]] = None,
    page: Any = None,
) -> list[dict[str, Any]]:
    out_dir = Path(out_dir) if out_dir else ROOT / "data"
    path = Path(audit_path) if audit_path else out_dir / "live_audit_log.csv"
    day = as_of or today_ist()
    frame = rows if rows is not None else read_audit(path)
    if frame.empty:
        print("[allotment] audit log empty")
        return []

    loaded = profiles if profiles is not None else load_pan_profiles()
    own = client is None
    http = client or HttpClient(cache_dir=out_dir / "cache_live", delay=1.5)
    updated_rows: list[dict[str, Any]] = []
    newly: list[dict[str, Any]] = []
    own_page = page is None and bool(loaded)
    session_cm = chromium_session() if own_page else nullcontext(None)
    try:
        with session_cm as context:
            lookup_page = page
            opened = None
            if lookup_page is None and context is not None:
                opened = context.new_page()
                lookup_page = opened
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
                        if loaded:
                            dispatch_pan_results(
                                inspected, loaded, page=lookup_page, dry_run=dry_run
                            )
                        else:
                            dispatch_allotment(inspected, dry_run=dry_run)
                    updated_rows.append(inspected)
            finally:
                if opened is not None:
                    try:
                        opened.close()
                    except Exception:
                        pass
    finally:
        if own:
            http.close()

    if not dry_run:
        persist = [{k: v for k, v in rec.items() if k in AUDIT_COLUMNS} for rec in updated_rows]
        write_audit(path, pd.DataFrame(persist))
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
