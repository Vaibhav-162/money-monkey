"""Post-listing price join. Network calls live only in scripts/fetch_prices.py."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

import pandas as pd

from analysis.load import sanitize
from chittorgarh.export import read_master as _read_master


def _yf_history(ticker: str, start: str, end: str) -> Optional[pd.DataFrame]:
    try:
        import yfinance as yf
    except ImportError:
        return None
    try:
        hist = yf.Ticker(ticker).history(start=start, end=end, auto_adjust=True)
    except Exception:
        return None
    if hist is None or hist.empty:
        return None
    hist = hist.reset_index()
    hist.columns = [str(c).lower() for c in hist.columns]
    if "date" not in hist.columns and "index" in hist.columns:
        hist = hist.rename(columns={"index": "date"})
    return hist


def _jugaad_history(symbol: str, start: str, end: str) -> Optional[pd.DataFrame]:
    try:
        from jugaad_data.nse import stock_df
    except ImportError:
        return None
    try:
        from datetime import datetime

        s = datetime.fromisoformat(start[:10])
        e = datetime.fromisoformat(end[:10])
        df = stock_df(symbol=symbol, from_date=s, to_date=e, series="EQ")
    except Exception:
        try:
            from datetime import datetime

            s = datetime.fromisoformat(start[:10])
            e = datetime.fromisoformat(end[:10])
            df = stock_df(symbol=symbol, from_date=s, to_date=e, series="SM")
        except Exception:
            return None
    if df is None or df.empty:
        return None
    df = df.copy()
    df.columns = [str(c).lower() for c in df.columns]
    return df


def resolve_history(
    nse_symbol: Any,
    bse_code: Any,
    start: str,
    end: str,
) -> tuple[Optional[pd.DataFrame], str]:
    nse = str(nse_symbol or "").strip()
    bse = str(bse_code or "").strip()
    if nse and nse.lower() not in {"nan", "none"}:
        hist = _yf_history(f"{nse}.NS", start, end)
        if hist is not None:
            return hist, f"{nse}.NS"
    if bse and bse.lower() not in {"nan", "none"}:
        hist = _yf_history(f"{bse}.BO", start, end)
        if hist is not None:
            return hist, f"{bse}.BO"
    if nse and nse.lower() not in {"nan", "none"}:
        hist = _jugaad_history(nse, start, end)
        if hist is not None:
            return hist, f"jugaad:{nse}"
    return None, "missing"


def fetch_nifty(prices_dir: Path) -> Optional[pd.DataFrame]:
    prices_dir.mkdir(parents=True, exist_ok=True)
    dest = prices_dir / "nifty.parquet"
    hist = _yf_history("^NSEI", "2016-01-01", "2027-01-01")
    if hist is None:
        return pd.read_parquet(dest) if dest.exists() else None
    hist.to_parquet(dest, index=False)
    return hist


def _close_col(df: pd.DataFrame) -> Optional[str]:
    for name in ("close", "adj close", "adj_close"):
        if name in df.columns:
            return name
    return None


def _session_return(hist: pd.DataFrame, listing: pd.Timestamp, sessions: int) -> Optional[float]:
    col = _close_col(hist)
    if col is None or "date" not in hist.columns:
        return None
    h = hist.copy()
    h["date"] = pd.to_datetime(h["date"], errors="coerce").dt.tz_localize(None)
    h = h.dropna(subset=["date", col]).sort_values("date")
    after = h[h["date"] >= pd.Timestamp(listing).normalize()]
    if len(after) < 2:
        return None
    start_px = float(after.iloc[0][col])
    idx = min(sessions, len(after) - 1)
    end_px = float(after.iloc[idx][col])
    if start_px <= 0:
        return None
    return end_px / start_px - 1.0


def _mdd(hist: pd.DataFrame, listing: pd.Timestamp, sessions: int) -> Optional[float]:
    col = _close_col(hist)
    if col is None or "date" not in hist.columns:
        return None
    h = hist.copy()
    h["date"] = pd.to_datetime(h["date"], errors="coerce").dt.tz_localize(None)
    after = h[h["date"] >= pd.Timestamp(listing).normalize()].head(sessions + 1)
    if after.empty:
        return None
    px = pd.to_numeric(after[col], errors="coerce").dropna()
    if px.empty:
        return None
    peak = px.cummax()
    dd = px / peak - 1.0
    return float(dd.min())


def _sharpe(hist: pd.DataFrame, listing: pd.Timestamp, sessions: int) -> Optional[float]:
    col = _close_col(hist)
    if col is None or "date" not in hist.columns:
        return None
    h = hist.copy()
    h["date"] = pd.to_datetime(h["date"], errors="coerce").dt.tz_localize(None)
    after = h[h["date"] >= pd.Timestamp(listing).normalize()].head(sessions + 1)
    px = pd.to_numeric(after[col], errors="coerce").dropna()
    if len(px) < 10:
        return None
    rets = px.pct_change().dropna()
    if rets.std() == 0:
        return None
    return float((rets.mean() / rets.std()) * (252 ** 0.5))


def nifty_return(nifty: Optional[pd.DataFrame], listing: pd.Timestamp, sessions: int) -> Optional[float]:
    if nifty is None or nifty.empty:
        return None
    return _session_return(nifty, listing, sessions)


def nifty_20d_asof(nifty: Optional[pd.DataFrame], as_of: pd.Timestamp) -> Optional[float]:
    if nifty is None or nifty.empty or pd.isna(as_of):
        return None
    col = _close_col(nifty)
    if col is None or "date" not in nifty.columns:
        return None
    h = nifty.copy()
    h["date"] = pd.to_datetime(h["date"], errors="coerce").dt.tz_localize(None)
    h = h.dropna(subset=["date", col]).sort_values("date")
    before = h[h["date"] <= pd.Timestamp(as_of).normalize()]
    if len(before) < 21:
        return None
    last = float(before.iloc[-1][col])
    prev = float(before.iloc[-21][col])
    if prev <= 0:
        return None
    return last / prev - 1.0


def spot_check(out_dir: Path, n_each: int = 10) -> dict[str, Any]:
    df = sanitize(_read_master(out_dir / "ipos.csv"))
    rows: list[dict[str, Any]] = []
    for board in ("mainboard", "sme"):
        chunk = df[df["exchange_type"] == board]
        chunk = chunk[chunk["listing_date"].notna() & (chunk["nse_symbol"].notna() | chunk["bse_code"].notna())]
        sample = chunk.tail(n_each)
        hits = 0
        for _, r in sample.iterrows():
            listing = pd.Timestamp(r["listing_date"])
            start = (listing - pd.Timedelta(days=5)).strftime("%Y-%m-%d")
            end = (listing + pd.Timedelta(days=400)).strftime("%Y-%m-%d")
            hist, src = resolve_history(r.get("nse_symbol"), r.get("bse_code"), start, end)
            ok = hist is not None
            hits += int(ok)
            rows.append({
                "board": board,
                "ipo_id": str(r["ipo_id"]),
                "company": r.get("company_name"),
                "nse_symbol": r.get("nse_symbol"),
                "bse_code": r.get("bse_code"),
                "source": src,
                "ok": ok,
            })
        # store per-board later
    summary = {}
    for board in ("mainboard", "sme"):
        part = [x for x in rows if x["board"] == board]
        n = len(part) or 1
        hit = sum(1 for x in part if x["ok"])
        summary[board] = {"tried": len(part), "hits": hit, "hit_rate": round(hit / n, 3)}
    return {"summary": summary, "rows": rows}


def listed_price_jobs(df: pd.DataFrame) -> pd.DataFrame:
    """Rows with a listing date that we can try to fetch prices for."""
    out = df[df["listing_date"].notna()].copy()
    return out.reset_index(drop=True)


def missing_daily_ids(daily_dir: Path, jobs: pd.DataFrame) -> list[str]:
    missing: list[str] = []
    for ipo_id in jobs["ipo_id"].astype(str):
        if not (daily_dir / f"{ipo_id}.parquet").exists():
            missing.append(ipo_id)
    return missing


def fetch_daily_bars(
    jobs: pd.DataFrame,
    daily_dir: Path,
    delay: float = 0.4,
    tag: str = "[serial]",
) -> dict[str, int]:
    """Network + parquet only. Does not write returns.csv."""
    import time

    daily_dir.mkdir(parents=True, exist_ok=True)
    fetched = cached = missing = 0
    n = len(jobs)
    for i, (_, r) in enumerate(jobs.iterrows(), 1):
        ipo_id = str(r["ipo_id"])
        cache = daily_dir / f"{ipo_id}.parquet"
        listing_ts = pd.Timestamp(r["listing_date"])
        start = (listing_ts - pd.Timedelta(days=7)).strftime("%Y-%m-%d")
        end = (listing_ts + pd.Timedelta(days=800)).strftime("%Y-%m-%d")
        if cache.exists():
            cached += 1
            print(f"{tag} [{i}/{n}] {ipo_id} cache", flush=True)
            continue
        hist, src = resolve_history(r.get("nse_symbol"), r.get("bse_code"), start, end)
        if hist is not None:
            hist.to_parquet(cache, index=False)
            fetched += 1
        else:
            missing += 1
            src = "missing"
        print(f"{tag} [{i}/{n}] {ipo_id} {src}", flush=True)
        time.sleep(delay)
    return {"fetched": fetched, "cached": cached, "missing": missing}


def _returns_record(
    ipo_id: str,
    listing_ts: pd.Timestamp,
    close_or_listing: Any,
    hist: Optional[pd.DataFrame],
    nifty: Optional[pd.DataFrame],
    src: str,
) -> dict[str, Any]:
    rec: dict[str, Any] = {"ipo_id": ipo_id, "price_source": src}
    if hist is None:
        rec["price_source"] = "missing"
        return rec
    for sess, name in ((21, "21"), (63, "63"), (126, "126"), (252, "252")):
        raw = _session_return(hist, listing_ts, sess)
        bench = nifty_return(nifty, listing_ts, sess)
        rec[f"ret_{name}"] = raw
        rec[f"exret_{name}"] = None if raw is None or bench is None else raw - bench
    rec["mdd_126"] = _mdd(hist, listing_ts, 126)
    rec["sharpe_126"] = _sharpe(hist, listing_ts, 126)
    rec["nifty_20d"] = nifty_20d_asof(nifty, close_or_listing)
    return rec


def build_returns_table(out_dir: Path) -> pd.DataFrame:
    """Local only: walk daily parquets + nifty.parquet → returns.csv frame."""
    df = sanitize(_read_master(out_dir / "ipos.csv"))
    jobs = listed_price_jobs(df)
    prices_dir = out_dir / "prices"
    daily_dir = prices_dir / "daily"
    nifty_path = prices_dir / "nifty.parquet"
    nifty = pd.read_parquet(nifty_path) if nifty_path.exists() else None
    records: list[dict[str, Any]] = []
    for _, r in jobs.iterrows():
        ipo_id = str(r["ipo_id"])
        cache = daily_dir / f"{ipo_id}.parquet"
        listing_ts = pd.Timestamp(r["listing_date"])
        as_of = r.get("ipo_close") if pd.notna(r.get("ipo_close")) else listing_ts
        if cache.exists():
            hist = pd.read_parquet(cache)
            rec = _returns_record(ipo_id, listing_ts, as_of, hist, nifty, "cache")
        else:
            rec = _returns_record(ipo_id, listing_ts, as_of, None, nifty, "missing")
        records.append(rec)
    return pd.DataFrame.from_records(records)


def fetch_all_returns(out_dir: Path, delay: float = 0.4, limit: Optional[int] = None) -> pd.DataFrame:
    """Sequential convenience wrapper: nifty + daily bars + returns table."""
    df = sanitize(_read_master(out_dir / "ipos.csv"))
    jobs = listed_price_jobs(df)
    if limit:
        jobs = jobs.head(limit)
    prices_dir = out_dir / "prices"
    daily_dir = prices_dir / "daily"
    daily_dir.mkdir(parents=True, exist_ok=True)
    fetch_nifty(prices_dir)
    fetch_daily_bars(jobs, daily_dir, delay=delay)
    return build_returns_table(out_dir)


def load_nifty_20d(df: pd.DataFrame, prices_dir: Path) -> pd.DataFrame:
    out = df.copy()
    ret_path = prices_dir / "returns.csv"
    if ret_path.exists():
        ret = pd.read_csv(ret_path, dtype={"ipo_id": str})
        if "nifty_20d" in ret.columns:
            mapped = dict(zip(ret["ipo_id"].astype(str), ret["nifty_20d"]))
            out["nifty_20d"] = out["ipo_id"].astype(str).map(mapped)
    return out
