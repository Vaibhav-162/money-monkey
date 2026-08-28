"""Playwright scraper for the IPO performance tracker index."""

from __future__ import annotations

import re
from typing import Any, Optional
from urllib.parse import urljoin

from playwright.sync_api import TimeoutError as PlaywrightTimeout

from chittorgarh.browser import chromium_page
from chittorgarh.http import BASE_URL
from chittorgarh.normalize import clean_text, parse_date, parse_number

TRACKER_URL = BASE_URL + "/ipo/ipo_perf_tracker.asp"
IPO_HREF = re.compile(r"/ipo/([^/]+)/(\d+)/?")


def tracker_url(exchange: str, year: int) -> str:
    exch = "mainline" if exchange in {"mainline", "mainboard"} else "sme"
    return f"{TRACKER_URL}?exchange={exch}&year={year}"


def _parse_href(href: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    if not href:
        return None, None, None
    m = IPO_HREF.search(href)
    if not m:
        return None, None, None
    slug, ipo_id = m.group(1), m.group(2)
    url = urljoin(BASE_URL, href.split("?")[0])
    if not url.endswith("/"):
        url += "/"
    return ipo_id, slug, url


def scrape_tracker(
    exchange: str,
    year: int,
    timeout_ms: int = 45000,
    headless: bool = True,
) -> list[dict[str, Any]]:
    url = tracker_url(exchange, year)
    exchange_type = "mainboard" if exchange in {"mainline", "mainboard"} else "sme"
    records: list[dict[str, Any]] = []
    seen: set[str] = set()

    with chromium_page(headless=headless) as page:
        page.goto(url, wait_until="load", timeout=timeout_ms)
        for sel in ("select[name$='_length']", ".dataTables_length select"):
            loc = page.locator(sel)
            if loc.count():
                try:
                    loc.first.select_option(value="-1")
                except Exception:
                    try:
                        loc.first.select_option(label="All")
                    except Exception:
                        pass
        try:
            page.wait_for_selector("table a[href*='/ipo/']", timeout=timeout_ms)
        except PlaywrightTimeout:
            page.wait_for_timeout(8000)
        html_ready = page.content()
        if "No Record Found" in html_ready and "/ipo/" not in html_ready:
            return []

        generic = {"ipo detail", "ipo details", "details", "click here"}
        rows = page.locator("table tr").all()
        for row in rows:
            links = row.locator("a[href*='/ipo/']").all()
            if not links:
                continue
            chosen_href = None
            chosen_name = ""
            for link in links:
                href = link.get_attribute("href") or ""
                ipo_id, slug, detail_url = _parse_href(href)
                if not ipo_id:
                    continue
                text = clean_text(link.inner_text())
                title = clean_text(link.get_attribute("title") or "")
                candidate = title if title and "detail" not in title.lower() else text
                if not chosen_href:
                    chosen_href, chosen_name = href, candidate
                if candidate and candidate.lower() not in generic and len(candidate) >= len(chosen_name):
                    chosen_href, chosen_name = href, candidate
            ipo_id, slug, detail_url = _parse_href(chosen_href or "")
            if not ipo_id or ipo_id in seen:
                continue
            cells = [clean_text(c.inner_text()) for c in row.locator("td").all()]
            if len(cells) < 5:
                continue
            name = chosen_name
            if not name or name.lower() in generic:
                for cell in cells:
                    if re.search(r"\bLtd\.?\b", cell) and "ipo detail" not in cell.lower():
                        name = re.sub(r"\s+IPO Detail.*", "", cell, flags=re.I).strip() or cell
                        break
            rec = {
                "ipo_id": ipo_id,
                "slug": slug,
                "url": detail_url,
                "company_name": name or cells[0],
                "exchange_type": exchange_type,
                "listing_year": year,
                "listed_on": parse_date(cells[1] if len(cells) > 1 else None),
                "issue_price": parse_number(cells[2] if len(cells) > 2 else None),
                "listing_day_close": parse_number(cells[3] if len(cells) > 3 else None),
                "listing_day_gain_pct": parse_number(cells[4] if len(cells) > 4 else None),
                "current_price": parse_number(cells[5] if len(cells) > 5 else None),
                "profit_loss_pct": parse_number(cells[6] if len(cells) > 6 else None),
            }
            records.append(rec)
            seen.add(ipo_id)
    return records
