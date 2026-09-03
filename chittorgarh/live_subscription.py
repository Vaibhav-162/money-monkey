"""Live category subscription from Chittorgarh's /ipo_subscription/ page.

WHAT THIS FILE DOES
--------------------
Open-issue detail pages do not publish Total x (often empty or paywalled).
The dedicated subscription URL (`/ipo_subscription/{slug}/{id}/`) is static
HTML and has a 'Total Subscription' row, so this uses `http.HttpClient`
(no browser). The only production caller is `scripts/live_scanner.py`, which
overwrites `sub_qib_x` / `sub_nii_x` / `sub_retail_x` / `sub_total_x` on the
master row after `pipeline.scrape_one`. Parser tests live in
`tests/test_live_dashboard.py`. Historical scrapes do *not* call this —
they keep the (possibly paywalled) numbers from `parse_ipo.py`.

KEY TERMS USED HERE
--------------------
- Subscription multiple (`sub_*_x`): how many times the reserved shares in
  that category were applied for. `2.78` means 2.78× retail demand.
- Total x: the all-categories multiple. This is the number the live scanner
  wants on close day; the detail page often hides it behind an IPOMatrix
  paywall while this dedicated page still shows it.
- QIB / NII / retail: Qualified Institutional Buyers, Non-Institutional
  Investors, and retail individuals — the three category rows this parser
  keeps. bNII/sNII splits are ignored here.
- Day-wise table: a history table headed `Date | … | Subscription (times)`.
  It must be skipped; otherwise the first `Total` row would win and we
  would store a stale daily print instead of the live category snapshot.
- Slug / `ipo_id`: the same Chittorgarh URL pieces as the detail page.

FUNCTIONS / CLASSES IN THIS FILE
---------------------------------
- `subscription_url(slug, ipo_id)`: builds the dedicated subscription URL.
- `parse_subscription_html(html)`: picks the table with the most filled
  category fields; returns Nones if none look like a live category table.
- `fetch_live_subscription(client, slug, ipo_id)`: GET with `use_cache=False`
  (live numbers change through the day) then parse.
- `_parse_subscription_table(rows)`: one table → category dict, or None if
  it is a day-wise Date table or has no usable numbers.
- `_cell_text(cell)`: strip scripts/whitespace from a cell.
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
    empty: dict[str, Any] = {
        "sub_qib_x": None,
        "sub_nii_x": None,
        "sub_retail_x": None,
        "sub_total_x": None,
    }
    best = dict(empty)
    best_score = -1
    for table in soup.find_all("table"):
        rows = []
        for tr in table.find_all("tr"):
            cells = [_cell_text(c) for c in tr.find_all(["th", "td"])]
            if any(cells):
                rows.append(cells)
        parsed = _parse_subscription_table(rows)
        if parsed is None:
            continue
        score = sum(parsed[k] is not None for k in empty)
        if score > best_score:
            best_score = score
            best = parsed
    return best


def _parse_subscription_table(rows: list[list[str]]) -> Optional[dict[str, Any]]:
    """Parse one table. Skip day-wise Date/Total tables in favour of live category rows."""
    if not rows:
        return None
    header = " ".join(c.lower() for c in rows[0])
    if "subscription" not in header or "times" not in header:
        return None
    # Day-wise history tables are headed "Date | ... | Subscription (times)"
    # and would otherwise win via `break` on the first Total row.
    if "date" in header and "category" not in header and "investor" not in header:
        return None
    out: dict[str, Any] = {
        "sub_qib_x": None,
        "sub_nii_x": None,
        "sub_retail_x": None,
        "sub_total_x": None,
    }
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
    if all(v is None for v in out.values()):
        return None
    return out


def fetch_live_subscription(
    client: HttpClient,
    slug: str,
    ipo_id: str,
) -> dict[str, Any]:
    html = client.get_text(subscription_url(slug, ipo_id), cache_name=None, use_cache=False)
    return parse_subscription_html(html)
