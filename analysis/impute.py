"""Two feature tables: imputed (logistic) vs raw NaN (LightGBM). Never impute GMP."""

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
