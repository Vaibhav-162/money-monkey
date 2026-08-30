"""Live category subscription from Chittorgarh's /ipo_subscription/ page.

Open-issue detail pages do not publish Total x (often empty or paywalled).
The dedicated subscription URL is static HTML and has a 'Total Subscription' row.
"""

from __future__ import annotations

from typing import Any, Optional

from bs4 import BeautifulSoup, Tag

from chittorgarh.http import BASE_URL, HttpClient
from chittorgarh.normalize import clean_text, parse_number


def subscription_url(slug: str, ipo_id: str) -> str:
    slug = (slug or "").strip().strip("/")
    return f"{BASE_URL}/ipo_subscription/{slug}/{ipo_id}/"


def _cell_text(cell: Optional[Tag]) -> str:
    if cell is None:
        return ""
    return clean_text(cell.get_text(" ", strip=True))


def parse_subscription_html(html: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "lxml")
    out: dict[str, Any] = {
        "sub_qib_x": None,
        "sub_nii_x": None,
        "sub_retail_x": None,
        "sub_total_x": None,
    }
    for table in soup.find_all("table"):
        rows = []
        for tr in table.find_all("tr"):
            cells = [_cell_text(c) for c in tr.find_all(["th", "td"])]
            if any(cells):
                rows.append(cells)
        if not rows:
            continue
        header = " ".join(c.lower() for c in rows[0])
        if "subscription" not in header or "times" not in header:
            continue
        for row in rows[1:]:
            if not row:
                continue
            label = row[0].lower()
            val = parse_number(row[1] if len(row) > 1 else None)
            if "total" in label:
                out["sub_total_x"] = val
            elif "retail" in label or "individual" in label:
                out["sub_retail_x"] = val
            elif "qib" in label or "qualified institutional" in label:
                out["sub_qib_x"] = val
            elif "nii" in label or "non institutional" in label:
                out["sub_nii_x"] = val
        if out["sub_total_x"] is not None:
            break
    return out


def fetch_live_subscription(
    client: HttpClient,
    slug: str,
    ipo_id: str,
) -> dict[str, Any]:
    html = client.get_text(subscription_url(slug, ipo_id), cache_name=None, use_cache=False)
    return parse_subscription_html(html)
