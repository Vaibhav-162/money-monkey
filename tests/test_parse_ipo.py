from chittorgarh.parse_ipo import _resolve_ofs_cr, parse_ipo_html


def test_resolve_ofs_cr_fresh_capital_only_backfills_zero() -> None:
    assert _resolve_ofs_cr(None, None, None, None, "Fresh capital only") == (0.0, 0)
    assert _resolve_ofs_cr(None, 12_345.0, None, None, "Fresh capital only") == (0.0, 12_345.0)


def test_resolve_ofs_cr_equal_fresh_and_total_backfills_zero() -> None:
    assert _resolve_ofs_cr(None, None, 100.0, 100.0, None) == (0.0, 0)


def test_resolve_ofs_cr_ofs_only_stays_undisclosed() -> None:
    assert _resolve_ofs_cr(None, None, None, None, "OFS only") == (None, None)


def test_resolve_ofs_cr_mixed_missing_row_stays_undisclosed() -> None:
    assert _resolve_ofs_cr(None, None, 80.0, 100.0, None) == (None, None)


def test_resolve_ofs_cr_existing_value_passthrough() -> None:
    assert _resolve_ofs_cr(50.0, 1_000.0, 50.0, 100.0, "Fresh capital only") == (50.0, 1_000.0)
    assert _resolve_ofs_cr(50.0, None, None, None, "OFS only") == (50.0, None)
    assert _resolve_ofs_cr(50.0, 1_000.0, 100.0, 100.0, None) == (50.0, 1_000.0)


def test_resolve_ofs_cr_fresh_only_label_does_not_override_size_gap() -> None:
    # Sale type says fresh-only but total > fresh: the omitted OFS row is
    # unknown, not 0%. A false PASS would hide a real cash-out red flag.
    assert _resolve_ofs_cr(None, None, 80.0, 100.0, "Fresh capital only") == (None, None)


def test_resolve_ofs_cr_ofs_only_equal_sizes_stay_undisclosed() -> None:
    # Equal fresh/total must not force ofs_cr=0 when Chittorgarh labeled OFS-only.
    assert _resolve_ofs_cr(None, None, 100.0, 100.0, "OFS only") == (None, None)
    assert _resolve_ofs_cr(None, None, 80.0, 100.0, "Fresh capital cum OFS") == (None, None)


def _issue_html(*, sale_type: str, fresh_cr: str, total_cr: str, ofs_row: str = "") -> str:
    return f"""
    <html><body>
    <h1>Acme Ltd IPO</h1>
    <table>
      <tr><td>IPO Date</td><td>1 Sep, 2026</td></tr>
      <tr><td>Sale Type</td><td>{sale_type}</td></tr>
      <tr><td>Face Value</td><td>10</td></tr>
      <tr><td>Lot Size</td><td>100</td></tr>
    </table>
    <table>
      <tr><td>Total Issue Size</td><td>{total_cr} Cr</td></tr>
      <tr><td>Fresh Issue</td><td>{fresh_cr} Cr</td></tr>
      {ofs_row}
      <tr><td>ISIN</td><td>INE123A01016</td></tr>
    </table>
    </body></html>
    """


def test_parse_ipo_html_fresh_only_missing_ofs_row_is_zero() -> None:
    parsed = parse_ipo_html(
        _issue_html(sale_type="Fresh Capital only", fresh_cr="100", total_cr="100"),
        url="https://www.chittorgarh.com/ipo/acme-ipo/9999/",
    )
    assert parsed["master"]["ofs_cr"] == 0.0
    assert parsed["master"]["ofs_shares"] == 0


def test_parse_ipo_html_fresh_only_label_with_size_gap_stays_undisclosed() -> None:
    parsed = parse_ipo_html(
        _issue_html(sale_type="Fresh Capital only", fresh_cr="80", total_cr="100"),
        url="https://www.chittorgarh.com/ipo/acme-ipo/9999/",
    )
    assert parsed["master"]["ofs_cr"] is None
    assert parsed["master"]["sale_type"] == "Fresh capital only"
