from chittorgarh.gmp import (
    _parse_gmp_rows,
    _subscription_header_index,
    investorgain_gmp_url,
    last_gmp_close,
    last_gmp_on_or_before,
)


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
    assert parsed[0]["sub_ig_x"] == 7.26
    assert parsed[0]["gmp_last_updated"] == "30-Jul-2026 9:30"
    close = last_gmp_close(parsed, "2026-07-30")
    assert close["gmp_close_date"] == "2026-07-30"
    assert close["gmp_rs"] == 17
    assert close["sub_ig_x"] == 7.26
    at_close = last_gmp_on_or_before(parsed, "2026-07-28")
    assert at_close == {}
    eve = last_gmp_on_or_before(parsed, "2026-07-29")
    assert eve["gmp_close_date"] == "2026-07-29"
    assert eve["gmp_rs"] == 17


def test_subscription_column_is_not_confused_with_sauda() -> None:
    rows = [
        ["GMP DATE", "GMP", "SUBSCRIPTION", "SUBJECT TO SAUDA", "KOSTAK"],
        ["31-08-2026 Close", "₹70", "97.92x", "₹500", "₹200"],
        ["30-08-2026", "₹65", "20.67x", "₹400", "₹180"],
    ]
    parsed = _parse_gmp_rows(rows, "2757")
    assert parsed[0]["sub_ig_x"] == 97.92
    assert parsed[0]["subject_to_sauda"] == 500
    assert parsed[0]["kostak_rs"] == 200
    header = ["GMP DATE", "GMP", "SUBJECT TO SAUDA", "KOSTAK"]
    assert _subscription_header_index(header) is None
    sauda_only = _parse_gmp_rows(
        [
            header,
            ["31-08-2026", "₹70", "₹500", "₹200"],
        ],
        "2757",
    )
    assert sauda_only[0]["sub_ig_x"] is None
    assert sauda_only[0]["subject_to_sauda"] == 500


def test_last_gmp_on_or_before_picks_close_day_row() -> None:
    history = [
        {"gmp_date": "2026-08-31", "gmp_rs": 70, "sub_ig_x": 97.92, "gmp_date_raw": "31-08-2026 Close"},
        {"gmp_date": "2026-08-30", "gmp_rs": 65, "sub_ig_x": 20.67, "gmp_date_raw": "30-08-2026"},
    ]
    out = last_gmp_on_or_before(history, "2026-08-31")
    assert out["gmp_close_date"] == "2026-08-31"
    assert out["gmp_rs"] == 70
    assert out["sub_ig_x"] == 97.92
    listing = last_gmp_on_or_before(history, "2026-09-03")
    assert listing["gmp_close_date"] == "2026-08-31"
    assert listing["gmp_rs"] == 70


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
