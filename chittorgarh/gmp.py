"""Scrape IPO GMP history from InvestorGain (Chittorgarh GMP tab now points there)."""

from __future__ import annotations

import re
from typing import Any, Optional
from urllib.parse import urlparse

from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from chittorgarh.browser import chromium_page
from chittorgarh.normalize import clean_text, parse_date, parse_number

GMP_HINTS = ("gmp", "grey market", "gray market", "kostak", "subject to sauda", "est. listing")


def investorgain_gmp_url(detail_url: str, ipo_id: str) -> str:
    path = urlparse(detail_url).path.strip("/")
    parts = [p for p in path.split("/") if p]
    slug = parts[1] if len(parts) >= 2 and parts[0] == "ipo" else (parts[0] if parts else "")
    slug = re.sub(r"-ipo$", "-ipo", slug) or f"ipo-{ipo_id}"
    return f"https://www.investorgain.com/chr-gmp/{slug}/{ipo_id}"


def _looks_like_gmp_table(rows: list[list[str]]) -> bool:
    if not rows:
        return False
    blob = " ".join(c.lower() for r in rows[:2] for c in r)
    if "no data" in blob or "no record" in blob:
        return False
    return any(h in blob for h in GMP_HINTS) or ("date" in blob and "gmp" in blob)


def _header_index(header: list[str], *names: str) -> Optional[int]:
    """Prefer exact header match so 'GMP' is not confused with 'GMP DATE'."""
    normed = [clean_text(h).lower() for h in header]
    wanted = [n.lower() for n in names]
    for n in wanted:
        for i, h in enumerate(normed):
            if h == n:
                return i
    for n in wanted:
        for i, h in enumerate(normed):
            if n in h and not (n == "gmp" and "date" in h):
                return i
    return None


def _subscription_header_index(header: list[str]) -> Optional[int]:
    """InvestorGain 'Subscription' column — never 'Subject to Sauda'."""
    normed = [clean_text(h).lower() for h in header]
    for i, h in enumerate(normed):
        if h == "subscription" or h == "sub":
            return i
    for i, h in enumerate(normed):
        if "subscription" in h and "sauda" not in h and "subject" not in h:
            return i
    return None


def _parse_gmp_rows(rows: list[list[str]], ipo_id: str) -> list[dict[str, Any]]:
    if not rows:
        return []
    header = rows[0]
    date_i = _header_index(header, "gmp date", "date") or 0
    gmp_i = _header_index(header, "gmp")
    pct_i = _header_index(header, "gmp %", "gmp%")
    est_i = _header_index(header, "est. listing price", "estimated listing", "est. listing")
    kostak_i = _header_index(header, "kostak")
    sub_i = _header_index(header, "subject to sauda", "sauda")
    sub_ig_i = _subscription_header_index(header)
    updated_i = _header_index(header, "last updated")

    out: list[dict[str, Any]] = []
    for row in rows[1:]:
        if not row:
            continue
        date_raw = row[date_i] if date_i < len(row) else ""
        if not date_raw or date_raw.lower() in {"date", "total", "gmp date"}:
            continue
        date_token = date_raw.split()[0] if date_raw else ""
        rec = {
            "ipo_id": ipo_id,
            "gmp_date": parse_date(date_token) or parse_date(date_raw),
            "gmp_date_raw": clean_text(date_raw),
            "gmp_rs": parse_number(row[gmp_i] if gmp_i is not None and gmp_i < len(row) else None),
            "gmp_pct": parse_number(row[pct_i] if pct_i is not None and pct_i < len(row) else None),
            "gmp_est_listing_price": parse_number(row[est_i] if est_i is not None and est_i < len(row) else None),
            "kostak_rs": parse_number(row[kostak_i] if kostak_i is not None and kostak_i < len(row) else None),
            "subject_to_sauda": parse_number(row[sub_i] if sub_i is not None and sub_i < len(row) else None),
            "sub_ig_x": parse_number(row[sub_ig_i] if sub_ig_i is not None and sub_ig_i < len(row) else None),
            "gmp_last_updated": clean_text(row[updated_i]) if updated_i is not None and updated_i < len(row) else None,
        }
        if rec["gmp_date"] or rec["gmp_rs"] is not None:
            out.append(rec)
    return out


def last_gmp_on_or_before(
    history: list[dict[str, Any]],
    as_of: Optional[str] = None,
) -> dict[str, Any]:
    """Last GMP observation with gmp_date <= as_of. Empty as_of = last row in history."""
    if not history:
        return {}
    rows = [r for r in history if r.get("gmp_date")]
    if as_of:
        bounded = [r for r in rows if str(r["gmp_date"]) <= str(as_of)]
        rows = bounded if bounded else []
    if not rows:
        return {}
    rows.sort(key=lambda r: r.get("gmp_date") or "")
    last = rows[-1]
    return {
        "gmp_close_date": last.get("gmp_date"),
        "gmp_rs": last.get("gmp_rs"),
        "gmp_pct": last.get("gmp_pct"),
        "gmp_est_listing_price": last.get("gmp_est_listing_price"),
        "kostak_rs": last.get("kostak_rs"),
        "subject_to_sauda": last.get("subject_to_sauda"),
        "sub_ig_x": last.get("sub_ig_x"),
        "gmp_date_raw": last.get("gmp_date_raw"),
        "gmp_last_updated": last.get("gmp_last_updated"),
    }


def last_gmp_close(history: list[dict[str, Any]], listing_date: Optional[str] = None) -> dict[str, Any]:
    """Backward-compatible wrapper. Prefer last_gmp_on_or_before(ipo_close) for scoring.

    Fallback (only hit when listing_date is before every recorded GMP date, or rows
    lack a usable date) reuses last_gmp_on_or_before with as_of=None, which sorts by
    gmp_date and takes the latest row -- not history[-1], since parsed tables are
    newest-first and that used to silently return the *oldest* row instead.
    """
    out = last_gmp_on_or_before(history, listing_date)
    if out:
        return out
    return last_gmp_on_or_before(history, None)


def scrape_gmp_with_page(
    page: Page,
    url: str,
    ipo_id: str,
    timeout_ms: int = 45000,
) -> list[dict[str, Any]]:
    """Scrape GMP history using an already-open Playwright page."""
    return _scrape_gmp_page(page, url, ipo_id, timeout_ms)


def scrape_gmp(url: str, ipo_id: str, timeout_ms: int = 45000, headless: bool = True) -> list[dict[str, Any]]:
    """One-shot scrape: launches and closes its own Chromium page."""
    with chromium_page(headless=headless) as page:
        return _scrape_gmp_page(page, url, ipo_id, timeout_ms)


def _scrape_gmp_page(page: Page, url: str, ipo_id: str, timeout_ms: int) -> list[dict[str, Any]]:
    target = investorgain_gmp_url(url, ipo_id)
    history: list[dict[str, Any]] = []
    page.goto(target, wait_until="domcontentloaded", timeout=timeout_ms)
    try:
        page.wait_for_selector("table tr", timeout=12000)
    except PlaywrightTimeout:
        pass
    page.wait_for_timeout(1500)
    if "ipo-gmp-live" in page.url.lower() or "/report/ipo-gmp-live" in page.url.lower():
        return []
    for table in page.locator("table").all():
        rows: list[list[str]] = []
        for tr in table.locator("tr").all():
            cells = [clean_text(c.inner_text()) for c in tr.locator("th, td").all()]
            if any(cells):
                rows.append(cells)
        if not _looks_like_gmp_table(rows):
            continue
        parsed = _parse_gmp_rows(rows, ipo_id)
        if parsed:
            history = parsed
            break
    return history
