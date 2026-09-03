"""Load the master sheet, sanitize, exclude non-operating IPOs, join GMP history and prices.

WHAT THIS FILE DOES
--------------------
This is the first step of the local analysis pipeline: turn the scraped master
CSV into a typed modeling frame. `run_analysis.py` calls `load_dataset()` and
then hands the result to `load_nifty_20d` / `add_features` / `add_targets`.
`analysis/prices.py` and `scripts/fetch_prices.py` reuse `sanitize()` so price
jobs see the same cleaned types. This file reads `chittorgarh.export.read_master`
and, for leak-free GMP, `chittorgarh.gmp.last_gmp_on_or_before`.

`attach_prices()` only merges `ret_*` / `exret_*` / `mdd_*` / `sharpe_*` from
`prices/returns.csv`. The Nifty 20-day feature is *not* joined here —
`run_analysis.py` calls `analysis.prices.load_nifty_20d` separately for that.

KEY TERMS USED HERE
--------------------
- IPO: a company's first sale of shares to the public. The master sheet also
  contains non-operating rows (follow-on issues, REIT/InvIT trusts, malformed
  "IPO information" titles) that this file excludes rather than scores.
- FPO / REIT / InvIT: follow-on public offer, real-estate trust, and
  infrastructure trust. They are not ordinary equity IPOs; `exclusion_reason`
  drops them into the excluded frame.
- GMP (Grey Market Premium): unofficial pre-listing premium, in rupees over
  issue price. `gmp_rs` on the master sheet is often a listing-day snapshot;
  `gmp_at_close` prefers the last history row dated on or before `ipo_close`.
- Data leakage: using a number that would not have been known at decision
  time. A listing-anchored GMP can peek past close day, so fallbacks are
  tagged `gmp_anchor=listing_date_leaky` instead of silently mixed in.
- Price band / issue price: the announced low–high application range and the
  final price at which shares are allotted. `flag_outliers` marks rows where
  issue price sits outside the band.
- Subscription multiple (`sub_total_x`): how many times the offer was bid
  for. Values above 1000x are flagged, not dropped.
- OFS (Offer For Sale): existing shareholders selling stock (vs a fresh
  issue of new shares). Parsed as `ofs_cr` / `ofs_shares` here; later files
  turn that into `ofs_ratio`.
- Issue size (crore): rupees raised, where 1 crore = 10 million. Stored as
  `issue_size_cr` and related share counts.
- Allotment date: when the registrar says who got shares. Parsed as a date
  only; probability math lives in `analysis/features.py`.

FUNCTIONS / CLASSES IN THIS FILE
---------------------------------
- `load_dataset(out_dir)`: pipeline entry. Reads `ipos.csv`, sanitizes,
  splits excluded rows, flags outliers, attaches GMP-at-close and post-listing
  returns. Returns `(modeling_frame, excluded_rows)`.
- `sanitize(df)`: strips currency/percent junk, maps "None"/"NaN" text to
  real missing values, and parses the numeric and date columns models need.
- `attach_gmp_at_close(df, gmp_history_path)`: leak-safe GMP join. History
  date ≤ `ipo_close` wins; otherwise copies `gmp_rs` and marks it leaky.
- `attach_prices(df, prices_dir)`: left-joins horizon returns from
  `prices/returns.csv` (not Nifty). Missing file is a no-op.
- `exclusion_reason(name)` / `flag_outliers(df)`: name-based drop vs
  keep-and-flag. Outliers stay in the sample so tail cases are not hidden.
- `empty_mask(s)`, `to_numeric(s)`, `_history_records(...)`: private
  cleaning helpers used by sanitize and the GMP join.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pandas as pd

from chittorgarh.export import FIELD_KEYS, read_master
from chittorgarh.gmp import last_gmp_on_or_before

NONE_TOKENS = {"", "none", "nan", "nat", "<na>", "null"}
EXCLUDE_NAME = re.compile(r"(FPO Details|REIT Details|InvIT Details|InvIT Fund|IPO information)", re.I)
NUMERIC_COLS = [
    "listing_year", "face_value", "price_band_low", "price_band_high", "issue_price",
    "lot_size", "retail_min_amount", "issue_size_shares", "issue_size_cr",
    "fresh_issue_shares", "fresh_issue_cr", "ofs_shares", "ofs_cr",
    "pre_issue_shares", "post_issue_shares", "market_cap_listing_cr",
    "qib_pct", "anchor_pct", "nii_pct", "retail_pct", "employee_pct", "market_maker_pct",
    "anchor_shares", "anchor_amount_cr",
    "fy1_assets", "fy1_total_income", "fy1_pat", "fy1_ebitda", "fy1_net_worth", "fy1_borrowings",
    "fy2_assets", "fy2_total_income", "fy2_pat", "fy2_ebitda", "fy2_net_worth", "fy2_borrowings",
    "fy3_assets", "fy3_total_income", "fy3_pat", "fy3_ebitda", "fy3_net_worth", "fy3_borrowings",
    "eps_pre", "eps_post", "pe_pre", "pe_post", "roe", "roce", "ronw", "debt_equity",
    "pat_margin", "ebitda_margin", "nav", "pbv",
    "promoter_pre_pct", "promoter_post_pct", "public_pre_pct", "public_post_pct",
    "gmp_rs", "gmp_pct", "gmp_est_listing_price", "gmp_obs_count", "kostak_rs", "subject_to_sauda",
    "sub_qib_x", "sub_nii_x", "sub_bnii_x", "sub_snii_x", "sub_retail_x", "sub_total_x",
    "total_applications", "listing_day_close", "listing_day_gain_pct",
    "current_price", "profit_loss_pct",
    "listing_bse_open", "listing_bse_high", "listing_bse_low", "listing_bse_last",
    "listing_nse_open", "listing_nse_high", "listing_nse_low", "listing_nse_last",
    "retail_min_lots", "retail_min_shares", "retail_max_lots", "retail_max_shares",
]


def empty_mask(s: pd.Series) -> pd.Series:
    if s is None:
        return pd.Series(dtype=bool)
    text = s.astype(str).str.strip()
    return s.isna() | text.str.lower().isin(NONE_TOKENS)


def to_numeric(s: pd.Series) -> pd.Series:
    cleaned = s.astype(str).str.replace(r"[₹$%,xX]", "", regex=True).str.replace(",", "", regex=False)
    cleaned = cleaned.str.strip()
    cleaned = cleaned.mask(cleaned.str.lower().isin(NONE_TOKENS))
    return pd.to_numeric(cleaned, errors="coerce")


def sanitize(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for col in out.columns:
        if out[col].dtype == object:
            out[col] = out[col].mask(empty_mask(out[col]))
    for col in NUMERIC_COLS:
        if col in out.columns:
            out[col] = to_numeric(out[col])
    for col in (
        "ipo_open", "ipo_close", "listing_date", "allotment_date", "gmp_close_date",
        "anchor_lockin_30d", "anchor_lockin_90d",
    ):
        if col in out.columns:
            out[col] = pd.to_datetime(out[col], errors="coerce")
    if "listing_year" in out.columns:
        out["listing_year"] = pd.to_numeric(out["listing_year"], errors="coerce").astype("Int64")
    return out


def exclusion_reason(name: Any) -> str | None:
    text = str(name or "")
    if EXCLUDE_NAME.search(text):
        return "fpo_reit_invit_or_malformed_name"
    return None


def flag_outliers(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    ip = out.get("issue_price")
    lo = out.get("price_band_low")
    hi = out.get("price_band_high")
    out["flag_high_sub"] = out.get("sub_total_x", pd.Series(index=out.index)) > 1000
    out["flag_sm_reit_unit"] = (ip.fillna(0) > 10_000) if ip is not None else False
    if ip is not None and lo is not None and hi is not None:
        both = lo.notna() & hi.notna() & ip.notna()
        out["flag_price_outside_band"] = both & ((ip < lo - 1) | (ip > hi + 1))
    else:
        out["flag_price_outside_band"] = False
    if "fy1_pat" in out.columns:
        out["flag_negative_pat"] = out["fy1_pat"] < 0
    return out


def _history_records(hist: pd.DataFrame, ipo_id: str) -> list[dict[str, Any]]:
    chunk = hist[hist["ipo_id"].astype(str) == str(ipo_id)]
    recs: list[dict[str, Any]] = []
    for _, r in chunk.iterrows():
        recs.append({
            "gmp_date": None if pd.isna(r.get("gmp_date")) else str(r.get("gmp_date"))[:10],
            "gmp_rs": r.get("gmp_rs"),
            "gmp_pct": r.get("gmp_pct"),
            "gmp_est_listing_price": r.get("gmp_est_listing_price"),
            "kostak_rs": r.get("kostak_rs"),
            "subject_to_sauda": r.get("subject_to_sauda"),
        })
    return recs


def attach_gmp_at_close(df: pd.DataFrame, gmp_history_path: Path | None) -> pd.DataFrame:
    """Prefer leak-free GMP (date <= ipo_close). Fall back to listing-anchored gmp_rs."""
    out = df.copy()
    out["gmp_at_close"] = pd.NA
    out["gmp_at_close_date"] = pd.NaT
    out["gmp_anchor"] = "none"
    hist = None
    if gmp_history_path and gmp_history_path.exists() and gmp_history_path.stat().st_size > 0:
        hist = pd.read_csv(gmp_history_path, dtype=str, keep_default_na=False)
        if "gmp_rs" in hist.columns:
            hist["gmp_rs"] = to_numeric(hist["gmp_rs"])
        if "gmp_date" in hist.columns:
            hist["gmp_date"] = hist["gmp_date"].mask(empty_mask(hist["gmp_date"]))

    for i, row in out.iterrows():
        close = row.get("ipo_close")
        close_s = None if pd.isna(close) else pd.Timestamp(close).strftime("%Y-%m-%d")
        used = False
        if hist is not None:
            recs = _history_records(hist, row["ipo_id"])
            recs = [r for r in recs if r.get("gmp_date")]
            picked = last_gmp_on_or_before(recs, close_s)
            if picked and picked.get("gmp_rs") is not None and str(picked.get("gmp_rs")) not in NONE_TOKENS:
                val = pd.to_numeric(picked["gmp_rs"], errors="coerce")
                if pd.notna(val):
                    out.at[i, "gmp_at_close"] = val
                    out.at[i, "gmp_at_close_date"] = picked.get("gmp_close_date")
                    out.at[i, "gmp_anchor"] = "ipo_close"
                    used = True
        if not used and pd.notna(row.get("gmp_rs")):
            out.at[i, "gmp_at_close"] = row["gmp_rs"]
            out.at[i, "gmp_at_close_date"] = row.get("gmp_close_date")
            out.at[i, "gmp_anchor"] = "listing_date_leaky"
    out["gmp_at_close"] = pd.to_numeric(out["gmp_at_close"], errors="coerce")
    return out


def attach_prices(df: pd.DataFrame, prices_dir: Path | None) -> pd.DataFrame:
    out = df.copy()
    ret_path = prices_dir / "returns.csv" if prices_dir else None
    if not ret_path or not ret_path.exists():
        return out
    ret = pd.read_csv(ret_path, dtype={"ipo_id": str})
    keep = [c for c in ret.columns if c == "ipo_id" or c.startswith("ret_") or c.startswith("exret_") or c.startswith("mdd_") or c.startswith("sharpe_")]
    return out.merge(ret[keep], on="ipo_id", how="left")


def load_dataset(out_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Returns (modeling frame, excluded rows)."""
    raw = read_master(out_dir / "ipos.csv")
    if list(raw.columns) != FIELD_KEYS[: len(raw.columns)] and len(raw.columns) != len(FIELD_KEYS):
        pass
    df = sanitize(raw)
    reasons = df["company_name"].map(exclusion_reason)
    excluded = df[reasons.notna()].copy()
    excluded["exclude_reason"] = reasons[reasons.notna()]
    keep = df[reasons.isna()].copy()
    keep = flag_outliers(keep)
    keep = attach_gmp_at_close(keep, out_dir / "gmp_history.csv")
    keep = attach_prices(keep, out_dir / "prices")
    return keep.reset_index(drop=True), excluded.reset_index(drop=True)
