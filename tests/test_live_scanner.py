from pathlib import Path

from analysis.live_audit import (
    build_alert_record,
    compute_actuals,
    performance_summary,
    to_score_row,
    upsert_audit,
)
from analysis.score import score_features
from datetime import date

from scripts import live_scanner
from scripts.live_scanner import _select_candidates, run_scan


MASTER = {
    "ipo_id": "2013",
    "company_name": "Lumino Industries",
    "exchange_type": "mainboard",
    "issue_price": 216.0,
    "price_band_high": 216.0,
    "lot_size": 69,
    "retail_min_amount": 14904.0,
    "issue_size_cr": 400.0,
    "ofs_cr": 80.0,
    "sub_total_x": 12.4,
    "gmp_rs": 18.0,
    "gmp_pct": 8.3,
    "roe": 18.0,
    "debt_equity": 0.3,
    "ipo_close": "2026-08-31",
}


def test_to_score_row_copies_gmp_rs_and_year() -> None:
    row = to_score_row(dict(MASTER), close_date="2026-08-31")
    assert row["gmp_at_close"] == 18.0
    assert row["listing_year"] == 2026
    assert row["sub_total_x"] == 12.4
    assert row["issue_price"] == 216.0


def test_score_features_accepts_glued_master(tmp_path: Path) -> None:
    out = score_features(to_score_row(dict(MASTER), "2026-08-31"), model_dir=tmp_path)
    assert "apply_s1" in out
    assert "ev_retail" in out
    assert "p_allot" in out
    assert out["quality_score"] >= 2
    assert isinstance(out["quality_breakdown"], list)
    assert out["apply_s2"] is (out["quality_score"] >= 3)


def test_build_alert_record_keys() -> None:
    score = score_features(to_score_row(dict(MASTER), "2026-08-31"), model_dir=Path("no_models_here"))
    rec = build_alert_record(MASTER, score, {"ipo_id": "2013", "company_name": "Lumino Industries", "exchange_type": "mainboard", "close_date": "2026-08-31", "url": "https://www.chittorgarh.com/ipo/lumino-industries-ipo/2013/"})
    assert rec["ipo_id"] == "2013"
    assert rec["apply_s1"] == score["apply_s1"]
    assert rec["verified"] is False
    assert rec["quality_breakdown_json"]


def test_upsert_audit_is_idempotent_on_ipo_and_close(tmp_path: Path) -> None:
    path = tmp_path / "live_audit_log.csv"
    first = [build_alert_record(MASTER, None, {"ipo_id": "2013", "close_date": "2026-08-31", "company_name": "Lumino", "exchange_type": "mainboard"})]
    upsert_audit(path, first)
    second = [build_alert_record({**MASTER, "gmp_rs": 22}, None, {"ipo_id": "2013", "close_date": "2026-08-31", "company_name": "Lumino", "exchange_type": "mainboard"})]
    out = upsert_audit(path, second)
    assert len(out) == 1
    assert float(out.iloc[0]["gmp_rs"]) == 22


def test_select_candidates_closing_and_include_open() -> None:
    rows = [
        {"ipo_id": "1", "status": "open", "close_date": "2026-08-31"},
        {"ipo_id": "2", "status": "open", "close_date": "2026-09-01"},
        {"ipo_id": "3", "status": "upcoming", "close_date": "2026-09-03"},
    ]
    as_of = date(2026, 8, 31)
    assert [r["ipo_id"] for r in _select_candidates(rows, as_of, False)] == ["1"]
    both = _select_candidates(rows, as_of, True)
    assert [r["ipo_id"] for r in both] == ["1", "2"]


def test_compute_actuals_clean_pop() -> None:
    actuals = compute_actuals(
        {
            "issue_price": 100,
            "listing_nse_open": 120,
            "listing_nse_low": 118,
            "listing_day_gain_pct": 22,
        }
    )
    assert actuals is not None
    assert abs(actuals["actual_open_return_pct"] - 20.0) < 1e-9
    assert actuals["actual_is_clean_pop"] is True
    assert compute_actuals({"issue_price": 100}) is None


def test_compute_actuals_does_not_need_tracker_gain() -> None:
    """Regression: a live re-fetch of the bare detail URL never passes a
    `tracker` dict into parse_ipo_html(), so `listing_day_gain_pct` is always
    None. compute_actuals() must still resolve a real True/False instead of
    permanently returning actual_is_clean_pop=None (which locked verified=True
    forever with a null outcome)."""
    actuals = compute_actuals(
        {
            "issue_price": 100,
            "listing_nse_open": 120,
            "listing_nse_low": 118,
            # no listing_day_gain_pct at all
        }
    )
    assert actuals is not None
    assert actuals["actual_is_clean_pop"] is True

    weak = compute_actuals(
        {
            "issue_price": 100,
            "listing_nse_open": 105,
            "listing_nse_low": 95,
        }
    )
    assert weak is not None
    assert weak["actual_is_clean_pop"] is False

    gap_down_low = compute_actuals(
        {
            "issue_price": 100,
            "listing_nse_open": 120,
            "listing_nse_low": 90,
        }
    )
    assert gap_down_low is not None
    assert gap_down_low["actual_is_clean_pop"] is False


def test_performance_summary_empty() -> None:
    import pandas as pd

    summary = performance_summary(pd.DataFrame())
    assert summary["n_alerts"] == 0


def test_upsert_audit_tolerates_duplicate_keys_in_existing_file(tmp_path: Path) -> None:
    path = tmp_path / "live_audit_log.csv"
    dup_row = build_alert_record(MASTER, None, {"ipo_id": "2013", "close_date": "2026-08-31", "company_name": "Lumino", "exchange_type": "mainboard"})
    upsert_audit(path, [dup_row])
    # Simulate a corrupted/hand-edited file with the same (ipo_id, close_date) twice.
    import pandas as pd

    doubled = pd.concat([pd.read_csv(path), pd.read_csv(path)], ignore_index=True)
    doubled.to_csv(path, index=False)
    other = [build_alert_record({**MASTER, "ipo_id": "9999"}, None, {"ipo_id": "9999", "close_date": "2026-08-31", "company_name": "Other", "exchange_type": "mainboard"})]
    out = upsert_audit(path, other)  # must not raise
    assert set(out["ipo_id"].astype(str)) == {"2013", "9999"}


def test_run_scan_sends_failure_alert_on_empty_discovery(monkeypatch, tmp_path: Path) -> None:
    calls = []
    monkeypatch.setattr(live_scanner, "send_failure_alert", lambda msg: calls.append(msg))
    out = run_scan(rows=[], out_dir=tmp_path, dry_run=False)
    assert out == []
    assert len(calls) == 1
    assert "0 rows" in calls[0]


def test_run_scan_no_failure_alert_when_simply_no_closers_today(monkeypatch, tmp_path: Path) -> None:
    calls = []
    monkeypatch.setattr(live_scanner, "send_failure_alert", lambda msg: calls.append(msg))
    rows = [{"ipo_id": "1", "status": "upcoming", "close_date": "2026-09-05"}]
    out = run_scan(rows=rows, as_of=date(2026, 8, 31), out_dir=tmp_path, dry_run=False)
    assert out == []
    assert calls == []
