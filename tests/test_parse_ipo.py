from chittorgarh.parse_ipo import _resolve_ofs_cr


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
