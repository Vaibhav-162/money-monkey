"""Notify when Chittorgarh shows allotment is out.

With PAN_PROFILES set, try an automated registrar lookup per person and email
that person only. Full PANs never go to git, logs, Telegram, or the audit CSV.

WHAT THIS FILE DOES
--------------------
A few days after the bidding window closes, the registrar publishes the
Basis of Allotment (who got shares). This script walks rows already in
`data/live_audit_log.csv`, re-fetches each IPO's Chittorgarh detail page,
and notifies the first time allotment is detected as out.

`.github/workflows/check_allotment.yml` is the production entrypoint: it
runs `python scripts/check_allotment.py --out data` on a weekday `schedule`
at 12:00 IST (`30 6 * * 1-5`) and on `workflow_dispatch` (same `dry_run`
checkbox as the close-day workflow — default true so a manual test cannot
write `allotment_notified` and burn the one real send). The job installs
Tesseract OCR because registrar portals put a captcha in front of the
PAN lookup. Locally this file is also a CLI (`--out`, `--as-of`,
`--dry-run`). Nothing else in the package imports `run_check()` except
tests.

Unlike the close-day alert (one shot per `(ipo_id, close_date)`), this
re-checks daily for IPOs that closed 1–4 IST days ago until allotment is
out (or the window expires). Once a successful (or intentionally skipped)
dispatch marks `allotment_notified`, later runs leave that row alone.

With `PAN_PROFILES` set (JSON `{label, pan, email}` in a GitHub secret,
never a repo file), Playwright + Tesseract try KFintech / MUFG Intime
lookups and email **that person only** for a confirmed Allotted or Not
allotted result. Unset `PAN_PROFILES` falls back to one generic
"allotment out" Telegram + digest email. This file does **not** call
`notify.dispatch()`; it uses `send_telegram` / `send_email` and the
allotment formatters from `scripts.notify`, plus `checker_for_registrar`
/ `load_pan_profiles` / `mask_pan` from `chittorgarh.registrar_allotment`.

`allotment_notified` is set only **after** dispatch returns. If
`NotificationDeliveryError` is raised, the flag is never persisted, so
the next run can retry instead of silently losing the alert.

KEY TERMS USED HERE
--------------------
- Allotment / Basis of Allotment (BoA): the registrar's published result
  of the application lottery. "Out" is detected from a BoA link/heading
  on the Chittorgarh page (`allotment_published`), or — if that is
  absent — from the timetable `allotment_date` having arrived.
- Registrar: the house that runs the lottery and the PAN-lookup portal
  (automated checkers exist for KFintech and MUFG Intime / Link Intime;
  other names stay on the generic "allotment out" card).
- PAN (Permanent Account Number): India's personal tax ID used to look
  up one person's application. Full PANs never go to git, logs,
  Telegram, the audit CSV, or a shared digest. Logs use `mask_pan()`.
  Telegram summaries are counts only ("N emailed, M skipped").
- PAN_PROFILES: optional Actions secret — a JSON array of
  `{label, pan, email}`. Each allotted/not-allotted result is emailed to
  that profile's address from the shared `GMAIL_USER` sender.
- Close date: the last bidding day. `is_allotment_due` only inspects rows
  whose close date is 1–4 IST days ago and not already notified.
- OCR / captcha: registrar pages require a captcha. The workflow
  installs Tesseract; `chittorgarh.registrar_allotment` OCRs it. A miss
  becomes `captcha_failed` — no email, and Telegram is skipped too if
  nobody in that batch was emailed.
- EMAIL_STATUSES (`allotted`, `not_allotted`): the only lookup outcomes
  that generate a personalized email. `no_application`, `captcha_failed`,
  and `lookup_failed` stay silent (but a detected "allotment out" still
  marks the row notified so we do not retry forever).
- Dry-run: print cards/emails, do not send and do not write the audit.
  Same reason as the close-day scan — a test must not consume the one
  real notification.
- Idempotency / re-check: daily re-inspect until allotment is out or
  the 4-day window ends; then one notification and stop. Different from
  the close-day presence-only gate, which fires once on the close date
  itself.

FUNCTIONS / CLASSES IN THIS FILE
---------------------------------
- `run_check(...)`: walk the audit CSV, inspect due rows, dispatch
  (PAN emails or generic card), then persist. Dispatch happens before
  `allotment_notified` is set; a raised delivery error aborts the write
  so the next run can retry.
- `is_allotment_due(row, as_of, ...)`: True when the row is inside the
  1–4 day post-close window and not already flagged notified.
- `allotment_is_out(master, row, as_of)`: BoA published, or the expected
  allotment date has arrived.
- `inspect_row(client, row, as_of)`: live-fetch the detail URL, refresh
  registrar / allotment_date, and set a transient `_just_notified`
  marker. Does not persist `allotment_notified` — that is `run_check`'s
  job after send succeeds.
- `dispatch_allotment(record, dry_run)`: generic "allotment out" Telegram
  + digest email (no PAN_PROFILES). Raises `NotificationDeliveryError`
  when both real channels throw; unconfigured channels stay a no-op.
- `dispatch_pan_results(record, profiles, page, dry_run)`: per-profile
  registrar lookup + personalized email. Raises only when every profile
  that reached Allotted/Not allotted had a real email exception. A
  count-only Telegram summary is sent if anyone was emailed; a Telegram
  failure there is printed, not raised (email already went out).
- `_lookup_status(checker, page, company, pan)`: one registrar call;
  captcha/timeout becomes `captcha_failed` with a masked-PAN log line.
- `_flag` / `_parse_day`: tiny parsers for audit-CSV strings.
- `main(argv)`: CLI wrapper around `run_check`.
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
from chittorgarh.registrar_allotment import (
    checker_for_registrar,
    load_pan_profiles,
    mask_pan,
    raise_if_systematic_lookup_failure,
)
from scripts.notify import (
    NotificationDeliveryError,
    _redact,
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
    # Detect "out" here but do NOT set allotment_notified yet -- that flag
    # gates all future re-checks, so it must only be persisted after
    # dispatch succeeds (or is an intentional no-op), same ordering fix as
    # live_scanner's dispatch-before-audit write.
    if allotment_is_out(master, updated, as_of):
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
    sent = 0
    telegram_errors: list[str] = []
    try:
        if send_telegram(card):
            sent += 1
    except Exception as exc:
        msg = f"Telegram error for {company}: {_redact(str(exc))}"
        print(f"[allotment] {msg}")
        telegram_errors.append(msg)
    email_error: Optional[str] = None
    try:
        send_email(f"Allotment out: {company}", format_email_digest([card]))
    except Exception as exc:
        email_error = f"Email error for {company}: {_redact(str(exc))}"
        print(f"[allotment] {email_error}")
    if sent == 0 and email_error is not None:
        details = "; ".join(telegram_errors + [email_error])
        raise NotificationDeliveryError(
            f"Both Telegram and email failed for allotment alert ({company}): {details}"
        )


def _lookup_status(checker, page, company: str, pan: str) -> dict[str, Any]:
    try:
        return checker(page, company, pan)
    except Exception as exc:
        print(f"[allotment] checker error for {mask_pan(pan)}: {exc}")
        return {"status": "lookup_failed", "shares": None}


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
    email_attempts = 0
    email_errors: list[str] = []
    lookup_results: list[dict[str, Any]] = []
    for profile in profiles:
        result: dict[str, Any] | None = None
        if checker is not None and page is not None:
            result = _lookup_status(checker, page, company, profile["pan"])
            lookup_results.append(result)
        status = (result or {}).get("status")
        if status not in EMAIL_STATUSES:
            n_skipped += 1
            continue
        body = format_allotment_result_email(
            label=profile.get("label") or "investor",
            company=company,
            registrar=registrar,
            status=status,
            shares=(result or {}).get("shares"),
        )
        subject = f"Allotment {status.replace('_', ' ')}: {company}"
        if dry_run:
            n_emailed += 1
            print(f"--- allotment email ({profile.get('label')}) ---")
            print(body)
            print("--------------------")
            continue
        email_attempts += 1
        label = profile.get("label") or "investor"
        try:
            if send_email(subject, format_email_digest([body]), to_addr=profile["email"]):
                n_emailed += 1
        except Exception as exc:
            msg = f"Email error for {label}: {_redact(str(exc))}"
            print(f"[allotment] {msg}")
            email_errors.append(msg)

    raise_if_systematic_lookup_failure(lookup_results)

    # Every profile that reached EMAIL_STATUSES got a real send attempt and
    # every one threw -- not the intentional "Gmail unset" False return.
    if email_attempts > 0 and n_emailed == 0 and len(email_errors) == email_attempts:
        raise NotificationDeliveryError(
            f"All {email_attempts} personalized allotment email(s) failed for {company}: "
            + "; ".join(email_errors)
        )

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
                print(f"[allotment] Telegram error for {company}: {_redact(str(exc))}")
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
                        # Only mark notified after delivery succeeds (or was an
                        # intentional skip). If dispatch raised above, this row
                        # never lands in the audit as notified and the next run
                        # can retry instead of silently losing the alert.
                        inspected["allotment_notified"] = True
                        inspected["allotment_notified_at"] = datetime.now(
                            timezone.utc
                        ).strftime("%Y-%m-%dT%H:%M:%SZ")
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
