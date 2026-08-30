"""Strategy 1 and 2 labels. Listing prices are labels only."""

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
