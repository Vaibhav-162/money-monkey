from analysis.baselines import evaluate_quality_checklist
from analysis.features import add_features, allotment_probability
from analysis.load import exclusion_reason
from analysis.targets import QUALITY_PASS_THRESHOLD, add_targets, quality_checklist_for_row, quality_ranker
import numpy as np
import pandas as pd


def test_exclusion_fpo_reit():
    assert exclusion_reason("Yes Bank FPO Details") == "fpo_reit_invit_or_malformed_name"
    assert exclusion_reason("Embassy Office Parks REIT Details")
    assert exclusion_reason("IndiGrid Infrastructure Trust InvIT Details")
    assert exclusion_reason("Sunshine Pictures Ltd. IPO information")
    assert exclusion_reason("Zomato") is None
    assert exclusion_reason("Lohia Corp") is None


def test_allotment_undersub_is_one():
    df = pd.DataFrame({
        "retail_pct": [35.0],
        "issue_size_shares": [1_000_000],
        "lot_size": [100],
        "total_applications": [pd.NA],
        "sub_total_x": [0.8],
    })
    p, tier = allotment_probability(df)
    assert float(p.iloc[0]) == 1.0
    assert tier.iloc[0] == "undersubscribed_full_allot"


def test_allotment_one_over_total():
    df = pd.DataFrame({
        "retail_pct": [pd.NA],
        "issue_size_shares": [pd.NA],
        "lot_size": [pd.NA],
        "total_applications": [pd.NA],
        "sub_total_x": [50.0],
    })
    p, tier = allotment_probability(df)
    assert abs(float(p.iloc[0]) - 0.02) < 1e-9
    assert tier.iloc[0] == "one_over_total_x"


def _minimal_frame(**overrides):
    base = {
        "listing_date": ["2024-01-10"],
        "anchor_lockin_30d": ["2024-02-09"],
        "anchor_lockin_90d": ["2024-04-09"],
        "exchange_type": ["mainboard"],
        "issue_price": [100.0],
        "price_band_high": [100.0],
        "gmp_at_close": [10.0],
        "issue_size_cr": [500.0],
        "ofs_cr": [100.0],
        "fy1_total_income": [1000.0],
        "fy1_pat": [100.0],
        "fy3_pat": [50.0],
        "industry": ["Tech"],
        "listing_year": [2024],
    }
    base.update(overrides)
    return pd.DataFrame(base)


def test_days_to_lockin_uses_real_dates_not_constants():
    out = add_features(_minimal_frame())
    assert int(out["days_to_lockin_30"].iloc[0]) == 30
    assert int(out["days_to_lockin_90"].iloc[0]) == 90


def test_days_to_lockin_missing_when_no_anchor_dates():
    out = add_features(_minimal_frame(anchor_lockin_30d=[pd.NA], anchor_lockin_90d=[pd.NA]))
    assert pd.isna(out["days_to_lockin_30"].iloc[0])
    assert pd.isna(out["days_to_lockin_90"].iloc[0])


def test_is_clean_pop_requires_gain_and_clean_low():
    df = pd.DataFrame({
        "issue_price": [100.0, 100.0, 100.0],
        "listing_nse_open": [120.0, 120.0, 116.0],
        "listing_nse_low": [110.0, 95.0, np.nan],
        "listing_bse_open": [np.nan, np.nan, np.nan],
        "listing_bse_low": [np.nan, np.nan, np.nan],
        "listing_day_gain_pct": [20.0, 20.0, 16.0],
    })
    out = add_targets(df)
    # gain>=15 and low held above issue price -> clean pop
    assert out["is_clean_pop"].iloc[0] == 1
    # gain>=15 but low dipped below issue price intraday -> not clean
    assert out["is_clean_pop"].iloc[1] == 0
    # low missing entirely -> low filter is skipped, gain alone decides
    assert out["is_clean_pop"].iloc[2] == 1


def test_quality_checklist_all_four_pass():
    row = pd.Series({
        "sub_total_x": 40.0,
        "ofs_ratio": 0.2,
        "roe": 18.0,
        "debt_equity": 0.3,
    })
    checks = {c["name"]: c for c in quality_checklist_for_row(row)}
    assert all(c["status"] == "pass" and c["awarded"] is True for c in checks.values())
    assert int(quality_ranker(pd.DataFrame([row])).iloc[0]) == 4


def test_quality_checklist_missing_sub_is_not_disclosed_fail():
    row = pd.Series({
        "sub_total_x": np.nan,
        "ofs_ratio": 0.2,
        "roe": 18.0,
        "debt_equity": 0.3,
    })
    checks = {c["name"]: c for c in quality_checklist_for_row(row)}
    assert checks["subscription"]["status"] == "not_disclosed"
    assert checks["subscription"]["awarded"] is False
    assert int(quality_ranker(pd.DataFrame([row])).iloc[0]) == 3


def test_quality_checklist_missing_debt_is_not_disclosed_pass():
    row = pd.Series({
        "sub_total_x": 40.0,
        "ofs_ratio": 0.2,
        "roe": 18.0,
        "debt_equity": np.nan,
    })
    checks = {c["name"]: c for c in quality_checklist_for_row(row)}
    assert checks["debt_equity"]["status"] == "not_disclosed"
    assert checks["debt_equity"]["awarded"] is True
    assert int(quality_ranker(pd.DataFrame([row])).iloc[0]) == 4


def test_evaluate_quality_checklist_buckets():
    df = pd.DataFrame({
        "exchange_type": ["mainboard"] * 6,
        "quality_score": [0, 1, 3, 3, 4, 2],
        "exret_126": [0.10, 0.00, 0.20, -0.10, 0.30, np.nan],
        "s2_beat": [1.0, 0.0, 1.0, 0.0, 1.0, np.nan],
    })
    out = evaluate_quality_checklist(df, "mainboard")
    assert out["n_with_prices"] == 5
    assert out["threshold"] == QUALITY_PASS_THRESHOLD
    by_score = {b["quality_score"]: b for b in out["buckets"]}
    assert by_score[0]["n"] == 1
    assert by_score[0]["hit_ratio"] == 1.0
    assert by_score[0]["mean_exret"] == 0.10
    assert by_score[3]["n"] == 2
    assert by_score[3]["hit_ratio"] == 0.5
    assert abs(by_score[3]["mean_exret"] - 0.05) < 1e-9
    assert by_score[4]["n"] == 1
    assert out["rollup"]["quality_pass"]["n"] == 3
    assert out["rollup"]["quality_fail"]["n"] == 2
    assert abs(out["rollup"]["quality_pass"]["hit_ratio"] - 2 / 3) < 1e-9
