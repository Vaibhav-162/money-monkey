"""Current-IPO dashboard: who is open, and who closes on a given IST day.

Chittorgarh's mainboard (`/ipo/`) and SME (`/ipo/ipo_dashboard.asp?a=sme`)
dashboards render the 'Company / Issue Date' table as static HTML. Each data
row is one cell:

    <a href="/ipo/{slug}/{id}/">{name}</a>
    <span class="badge" title="Open">O</span>   <!-- optional; P = pending list -->
    <span class="float-end">28 Aug - 01 Sep</span>

Date text has no year. Year is inferred from `as_of` (default: today IST).
"""

from __future__ import annotations

import argparse
import calendar
import logging
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup, Tag

from chittorgarh.http import BASE_URL, HttpClient
from chittorgarh.normalize import MONTHS, clean_text

log = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")
IPO_HREF = re.compile(r"/ipo/([^/]+)/(\d+)/?")

DASHBOARD_URLS = {
    "mainboard": f"{BASE_URL}/ipo/",
    "mainline": f"{BASE_URL}/ipo/",
    "sme": f"{BASE_URL}/ipo/ipo_dashboard.asp?a=sme",
}

_CROSS_MONTH = re.compile(
    r"(\d{1,2})\s+([A-Za-z]{3,9})\s*[-–]\s*(\d{1,2})\s+([A-Za-z]{3,9})"
    r"(?:\s+,?\s*(\d{4}))?",
    flags=re.I,
)
_SAME_MONTH = re.compile(
    r"(\d{1,2})\s*[-–]\s*(\d{1,2})\s+([A-Za-z]{3,9})(?:\s+,?\s*(\d{4}))?",
    flags=re.I,
)

_STATUS_MAP = {
    "o": "open",
    "open": "open",
    "p": "pending",
    "pending": "pending",
}


def today_ist(now: Optional[datetime] = None) -> date:
    stamp = now if now is not None else datetime.now(IST)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=IST)
    return stamp.astimezone(IST).date()


def dashboard_url(exchange: str) -> str:
    key = (exchange or "").strip().lower()
    if key not in DASHBOARD_URLS:
        raise ValueError(f"unknown exchange={exchange!r}; pass mainboard or sme")
    return DASHBOARD_URLS[key]


def _month_num(name: str) -> Optional[int]:
    key = clean_text(name).lower()
    if key in MONTHS:
        return MONTHS[key]
    return MONTHS.get(key[:3])


def _date_with_year(year: int, month: int, day: int) -> Optional[date]:
    last = calendar.monthrange(year, month)[1]
    if day < 1 or day > last:
        return None
    return date(year, month, day)


def _closest_date(month: int, day: int, as_of: date, year: Optional[int] = None) -> Optional[date]:
    if year is not None:
        return _date_with_year(year, month, day)
    candidates = [
        d
        for y in (as_of.year - 1, as_of.year, as_of.year + 1)
        if (d := _date_with_year(y, month, day)) is not None
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda d: abs((d - as_of).days))


def parse_issue_dates(
    text: str,
    as_of: date,
) -> tuple[Optional[date], Optional[date]]:
    """Parse dashboard spans like '01 - 03 Sep' or '31 Aug - 02 Sep'."""
    raw = clean_text(text)
    if not raw:
        return None, None

    cross = _CROSS_MONTH.search(raw)
    if cross:
        d1, m1, d2, m2, year_s = cross.groups()
        month_open = _month_num(m1)
        month_close = _month_num(m2)
        if month_open is None or month_close is None:
            return None, None
        year = int(year_s) if year_s else None
        open_d = _closest_date(month_open, int(d1), as_of, year)
        close_d = _closest_date(month_close, int(d2), as_of, year)
    else:
        same = _SAME_MONTH.search(raw)
        if not same:
            return None, None
        d1, d2, month_s, year_s = same.groups()
        month = _month_num(month_s)
        if month is None:
            return None, None
        year = int(year_s) if year_s else None
        open_d = _closest_date(month, int(d1), as_of, year)
        close_d = _closest_date(month, int(d2), as_of, year)

    if open_d and close_d and open_d > close_d:
        bumped = _date_with_year(close_d.year + 1, close_d.month, close_d.day)
        if bumped is not None:
            close_d = bumped
    return open_d, close_d


def _parse_href(href: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    if not href:
        return None, None, None
    match = IPO_HREF.search(href)
    if not match:
        return None, None, None
    slug, ipo_id = match.group(1), match.group(2)
    url = urljoin(BASE_URL, href.split("?")[0])
    if not url.endswith("/"):
        url += "/"
    return ipo_id, slug, url


def _issue_date_table(soup: BeautifulSoup) -> Optional[Tag]:
    for table in soup.find_all("table"):
        header = table.find("tr")
        if header is None:
            continue
        blob = clean_text(header.get_text(" ")).lower()
        if "company" in blob and "issue date" in blob:
            return table
    return None


def _status_from_row(
    row: Tag,
    open_d: Optional[date],
    close_d: Optional[date],
    as_of: date,
) -> str:
    badge = row.select_one("span.badge")
    if badge is not None:
        title = clean_text(badge.get("title") or "").lower()
        letter = clean_text(badge.get_text()).lower()
        mapped = _STATUS_MAP.get(title) or _STATUS_MAP.get(letter)
        if mapped:
            return mapped
    if open_d and close_d:
        if as_of < open_d:
            return "upcoming"
        if open_d <= as_of <= close_d:
            return "open"
        return "closed"
    return "unknown"


def parse_dashboard_html(
    html: str,
    exchange_type: str,
    as_of: date,
) -> list[dict[str, Any]]:
    """Parse a dashboard HTML snapshot. Never raises on a bad row."""
    soup = BeautifulSoup(html, "lxml")
    table = _issue_date_table(soup)
    if table is None:
        log.warning("live dashboard: no Company / Issue Date table (exchange=%s)", exchange_type)
        return []

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in table.find_all("tr"):
        link = row.find("a", href=True)
        if link is None:
            continue
        ipo_id, slug, url = _parse_href(link.get("href") or "")
        if not ipo_id or ipo_id in seen:
            continue
        company = clean_text(link.get("title") or "") or clean_text(link.get_text())
        date_span = row.select_one("span.float-end")
        date_text = clean_text(date_span.get_text()) if date_span is not None else ""
        if not date_text:
            log.warning("live dashboard: missing date span for ipo_id=%s", ipo_id)
        open_d, close_d = parse_issue_dates(date_text, as_of)
        if date_text and open_d is None and close_d is None:
            log.warning("live dashboard: unparsed dates ipo_id=%s text=%r", ipo_id, date_text)
        out.append(
            {
                "ipo_id": ipo_id,
                "slug": slug,
                "url": url,
                "company_name": company or None,
                "exchange_type": exchange_type,
                "open_date": open_d.isoformat() if open_d else None,
                "close_date": close_d.isoformat() if close_d else None,
                "status": _status_from_row(row, open_d, close_d, as_of),
            }
        )
        seen.add(ipo_id)
    return out


def closing_on(rows: list[dict[str, Any]], as_of: date) -> list[dict[str, Any]]:
    target = as_of.isoformat()
    return [r for r in rows if r.get("close_date") == target]


def scrape_open_ipos(
    exchange: str,
    *,
    client: Optional[HttpClient] = None,
    as_of: Optional[date] = None,
    html: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Fetch (or parse supplied HTML) the current-IPO dashboard for one board."""
    key = (exchange or "").strip().lower()
    exchange_type = "mainboard" if key in {"mainline", "mainboard"} else "sme"
    if key not in DASHBOARD_URLS and html is None:
        raise ValueError(f"unknown exchange={exchange!r}; pass mainboard or sme")
    day = as_of or today_ist()

    if html is None:
        own = client is None
        http = client or HttpClient(cache_dir=Path(".cache") / "live_dashboard", delay=1.5)
        try:
            html = http.get_text(dashboard_url(key), cache_name=None, use_cache=False)
        finally:
            if own:
                http.close()
    return parse_dashboard_html(html, exchange_type, day)


def scrape_all_open_ipos(
    *,
    client: Optional[HttpClient] = None,
    as_of: Optional[date] = None,
) -> list[dict[str, Any]]:
    day = as_of or today_ist()
    own = client is None
    http = client or HttpClient(cache_dir=Path(".cache") / "live_dashboard", delay=1.5)
    try:
        rows: list[dict[str, Any]] = []
        for exchange in ("mainboard", "sme"):
            rows.extend(scrape_open_ipos(exchange, client=http, as_of=day))
        return rows
    finally:
        if own:
            http.close()


def _print_table(rows: list[dict[str, Any]]) -> None:
    if not rows:
        print("  (none)")
        return
    print(f"  {'board':<10} {'status':<10} {'open':<12} {'close':<12} {'id':<6} company")
    for row in rows:
        print(
            f"  {row.get('exchange_type') or '':<10} "
            f"{row.get('status') or '':<10} "
            f"{row.get('open_date') or '':<12} "
            f"{row.get('close_date') or '':<12} "
            f"{row.get('ipo_id') or '':<6} "
            f"{row.get('company_name') or ''}"
        )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Dry-run: current Chittorgarh IPO dashboard")
    parser.add_argument("--exchange", choices=["mainboard", "sme", "both"], default="both")
    parser.add_argument("--as-of", default=None, help="YYYY-MM-DD (IST). Default: today IST.")
    args = parser.parse_args(argv)

    as_of = date.fromisoformat(args.as_of) if args.as_of else today_ist()
    exchanges = ("mainboard", "sme") if args.exchange == "both" else (args.exchange,)

    print(f"as_of (IST) {as_of.isoformat()}")
    all_rows: list[dict[str, Any]] = []
    http = HttpClient(cache_dir=Path(".cache") / "live_dashboard", delay=1.5)
    try:
        for exchange in exchanges:
            rows = scrape_open_ipos(exchange, client=http, as_of=as_of)
            all_rows.extend(rows)
            print(f"\n[{exchange}] {len(rows)} current-table rows")
            _print_table(rows)
    finally:
        http.close()

    open_now = [r for r in all_rows if r.get("status") == "open"]
    closing = closing_on(all_rows, as_of)
    print(f"\nopen now: {len(open_now)}")
    _print_table(open_now)
    print(f"\nclosing today ({as_of.isoformat()}): {len(closing)}")
    _print_table(closing)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
