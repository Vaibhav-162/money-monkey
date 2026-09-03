"""Write JSON/CSV artifacts for the analysis run.

WHAT THIS FILE DOES
--------------------
Tiny I/O helper for the local trainer. `run_analysis.py` is the only
caller: `dump_json` writes every `data/analysis/*.json` summary (EDA,
baselines, walk-forward, ablation, stop-loss, quality-checklist eval,
run `summary.json`); `write_predictions` writes `predictions.csv` with
the identity/GMP/EV/quality columns plus any `pred_*` or `exret_*`
fields the pipeline added. Nothing here models, scores, or scrapes.

KEY TERMS USED HERE
--------------------
- GMP / `gmp_at_close` / `gmp_anchor`: unofficial pre-listing premium,
  and whether that quote is dated at IPO close or leakily at listing.
- Subscription multiple (`sub_total_x`): times-oversubscribed.
- Allotment probability (`p_allot`) / realized EV: chance of getting
  shares, and historical rupee profit per lot.
- Clean pop / listing-day gain / open-return %: S1 labels copied onto
  the predictions CSV so a fold can be inspected without re-joining.
- Quality score / quality pass: the 0–4 checklist and its pass flag
  (Strategy 2 live decision), not the experimental S2 regressor.
- `pred_*` / `exret_*`: extra columns `run_analysis.py` may have attached
  (e.g. `pred_s1_p_pop`, 6-month excess return) — included if present.

FUNCTIONS / CLASSES IN THIS FILE
---------------------------------
- `dump_json(path, payload)`: pretty-print JSON, creating parent dirs.
  `default=str` so datetimes/numpy scalars do not crash the dump.
- `write_predictions(df, path)`: subset to the known report columns
  (plus pred/exret extras) and write CSV without the index.
"""

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
