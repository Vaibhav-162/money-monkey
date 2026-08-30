"""Zero-cost alert dispatch: Telegram (primary) and optional SMTP email."""

from __future__ import annotations

import os
import re
import smtplib
from email.mime.text import MIMEText
from typing import Any, Optional

import httpx

_TOKEN_IN_URL = re.compile(r"/bot\d+:[A-Za-z0-9_-]+/")


def _redact(text: str) -> str:
    """Strip a Telegram bot token out of an exception string before it is
    printed to logs (GitHub Actions logs are not private to just the owner
    if the repo/workflow is ever made public)."""
    return _TOKEN_IN_URL.sub("/bot***REDACTED***/", text)

CHECK_LABELS = {
    "subscription": "sub_gt_20",
    "ofs_ratio": "ofs_lt_50",
    "roe": "roe_gt_15",
    "debt_equity": "de_le_05",
}


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def _fmt_num(value: Any, digits: int = 2, suffix: str = "") -> str:
    if value is None or value == "":
        return "n/a"
    try:
        num = float(value)
    except (TypeError, ValueError):
        return str(value)
    if num != num:  # NaN
        return "n/a"
    return f"{num:.{digits}f}{suffix}"


def _fmt_pct_odds(p_allot: Any) -> str:
    if p_allot is None or p_allot == "":
        return "n/a"
    try:
        num = float(p_allot)
    except (TypeError, ValueError):
        return "n/a"
    if num != num:
        return "n/a"
    return f"{num * 100:.1f}%"


def _fmt_gmp(record: dict[str, Any]) -> str:
    gmp = record.get("gmp_rs")
    pct = record.get("gmp_pct")
    if gmp is None or gmp == "":
        return "not available"
    try:
        if float(gmp) != float(gmp):
            return "not available"
    except (TypeError, ValueError):
        return "not available"
    extra = f" ({_fmt_num(pct, 1, '%')})" if pct not in (None, "") else ""
    return f"Rs {_fmt_num(gmp, 1)}{extra}"


def _check_lines(record: dict[str, Any]) -> list[str]:
    raw = record.get("quality_breakdown")
    if raw is None and record.get("quality_breakdown_json"):
        import json

        try:
            raw = json.loads(record["quality_breakdown_json"])
        except (TypeError, ValueError):
            raw = []
    lines = []
    for check in raw or []:
        label = CHECK_LABELS.get(check.get("name"), check.get("name"))
        status = str(check.get("status") or "unknown").upper()
        lines.append(f"  {label}: {status}")
    return lines or ["  (no checklist breakdown)"]


def format_card(record: dict[str, Any]) -> str:
    company = record.get("company_name") or record.get("ipo_id") or "Unknown IPO"
    board = record.get("board") or record.get("exchange_type") or "?"
    close = record.get("close_date") or "?"
    error = record.get("error")
    if error:
        return "\n".join(
            [
                f"{company}",
                f"Board: {board} | Close: {close}",
                "",
                "SCAN ERROR - no decision produced. This is NOT a SKIP; the",
                "scrape or scoring step failed for this IPO and the numbers",
                "below cannot be trusted.",
                f"  {str(error)[:400]}",
                "",
                "Check the site structure or logs before treating this as a signal.",
            ]
        )
    band = record.get("price_band_high")
    lot = record.get("lot_size")
    apply_s1 = bool(record.get("apply_s1"))
    decision = "APPLY" if apply_s1 else "SKIP"
    apply_s2 = bool(record.get("apply_s2"))
    hold = "HOLD CANDIDATE" if apply_s2 else "FLIP ONLY"
    q = record.get("quality_score")
    q_txt = _fmt_num(q, 0) if q not in (None, "") else "n/a"
    lines = [
        f"{company}",
        f"Board: {board} | Close: {close}",
        f"Price band (high): {_fmt_num(band)} | Lot: {lot if lot not in (None, '') else 'n/a'}",
        f"GMP: {_fmt_gmp(record)} | Sub: {_fmt_num(record.get('sub_total_x'), 2, 'x')}",
        "",
        f"Strategy 1 (listing pop): {decision}",
        f"  EV per lot: Rs {_fmt_num(record.get('ev_retail'), 0)}",
        f"  Allotment odds: {_fmt_pct_odds(record.get('p_allot'))}",
        f"  P(clean pop): {_fmt_num(record.get('p_pop'), 3)}",
        "",
        f"Strategy 2 (quality): {hold}  ({q_txt}/4)",
        *_check_lines(record),
        "",
        "Historical statistical scorer, not investment advice.",
    ]
    return "\n".join(lines)


def send_telegram(text: str, *, token: Optional[str] = None, chat_id: Optional[str] = None) -> bool:
    token = token if token is not None else _env("TELEGRAM_BOT_TOKEN")
    chat_id = chat_id if chat_id is not None else _env("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("[notify] Telegram skipped (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID unset)")
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    response = httpx.post(
        url,
        data={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
        timeout=30.0,
    )
    response.raise_for_status()
    return True


def send_email(
    subject: str,
    body: str,
    *,
    user: Optional[str] = None,
    password: Optional[str] = None,
    to_addr: Optional[str] = None,
    host: Optional[str] = None,
    port: Optional[int] = None,
) -> bool:
    user = user if user is not None else _env("GMAIL_USER")
    password = password if password is not None else (_env("GMAIL_APP_PASSWORD") or _env("GMAIL_PASS"))
    to_addr = to_addr if to_addr is not None else (_env("ALERT_EMAIL_TO") or user)
    host = host if host is not None else (_env("SMTP_HOST") or "smtp.gmail.com")
    port = port if port is not None else int(_env("SMTP_PORT") or "587")
    if not user or not password or not to_addr:
        print("[notify] Email skipped (GMAIL_USER / GMAIL_APP_PASSWORD unset)")
        return False
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to_addr
    with smtplib.SMTP(host, port, timeout=30) as smtp:
        smtp.starttls()
        smtp.login(user, password)
        smtp.sendmail(user, [to_addr], msg.as_string())
    return True


def send_failure_alert(error: str) -> None:
    text = f"IPO live scanner FAILED\n{error[:1500]}"
    try:
        send_telegram(text)
    except Exception as exc:
        print(f"[notify] failure Telegram also failed: {_redact(str(exc))}")
    try:
        send_email("IPO live scanner FAILED", text)
    except Exception as exc:
        print(f"[notify] failure email also failed: {_redact(str(exc))}")


def dispatch(records: list[dict[str, Any]], *, dry_run: bool = False) -> int:
    """One card per IPO. Missing credentials are a no-op, not an error."""
    sent = 0
    for rec in records:
        card = format_card(rec)
        company = rec.get("company_name") or rec.get("ipo_id") or "IPO"
        if dry_run:
            print("--- dry-run card ---")
            print(card)
            print("--------------------")
            continue
        try:
            if send_telegram(card):
                sent += 1
        except Exception as exc:
            print(f"[notify] Telegram error for {company}: {_redact(str(exc))}")
        try:
            send_email(f"IPO alert: {company}", card)
        except Exception as exc:
            print(f"[notify] Email error for {company}: {_redact(str(exc))}")
    return sent
