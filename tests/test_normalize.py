from chittorgarh.normalize import (
    parse_bse_nse,
    parse_date,
    parse_date_range,
    parse_number,
    parse_price_band,
    parse_shares_and_cr,
)


def test_parse_number_indian_commas_and_crore() -> None:
    assert parse_number("₹1,101 Cr") == 1101
    assert parse_number("40.92%") == 40.92
    assert parse_number("9.11x") == 9.11
    assert parse_number("2,59,31,407") == 25931407


def test_parse_shares_and_cr() -> None:
    shares, cr = parse_shares_and_cr("2,59,31,407 shares (agg. up to ₹1,101 Cr)")
    assert shares == 25931407
    assert cr == 1101


def test_parse_price_band() -> None:
    assert parse_price_band("₹404 to ₹425") == (404.0, 425.0)


def test_parse_bse_nse() -> None:
    assert parse_bse_nse("544839 / LCL") == ("544839", "LCL")


def test_parse_dates() -> None:
    assert parse_date("Thu, Jul 30, 2026") == "2026-07-30"
    assert parse_date("30-07-2026") == "2026-07-30"
    assert parse_date_range("23 to 27 Jul, 2026") == ("2026-07-23", "2026-07-27")
