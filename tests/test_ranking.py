from analysis.live_audit import rank_same_day_candidates, read_audit
from pathlib import Path


def _rec(**extra):
    rec = {
        "ipo_id": "1",
        "company_name": "A",
        "close_date": "2026-09-01",
        "price_band_high": 100,
        "lot_size": 100,
        "ev_retail": 2000,
        "apply_s1": True,
        "sub_qib_x": 5,
    }
    rec.update(extra)
    return rec


def test_rank_same_day_by_ev_capital_then_qib() -> None:
    # capital 10k, 15k, 10k; ratios 0.2, 0.2, 0.05
    rows = rank_same_day_candidates(
        [
            _rec(ipo_id="high_qib", company_name="High QIB", ev_retail=2000, lot_size=100, sub_qib_x=20),
            _rec(ipo_id="low_qib", company_name="Low QIB", ev_retail=2000, lot_size=100, sub_qib_x=2),
            _rec(ipo_id="cheap", company_name="Cheap EV", ev_retail=500, lot_size=100, sub_qib_x=50),
            _rec(ipo_id="skip", company_name="Skip", apply_s1=False, ev_retail=9000, sub_qib_x=99),
        ]
    )
    ranked = {r["ipo_id"]: r for r in rows}
    assert ranked["high_qib"]["rank_of_day"] == 1
    assert ranked["low_qib"]["rank_of_day"] == 2
    assert ranked["cheap"]["rank_of_day"] == 3
    assert ranked["high_qib"]["rank_total_of_day"] == 3
    assert ranked["skip"]["rank_of_day"] is None
    assert ranked["high_qib"]["ev_capital_ratio"] == 0.2
    assert ranked["cheap"]["capital_required"] == 10000


def test_lone_applicant_has_no_rank() -> None:
    rows = rank_same_day_candidates([_rec()])
    assert rows[0]["rank_of_day"] is None
    assert rows[0]["rank_total_of_day"] is None
    assert rows[0]["capital_required"] == 10000


def test_missing_price_does_not_divide_by_zero() -> None:
    rows = rank_same_day_candidates(
        [
            _rec(ipo_id="a", price_band_high=None, lot_size=None, ev_retail=100, apply_s1=True),
            _rec(ipo_id="b", ev_retail=50, apply_s1=True),
        ]
    )
    by_id = {r["ipo_id"]: r for r in rows}
    assert by_id["a"]["capital_required"] is None
    assert by_id["a"]["ev_capital_ratio"] is None
    assert by_id["a"]["rank_of_day"] == 2  # missing ratio sorts worst
    assert by_id["b"]["rank_of_day"] == 1


def test_sme_missing_qib_sorts_worst_on_tie() -> None:
    rows = rank_same_day_candidates(
        [
            _rec(ipo_id="sme", ev_retail=2000, sub_qib_x=None),
            _rec(ipo_id="mb", ev_retail=2000, sub_qib_x=1.1),
        ]
    )
    by_id = {r["ipo_id"]: r for r in rows}
    assert by_id["mb"]["rank_of_day"] == 1
    assert by_id["sme"]["rank_of_day"] == 2


def test_read_audit_fills_new_columns_on_old_header(tmp_path: Path) -> None:
    path = tmp_path / "live_audit_log.csv"
    path.write_text("timestamp_utc,ipo_id,company_name,board,close_date\n,1,Foo,mainboard,2026-08-31\n", encoding="utf-8")
    frame = read_audit(path)
    assert "sub_qib_x" in frame.columns
    assert "rank_of_day" in frame.columns
    assert "allotment_notified" in frame.columns
    assert "market_regime" in frame.columns
