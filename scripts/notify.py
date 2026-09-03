"""Zero-cost alert dispatch: Telegram (primary) and optional SMTP email.

WHAT THIS FILE DOES
--------------------
This is the single place that turns a scored IPO record into human-readable
messages and actually sends them. Nothing else in the codebase talks to
Telegram or Gmail directly.

`scripts/live_scanner.py` is the close-day caller: it imports `dispatch()`
(one Telegram card per IPO plus one email digest) and `send_failure_alert()`
(pipeline-crash / empty-dashboard heads-up). `.github/workflows/daily_ipo_alert.yml`
invokes that scanner as `python scripts/live_scanner.py --out data` on three
weekday crons (15:15 / 15:30 / 16:00 IST) and on `workflow_dispatch`.

`scripts/check_allotment.py` is the allotment-day caller: it does **not**
use `dispatch()`. It imports the allotment formatters plus `send_telegram()` /
`send_email()` / `NotificationDeliveryError` and sends its own cards.
`.github/workflows/check_allotment.yml` runs that script as
`python scripts/check_allotment.py --out data` at 12:00 IST weekdays.

`dispatch()` raises `NotificationDeliveryError` when a real (non-dry) batch
has records and **both** channels genuinely fail (an exception was raised).
A channel that is simply unconfigured returns False and is an intentional
silent no-op. The raise exists so a dead Gmail app password or bad Telegram
token becomes a red CI failure instead of a green run that delivered
nothing. Callers therefore send **before** writing the audit /
`allotment_notified` flag, so a failed delivery does not consume the day's
one-alert slot.

KEY TERMS USED HERE
--------------------
- GMP (Grey Market Premium): the unofficial premium buyers pay for IPO
  shares before listing, in rupees over the issue price. Cards show
  `gmp_rs` from InvestorGain, plus the as-of date when we have one.
- Subscription multiple (`sub_total_x` / `sub_ig_x`): how many times the
  issue is oversubscribed. Chittorgarh (`sub_total_x`) and InvestorGain
  (`sub_ig_x`) can disagree; the card prints both when present.
- QIB (Qualified Institutional Buyer): large funds/banks whose subscription
  (`sub_qib_x`) is shown as an informational fifth checklist line. It does
  not affect the /4 quality score.
- OFS (Offer For Sale): existing shareholders selling their shares, as
  opposed to the company raising fresh capital. The checklist row
  "Fresh Capital / Low OFS" is one of the four scored quality checks.
- apply_s1 / apply_s2: the two trading-strategy decisions. S1 is "apply for
  a listing-day pop" (uses `p_pop`, `p_allot`, `ev_retail`). S2 is
  "quality / hold" and drives the hold-vs-flip copy from `quality_score`
  plus market regime.
- EV (Expected Value): `ev_retail` — predicted rupees of expected listing
  gain per retail lot after haircutting for allotment odds. The S1 APPLY
  copy cites it as the reason to lock up capital.
- Quality checklist: four scored PASS/FAIL/UNKNOWN rows (subscription,
  OFS, ROE, debt-to-equity) rendered with emoji. Machine keys like
  `sub_gt_20` are never printed.
- Market regime (`BULLISH` / `BEARISH` / `NEUTRAL`): Nifty-50 5-session
  flag stamped by the scanner. Only the S2 score-2 (moderate) copy
  branches on it.
- Price band / lot size: the high end of the issue price range
  (`price_band_high`) and the minimum-application share count. Together
  they are the cash a retail applicant must lock up.
- Mainboard vs SME: `board` / `exchange_type` on the card header. NSE/BSE
  mainboard issues vs the smaller SME boards; the scorer trains them
  separately.
- Close date: the last day of the bidding window — the deadline the
  weekday afternoon scan is racing. Shown on every card.
- Registrar: the house that runs the allotment lottery and PAN-lookup
  portal (KFin / Karvy, MUFG Intime / Link Intime, Bigshare, Cameo,
  Skyline, Purva). Allotment cards link the matching portal, or fall
  back to Chittorgarh's allotment-status page.
- Allotment: whether an applicant actually received shares. Personalized
  emails say Allotted / Not allotted / No application; they never include
  a PAN.
- PAN (Permanent Account Number): India's personal tax ID used to look up
  one person's application. Never put a PAN in a Telegram message, email
  subject, or log line. Telegram allotment summaries are counts only.
- Dry-run: `--dry-run` / the Actions `workflow_dispatch` `dry_run`
  checkbox. `dispatch()` prints cards and returns 0 without sending, so a
  manual test cannot consume the day's one real alert.
- Failure-alert dedup: `send_failure_alert()` writes
  `data/live_alert_state.json` with today's IST date so a crashing scanner
  sends at most one "FAILED" Telegram/email per calendar day.

FUNCTIONS / CLASSES IN THIS FILE
---------------------------------
- `dispatch(records, dry_run)`: the main close-day entry point. One
  Telegram message per IPO plus one email digest. Raises
  `NotificationDeliveryError` if a real send fails on both channels so a
  broken credential is a loud CI failure, not a silent one. Unconfigured
  channels stay a silent no-op.
- `NotificationDeliveryError`: raised by `dispatch()` (and by
  `check_allotment`'s own send helpers) when every attempted real channel
  threw. Callers must not mark the audit "already alerted" until this
  either succeeds or is an intentional skip.
- `send_telegram(text)` / `send_email(subject, body, ...)`: thin HTTP/SMTP
  wrappers. Return False (no exception) when secrets are unset; raise on
  a genuine API/SMTP error. Email `to_addr` overrides `ALERT_EMAIL_TO`
  so allotment results can go to one person only.
- `send_failure_alert(error, state_path)`: best-effort Telegram + email
  of a scanner crash / empty-dashboard warning, capped at one send per
  IST day via the state JSON. Its own send exceptions are printed, not
  re-raised — a failure-alert must not hide the original traceback.
- `format_card(record)`: close-day Telegram/email HTML. SCAN ERROR cards
  are distinct from SKIP so a scrape/score failure is never mistaken for
  a model decision.
- `format_email_digest(cards)`: wraps one or more cards in a Gmail-safe
  HTML document (newlines become `<br>`).
- `format_allotment_card(record)` / `format_allotment_result_email(...)` /
  `format_allotment_telegram_summary(...)`: allotment-out copy used by
  `check_allotment.py`. Personalized emails never include PAN; Telegram
  gets counts only.
- `registrar_portal_url(name)`: maps a registrar name blob onto that
  house's public PAN-lookup URL.
- Secret / format helpers (`_redact`, `_clean_secret`, `_env`,
  `_split_recipients`, `_app_password`, `_fmt_*`, `_checklist_lines`,
  `_s2_status_and_copy`, `_safe_print`): strip Windows-quoted secrets and
  Telegram tokens out of logs, skip malformed `ALERT_EMAIL_TO` addresses,
  and keep ₹ from crashing a Windows cp1252 dry-run print.
"""

from __future__ import annotations

import html
import json
import os
import re
import smtplib
from datetime import datetime
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

import httpx

_TOKEN_IN_URL = re.compile(r"/bot\d+:[A-Za-z0-9_-]+/")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Canonical checklist: never print machine keys like sub_gt_20.
CHECKLIST = (
    (("subscription", "sub_gt_20"), "Total Subscription (>20x)"),
    (("ofs_ratio", "ofs_lt_50"), "Fresh Capital / Low OFS"),
    (("roe", "roe_gt_15"), "Return on Equity (>15%)"),
    (("debt_equity", "de_le_05"), "Low Debt-to-Equity"),
)
CHECK_KEY_TO_LABEL = {key: label for keys, label in CHECKLIST for key in keys}

S1_APPLY = (
    "The model predicts an {p_pop}% probability of a clean listing pop. "
    "While your odds of getting an allotment are roughly {p_allot}%, the "
    "Expected Value (EV) per lot is ₹{ev}. The statistical reward justifies "
    "locking up the capital."
)
S1_SKIP = (
    "The model advises against applying. With a {p_pop}% chance of a clean "
    "pop and an Expected Value of ₹{ev} per lot, the mathematical risk "
    "heavily outweighs the reward."
)
S2_PARTIAL = (
    "Valuations and fundamentals are strong. Book 50% profit at market open "
    "to recover capital. Hold the remaining 50% with a strict 10% trailing "
    "stop-loss below the listing price."
)
S2_MODERATE_BULL = (
    "Fundamentals are mixed, but broader markets are bullish. You may hold "
    "with a strict 5% stop-loss, or flip entirely for safety."
)
S2_MODERATE_BEAR = (
    "Fundamentals are mixed and the broader market is weak. Sell 100% on "
    "listing day to secure profits."
)
S2_MODERATE_NEUTRAL = (
    "Fundamentals are mixed and market direction is unclear. Take a small "
    "partial exit and hold only a token position with a tight stop-loss."
)
S2_WEAK = "Weak fundamentals. Liquidate 100% at market open."
DISCLAIMER = "Historical statistical scorer, not investment advice."

CHITTORGARH_ALLOTMENT_URL = "https://www.chittorgarh.com/ipo_allotment_status/"
REGISTRAR_PORTALS = (
    (("mufg", "link intime", "linkintime"), "https://linkintime.co.in/offer/public-issue/"),
    (("kfin", "karvy"), "https://kosmic.kfintech.com/ipostatus/"),
    (("bigshare",), "https://ipo.bigshareonline.com/IPO_Status.html"),
    (("cameo",), "https://ipostatus.cameoindia.com/"),
    (("skyline",), "https://www.skylinerta.com/ipo.php"),
    (("purva",), "https://www.purvashare.com/investor-service/ipo-query"),
)


def _redact(text: str) -> str:
    """Strip a Telegram bot token out of an exception string before it is
    printed to logs (GitHub Actions logs are not private to just the owner
    if the repo/workflow is ever made public)."""
    return _TOKEN_IN_URL.sub("/bot***REDACTED***/", text)


def _clean_secret(value: str, *, drop_internal_space: bool = False) -> str:
    """Undo Windows `set VAR="quoted"` and optional Google 4x4 app-password spaces."""
    raw = (value or "").strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in {"'", '"'}:
        raw = raw[1:-1].strip()
    if drop_internal_space:
        raw = re.sub(r"\s+", "", raw)
    return raw


def _env(name: str) -> str:
    return _clean_secret(os.environ.get(name) or "")


def _split_recipients(value: str) -> list[str]:
    """Split a comma-separated recipient string into unique, stripped addresses.

    Malformed entries are skipped with a warning so one bad ALERT_EMAIL_TO
    token cannot make smtplib.sendmail() reject the whole batch.
    """
    cleaned = _clean_secret(value)
    recipients: list[str] = []
    seen: set[str] = set()
    for part in cleaned.split(","):
        addr = part.strip()
        if not addr:
            continue
        if not _EMAIL_RE.match(addr):
            print(f"[notify] skipping malformed email recipient: {addr!r}")
            continue
        key = addr.lower()
        if key in seen:
            continue
        seen.add(key)
        recipients.append(addr)
    return recipients


def _app_password(value: Optional[str] = None) -> str:
    raw = value if value is not None else (_env("GMAIL_APP_PASSWORD") or _env("GMAIL_PASS"))
    return _clean_secret(raw, drop_internal_space=True)


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes"}
    return bool(value)


def _fmt_prob_pct(value: Any) -> str:
    if value is None or value == "":
        return "n/a"
    try:
        num = float(value)
    except (TypeError, ValueError):
        return "n/a"
    if num != num:
        return "n/a"
    return f"{num * 100:.1f}"


def _fmt_money(value: Any, *, digits: int | None = None) -> str:
    if value is None or value == "":
        return "n/a"
    try:
        num = float(value)
    except (TypeError, ValueError):
        return "n/a"
    if num != num:
        return "n/a"
    if digits is None:
        if abs(num - round(num)) < 1e-9:
            return f"{int(round(num)):,}"
        return f"{num:,.2f}"
    return f"{num:,.{digits}f}"


_MONTHS = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)


def _fmt_gmp_asof(record: dict[str, Any]) -> str:
    raw = record.get("gmp_date_raw")
    if raw:
        return str(raw).split()[0]
    iso = record.get("gmp_as_of") or record.get("gmp_close_date")
    if not iso:
        return ""
    text = str(iso)[:10]
    try:
        _year, month, day = text.split("-")
        return f"{int(day):02d}-{_MONTHS[int(month) - 1]}"
    except (TypeError, ValueError, IndexError):
        return str(iso)


def _fmt_gmp_hype(record: dict[str, Any]) -> str:
    gmp = _fmt_gmp_amount(record)
    asof = _fmt_gmp_asof(record)
    src = "InvestorGain, as of " + asof if asof else "InvestorGain"
    if gmp == "not available":
        return f"Grey Market Premium is currently not available ({src})."
    return f"Grey Market Premium is currently {gmp} ({src})."


def _fmt_sub_hype(record: dict[str, Any]) -> str:
    chit = _fmt_money(record.get("sub_total_x"), digits=2)
    ig = _fmt_money(record.get("sub_ig_x"), digits=2)
    qib = _fmt_qib(record)
    parts: list[str] = []
    if chit != "n/a":
        parts.append(f"{chit}x Chittorgarh")
    if ig != "n/a":
        parts.append(f"{ig}x InvestorGain")
    sub_txt = " | ".join(parts) if parts else "n/a"
    return f"Subscription: {sub_txt} ({qib})."


def _fmt_gmp_amount(record: dict[str, Any]) -> str:
    gmp = record.get("gmp_rs")
    if gmp is None or gmp == "":
        return "not available"
    try:
        if float(gmp) != float(gmp):
            return "not available"
    except (TypeError, ValueError):
        return "not available"
    return f"₹{_fmt_money(gmp)}"


def _quality_score(record: dict[str, Any]) -> int | None:
    q = record.get("quality_score")
    if q in (None, ""):
        return 3 if _as_bool(record.get("apply_s2")) else 0
    try:
        return int(float(q))
    except (TypeError, ValueError):
        return None


def _breakdown_status_map(record: dict[str, Any]) -> dict[str, str]:
    raw = record.get("quality_breakdown")
    if raw is None and record.get("quality_breakdown_json"):
        try:
            raw = json.loads(record["quality_breakdown_json"])
        except (TypeError, ValueError):
            raw = []
    mapped: dict[str, str] = {}
    if isinstance(raw, dict):
        pairs = raw.items()
    else:
        pairs = ((check.get("name"), check.get("status")) for check in raw or [])
    for key, status in pairs:
        label = CHECK_KEY_TO_LABEL.get(str(key or ""))
        if label:
            mapped[label] = str(status or "unknown").upper()
    return mapped


def _checklist_lines(record: dict[str, Any]) -> list[str]:
    """Four scored rows in order, then one informational QIB line."""
    statuses = _breakdown_status_map(record)
    lines = []
    for _keys, label in CHECKLIST:
        status = statuses.get(label, "UNKNOWN")
        pretty = status.replace("_", " ")
        lines.append(f"{_check_icon(status)} {_esc(label)}: {_esc(pretty)}")
    # Informational only — ℹ️ is distinct from PASS/FAIL/NOT DISCLOSED icons.
    lines.append(f"ℹ️ QIB Demand: {_esc(_fmt_qib(record))}")
    return lines


def _check_icon(status: str) -> str:
    if status == "PASS":
        return "✅"
    if status == "FAIL":
        return "❌"
    return "❓"


def _fmt_qib(record: dict[str, Any]) -> str:
    qib = record.get("sub_qib_x")
    if qib is None or qib == "":
        return "QIB not available"
    try:
        num = float(qib)
    except (TypeError, ValueError):
        return "QIB not available"
    if num != num:
        return "QIB not available"
    return f"QIB {num:.2f}x"


def _s2_status_and_copy(record: dict[str, Any]) -> tuple[str, str]:
    q = _quality_score(record)
    q_txt = str(q) if q is not None else "n/a"
    regime = str(record.get("market_regime") or "NEUTRAL").upper()
    if q is not None and q >= 3:
        return (
            f"💎 <b>PARTIAL HOLD (Score {q_txt}/4)</b>",
            S2_PARTIAL,
        )
    if q == 2 and regime == "BULLISH":
        return (
            f"⚖️ <b>MODERATE (Score {q_txt}/4) - MARKET TAILWIND</b>",
            S2_MODERATE_BULL,
        )
    if q == 2 and regime == "BEARISH":
        return (
            f"⚠️ <b>FLIP ONLY (Score {q_txt}/4) - MARKET HEADWIND</b>",
            S2_MODERATE_BEAR,
        )
    if q == 2:
        return (
            f"⚖️ <b>MODERATE (Score {q_txt}/4) - MARKET UNCLEAR</b>",
            S2_MODERATE_NEUTRAL,
        )
    return (
        f"🔴 <b>FLIP ONLY (Score {q_txt}/4)</b>",
        S2_WEAK,
    )


def registrar_portal_url(name: Any) -> str:
    blob = str(name or "").lower()
    for keys, url in REGISTRAR_PORTALS:
        if any(key in blob for key in keys):
            return url
    return CHITTORGARH_ALLOTMENT_URL


def format_allotment_card(record: dict[str, Any]) -> str:
    company = record.get("company_name") or record.get("ipo_id") or "Unknown IPO"
    registrar = record.get("registrar") or "unknown registrar"
    portal = registrar_portal_url(registrar)
    label = str(record.get("label") or "").strip()
    greeting = f"Hi {_esc(label)},\n" if label else ""
    return (
        f"{greeting}"
        f"📢 <b>ALLOTMENT OUT: {_esc(company)}</b>\n"
        f"Registrar: {_esc(registrar)}.\n"
        "Check your PAN status at their portal "
        f'(do not send PAN to this bot): <a href="{_esc(portal)}">{_esc(portal)}</a>'
    )


def format_allotment_result_email(
    *,
    label: str,
    company: str,
    registrar: str,
    status: str,
    shares: Any = None,
) -> str:
    """Personalized Allotted / Not allotted / No application card. Never includes PAN."""
    who = str(label or "investor").strip() or "investor"
    name = company or "Unknown IPO"
    house = registrar or "the registrar"
    portal = registrar_portal_url(house)
    key = str(status or "").strip().lower()
    if key == "allotted":
        headline = "ALLOTTED"
        if shares not in (None, ""):
            detail = f"Shares allotted: {_esc(shares)}."
        else:
            detail = "Shares allotted: the registrar confirmed an allotment (share count not parsed)."
    elif key == "not_allotted":
        headline = "NOT ALLOTTED"
        detail = "The registrar shows no shares allotted for this application."
    elif key == "no_application":
        headline = "NO APPLICATION FOUND"
        detail = "The registrar did not find an application under this profile."
    else:
        headline = "CHECK MANUALLY"
        detail = "Automated lookup could not confirm a result."
    return (
        f"Hi {_esc(who)},\n"
        f"📢 <b>{_esc(headline)}: {_esc(name)}</b>\n"
        f"Registrar: {_esc(house)}.\n"
        f"{detail}\n"
        "Confirm on the registrar portal before acting: "
        f'<a href="{_esc(portal)}">{_esc(portal)}</a>'
    )


def format_allotment_telegram_summary(
    record: dict[str, Any],
    *,
    n_profiles: int,
    n_emailed: int,
    n_skipped: int,
) -> str:
    """Shared Telegram line: counts only, never PAN or per-person results."""
    company = record.get("company_name") or record.get("ipo_id") or "Unknown IPO"
    registrar = record.get("registrar") or "unknown registrar"
    return (
        f"📢 <b>ALLOTMENT OUT: {_esc(company)}</b>\n"
        f"Registrar: {_esc(registrar)}.\n"
        f"Checked {_esc(n_profiles)} PAN(s): {_esc(n_emailed)} emailed, "
        f"{_esc(n_skipped)} skipped."
    )


def _is_smtp_auth_error(exc: BaseException) -> bool:
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return True
    text = str(exc)
    return "535" in text or "BadCredentials" in text or "Username and Password" in text


def format_card(record: dict[str, Any]) -> dict[str, str]:
    """One IPO card as Telegram-safe HTML (`<b>` / `<i>` / newlines)."""
    company = record.get("company_name") or record.get("ipo_id") or "Unknown IPO"
    board = str(record.get("board") or record.get("exchange_type") or "?").title()
    close = record.get("close_date") or "?"
    error = record.get("error")
    if error:
        err = str(error)[:400]
        body = (
            f"⚠️ <b>SCAN ERROR: {_esc(company)}</b>\n"
            f"Board: {_esc(board)} | Close: {_esc(close)}\n\n"
            "No decision produced. This is NOT a SKIP; the scrape or scoring "
            "step failed for this IPO and the numbers cannot be trusted.\n"
            f"<code>{_esc(err)}</code>\n\n"
            "Check the site structure or logs before treating this as a signal."
        )
        return {"html": body, "markdown": body}

    apply_s1 = _as_bool(record.get("apply_s1"))
    price = _fmt_money(record.get("price_band_high"))
    lot = record.get("lot_size")
    lot_txt = _fmt_money(lot) if lot not in (None, "") else "n/a"
    p_pop = _fmt_prob_pct(record.get("p_pop"))
    p_allot = _fmt_prob_pct(record.get("p_allot"))
    ev = _fmt_money(record.get("ev_retail"))
    s1_copy = (S1_APPLY if apply_s1 else S1_SKIP).format(p_pop=p_pop, p_allot=p_allot, ev=ev)
    s2_status, s2_copy = _s2_status_and_copy(record)
    if apply_s1:
        s1_status = "🟢 <b>APPLY FOR LISTING GAINS</b>"
    else:
        s1_status = "🔴 <b>SKIP</b>"

    checks_text = "\n".join(_checklist_lines(record))
    hype = f"{_fmt_gmp_hype(record)}\n{_fmt_sub_hype(record)}"

    rank_line = ""
    rank = record.get("rank_of_day")
    total = record.get("rank_total_of_day")
    if rank not in (None, "") and total not in (None, ""):
        rank_line = (
            f"🏆 <b>Rank {_esc(rank)} of {_esc(total)} Closing Today</b> "
            "(by EV/capital)\n"
        )

    fetched = record.get("scraped_at_ist") or ""
    fetched_line = ""
    if fetched:
        fetched_line = (
            f"🕒 <b>Fetched:</b> {_esc(fetched)} "
            "(live snapshot; numbers can still move)\n"
        )

    body = (
        f"🏢 <b>{_esc(company)}</b> ({_esc(board)})\n"
        f"{rank_line}"
        f"⏳ <b>Closes:</b> {_esc(close)}\n"
        f"{fetched_line}"
        f"💰 <b>Issue:</b> ₹{_esc(price)} / share | 📦 <b>Lot Size:</b> {_esc(lot_txt)}\n"
        "\n"
        f"📊 <b>Live Market Hype</b>\n"
        f"{_esc(hype)}\n"
        "\n"
        f"🤖 <b>Strategy 1: Listing Pop</b>\n"
        f"{s1_status}\n"
        f"{_esc(s1_copy)}\n"
        "\n"
        f"📈 <b>Strategy 2: Long-Term Quality</b>\n"
        f"{s2_status}\n"
        f"{_esc(s2_copy)}\n"
        "\n"
        f"<b>Quality Checklist:</b>\n"
        f"{checks_text}\n"
        "\n"
        f"<i>{_esc(DISCLAIMER)}</i>"
    )
    return {"html": body, "markdown": body}


def format_email_digest(cards: list[str]) -> str:
    """One Gmail-safe HTML document for the whole scan (newlines become breaks)."""
    parts = []
    for i, card in enumerate(cards):
        if i:
            parts.append("<hr>")
        parts.append(card.replace("\n", "<br>\n"))
    inner = "\n".join(parts) or "<p>No IPO cards.</p>"
    return (
        '<html><body style="font-family:sans-serif;font-size:15px;line-height:1.45">'
        f"{inner}</body></html>"
    )


def send_telegram(text: str, *, token: Optional[str] = None, chat_id: Optional[str] = None) -> bool:
    token = _clean_secret(token) if token is not None else _env("TELEGRAM_BOT_TOKEN")
    chat_id = _clean_secret(chat_id) if chat_id is not None else _env("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("[notify] Telegram skipped (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID unset)")
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    response = httpx.post(
        url,
        data={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
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
    html: bool = True,
) -> bool:
    user = _clean_secret(user) if user is not None else _env("GMAIL_USER")
    password = _app_password(password if password is not None else None)
    if to_addr is not None:
        recipients = _split_recipients(to_addr)
    else:
        recipients = _split_recipients(_env("ALERT_EMAIL_TO"))
    if not recipients:
        recipients = [user] if user else []
    host = host if host is not None else (_env("SMTP_HOST") or "smtp.gmail.com")
    port = port if port is not None else int(_env("SMTP_PORT") or "587")
    if not user or not password or not recipients:
        print("[notify] Email skipped (GMAIL_USER / GMAIL_APP_PASSWORD unset)")
        return False
    subtype = "html" if html else "plain"
    msg = MIMEText(body, subtype, "utf-8")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = ", ".join(recipients)
    with smtplib.SMTP(host, port, timeout=30) as smtp:
        smtp.starttls()
        smtp.login(user, password)
        smtp.sendmail(user, recipients, msg.as_string())
    return True


def send_failure_alert(error: str, state_path: Optional[Path] = None) -> None:
    path = state_path if state_path is not None else Path("data") / "live_alert_state.json"
    today = datetime.now(ZoneInfo("Asia/Kolkata")).date().isoformat()
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            payload = {}
        if isinstance(payload, dict) and payload.get("failure_alerted_ist") == today:
            print("[notify] failure alert already sent today; skipping Telegram/email")
            return

    snippet = error[:1500]
    html_body = f"⚠️ <b>IPO live scanner FAILED</b>\n<code>{_esc(snippet)}</code>"
    try:
        send_telegram(html_body)
    except Exception as exc:
        print(f"[notify] failure Telegram also failed: {_redact(str(exc))}")
    try:
        send_email("IPO live scanner FAILED", format_email_digest([html_body]))
    except Exception as exc:
        print(f"[notify] failure email also failed: {_redact(str(exc))}")

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"failure_alerted_ist": today}), encoding="utf-8")
    except Exception as exc:
        print(f"[notify] could not write failure-alert state: {_redact(str(exc))}")


def _safe_print(text: str) -> None:
    """Windows cp1252 consoles cannot encode ₹; don't crash a dry-run on that."""
    try:
        print(text)
    except UnicodeEncodeError:
        print(text.encode("ascii", errors="replace").decode("ascii"))


class NotificationDeliveryError(RuntimeError):
    """Both Telegram and email genuinely errored for a real (non-dry) alert
    batch. Raising this (instead of the old print-and-swallow) turns a
    false-green Actions run into a red, visible failure so a dead Gmail app
    password or a bad Telegram token cannot go unnoticed for days."""


def dispatch(records: list[dict[str, Any]], *, dry_run: bool = False) -> int:
    """One Telegram message per IPO; one email digest for the whole run."""
    cards = [format_card(rec)["html"] for rec in records]
    if dry_run:
        for rec, card in zip(records, cards):
            print("--- dry-run telegram (html) ---")
            _safe_print(card)
            print("--------------------")
        print("--- dry-run email digest (1 message) ---")
        _safe_print(format_email_digest(cards))
        print("--------------------")
        return 0

    sent = 0
    telegram_errors: list[str] = []
    for rec, card in zip(records, cards):
        company = rec.get("company_name") or rec.get("ipo_id") or "IPO"
        try:
            if send_telegram(card):
                sent += 1
        except Exception as exc:
            msg = f"Telegram error for {company}: {_redact(str(exc))}"
            print(f"[notify] {msg}")
            telegram_errors.append(msg)

    email_error: Optional[str] = None
    if records:
        names = [str(r.get("company_name") or r.get("ipo_id") or "IPO") for r in records]
        subject = f"IPO alerts ({len(names)}): " + ", ".join(names[:4])
        if len(names) > 4:
            subject += f" +{len(names) - 4} more"
        try:
            send_email(subject, format_email_digest(cards))
        except Exception as exc:
            email_error = f"Email digest error: {_redact(str(exc))}"
            print(f"[notify] {email_error}")

    # Only escalate on a genuine send failure (an exception was actually
    # raised), never on Telegram being merely unconfigured (send_telegram
    # returns False + prints a skip notice, no exception) -- that stays a
    # silent, intentional no-op like before. But if email actively errored
    # and nothing at all got through Telegram either, the whole batch of
    # real alerts was lost with no visible sign of it -- that is exactly
    # the failure mode that hid this bug for two days, so raise loudly
    # instead of returning a clean 0.
    if records and sent == 0 and email_error is not None:
        details = "; ".join(telegram_errors + [email_error])
        raise NotificationDeliveryError(
            f"Both Telegram and email failed to deliver {len(records)} "
            f"real alert(s): {details}"
        )
    return sent
