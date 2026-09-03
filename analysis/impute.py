"""Two feature tables: imputed (logistic) vs raw NaN (LightGBM). Never impute GMP.

WHAT THIS FILE DOES
--------------------
Fills gaps in a handful of fundamental ratios so the logistic-regression
baseline in `analysis/models.py` has no NaNs. The only production caller is
`analysis/features.py` (`add_features` starts with `impute_ratios`). LightGBM
training later reads the raw columns via `feature_matrix(..., filled=False)`
and can leave NaN in place; the logistic path uses the `_filled` siblings
(`filled=True`). This file never touches GMP — a missing grey-market quote
stays missing on purpose.

KEY TERMS USED HERE
--------------------
- Feature imputation: replacing a missing number with a stand-in so a model
  that cannot accept NaN can still train. Here the stand-in is a median, not
  a mean, so a few extreme IPOs do not drag the fill value.
- Group median: first try same-industry + same-listing-year, then same year,
  then the global median. Narrower groups keep the fill closer to peers.
- ROE / ROCE: return on equity and return on capital employed — profitability
  ratios used as model features. Missing values are common on SME filings.
- Debt-equity: leverage. Same fill ladder as the profitability ratios.
- PE (`pe_pre`) / PBV: pre-issue price-to-earnings and price-to-book. Filled
  for logistic only; the raw column (or NaN) is what trees see.
- GMP (Grey Market Premium): deliberately *not* in `RATIO_COLS`. Inventing a
  grey-market number would invent demand the street never showed.

FUNCTIONS / CLASSES IN THIS FILE
---------------------------------
- `impute_ratios(df, cols)`: for each ratio, writes `{col}_missing`,
  `{col}_imputed`, `{col}_raw` (untouched), and `{col}_filled` (median
  ladder). Default `cols` is `RATIO_COLS` (roe, roce, debt_equity, pe_pre, pbv).
"""

from __future__ import annotations

import pandas as pd

RATIO_COLS = ["roe", "roce", "debt_equity", "pe_pre", "pbv"]


def impute_ratios(df: pd.DataFrame, cols: list[str] | None = None) -> pd.DataFrame:
    out = df.copy()
    cols = cols or RATIO_COLS
    year = out["listing_year"] if "listing_year" in out.columns else None
    industry = out["industry"] if "industry" in out.columns else None
    for col in cols:
        if col not in out.columns:
            continue
        src = pd.to_numeric(out[col], errors="coerce")
        imputed = src.copy()
        flag = src.isna()
        out[f"{col}_missing"] = flag.astype(int)
        out[f"{col}_imputed"] = False
        if year is not None and industry is not None:
            grouped = src.groupby([industry.fillna("_unk"), year]).transform("median")
            fill = flag & grouped.notna()
            imputed = imputed.where(~fill, grouped)
            out.loc[fill, f"{col}_imputed"] = True
        still = imputed.isna()
        if year is not None:
            ymed = src.groupby(year).transform("median")
            fill = still & ymed.notna()
            imputed = imputed.where(~fill, ymed)
            out.loc[fill, f"{col}_imputed"] = True
        still = imputed.isna()
        gmed = src.median()
        if pd.notna(gmed):
            fill = still
            imputed = imputed.where(~fill, gmed)
            out.loc[fill, f"{col}_imputed"] = True
        out[f"{col}_raw"] = src
        out[f"{col}_filled"] = imputed
    return out
