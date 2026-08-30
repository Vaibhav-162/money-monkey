"""Write JSON/CSV artifacts for the analysis run."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd


def dump_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def write_predictions(df: pd.DataFrame, path: Path) -> None:
    cols = [
        "ipo_id", "company_name", "exchange_type", "listing_year", "listing_date",
        "issue_price", "issue_size_cr", "sub_total_x", "gmp_at_close", "gmp_pct_vs_issue",
        "gmp_anchor", "p_allot", "is_clean_pop", "listing_day_gain_pct", "open_return_pct",
        "realized_ev", "quality_score", "quality_pass",
    ]
    have = [c for c in cols if c in df.columns]
    extra = [c for c in df.columns if c.startswith("pred_") or c.startswith("exret_")]
    df[have + extra].to_csv(path, index=False)
