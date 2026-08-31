"""Heading- and key-driven parser for Chittorgarh IPO detail pages."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from chittorgarh.http import BASE_URL
from chittorgarh.normalize import (
    clean_text,
    norm_key,
    parse_bse_nse,
    parse_date,
    parse_date_range,
    parse_int,
    parse_number,
    parse_price_band,
    parse_shares_and_cr,
)

PAYWALL_HINTS = ("ipomatrix", "preview limited", "login to view")


def _cell_text(cell: Optional[Tag]) -> str:
    if cell is None:
        return ""
    for junk in cell.select("script, style, .hidden, noscript"):
        junk.decompose()
    return clean_text(cell.get_text(" ", strip=True))


def _tables(soup: BeautifulSoup) -> list[Tag]:
    return [t for t in soup.find_all("table") if t.find("tr")]


def _table_rows(table: Tag) -> list[list[str]]:
    rows: list[list[str]] = []
    for tr in table.find_all("tr"):
        cells = tr.find_all(["th", "td"])
        if not cells:
            continue
        rows.append([_cell_text(c) for c in cells])
    return [r for r in rows if any(c for c in r)]


def _is_paywalled(rows: list[list[str]]) -> bool:
    blob = " ".join(c.lower() for r in rows for c in r)
    return any(h in blob for h in PAYWALL_HINTS)


def _kv_map(rows: list[list[str]]) -> dict[str, str]:
    out: dict[str, str] = {}
    for row in rows:
        if len(row) < 2:
            continue
        key = norm_key(row[0])
        if key and key not in {"#"}:
            out[key] = row[1] if len(row) == 2 else " | ".join(row[1:])
    return out


def _find_key(kv: dict[str, str], *needles: str) -> Optional[str]:
    for key, val in kv.items():
        for needle in needles:
            if needle in key:
                return val
    return None


def _heading_text(tag: Tag) -> str:
    return clean_text(tag.get_text(" ", strip=True))


def _page_text(soup: BeautifulSoup) -> str:
    return clean_text(soup.get_text(" ", strip=True))


def _classify_table(rows: list[list[str]]) -> Optional[str]:
    if not rows:
        return None
    header = " ".join(norm_key(c) for c in rows[0])
    first_col = [norm_key(r[0]) for r in rows if r]
    blob = " ".join(first_col)
    if _is_paywalled(rows) and "subscription" in header:
        return "subscription"
    if any(k in blob for k in ("ipo date", "face value", "lot size", "issue type", "listed on", "listing at")):
        if "total issue size" in blob or "fresh issue" in blob or "isin" in blob:
            return "issue_size"
        return "ipo_details"
    if any(k in blob for k in ("total issue size", "fresh issue", "offer for sale", "isin", "bse script", "nse symbol")):
        return "issue_size"
    if "investor category" in header or "shares offered" in header and "max allottees" in header:
        return "reservation"
    if header.startswith("application") and "lots" in header:
        return "lot_size"
    if "bid date" in blob and ("lock in" in blob or "anchor" in blob):
        return "anchor"
    if "assets" in first_col and any("income" in k or "profit after tax" in k for k in first_col):
        return "financials"
    if any(k in first_col for k in ("roe", "roce", "ronw", "pat margin", "debt equity")):
        return "kpis"
    if any("eps" == k or k.startswith("eps") or k.startswith("p e") for k in first_col) and any(
        "pre ipo" in h or "post ipo" in h for h in [header]
    ):
        return "valuation"
    if "promoter and promoter group" in blob and "public" in blob:
        return "shareholding"
    if "no of shares offered" in header or ("category" in header and "amount" in header and len(rows[0]) >= 3):
        if any("promoter" in " ".join(r).lower() or "other" in " ".join(r).lower() for r in rows[1:3]):
            return "ofs"
    if "subscribe" in header and "may apply" in header:
        return "reviews"
    if "subscription" in header or any("qib" in c and "anchor" in c for c in first_col):
        return "subscription"
    if "price details" in header or (header.startswith("price details") or "open" in header and "high" in header and "low" in header):
        return "listing_day"
    if "issue objects" in header or (len(rows[0]) >= 2 and "est amt" in header):
        return "objects"
    return None


def _parse_id_slug(url: str) -> tuple[Optional[str], Optional[str]]:
    match = re.search(r"/ipo/([^/]+)/(\d+)/?", url)
    if not match:
        return None, None
    return match.group(2), match.group(1)


def _parse_timetable(soup: BeautifulSoup) -> dict[str, Optional[str]]:
    out: dict[str, Optional[str]] = {}
    text = soup.get_text("\n", strip=True)
    patterns = {
        "ipo_open": r"IPO Open\s*[:\n]?\s*([A-Za-z]{3,9},?\s+\w+\s+\d{1,2},\s+\d{4}|\w+\s+\d{1,2},\s+\d{4})",
        "ipo_close": r"IPO Close\s*[:\n]?\s*([A-Za-z]{3,9},?\s+\w+\s+\d{1,2},\s+\d{4}|\w+\s+\d{1,2},\s+\d{4})",
        "allotment_date": r"Allotment\s*[:\n]?\s*([A-Za-z]{3,9},?\s+\w+\s+\d{1,2},\s+\d{4}|\w+\s+\d{1,2},\s+\d{4})",
        "refund_date": r"Refund\s*[:\n]?\s*([A-Za-z]{3,9},?\s+\w+\s+\d{1,2},\s+\d{4}|\w+\s+\d{1,2},\s+\d{4})",
        "credit_date": r"Credit of Shares\s*[:\n]?\s*([A-Za-z]{3,9},?\s+\w+\s+\d{1,2},\s+\d{4}|\w+\s+\d{1,2},\s+\d{4})",
        "listing_date": r"Listing\s*[:\n]?\s*([A-Za-z]{3,9},?\s+\w+\s+\d{1,2},\s+\d{4}|\w+\s+\d{1,2},\s+\d{4})",
    }
    for field, pat in patterns.items():
        m = re.search(pat, text, flags=re.I)
        if m:
            out[field] = parse_date(m.group(1))
    return out


def _parse_industry(soup: BeautifulSoup) -> Optional[str]:
    for tag in soup.find_all(["h2", "h3", "h4", "strong", "b"]):
        text = _heading_text(tag)
        m = re.search(r"Recently Listed IPOs in\s+(.+)", text, flags=re.I)
        if m:
            industry = clean_text(m.group(1))
            industry = re.sub(r"\s+", " ", industry)
            if industry and len(industry) < 80:
                return industry
    m = re.search(r"Recently Listed IPOs in\s+([A-Za-z0-9&/,\.\- ]{3,80})", _page_text(soup), flags=re.I)
    if m:
        return clean_text(m.group(1))
    return None


def _parse_about(soup: BeautifulSoup) -> tuple[Optional[str], Optional[str]]:
    heading = None
    for tag in soup.find_all(["h2", "h3", "h4"]):
        text = _heading_text(tag)
        if not re.match(r"About\s+", text, flags=re.I):
            continue
        if re.search(r"chittorgarh|about us", text, flags=re.I):
            continue
        heading = tag
        break
    if heading is None:
        return None, None
    container = heading.find_parent(class_=re.compile(r"ipo-summary")) or heading.parent
    raw = container.get_text("\n", strip=True) if container else ""
    lines = [clean_text(ln) for ln in raw.splitlines()]
    lines = [
        ln
        for ln in lines
        if ln
        and not re.match(r"^(About |Updated on|\+?\s*Read More)", ln, flags=re.I)
        and not re.match(r"^[A-Za-z]{3,9}\s+\d{1,2},\s+\d{4}$", ln)
    ]
    strengths: list[str] = []
    about_lines: list[str] = []
    in_strengths = False
    for line in lines:
        if re.match(r"competitive strengths", line, flags=re.I):
            in_strengths = True
            continue
        if in_strengths:
            if len(line) > 20:
                strengths.append(line)
        else:
            about_lines.append(line)
    if container:
        for li in container.find_all("li"):
            item = clean_text(li.get_text(" ", strip=True))
            if item and item not in strengths:
                strengths.append(item)
    about = clean_text(" ".join(about_lines))[:4000] or None
    return about, ("; ".join(strengths)[:2000] if strengths else None)


def parse_allotment_published(soup: BeautifulSoup) -> bool:
    """True if Chittorgarh has a Basis of Allotment / allotment-out signal.

    Used by the live allotment notifier. The expected timetable date is a
    fallback only; this looks for a published document/link on the page.
    """
    blob = soup.get_text(" ", strip=True).lower()
    if "basis of allotment" in blob:
        return True
    if re.search(r"\ballotment\s+(out|finali[sz]ed|published)\b", blob):
        return True
    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").lower()
        text = clean_text(a.get_text(" ")).lower()
        if "basis-of-allotment" in href or "basis of allotment" in text:
            return True
        if "allotment" in href and ("pdf" in href or "basis" in href):
            return True
    return False


def _parse_registrar(soup: BeautifulSoup) -> Optional[str]:
    for tag in soup.find_all(["h2", "h3", "h4"]):
        if "registrar" in _heading_text(tag).lower():
            for sib in tag.next_siblings:
                if isinstance(sib, Tag) and sib.name in {"h2", "h3", "h4"}:
                    break
                if isinstance(sib, Tag):
                    text = clean_text(sib.get_text(" ", strip=True))
                    if text and "visit website" not in text.lower() and len(text) < 200:
                        # first substantial line
                        line = text.split("  ")[0].split("\n")[0]
                        if re.search(r"ltd|limited|pvt|private|link|intime|kfin|cameo|bigshare|skyline", line, re.I):
                            return clean_text(line.split("*")[0])
                        if len(line) > 5 and "@" not in line and not line.startswith("0"):
                            return clean_text(re.split(r"\d{3,}", line)[0])
    return None


def _parse_lead_managers(soup: BeautifulSoup) -> Optional[str]:
    names: list[str] = []
    for tag in soup.find_all(["h2", "h3", "h4"]):
        if re.search(r"lead manager", _heading_text(tag), flags=re.I):
            for sib in tag.next_siblings:
                if isinstance(sib, Tag) and sib.name in {"h2", "h3", "h4"}:
                    break
                if isinstance(sib, Tag) and sib.name in {"ol", "ul"}:
                    for li in sib.find_all("li"):
                        name = clean_text(re.sub(r"^\d+\.\s*", "", li.get_text(" ", strip=True)))
                        if name and "lead manager" not in name.lower() and "report" not in name.lower():
                            names.append(name)
                elif isinstance(sib, Tag) and sib.name in {"p", "div"}:
                    text = clean_text(sib.get_text(" ", strip=True))
                    m = re.match(r"\d+\.\s*(.+)", text)
                    if m:
                        name = clean_text(m.group(1))
                        if name and "report" not in name.lower():
                            names.append(name)
            break
    # fallback: numbered lines in page text
    if not names:
        text = soup.get_text("\n", strip=True)
        block = re.search(
            r"IPO Lead Manager[s]?\s*(?:\n|.){0,40}((?:\d+\.\s*.+\n?){1,6})",
            text,
            flags=re.I,
        )
        if block:
            for line in block.group(1).splitlines():
                m = re.match(r"\d+\.\s*(.+)", line.strip())
                if m and "report" not in m.group(1).lower():
                    names.append(clean_text(m.group(1)))
    unique: list[str] = []
    for n in names:
        if n and n not in unique:
            unique.append(n)
    return "; ".join(unique) if unique else None


def _parse_contact(soup: BeautifulSoup) -> tuple[Optional[str], Optional[str]]:
    address = None
    email = None
    for tag in soup.find_all(["h2", "h3", "h4"]):
        if "contact" in _heading_text(tag).lower():
            parts: list[str] = []
            for sib in tag.next_siblings:
                if isinstance(sib, Tag) and sib.name in {"h2", "h3", "h4"}:
                    break
                if isinstance(sib, Tag):
                    text = clean_text(sib.get_text(" ", strip=True))
                    if text:
                        parts.append(text)
            blob = " ".join(parts)
            em = re.search(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", blob)
            if em:
                email = em.group(0)
            address = blob[:500] or None
            break
    if email is None:
        em = re.search(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", _page_text(soup)[:8000])
        if em and "chittorgarh" not in em.group(0).lower():
            email = em.group(0)
    return address, email


def _parse_financials(rows: list[list[str]]) -> list[dict[str, Any]]:
    if len(rows) < 2:
        return []
    header = rows[0]
    periods = [clean_text(c) for c in header[1:] if clean_text(c)]
    out: list[dict[str, Any]] = []
    for row in rows[1:]:
        metric = clean_text(row[0])
        if not metric or metric.lower().startswith("amount in"):
            continue
        for i, period in enumerate(periods):
            val = row[i + 1] if i + 1 < len(row) else ""
            out.append(
                {
                    "period": parse_date(period) or period,
                    "metric": metric,
                    "metric_key": norm_key(metric),
                    "value": parse_number(val),
                    "value_raw": clean_text(val),
                }
            )
    return out


def _parse_kpis(rows: list[list[str]]) -> list[dict[str, Any]]:
    return _parse_financials(rows)


def _category_name(text: str) -> str:
    t = norm_key(text)
    t = t.replace("shares offered", "").replace("ex anchor", "ex anchor").strip()
    return t or text


def _parse_reservation(rows: list[list[str]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not rows:
        return out
    header = [norm_key(c) for c in rows[0]]
    for row in rows[1:]:
        if not row or norm_key(row[0]) in {"total", "total shares offered"}:
            continue
        item = {
            "category": clean_text(row[0]),
            "category_key": _category_name(row[0]),
            "shares": parse_number(row[1] if len(row) > 1 else None),
            "pct_net_issue": None,
            "pct_total_issue": None,
            "max_allottees": None,
        }
        # columns vary: Shares, % of Total, Max Allottees  OR  Shares, % of Net, % of Total, Max
        nums = [parse_number(c) for c in row[1:]]
        pcts = []
        for i, cell in enumerate(row[1:]):
            if "%" in cell:
                pcts.append(parse_number(cell))
        if len(row) >= 4 and "%" in (row[2] if len(row) > 2 else "") and "%" in (row[3] if len(row) > 3 else ""):
            item["pct_net_issue"] = parse_number(row[2])
            item["pct_total_issue"] = parse_number(row[3])
            item["max_allottees"] = parse_int(row[4]) if len(row) > 4 else None
        elif pcts:
            item["pct_total_issue"] = pcts[-1]
            if len(pcts) > 1:
                item["pct_net_issue"] = pcts[0]
            if len(row) > 3:
                last = row[-1]
                if "%" not in last:
                    item["max_allottees"] = parse_int(last)
        elif "shares" in " ".join(header):
            item["shares"] = parse_number(row[1]) if len(row) > 1 else None
            item["pct_total_issue"] = parse_number(row[2]) if len(row) > 2 else None
        out.append(item)
    return out


def _parse_subscription(rows: list[list[str]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows[1:]:
        if not row:
            continue
        cat = clean_text(row[0])
        if _is_paywalled([row]) and "total" not in cat.lower():
            continue
        item = {
            "category": cat,
            "category_key": _category_name(cat),
            "subscription_x": parse_number(row[1] if len(row) > 1 else None),
            "shares_offered": parse_number(row[2] if len(row) > 2 else None),
            "shares_bid": parse_number(row[3] if len(row) > 3 else None),
            "applications": parse_int(row[4] if len(row) > 4 else None),
        }
        out.append(item)
    return out


def _parse_listing_day(rows: list[list[str]]) -> list[dict[str, Any]]:
    if not rows or len(rows[0]) < 2:
        return []
    exchanges = [clean_text(c) for c in rows[0][1:]]
    grid: dict[str, list[str]] = {}
    for row in rows[1:]:
        if not row:
            continue
        grid[norm_key(row[0])] = row[1:]
    out = []
    for i, exch in enumerate(exchanges):
        def col(key: str) -> Optional[float]:
            vals = grid.get(key) or []
            return parse_number(vals[i]) if i < len(vals) else None

        out.append(
            {
                "exchange": exch,
                "issue_price": col("final issue price"),
                "open": col("open"),
                "low": col("low"),
                "high": col("high"),
                "last": col("last trade") or col("close") or col("last"),
            }
        )
    return out


def _parse_objects(rows: list[list[str]]) -> list[dict[str, Any]]:
    out = []
    for row in rows[1:]:
        if not row or norm_key(row[0]) == "total":
            continue
        desc = row[1] if len(row) > 2 else row[0]
        amt = row[-1]
        if norm_key(desc) == "total":
            continue
        out.append(
            {
                "serial": parse_int(row[0]) if len(row) > 2 else None,
                "object": clean_text(desc),
                "amount_cr": parse_number(amt),
            }
        )
    return out


def _parse_ofs(rows: list[list[str]]) -> list[dict[str, Any]]:
    out = []
    for row in rows[1:]:
        if not row or norm_key(row[0]) == "total":
            continue
        out.append(
            {
                "name": clean_text(row[0]),
                "category": clean_text(row[1]) if len(row) > 1 else None,
                "shares": parse_number(row[2] if len(row) > 2 else None),
                "amount_cr": parse_number(row[3] if len(row) > 3 else None),
            }
        )
    return out


def _parse_lot_size(rows: list[list[str]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    mapping = {
        "retail min": "retail_min",
        "retail max": "retail_max",
        "s hni min": "shni_min",
        "s hni max": "shni_max",
        "b hni min": "bhni_min",
        "hni min": "shni_min",
    }
    for row in rows[1:]:
        if not row:
            continue
        key = norm_key(row[0])
        prefix = None
        for needle, pref in mapping.items():
            if needle in key:
                prefix = pref
                break
        if not prefix:
            continue
        out[f"{prefix}_lots"] = parse_int(row[1] if len(row) > 1 else None)
        out[f"{prefix}_shares"] = parse_int(row[2] if len(row) > 2 else None)
        out[f"{prefix}_amount"] = parse_number(row[3] if len(row) > 3 else None)
    return out


def _parse_reviews(rows: list[list[str]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for row in rows[1:]:
        if not row:
            continue
        who = norm_key(row[0])
        if "broker" in who:
            out["broker_subscribe"] = parse_int(row[1] if len(row) > 1 else None)
            out["broker_may_apply"] = parse_int(row[2] if len(row) > 2 else None)
            out["broker_neutral"] = parse_int(row[3] if len(row) > 3 else None)
            out["broker_avoid"] = parse_int(row[4] if len(row) > 4 else None)
    return out


def _parse_valuation(rows: list[list[str]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for row in rows[1:]:
        if len(row) < 2:
            continue
        key = norm_key(row[0])
        pre = parse_number(row[1] if len(row) > 1 else None)
        post = parse_number(row[2] if len(row) > 2 else None)
        if key.startswith("eps"):
            out["eps_pre"] = pre
            out["eps_post"] = post
        elif "p e" in key or key.startswith("pe"):
            out["pe_pre"] = pre
            out["pe_post"] = post
        elif "market cap" in key:
            out["market_cap_offer_cr"] = pre
            out["market_cap_listing_cr"] = post if post is not None else pre
    return out


def _parse_shareholding(rows: list[list[str]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for row in rows[1:]:
        if not row:
            continue
        key = norm_key(row[0])
        pre = parse_number(row[1] if len(row) > 1 else None)
        post = parse_number(row[2] if len(row) > 2 else None)
        if "promoter" in key:
            out["promoter_pre_pct"] = pre
            out["promoter_post_pct"] = post
        elif key.startswith("public"):
            out["public_pre_pct"] = pre
            out["public_post_pct"] = post
    return out


def _promoters(soup: BeautifulSoup) -> Optional[str]:
    text = soup.get_text(" ", strip=True)
    m = re.search(r"Company Promoters?:\s*(.+?)(?:Offer For Sale|IPO Review|$)", text, flags=re.I)
    if not m:
        return None
    names = clean_text(m.group(1))
    names = re.sub(r"\s+", " ", names)
    return names[:500] if names else None


def _latest_kpi(kpis: list[dict[str, Any]], metric_key: str) -> Optional[float]:
    rows = [k for k in kpis if k["metric_key"] == metric_key and k["value"] is not None]
    if not rows:
        # fuzzy
        rows = [k for k in kpis if metric_key in k["metric_key"] and k["value"] is not None]
    if not rows:
        return None
    rows.sort(key=lambda r: r.get("period") or "")
    return rows[-1]["value"]


def _latest_fin(financials: list[dict[str, Any]], metric_key: str) -> tuple[Optional[str], Optional[float]]:
    rows = [k for k in financials if metric_key in k["metric_key"] and k["value"] is not None]
    if not rows:
        return None, None
    rows.sort(key=lambda r: r.get("period") or "")
    last = rows[-1]
    return last.get("period"), last["value"]


def _reservation_pct(reservations: list[dict[str, Any]], *needles: str, exclude: tuple[str, ...] = ()) -> Optional[float]:
    for row in reservations:
        key = row["category_key"]
        if any(e in key for e in exclude):
            continue
        if any(n in key for n in needles):
            return row.get("pct_net_issue") or row.get("pct_total_issue")
    return None


def _sub_x(subs: list[dict[str, Any]], *needles: str, exclude: tuple[str, ...] = ()) -> Optional[float]:
    for row in subs:
        key = row["category_key"]
        if any(e in key for e in exclude):
            continue
        if any(n in key for n in needles):
            return row.get("subscription_x")
    return None


def _norm_sale_type(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    t = raw.lower()
    if "ofs only" in t or (t.strip() in {"ofs", "offer for sale"}):
        return "OFS only"
    if "fresh" in t and ("ofs" in t or "offer for sale" in t or "cum" in t):
        return "Fresh capital cum OFS"
    if "fresh" in t:
        return "Fresh capital only"
    return clean_text(raw)


def parse_ipo_html(
    html: str,
    url: str,
    exchange_type: Optional[str] = None,
    listing_year: Optional[int] = None,
    tracker: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    soup = BeautifulSoup(html, "lxml")
    warnings: list[str] = []
    ipo_id, slug = _parse_id_slug(url)
    tracker = tracker or {}

    classified: dict[str, list[list[str]]] = {}
    for table in _tables(soup):
        rows = _table_rows(table)
        kind = _classify_table(rows)
        if kind and kind not in classified:
            classified[kind] = rows
        elif kind == "ipo_details" and "issue_size" not in classified:
            # second kv table often follows
            kv = _kv_map(rows)
            if _find_key(kv, "total issue size", "isin", "fresh issue"):
                classified["issue_size"] = rows

    details_kv = _kv_map(classified.get("ipo_details") or [])
    size_kv = _kv_map(classified.get("issue_size") or [])
    # merge if details table also had size keys
    if not size_kv and details_kv:
        size_kv = {k: v for k, v in details_kv.items() if any(
            x in k for x in ("issue size", "fresh", "offer for sale", "isin", "bse", "nse", "share holding")
        )}

    financials = _parse_financials(classified.get("financials") or [])
    kpis = _parse_kpis(classified.get("kpis") or [])
    reservations = _parse_reservation(classified.get("reservation") or [])
    subscription = _parse_subscription(classified.get("subscription") or [])
    listing_day = _parse_listing_day(classified.get("listing_day") or [])
    objects = _parse_objects(classified.get("objects") or [])
    ofs = _parse_ofs(classified.get("ofs") or [])
    lots = _parse_lot_size(classified.get("lot_size") or [])
    reviews = _parse_reviews(classified.get("reviews") or [])
    valuation = _parse_valuation(classified.get("valuation") or [])
    holding = _parse_shareholding(classified.get("shareholding") or [])
    timetable = _parse_timetable(soup)

    if _is_paywalled(classified.get("subscription") or []):
        warnings.append("subscription_category_paywalled")

    ipo_date_raw = _find_key(details_kv, "ipo date")
    open_d, close_d = parse_date_range(ipo_date_raw)
    listed_raw = _find_key(details_kv, "listed on", "listing date")
    listing_date = parse_date(listed_raw) or timetable.get("listing_date")

    price_band_raw = _find_key(details_kv, "price band")
    band_low, band_high = parse_price_band(price_band_raw)
    issue_price = parse_number(_find_key(details_kv, "issue price")) or tracker.get("issue_price")
    if issue_price is None and band_high is not None:
        issue_price = band_high

    codes_raw = _find_key(size_kv, "bse script", "nse symbol")
    bse_code, nse_symbol = parse_bse_nse(codes_raw)
    if nse_symbol is None:
        nse_symbol = clean_text(_find_key(size_kv, "nse symbol") or "") or None
    if bse_code is None:
        bse_code = re.sub(r"\D", "", _find_key(size_kv, "bse script") or "") or None

    total_shares, total_cr = parse_shares_and_cr(_find_key(size_kv, "total issue size"))
    fresh_shares, fresh_cr = parse_shares_and_cr(_find_key(size_kv, "fresh issue"))
    ofs_shares, ofs_cr = parse_shares_and_cr(_find_key(size_kv, "offer for sale"))

    fy_latest, assets = _latest_fin(financials, "assets")
    _, income = _latest_fin(financials, "total income")
    _, pat = _latest_fin(financials, "profit after tax")
    _, ebitda = _latest_fin(financials, "ebitda")
    _, net_worth = _latest_fin(financials, "net worth")
    _, borrowings = _latest_fin(financials, "borrow")

    nse_row = next((r for r in listing_day if "nse" in (r.get("exchange") or "").lower()), None)
    bse_row = next((r for r in listing_day if "bse" in (r.get("exchange") or "").lower()), None)
    list_row = nse_row or bse_row or (listing_day[0] if listing_day else {})

    apps = None
    m_apps = re.search(r"Total Applications:\s*([\d,]+)", _page_text(soup), flags=re.I)
    if m_apps:
        apps = parse_int(m_apps.group(1))
    if apps is None:
        for row in subscription:
            if "total" in row["category_key"] and row.get("applications"):
                apps = row["applications"]

    about, strengths = _parse_about(soup)
    address, email = _parse_contact(soup)
    h1 = soup.find("h1")
    company_name = tracker.get("company_name")
    if h1:
        title = clean_text(h1.get_text(" ", strip=True))
        title = re.sub(r"\s+IPO.*$", "", title, flags=re.I).strip()
        if title:
            company_name = title

    market_cap = parse_number(
        _find_key(details_kv, "market cap") or ""
    )
    if market_cap is None:
        mcap_m = re.search(r"Market Cap\*?\s*₹?\s*([\d,.]+)\s*Cr", _page_text(soup), flags=re.I)
        if mcap_m:
            market_cap = parse_number(mcap_m.group(1))
    if market_cap is None:
        market_cap = valuation.get("market_cap_listing_cr")

    anchor_kv = _kv_map(classified.get("anchor") or [])
    sale_type = _norm_sale_type(_find_key(details_kv, "sale type"))

    master = {
        "ipo_id": ipo_id,
        "slug": slug,
        "url": urljoin(BASE_URL, url) if url.startswith("/") else url,
        "company_name": company_name,
        "exchange_type": exchange_type,
        "listing_year": listing_year,
        "industry": _parse_industry(soup),
        "issue_type": clean_text(_find_key(details_kv, "issue type") or "") or None,
        "sale_type": sale_type,
        "listing_at": clean_text(_find_key(details_kv, "listing at") or "") or None,
        "ipo_open": timetable.get("ipo_open") or open_d,
        "ipo_close": timetable.get("ipo_close") or close_d,
        "allotment_date": timetable.get("allotment_date"),
        "refund_date": timetable.get("refund_date"),
        "credit_date": timetable.get("credit_date"),
        "listing_date": listing_date,
        "anchor_bid_date": parse_date(_find_key(anchor_kv, "bid date")),
        "face_value": parse_number(_find_key(details_kv, "face value")),
        "price_band_low": band_low,
        "price_band_high": band_high,
        "issue_price": issue_price,
        "lot_size": parse_int(_find_key(details_kv, "lot size")),
        "retail_min_amount": lots.get("retail_min_amount"),
        "issue_size_shares": total_shares,
        "issue_size_cr": total_cr,
        "fresh_issue_shares": fresh_shares,
        "fresh_issue_cr": fresh_cr,
        "ofs_shares": ofs_shares,
        "ofs_cr": ofs_cr,
        "pre_issue_shares": parse_number(_find_key(size_kv, "share holding pre", "pre issue")),
        "post_issue_shares": parse_number(_find_key(size_kv, "share holding post", "post issue")),
        "bse_code": bse_code,
        "nse_symbol": nse_symbol,
        "isin": clean_text(_find_key(size_kv, "isin") or "") or None,
        "market_cap_listing_cr": market_cap,
        "qib_pct": _reservation_pct(reservations, "qib", exclude=("anchor", "ex anchor")),
        "anchor_pct": _reservation_pct(reservations, "anchor"),
        "nii_pct": _reservation_pct(reservations, "nii", "hni", exclude=("bnii", "snii", "b nii", "s nii", "> ", "< ")),
        "retail_pct": _reservation_pct(reservations, "retail", "rii"),
        "employee_pct": _reservation_pct(reservations, "employee"),
        "market_maker_pct": _reservation_pct(reservations, "market maker"),
        "anchor_shares": parse_number(_find_key(anchor_kv, "shares offered")),
        "anchor_amount_cr": parse_number(_find_key(anchor_kv, "anchor portion", "cr")),
        "anchor_lockin_30d": parse_date(_find_key(anchor_kv, "30 day", "50%")),
        "anchor_lockin_90d": parse_date(_find_key(anchor_kv, "90 day", "remaining")),
        **lots,
        "fy_latest": fy_latest,
        "assets_cr": assets,
        "total_income_cr": income,
        "pat_cr": pat,
        "ebitda_cr": ebitda,
        "net_worth_cr": net_worth,
        "borrowings_cr": borrowings,
        "eps_pre": valuation.get("eps_pre"),
        "eps_post": valuation.get("eps_post"),
        "pe_pre": valuation.get("pe_pre"),
        "pe_post": valuation.get("pe_post"),
        "roe": _latest_kpi(kpis, "roe"),
        "roce": _latest_kpi(kpis, "roce"),
        "ronw": _latest_kpi(kpis, "ronw"),
        "debt_equity": _latest_kpi(kpis, "debt equity") or _latest_kpi(kpis, "debt/equity"),
        "pat_margin": _latest_kpi(kpis, "pat margin"),
        "ebitda_margin": _latest_kpi(kpis, "ebitda margin"),
        "nav": _latest_kpi(kpis, "nav"),
        "pbv": _latest_kpi(kpis, "price to book") or _latest_kpi(kpis, "pbv"),
        **holding,
        "promoters": _promoters(soup),
        "gmp_close_date": None,
        "gmp_rs": None,
        "gmp_pct": None,
        "gmp_est_listing_price": None,
        "kostak_rs": None,
        "subject_to_sauda": None,
        "sub_qib_x": _sub_x(subscription, "qib"),
        "sub_nii_x": _sub_x(subscription, "nii", exclude=("bnii", "snii", "b nii", "s nii", "> ", "< ")),
        "sub_bnii_x": _sub_x(subscription, "bnii", "b nii", ">"),
        "sub_snii_x": _sub_x(subscription, "snii", "s nii", "<"),
        "sub_retail_x": _sub_x(subscription, "retail"),
        "sub_total_x": _sub_x(subscription, "total"),
        "total_applications": apps,
        **reviews,
        "registrar": _parse_registrar(soup),
        "allotment_published": parse_allotment_published(soup),
        "lead_managers": _parse_lead_managers(soup),
        "listing_day_close": tracker.get("listing_day_close"),
        "listing_day_gain_pct": tracker.get("listing_day_gain_pct"),
        "current_price": tracker.get("current_price"),
        "profit_loss_pct": tracker.get("profit_loss_pct"),
        "list_open": list_row.get("open") if list_row else None,
        "list_high": list_row.get("high") if list_row else None,
        "list_low": list_row.get("low") if list_row else None,
        "list_last": list_row.get("last") if list_row else None,
        "about_text": about,
        "strengths": strengths,
        "company_address": address,
        "company_email": email,
        "scraped_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "parse_warnings": "; ".join(warnings) if warnings else None,
    }

    satellites = {
        "financials": [{"ipo_id": ipo_id, **r} for r in financials],
        "kpis": [{"ipo_id": ipo_id, **r} for r in kpis],
        "reservation": [{"ipo_id": ipo_id, **r} for r in reservations],
        "subscription": [{"ipo_id": ipo_id, **r} for r in subscription],
        "listing_day": [{"ipo_id": ipo_id, **r} for r in listing_day],
        "objects": [{"ipo_id": ipo_id, **r} for r in objects],
        "ofs_shareholders": [{"ipo_id": ipo_id, **r} for r in ofs],
    }
    return {"master": master, "satellites": satellites}
