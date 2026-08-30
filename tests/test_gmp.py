from chittorgarh.gmp import _parse_gmp_rows, investorgain_gmp_url, last_gmp_close, last_gmp_on_or_before


def test_investorgain_url() -> None:
    url = "https://www.chittorgarh.com/ipo/lohia-corp-ipo/2574/"
    assert investorgain_gmp_url(url, "2574") == "https://www.investorgain.com/chr-gmp/lohia-corp-ipo/2574"


def test_parse_investorgain_gmp_table() -> None:
    rows = [
        ["GMP DATE", "IPO PRICE", "GMP", "SUBSCRIPTION", "EST. LISTING PRICE (CAP PRICE + GMP)", "EST. PROFIT*", "LAST UPDATED"],
        ["30-07-2026 Listing", "₹425.00", "₹17", "7.26x", "₹442 (4.00%)", "₹595", "30-Jul-2026 9:30"],
        ["29-07-2026", "₹425.00", "₹17", "7.26x", "₹442 (4.00%)", "₹595", "29-Jul-2026 23:29"],
    ]
    parsed = _parse_gmp_rows(rows, "2574")
    assert len(parsed) == 2
    assert parsed[0]["gmp_date"] == "2026-07-30"
    assert parsed[0]["gmp_rs"] == 17
    assert parsed[0]["gmp_est_listing_price"] == 442
    close = last_gmp_close(parsed, "2026-07-30")
    assert close["gmp_close_date"] == "2026-07-30"
    assert close["gmp_rs"] == 17
    at_close = last_gmp_on_or_before(parsed, "2026-07-28")
    assert at_close == {}
    eve = last_gmp_on_or_before(parsed, "2026-07-29")
    assert eve["gmp_close_date"] == "2026-07-29"
    assert eve["gmp_rs"] == 17


def test_last_gmp_close_fallback_picks_latest_not_last_list_element():
    # Table is newest-first (as investorgain renders it), so history[-1] would be
    # the OLDEST row -- the fallback must sort by date and pick the latest one.
    history = [
        {"gmp_date": "2026-07-30", "gmp_rs": 20},
        {"gmp_date": "2026-07-29", "gmp_rs": 17},
    ]
    out = last_gmp_close(history, "2026-07-01")  # as_of before every recorded date
    assert out["gmp_close_date"] == "2026-07-30"
    assert out["gmp_rs"] == 20
