"""Numeric, date, and text normalizers for Chittorgarh HTML.

WHAT THIS FILE DOES
--------------------
Every scraper in this package hits messy Indian-market strings (₹, lakh/crore
suffixes, day-month ranges without a consistent format, BSE numeric codes
mixed with NSE tickers). This file is the shared converter from those strings
to typed values. Callers inside the package: `parse_ipo.py` (almost every
helper), `gmp.py`, `tracker.py`, `live_dashboard.py` (`MONTHS`, `clean_text`),
`live_subscription.py`, and `registrar_allotment.py` (`parse_number`). Scripts
do not import this directly — they go through those parsers. Covered by
`tests/test_normalize.py`.

KEY TERMS USED HERE
--------------------
- Crore (`Cr`): 10 million rupees. Issue size, OFS amount, and market cap
  are stored as crore floats after the suffix is stripped.
- Indian commas: grouping like `2,59,31,407` (thousands/lakhs/crores), not
  Western `2,593,1407`. `parse_number` just deletes commas before the float.
- Price band: the ₹low–₹high range at which the IPO is offered before the
  final issue price is set. `parse_price_band` returns `(low, high)`.
- `x` / `×`: subscription-multiple suffix ("7.26x" means 7.26 times shares
  offered). Stripped so the stored value is a plain float.
- BSE code vs NSE symbol: BSE uses a numeric scrip code; NSE uses a letter
  ticker. Chittorgarh often jams both into one cell (`544839 / LCL`);
  `parse_bse_nse` splits them.
- Date range: cells like `23 to 27 Jul, 2026` (bidding window). `parse_date`
  returns the start; `parse_date_range` returns both ends.

FUNCTIONS / CLASSES IN THIS FILE
---------------------------------
- `clean_text(value)`: collapse whitespace / nbsp; empty string for None.
- `parse_number(value)`: ₹ / Rs / % / x / Cr / Indian commas → float, or
  None for `.`, `-`, `NA`, `To be declared`.
- `parse_int(value)`: rounded `parse_number`.
- `parse_shares_and_cr(value)`: split
  `2,59,31,407 shares (agg. up to ₹1,101 Cr)` into `(shares, crore)`.
- `parse_price_band(value)`: `(low, high)`; a single number is used for both.
- `parse_bse_nse(value)`: `(bse_code, nse_symbol)` from a combined cell.
- `parse_date(value)`: ISO `YYYY-MM-DD`; for a range, the start date.
- `parse_date_range(value)`: `(open, close)` ISO dates.
- `norm_key(value)`: lowercase alphanumerics-only key used to match table
  labels in `parse_ipo.py` (`Face Value` → `face value`).
- `MONTHS` / `WEEKDAYS`: month-name maps (dashboard year inference) and
  weekday tokens stripped from dates.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

MONTHS = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}

WEEKDAYS = {"mon", "tue", "wed", "thu", "fri", "sat", "sun",
            "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"}


def clean_text(value: Optional[str]) -> str:
    if value is None:
        return ""
    text = re.sub(r"\s+", " ", str(value)).replace("\xa0", " ").strip()
    return text


def parse_number(value: Optional[str]) -> Optional[float]:
    """Parse ₹, Indian commas, Cr, %, x, share counts into a float."""
    text = clean_text(value)
    if not text or text in {".", "-", "NA", "N/A", "–", "—", "[.]", "To be declared"}:
        return None
    text = text.replace("₹", "").replace("Rs.", "").replace("Rs", "")
    text = text.replace("%", "").replace("×", "")
    text = re.sub(r"(\d)\s*[xX]\b", r"\1", text)
    text = re.sub(r"\b(cr\.?|crore|crores)\b", "", text, flags=re.I)
    text = text.replace(",", "")
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def parse_int(value: Optional[str]) -> Optional[int]:
    num = parse_number(value)
    if num is None:
        return None
    return int(round(num))


def parse_shares_and_cr(value: Optional[str]) -> tuple[Optional[float], Optional[float]]:
    """Split '2,59,31,407 shares (agg. up to ₹1,101 Cr)' into shares and crore amount."""
    text = clean_text(value)
    if not text:
        return None, None
    shares = None
    share_match = re.search(r"([\d,]+)\s*shares?", text, flags=re.I)
    if share_match:
        shares = parse_number(share_match.group(1))
    cr = None
    cr_match = re.search(r"₹?\s*([\d,.]+)\s*(?:cr|crore)", text, flags=re.I)
    if cr_match:
        cr = parse_number(cr_match.group(1))
    if shares is None and cr is None:
        only = parse_number(text)
        if only is not None and "share" in text.lower():
            shares = only
        elif only is not None:
            cr = only
    return shares, cr


def parse_price_band(value: Optional[str]) -> tuple[Optional[float], Optional[float]]:
    text = clean_text(value)
    if not text:
        return None, None
    nums = re.findall(r"[\d,.]+", text.replace("₹", ""))
    parsed = [parse_number(n) for n in nums]
    parsed = [p for p in parsed if p is not None]
    if not parsed:
        return None, None
    if len(parsed) == 1:
        return parsed[0], parsed[0]
    return parsed[0], parsed[1]


def parse_bse_nse(value: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    text = clean_text(value)
    if not text:
        return None, None
    parts = [p.strip() for p in re.split(r"[/,|]", text) if p.strip()]
    bse = None
    nse = None
    for part in parts:
        digits = re.sub(r"\D", "", part)
        letters = re.sub(r"[^A-Za-z0-9.\-]", "", part)
        if digits and len(digits) >= 4 and digits == re.sub(r"\D", "", part):
            bse = digits
        elif letters and not letters.isdigit():
            nse = letters.upper()
    if bse is None and parts and re.fullmatch(r"\d+", parts[0].replace(" ", "")):
        bse = re.sub(r"\D", "", parts[0])
    if nse is None and len(parts) > 1:
        nse = re.sub(r"[^A-Za-z0-9.\-]", "", parts[-1]).upper() or None
    return bse, nse


def _try_datetime(text: str, fmt: str) -> Optional[str]:
    try:
        return datetime.strptime(text, fmt).date().isoformat()
    except ValueError:
        return None


def parse_date(value: Optional[str]) -> Optional[str]:
    """Return ISO date (YYYY-MM-DD) from Chittorgarh date strings. Range start if a range."""
    text = clean_text(value)
    if not text or text in {".", "-", "[.]"}:
        return None
    text = re.sub(r"^[A-Za-z]{3,9},?\s+", "", text).strip()
    # "23 to 27 Jul, 2026" / "19 to 21 Aug, 2026" — use start date for ipo_open via parse_date_range
    iso = (
        _try_datetime(text, "%d %b, %Y")
        or _try_datetime(text, "%d %B, %Y")
        or _try_datetime(text, "%b %d, %Y")
        or _try_datetime(text, "%B %d, %Y")
        or _try_datetime(text, "%d %b %Y")
        or _try_datetime(text, "%d-%b-%Y")
        or _try_datetime(text, "%Y-%m-%d")
        or _try_datetime(text, "%d/%m/%Y")
        or _try_datetime(text, "%d-%m-%Y")
    )
    if iso:
        return iso
    range_match = re.search(
        r"(\d{1,2})\s*(?:to|-)\s*\d{1,2}\s+([A-Za-z]+),?\s+(\d{4})",
        text,
        flags=re.I,
    )
    if range_match:
        day, month, year = range_match.groups()
        return parse_date(f"{day} {month}, {year}")
    return None


def parse_date_range(value: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    text = clean_text(value)
    if not text:
        return None, None
    range_match = re.search(
        r"(\d{1,2})\s*(?:to|-)\s*(\d{1,2})\s+([A-Za-z]+),?\s+(\d{4})",
        text,
        flags=re.I,
    )
    if range_match:
        d1, d2, month, year = range_match.groups()
        return parse_date(f"{d1} {month}, {year}"), parse_date(f"{d2} {month}, {year}")
    single = parse_date(text)
    return single, single


def norm_key(value: Optional[str]) -> str:
    text = clean_text(value).lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()
