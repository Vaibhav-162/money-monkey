from __future__ import annotations

from pathlib import Path

import pytest

from chittorgarh.export import flatten_into_master
from chittorgarh.parse_ipo import parse_ipo_html
from chittorgarh.smoke import check_golden, print_results

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "lohia_2574.html"
URL = "https://www.chittorgarh.com/ipo/lohia-corp-ipo/2574/"


@pytest.mark.skipif(not FIXTURE.exists(), reason="Run python scrape_ipos.py --smoke first to save the fixture")
def test_parse_lohia_fixture() -> None:
    html = FIXTURE.read_text(encoding="utf-8", errors="replace")
    parsed = parse_ipo_html(
        html,
        url=URL,
        exchange_type="mainboard",
        listing_year=2026,
        tracker={"company_name": "Lohia Corp Ltd.", "issue_price": 425, "ipo_id": "2574"},
    )
    row = flatten_into_master(parsed["master"], parsed["satellites"])
    results = check_golden(row, sats={})
    assert print_results(results)
    assert len(parsed["satellites"]["listing_day"]) == 2
    assert any("roce" in r["metric_key"] for r in parsed["satellites"]["kpis"])
