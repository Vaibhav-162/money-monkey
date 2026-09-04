from pathlib import Path

import pytest
from analysis.live_audit import (
    build_alert_record,
    compute_actuals,
    performance_summary,
    records_needing_alert,
    scrape_timestamps,
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


def test_build_alert_record_copies_qib_and_registrar() -> None:
    rec = build_alert_record(
        {**MASTER, "sub_qib_x": 9.11, "registrar": "MUFG Intime", "allotment_date": "2026-09-02"},
        None,
        {"ipo_id": "2013", "close_date": "2026-08-31", "company_name": "Lumino", "exchange_type": "mainboard"},
    )
    assert rec["sub_qib_x"] == 9.11
    assert rec["registrar"] == "MUFG Intime"
    assert rec["allotment_date"] == "2026-09-02"


def test_build_alert_record_copies_scraped_at_and_sub_ig() -> None:
    rec = build_alert_record(
        {
            **MASTER,
            "scraped_at_utc": "2026-08-31T10:11:00Z",
            "scraped_at_ist": "31-Aug 15:41 IST",
            "gmp_close_date": "2026-08-31",
            "gmp_date_raw": "31-08-2026 Close",
            "sub_ig_x": 97.92,
        },
        None,
        {"ipo_id": "2013", "close_date": "2026-08-31", "company_name": "Lumino", "exchange_type": "mainboard"},
    )
    assert rec["scraped_at_utc"] == "2026-08-31T10:11:00Z"
    assert rec["scraped_at_ist"] == "31-Aug 15:41 IST"
    assert rec["gmp_as_of"] == "2026-08-31"
    assert rec["gmp_date_raw"] == "31-08-2026 Close"
    assert rec["sub_ig_x"] == 97.92


def test_scrape_timestamps_are_locale_stable() -> None:
    from datetime import datetime, timezone

    stamps = scrape_timestamps(datetime(2026, 8, 31, 10, 11, tzinfo=timezone.utc))
    assert stamps["scraped_at_utc"] == "2026-08-31T10:11:00Z"
    assert stamps["scraped_at_ist"] == "31-Aug 15:41 IST"


def test_records_needing_alert_only_alerts_when_row_missing() -> None:
    import pandas as pd

    prior = pd.DataFrame(
        [
            {
                "ipo_id": "2757",
                "close_date": "2026-08-31",
                "gmp_rs": "70.0",
                "sub_total_x": "150.09",
            }
        ]
    )
    same = {"ipo_id": "2757", "close_date": "2026-08-31", "gmp_rs": 70.0, "sub_total_x": 150.09}
    changed = {"ipo_id": "2757", "close_date": "2026-08-31", "gmp_rs": 85.0, "sub_total_x": 150.09}
    fresh = {"ipo_id": "2013", "close_date": "2026-08-31", "gmp_rs": 54.0, "sub_total_x": 5.14}
    errored = {"ipo_id": "2757", "close_date": "2026-08-31", "gmp_rs": 70.0, "sub_total_x": 150.09, "error": "scrape:timeout"}
    errored_fresh = {
        "ipo_id": "9999",
        "close_date": "2026-08-31",
        "gmp_rs": 1.0,
        "sub_total_x": 1.0,
        "error": "scrape:timeout",
    }
    assert records_needing_alert([same], prior) == []
    assert records_needing_alert([changed], prior) == []
    assert records_needing_alert([fresh], prior) == [fresh]
    assert records_needing_alert([errored], prior) == []
    assert records_needing_alert([errored_fresh], prior) == [errored_fresh]
    assert records_needing_alert([same], None) == [same]


def test_records_needing_alert_normalizes_ipo_id_and_close_date() -> None:
    import pandas as pd
    from datetime import date, datetime

    prior = pd.DataFrame([{"ipo_id": "2013.0", "close_date": "2026-08-31T00:00:00"}])
    # Same IPO: float-ish id / date object / ISO datetime must not re-alert.
    assert records_needing_alert(
        [{"ipo_id": 2013, "close_date": "2026-08-31"}], prior
    ) == []
    assert records_needing_alert(
        [{"ipo_id": "2013", "close_date": date(2026, 8, 31)}], prior
    ) == []
    assert records_needing_alert(
        [{"ipo_id": "2013", "close_date": datetime(2026, 8, 31, 10, 0)}], prior
    ) == []
    # A different IPO still alerts.
    fresh = {"ipo_id": "9999", "close_date": "2026-08-31"}
    assert records_needing_alert([fresh], prior) == [fresh]


def test_upsert_audit_collapses_canonical_duplicate_keys(tmp_path: Path) -> None:
    path = tmp_path / "live_audit_log.csv"
    first = [
        build_alert_record(
            MASTER, None, {"ipo_id": "2013.0", "close_date": "2026-08-31T00:00:00", "company_name": "Lumino"}
        )
    ]
    upsert_audit(path, first)
    second = [
        build_alert_record(
            {**MASTER, "gmp_rs": 99},
            None,
            {"ipo_id": 2013, "close_date": "2026-08-31", "company_name": "Lumino"},
        )
    ]
    out = upsert_audit(path, second)
    assert len(out) == 1
    assert str(out.iloc[0]["ipo_id"]) == "2013"
    assert str(out.iloc[0]["close_date"]) == "2026-08-31"
    assert float(out.iloc[0]["gmp_rs"]) == 99

    # Two incoming rows that only look different before canonicalization.
    twins = [
        build_alert_record(MASTER, None, {"ipo_id": "8888", "close_date": "2026-09-01", "company_name": "A"}),
        build_alert_record(
            {**MASTER, "gmp_rs": 1},
            None,
            {"ipo_id": "8888.0", "close_date": "2026-09-01T00:00:00", "company_name": "A"},
        ),
    ]
    out2 = upsert_audit(path, twins)
    ids = list(out2["ipo_id"].astype(str))
    assert ids.count("8888") == 1
    assert ids.count("2013") == 1


def test_run_scan_skips_dispatch_when_row_already_exists(monkeypatch, tmp_path: Path) -> None:
    discovery = {
        "ipo_id": "2013",
        "company_name": "Lumino",
        "exchange_type": "mainboard",
        "close_date": "2026-08-31",
        "status": "open",
        "url": "https://example.test/ipo/2013/",
    }
    rec = build_alert_record(MASTER, None, discovery)
    rec["gmp_rs"] = 70.0
    rec["sub_total_x"] = 150.09
    rec["apply_s1"] = True
    upsert_audit(tmp_path / "live_audit_log.csv", [rec])

    monkeypatch.setattr(live_scanner, "_score_one", lambda *a, **k: dict(rec))
    monkeypatch.setattr(live_scanner, "fetch_market_regime", lambda **k: "NEUTRAL")
    dispatched: list[list] = []
    monkeypatch.setattr(live_scanner, "dispatch", lambda records, dry_run=False: dispatched.append(list(records)))

    run_scan(
        rows=[discovery],
        as_of=date(2026, 8, 31),
        out_dir=tmp_path,
        fetch_gmp=False,
        dry_run=False,
    )
    assert dispatched == [[]]

    rec2 = dict(rec)
    rec2["gmp_rs"] = 85.0
    monkeypatch.setattr(live_scanner, "_score_one", lambda *a, **k: dict(rec2))
    dispatched.clear()
    run_scan(
        rows=[discovery],
        as_of=date(2026, 8, 31),
        out_dir=tmp_path,
        fetch_gmp=False,
        dry_run=False,
    )
    assert dispatched == [[]]

    discovery3 = {**discovery, "ipo_id": "9999"}
    rec3 = dict(rec)
    rec3["ipo_id"] = "9999"
    rec3["gmp_rs"] = 10.0
    monkeypatch.setattr(live_scanner, "_score_one", lambda *a, **k: dict(rec3))
    dispatched.clear()
    run_scan(
        rows=[discovery3],
        as_of=date(2026, 8, 31),
        out_dir=tmp_path,
        fetch_gmp=False,
        dry_run=False,
    )
    assert len(dispatched) == 1
    assert len(dispatched[0]) == 1
    assert dispatched[0][0]["ipo_id"] == "9999"


def test_daily_alert_has_no_github_schedule() -> None:
    text = Path(".github/workflows/daily_ipo_alert.yml").read_text(encoding="utf-8")
    assert "schedule:" not in text
    assert 'cron: "45 9 * * 1-5"' not in text
    assert 'cron: "0 10 * * 1-5"' not in text
    assert 'cron: "30 10 * * 1-5"' not in text
    assert "repository_dispatch:" in text
    assert "types: [trigger-daily-ipo-alert]" in text
    assert "workflow_dispatch:" in text
    assert "dry_run:" in text


def test_daily_alert_manual_dispatch_defaults_to_dry_run() -> None:
    # A stray manual test run must never write the audit log / send alerts
    # by default: that would silently burn the day's one-alert-per-IPO slot
    # (presence-only gate) hours before the real scheduled ticks run.
    text = Path(".github/workflows/daily_ipo_alert.yml").read_text(encoding="utf-8")
    assert "workflow_dispatch:" in text
    assert "dry_run:" in text
    assert "default: true" in text
    assert "--dry-run" in text
    assert "github.event_name == 'workflow_dispatch' && inputs.dry_run" in text


def test_daily_alert_repository_dispatch_is_live_not_dry_run() -> None:
    # cron-job.org POSTs event_type trigger-daily-ipo-alert. That must
    # start a real send; --dry-run is only for workflow_dispatch.
    text = Path(".github/workflows/daily_ipo_alert.yml").read_text(encoding="utf-8")
    assert "repository_dispatch:" in text
    assert "types: [trigger-daily-ipo-alert]" in text
    dry_run_line = next(
        line for line in text.splitlines() if "--dry-run" in line and "github.event_name" in line
    )
    assert "workflow_dispatch" in dry_run_line
    assert "repository_dispatch" not in dry_run_line


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
    monkeypatch.setattr(live_scanner, "send_failure_alert", lambda msg, **kw: calls.append(msg))
    out = run_scan(rows=[], out_dir=tmp_path, dry_run=False)
    assert out == []
    assert len(calls) == 1
    assert "0 rows" in calls[0]


def test_run_scan_no_failure_alert_when_simply_no_closers_today(monkeypatch, tmp_path: Path) -> None:
    calls = []
    monkeypatch.setattr(live_scanner, "send_failure_alert", lambda msg, **kw: calls.append(msg))
    rows = [{"ipo_id": "1", "status": "upcoming", "close_date": "2026-09-05"}]
    out = run_scan(rows=rows, as_of=date(2026, 8, 31), out_dir=tmp_path, dry_run=False)
    assert out == []
    assert calls == []


def test_run_scan_propagates_discovery_exception_instead_of_swallowing(
    monkeypatch, tmp_path: Path
) -> None:
    def boom(*args, **kwargs):
        raise RuntimeError("dashboard exploded")

    monkeypatch.setattr(live_scanner, "scrape_all_open_ipos", boom)
    with pytest.raises(RuntimeError, match="dashboard exploded"):
        run_scan(out_dir=tmp_path, fetch_gmp=False)


def test_run_scan_does_not_mark_audit_row_seen_when_dispatch_fails(
    monkeypatch, tmp_path: Path
) -> None:
    # Regression: dispatch() now raises NotificationDeliveryError when
    # Telegram+email both genuinely fail for a real batch. If the audit row
    # got written anyway, the presence-only gate would treat this IPO as
    # "already alerted" forever that day even though nobody ever received
    # it. The row must stay absent so a fixed-credentials retry can still
    # send the real alert.
    discovery = {
        "ipo_id": "2013",
        "company_name": "Lumino",
        "exchange_type": "mainboard",
        "close_date": "2026-08-31",
        "status": "open",
        "url": "https://example.test/ipo/2013/",
    }
    rec = build_alert_record(MASTER, None, discovery)
    monkeypatch.setattr(live_scanner, "_score_one", lambda *a, **k: dict(rec))
    monkeypatch.setattr(live_scanner, "fetch_market_regime", lambda **k: "NEUTRAL")

    def boom(records, dry_run=False):
        raise RuntimeError("both channels down")

    monkeypatch.setattr(live_scanner, "dispatch", boom)
    audit_path = tmp_path / "live_audit_log.csv"

    with pytest.raises(RuntimeError, match="both channels down"):
        run_scan(
            rows=[discovery],
            as_of=date(2026, 8, 31),
            out_dir=tmp_path,
            fetch_gmp=False,
            dry_run=False,
        )
    assert not audit_path.exists()
