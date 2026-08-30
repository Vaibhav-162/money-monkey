"""Strategy 2 live output is the checklist; Strategy 1 fields stay isolated."""

from __future__ import annotations

from pathlib import Path

from analysis.features import S2_FEATURE_COLS
from analysis.models import FittedBundle
from analysis.score import Scorer


S1_KEYS = (
    "p_pop",
    "exp_open_return_pct",
    "p_allot",
    "ev_retail",
    "ev_retail_source",
    "apply_s1",
    "apply_threshold_s1",
)

# Captured from Scorer.score_row with no models loaded — S1 must stay identical
# after an S2 regressor is injected.
S1_FIXTURE_NO_MODELS = {
    "p_pop": None,
    "exp_open_return_pct": None,
    "p_allot": 0.2,
    "ev_retail": None,
    "ev_retail_source": None,
    "apply_s1": False,
    "apply_threshold_s1": None,
}


def _low_quality_row() -> dict:
    return {
        "exchange_type": "mainboard",
        "issue_price": 100.0,
        "price_band_high": 100.0,
        "gmp_at_close": 5.0,
        "issue_size_cr": 500.0,
        "ofs_cr": 400.0,
        "sub_total_x": 5.0,
        "roe": 5.0,
        "debt_equity": 0.2,
        "lot_size": 100,
        "retail_min_amount": 15000.0,
    }


class _AlwaysHighReg:
    def predict(self, x):
        return [0.42] * len(x)


def test_s2_model_cannot_override_checklist_apply(tmp_path: Path):
    row = _low_quality_row()
    scorer = Scorer(model_dir=tmp_path)
    baseline = scorer.score_row(row)
    for key in S1_KEYS:
        assert baseline[key] == S1_FIXTURE_NO_MODELS[key]

    fake = FittedBundle(
        board="mainboard",
        strategy="s2",
        feature_cols=list(S2_FEATURE_COLS),
        lgbm=_AlwaysHighReg(),
    )
    scorer._cache["mainboard_s2"] = fake
    out = scorer.score_row(row)

    assert out["s2_score"] < 3
    assert out["quality_score"] == out["s2_score"]
    assert out["apply_s2"] is False
    assert out["s2_model_exret_pred"] == 0.42
    assert out["s2_model_status"] == "experimental_unvalidated"
    assert any(c["name"] == "subscription" and c["status"] == "fail" for c in out["quality_breakdown"])
    for key in S1_KEYS:
        assert out[key] == S1_FIXTURE_NO_MODELS[key]
        assert out[key] == baseline[key]
