"""Strategy 1 and 2 labels. Listing prices are labels only.

WHAT THIS FILE DOES
--------------------
Builds the *outcomes* the models learn and the backtest scores against —
never inputs. Listing open/low and first-day gain are used here as labels
only; they must not leak into `analysis/features.py`.

`run_analysis.py` calls `add_targets()` after `add_features()`. Live scoring
does *not* call `add_targets`: `analysis/score.py` imports `quality_ranker`,
`quality_checklist_for_row`, and `QUALITY_PASS_THRESHOLD` and runs those on
the featured row (listing prices are unknown at close day anyway).
`analysis/baselines.py` imports only the threshold, to evaluate the same
pass/fail cut against realized 6-month excess returns.

KEY TERMS USED HERE
--------------------
- Target variable: the thing we are trying to predict or evaluate. Strategy 1
  uses a binary "clean pop" and a continuous open-day return; Strategy 2
  uses a 6-month Nifty-excess hit (`s2_beat`) plus a 0–4 quality checklist.
- Listing pop / `open_return_pct`: first-trade price vs issue price, in
  percent. This is the continuous Strategy 1 label and the rupee-gain input
  to expected value.
- `is_clean_pop`: 1 only if listing-day gain is at least 15% *and* the day's
  low never traded at or below issue (or low is missing). A gap-and-crash
  print is not a "clean" pop.
- Issue price: the allotment price. Denominator for open return; also the
  floor that `listing_low` is compared against.
- Allotment probability (`p_allot`): chance a retail applicant gets shares,
  already computed in `add_features`. Multiplies expected rupee gain into
  `realized_ev` — a huge pop is worthless if you are not allotted.
- EV / Expected Value (`expected_gain_amt`, `realized_ev`): predicted
  average profit per lot = (open return) × lot rupees × liquidity haircut,
  then × `p_allot`. This is why friend rules that ignore allotment overstate
  what a retail applicant actually makes.
- Lot size / `retail_min_amount`: shares per lot and the rupee ticket for
  one retail application. Percent gains are converted to rupees here
  because a 15% pop on a ₹15,000 lot is not the same as on a ₹1,50,000 lot.
- EV haircut (`ev_haircut`): 0.7 on small, high-GMP SME names (set in
  features); otherwise 1.0. Discounts expected gain for illiquid prints.
- `exret_126` / `s2_beat`: 126-session (~6 month) return minus Nifty over
  the same window, and a 1/0 "beat by more than 5%" label. Comes from the
  price join, not from listing OHLC.
- Quality checklist (`quality_score` / `quality_pass`): 0–4 sanity score
  (subscription, OFS, raw ROE, debt-equity). Pass is ≥ 3. This is what live
  Strategy 2 `apply_s2` uses — not the experimental S2 regressor.
- OFS ratio: fraction of the issue that is existing holders selling. Missing
  OFS still *awards* a quality point (treated as "not a cash-out red flag").
- ROE: return on equity, raw, never imputed in the ranker. Missing ROE does
  *not* award a point.
- Debt-equity: leverage. Missing D/E still awards a point (same "not
  disclosed ≠ fail" idea as OFS).
- Subscription multiple (`sub_total_x`): times-oversubscribed. The quality
  check is a hard `> 20` cut.

FUNCTIONS / CLASSES IN THIS FILE
---------------------------------
- `add_targets(df)`: writes open return, clean-pop, EV fields, `s2_beat`,
  and the quality score/pass columns used by training and `run_analysis.py`.
- `listing_open(df)` / `listing_low(df)`: NSE print if present, else BSE.
  Label construction only.
- `quality_ranker(df)`: 0–4 fallback Strategy 2 score from raw fundamentals.
  Makes no return forecast — it is a cleanliness checklist.
- `quality_checklist_for_row(row)`: same four checks, but per-item
  pass / fail / not_disclosed so a missing OFS is not shown as a true pass.
- `_num_col` / `_row_num`: NaN-safe getters. `df.get` + `pd.to_numeric` on a
  missing column returns a scalar NaN and breaks chained `.fillna()`.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

QUALITY_PASS_THRESHOLD = 3


def _num_col(df: pd.DataFrame, name: str) -> pd.Series:
    """pd.to_numeric(df.get(name)) returns a bare NaN scalar (not a Series) when
    the column is absent, which breaks any chained .fillna()/.where() call. Always
    go through this instead of df.get(...) + pd.to_numeric directly."""
    if name in df.columns:
        return pd.to_numeric(df[name], errors="coerce")
    return pd.Series(np.nan, index=df.index, dtype=float)


def listing_open(df: pd.DataFrame) -> pd.Series:
    nse = _num_col(df, "listing_nse_open")
    bse = _num_col(df, "listing_bse_open")
    return nse.where(nse.notna(), bse)


def listing_low(df: pd.DataFrame) -> pd.Series:
    nse = _num_col(df, "listing_nse_low")
    bse = _num_col(df, "listing_bse_low")
    return nse.where(nse.notna(), bse)


def add_targets(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    ip = _num_col(out, "issue_price")
    opn = listing_open(out)
    low = listing_low(out)
    gain = _num_col(out, "listing_day_gain_pct")
    open_ret = (opn / ip - 1.0) * 100.0
    out["open_return_pct"] = open_ret
    out["listing_open"] = opn
    out["listing_low"] = low
    clean_low = low.isna() | (low > ip)
    out["is_clean_pop"] = ((gain >= 15) & clean_low).astype(int)
    out.loc[gain.isna() | ip.isna(), "is_clean_pop"] = np.nan
    p = _num_col(out, "p_allot").fillna(0)
    haircut = _num_col(out, "ev_haircut").fillna(1)
    lot_amt = _num_col(out, "retail_min_amount")
    lot_fallback = _num_col(out, "lot_size") * ip
    lot_amt = lot_amt.where(lot_amt.notna() & (lot_amt > 0), lot_fallback)
    out["expected_gain_amt"] = (open_ret / 100.0) * lot_amt * haircut
    out["realized_ev"] = p * out["expected_gain_amt"]
    if "exret_126" in out.columns:
        ex = _num_col(out, "exret_126")
        out["s2_beat"] = (ex > 0.05).astype(float)
        out.loc[ex.isna(), "s2_beat"] = np.nan
    else:
        out["s2_beat"] = np.nan
    out["quality_score"] = quality_ranker(out)
    out["quality_pass"] = (out["quality_score"] >= QUALITY_PASS_THRESHOLD).astype(int)
    return out


def quality_ranker(df: pd.DataFrame) -> pd.Series:
    """Fallback Strategy 2 score. Uses raw ROE, never imputed. No return claim."""
    sub = _num_col(df, "sub_total_x")
    ofs = _num_col(df, "ofs_ratio")
    roe = _num_col(df, "roe")
    de = _num_col(df, "debt_equity")
    score = pd.Series(0.0, index=df.index)
    score += (sub > 20).astype(float)
    score += (ofs.isna() | (ofs < 0.5)).astype(float)
    score += (roe > 15).astype(float)
    score += (de.isna() | (de <= 0.5)).astype(float)
    return score


def _row_num(row: pd.Series, name: str) -> float:
    if name not in row.index:
        return float("nan")
    return float(pd.to_numeric(row[name], errors="coerce"))


def quality_checklist_for_row(row: pd.Series) -> list[dict[str, Any]]:
    """Per-check breakdown of quality_ranker(). Aggregate math is unchanged.

    status is pass / fail / not_disclosed. awarded matches quality_ranker()
    point-counting, including the cases where a missing OFS or D/E still
    awards a point.
    """
    checks: list[dict[str, Any]] = []

    sub = _row_num(row, "sub_total_x")
    if pd.isna(sub):
        checks.append({"name": "subscription", "value": None, "status": "not_disclosed", "awarded": False})
    elif sub > 20:
        checks.append({"name": "subscription", "value": sub, "status": "pass", "awarded": True})
    else:
        checks.append({"name": "subscription", "value": sub, "status": "fail", "awarded": False})

    ofs = _row_num(row, "ofs_ratio")
    if pd.isna(ofs):
        checks.append({"name": "ofs_ratio", "value": None, "status": "not_disclosed", "awarded": True})
    elif ofs < 0.5:
        checks.append({"name": "ofs_ratio", "value": ofs, "status": "pass", "awarded": True})
    else:
        checks.append({"name": "ofs_ratio", "value": ofs, "status": "fail", "awarded": False})

    roe = _row_num(row, "roe")
    if pd.isna(roe):
        checks.append({"name": "roe", "value": None, "status": "not_disclosed", "awarded": False})
    elif roe > 15:
        checks.append({"name": "roe", "value": roe, "status": "pass", "awarded": True})
    else:
        checks.append({"name": "roe", "value": roe, "status": "fail", "awarded": False})

    de = _row_num(row, "debt_equity")
    if pd.isna(de):
        checks.append({"name": "debt_equity", "value": None, "status": "not_disclosed", "awarded": True})
    elif de <= 0.5:
        checks.append({"name": "debt_equity", "value": de, "status": "pass", "awarded": True})
    else:
        checks.append({"name": "debt_equity", "value": de, "status": "fail", "awarded": False})

    return checks
