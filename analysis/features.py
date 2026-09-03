"""Close-day-only features. Listing OHLC and tracker P/L are never features.

WHAT THIS FILE DOES
--------------------
Turns a sanitized IPO row into the numbers the models actually train and
score on — and only from fields that exist at 3:30pm on close day. Listing
open/high/low/close and the tracker's current P/L are labels or display
fields, never inputs here.

`run_analysis.py` calls `add_features()` after load + Nifty join;
`analysis/score.py` (`Scorer.score_row`) calls it again on a one-row frame
so live alerts use the same math. `analysis/models.py` and
`analysis/tuning.py` do not re-engineer features; they pull columns through
`feature_matrix()` using `S1_FEATURE_COLS` / `S2_FEATURE_COLS`. This file
calls `analysis.impute.impute_ratios`, then puts the *raw* ratio values back
on the main column names so trees still see NaN.

KEY TERMS USED HERE
--------------------
- Feature engineering / feature matrix: derived columns (`ofs_ratio`,
  `p_allot`, dual GMP %, …) and the later rectangular `X` that
  `feature_matrix()` builds for sklearn / LightGBM.
- Close-day-only: every input must have been knowable when the IPO closed
  for subscription. Using listing OHLC as a feature would be circular
  leakage — you cannot know the first-day print before it happens.
- GMP (Grey Market Premium): unofficial pre-listing premium in rupees.
  Converted to `%` of issue price and of the band cap. Never imputed;
  `gmp_missing` tells the model "no street quote" instead.
- Subscription multiple (`sub_total_x`): times-oversubscribed. Also the
  fallback denominator for allotment probability (`1 / sub_total_x`).
- Allotment probability (`p_allot`): chance a retail applicant gets any
  shares. Best estimate is retail lots ÷ applications; else 1/total-sub;
  undersubscribed issues get 1.0.
- OFS ratio: Offer-For-Sale crores ÷ issue size. High OFS means existing
  owners are cashing out rather than the company raising fresh capital.
- Issue size (crore) / `size_lt_600`: rupees raised (1 crore = 10 million)
  and a small/mid-issue flag. Smaller books are easier for demand to swamp.
- Lot size: shares per application lot. Used with retail quota to estimate
  how many retail lots exist.
- ROE / ROCE / debt-equity / PE / PBV: fundamental ratios. Main columns
  stay raw (NaN allowed); `_filled` copies exist for logistic only.
- PAT CAGR / `fy1_pat_margin`: two-year profit growth and latest-year
  profit margin, from the three fiscal years on the master sheet.
- Promoter holding pre/post: founder stake before vs after the IPO. A sharp
  drop can mean insiders selling.
- Peer-relative PE: this IPO's P/E minus the same-industry, same-year
  median, as a fraction of that median — overpricing vs peers, not vs a
  universal P/E rule.
- Nifty 20-day (`nifty_20d`): India's benchmark index return over ~20
  sessions before close day. Passed through if already joined; otherwise
  left NaN (this file does not fetch prices).
- Lock-in days: calendar days from listing to the 30- and 90-day anchor
  unlock dates. Strategy 2 only (`S2_FEATURE_COLS`).
- SME / liquidity risk: small SME issues with a very high GMP get
  `liquidity_risk=1` and `ev_haircut=0.7` so expected-value math later
  discounts erratic, illiquid listing prints.
- Price band high / issue price: cap of the application range and the
  final allotment price; used as GMP % denominators.

FUNCTIONS / CLASSES IN THIS FILE
---------------------------------
- `add_features(df)`: the public builder. Imputes ratios, derives GMP %,
  OFS, margins, CAGR, allotment odds, calendar/peer/size flags, lock-in
  gaps, and the SME haircut. Restores raw ratios onto the main names.
- `feature_matrix(df, cols, filled)`: slices those columns into an `X`
  frame. `filled=True` prefers `{col}_filled` (logistic); `False` uses
  raw/NaN (LightGBM).
- `allotment_probability(df)`: returns `(p_allot, tier)` with the three
  estimation paths above, so EV can multiply pop by "will I even get shares?"
- `_cagr(later, earlier, years)` / `_num_col(df, name)`: growth math and
  a NaN-safe numeric getter (missing column → all-NaN Series, not a scalar).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from analysis.impute import impute_ratios

S1_FEATURE_COLS = [
    "sub_total_x",
    "issue_size_cr",
    "ofs_ratio",
    "gmp_pct_vs_issue",
    "gmp_pct_vs_cap",
    "gmp_missing",
    "pe_pre",
    "pbv",
    "roe",
    "roce",
    "debt_equity",
    "pat_cagr",
    "fy1_pat_margin",
    "promoter_pre_pct",
    "promoter_post_pct",
    "p_allot",
    "nifty_20d",
    "listing_month",
    "peer_rel_pe",
    "size_lt_600",
    "roe_missing",
    "pe_pre_missing",
    "gmp_obs_count",
]

S2_FEATURE_COLS = S1_FEATURE_COLS + ["days_to_lockin_30", "days_to_lockin_90"]


def _cagr(later: pd.Series, earlier: pd.Series, years: float = 2.0) -> pd.Series:
    later_n = pd.to_numeric(later, errors="coerce")
    earlier_n = pd.to_numeric(earlier, errors="coerce")
    ok = later_n.notna() & earlier_n.notna() & (earlier_n > 0) & (later_n > 0)
    out = pd.Series(np.nan, index=later.index, dtype=float)
    out.loc[ok] = (later_n.loc[ok] / earlier_n.loc[ok]) ** (1.0 / years) - 1.0
    return out


def _num_col(df: pd.DataFrame, name: str) -> pd.Series:
    if name in df.columns:
        return pd.to_numeric(df[name], errors="coerce")
    return pd.Series(np.nan, index=df.index, dtype=float)


def allotment_probability(df: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    retail_pct = _num_col(df, "retail_pct") / 100.0
    shares = _num_col(df, "issue_size_shares")
    lot = _num_col(df, "lot_size").replace(0, np.nan)
    apps = _num_col(df, "total_applications")
    sub = _num_col(df, "sub_total_x")
    retail_lots = retail_pct * shares / lot
    p = pd.Series(np.nan, index=df.index, dtype=float)
    tier = pd.Series("none", index=df.index, dtype=object)
    use_apps = retail_lots.notna() & apps.notna() & (apps > 0)
    p.loc[use_apps] = np.minimum(1.0, retail_lots.loc[use_apps] / apps.loc[use_apps])
    tier.loc[use_apps] = "retail_lots_over_applications"
    use_sub = p.isna() & sub.notna() & (sub > 0)
    p.loc[use_sub] = np.minimum(1.0, 1.0 / sub.loc[use_sub])
    tier.loc[use_sub] = "one_over_total_x"
    full = sub.notna() & (sub <= 1)
    p.loc[full] = 1.0
    tier.loc[full] = "undersubscribed_full_allot"
    return p.clip(0, 1), tier


def add_features(df: pd.DataFrame) -> pd.DataFrame:
    out = impute_ratios(df)
    ip = _num_col(out, "issue_price")
    cap = _num_col(out, "price_band_high")
    gmp = _num_col(out, "gmp_at_close")
    size = _num_col(out, "issue_size_cr")
    ofs = _num_col(out, "ofs_cr")
    out["ofs_ratio"] = ofs / size.replace(0, np.nan)
    out["gmp_pct_vs_issue"] = np.where(ip > 0, gmp / ip * 100.0, np.nan)
    out["gmp_pct_vs_cap"] = np.where(cap > 0, gmp / cap * 100.0, np.nan)
    out["gmp_missing"] = gmp.isna().astype(int)
    income = _num_col(out, "fy1_total_income")
    pat = _num_col(out, "fy1_pat")
    out["fy1_pat_margin"] = pat / income.replace(0, np.nan)
    out["pat_cagr"] = _cagr(_num_col(out, "fy1_pat"), _num_col(out, "fy3_pat"))
    p_allot, tier = allotment_probability(out)
    out["p_allot"] = p_allot
    out["p_allot_tier"] = tier
    out["size_lt_600"] = (size < 600).astype(float)
    if "listing_date" in out.columns:
        out["listing_month"] = pd.to_datetime(out["listing_date"], errors="coerce").dt.month
    else:
        out["listing_month"] = np.nan
    pe = _num_col(out, "pe_pre")
    industry = out["industry"] if "industry" in out.columns else pd.Series("_unk", index=out.index)
    year = out["listing_year"] if "listing_year" in out.columns else pd.Series(0, index=out.index)
    peer = pe.groupby([industry.fillna("_unk"), year]).transform("median")
    out["peer_rel_pe"] = (pe - peer) / peer.replace(0, np.nan)
    if "nifty_20d" not in out.columns:
        out["nifty_20d"] = np.nan
    listing_dt = pd.to_datetime(out.get("listing_date"), errors="coerce") if "listing_date" in out.columns else pd.Series(pd.NaT, index=out.index)
    for src_col, out_col in (("anchor_lockin_30d", "days_to_lockin_30"), ("anchor_lockin_90d", "days_to_lockin_90")):
        if src_col in out.columns:
            lockin_dt = pd.to_datetime(out[src_col], errors="coerce")
            out[out_col] = (lockin_dt - listing_dt).dt.days
        else:
            out[out_col] = np.nan
    for col in ("roe", "roce", "debt_equity", "pe_pre", "pbv"):
        raw_name = f"{col}_raw"
        raw = _num_col(out, raw_name) if raw_name in out.columns else _num_col(out, col)
        out[col] = raw
        if f"{col}_missing" not in out.columns:
            out[f"{col}_missing"] = out[col].isna().astype(int)
    gmp_pct = pd.to_numeric(out["gmp_pct_vs_issue"], errors="coerce")
    is_sme = out["exchange_type"].eq("sme") if "exchange_type" in out.columns else False
    out["liquidity_risk"] = (is_sme & (size.fillna(10_000) < 150) & (gmp_pct.fillna(0) >= 50)).astype(int)
    out["ev_haircut"] = np.where(out["liquidity_risk"] == 1, 0.7, 1.0)
    return out


def feature_matrix(df: pd.DataFrame, cols: list[str], filled: bool = False) -> pd.DataFrame:
    frame = pd.DataFrame(index=df.index)
    for col in cols:
        if filled and f"{col}_filled" in df.columns:
            frame[col] = pd.to_numeric(df[f"{col}_filled"], errors="coerce")
        elif col in df.columns:
            frame[col] = pd.to_numeric(df[col], errors="coerce")
        else:
            frame[col] = np.nan
    return frame
