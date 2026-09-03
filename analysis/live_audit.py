"""Forward-test ledger for close-day alerts (`data/live_audit_log.csv`).

WHAT THIS FILE DOES
--------------------
This is the shared schema and I/O for the live paper-trade log — one row
per `(ipo_id, close_date)`. It does not scrape, train, or send Telegram;
three scripts own those jobs and all three talk to the same CSV:

- `scripts/live_scanner.py` (close-day 15:30 path): `to_score_row` →
  `score_features`, `build_alert_record`, `scrape_timestamps`,
  `rank_same_day_candidates`, `read_audit` + `records_needing_alert`
  (the presence-only gate), then `dispatch()` **before** `upsert_audit()`.
  If delivery raises, the row must stay absent so a retry can still alert.
- `scripts/verify_outcomes.py` (next-morning listing check): `read_audit`,
  `compute_actuals`, `write_audit`, `performance_summary`.
- `scripts/check_allotment.py` (allotment-out notify): `AUDIT_COLUMNS`,
  `read_audit`, `write_audit` — it sets `allotment_notified` only after
  a successful send, same "don't persist a failed attempt" idea.

`upsert_audit` refreshes scores for *all* of today's scored IPOs but
preserves `verified*` and `allotment_notified*` if a later tick re-writes
the same key. Dry-run in the scanner must not call `upsert_audit`: the
gate treats any existing key as "already alerted", so a test write would
suppress the real 15:30/16:00 card.

KEY TERMS USED HERE
--------------------
- Presence-only gate: `records_needing_alert` keeps a record only when
  no `(ipo_id, close_date)` row exists yet. It does **not** compare GMP,
  subscription, or apply flags — the first real write of the day is the
  one alert; later ticks stay silent even if numbers moved. A bug that
  writes the row *before* a successful send permanently suppresses that
  IPO for the rest of the day (a credentials fix cannot re-alert).
- Audit log (`data/live_audit_log.csv`): the forward-test ledger. Close-
  day predictions live here before the stock lists; verify fills actuals;
  allotment-check flips `allotment_notified`.
- ipo_id / close_date: Chittorgarh id and last bidding day. Together they
  are the unique key for upsert and the gate.
- GMP (`gmp_rs` / `gmp_pct`): unofficial pre-listing premium in rupees
  over issue price. Live scrape uses `gmp_rs`; `to_score_row` copies it
  onto `gmp_at_close` so the trainer's feature name is filled.
- Subscription (`sub_total_x`, `sub_ig_x`, `sub_qib_x`): times-
  oversubscribed from Chittorgarh, InvestorGain (display), and QIB.
  QIB is the tie-breaker for same-day APPLY ranking, not a model feature.
- QIB: Qualified Institutional Buyer book. Used only as the second sort
  key in `rank_same_day_candidates`.
- Price band high / issue price / lot size: cap of the application
  range, allotment price, and shares per lot. Capital at risk is
  `price_band_high * lot_size` (issue price if the band cap is missing).
- apply_s1 / apply_s2 / EV (`ev_retail`): the two strategy decisions and
  the rupee ranking number from `score_features`. Ranking never changes
  those flags.
- EV/capital ratio: `ev_retail / capital_required`. Same-day APPLY-S1
  names are ranked by this, then QIB. A lone applicant is left unranked
  (no "1 of 1" banner). Missing ratio or QIB sorts as worst, not as zero.
- Quality score / breakdown: the 0–4 checklist and per-item JSON stored
  for the card; `apply_s2` is the live hold/skip from that checklist.
- Market regime: `BULLISH`/`BEARISH`/`NEUTRAL` column on the CSV.
  Stamped by `live_scanner` (Nifty helper), not computed here — this
  file only stores it.
- Allotment date / `allotment_notified`: registrar timetable and "we
  already sent the allotment-out ping". Upsert must not clear a True.
- Clean pop actuals: `actual_listing_open`, `actual_open_return_pct`,
  `actual_is_clean_pop`. A clean pop is open-return ≥ 15% *and* the
  day's low held above issue — matching the EV "exit at listing open"
  assumption, not the historical tracker gain (a live re-fetch of the
  bare detail URL never has a `tracker` dict, so that field would stay
  None forever).
- Verified: listing OHLC was found and actuals written. `upsert_audit`
  keeps prior verified fields when the same key is re-scored.

FUNCTIONS / CLASSES IN THIS FILE
---------------------------------
- `AUDIT_COLUMNS`: canonical CSV header. `read_audit` fills missing
  columns so an older file still loads.
- `scrape_timestamps(now)`: UTC ISO + locale-stable IST stamp
  (`DD-Mon HH:MM IST`) for cards and the log.
- `to_score_row(master, close_date)`: map a live scrape onto
  `score_features()` inputs (GMP name + `listing_year` fallback). S1/S2
  schema unchanged.
- `build_alert_record(master, score, discovery, error)`: one audit-shaped
  dict. Scrape/score failures set `error` and leave score fields empty
  so a card cannot pretend the model decided.
- `read_audit` / `write_audit`: CSV round-trip as strings; write always
  emits `AUDIT_COLUMNS` order.
- `upsert_audit(path, records)`: replace existing `(ipo_id, close_date)`
  rows, keep prior verification and allotment-notified, drop duplicate
  keys defensively (a dup would crash the scalar merge).
- `records_needing_alert(records, existing)`: the 16:00 catch-up gate —
  presence of the key only, not a field-level diff.
- `rank_same_day_candidates(records)`: fill capital / EV-capital / rank
  for APPLY-S1 names sharing a close_date. Does not change apply flags.
- `capital_required` / `ev_capital_ratio`: lot rupees at the band cap,
  and EV as a fraction of that ticket.
- `listing_open_price` / `listing_low_price`: NSE then BSE then generic
  keys, for outcome math.
- `compute_actuals(master)`: realized S1 fields once listing open is on
  the detail page; None until then so verify can retry tomorrow.
- `performance_summary(frame)`: aggregate precision / predicted vs
  realized-EV proxy on verified APPLY-S1 rows. Notes that S2 listing-day
  return is not the 6-month target.
- `_iso` / `_as_bool` / `_safe_float`: parsers so CSV strings and live
  datetimes do not blow up comparisons.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

AUDIT_COLUMNS = [
    "timestamp_utc",
    "scraped_at_utc",
    "scraped_at_ist",
    "ipo_id",
    "company_name",
    "board",
    "close_date",
    "url",
    "price_band_high",
    "issue_price",
    "lot_size",
    "gmp_rs",
    "gmp_pct",
    "gmp_as_of",
    "gmp_date_raw",
    "sub_total_x",
    "sub_ig_x",
    "sub_qib_x",
    "registrar",
    "capital_required",
    "ev_capital_ratio",
    "rank_of_day",
    "rank_total_of_day",
    "p_pop",
    "p_allot",
    "ev_retail",
    "apply_s1",
    "quality_score",
    "apply_s2",
    "quality_breakdown_json",
    "s2_model_exret_pred",
    "listing_date_expected",
    "allotment_date",
    "market_regime",
    "error",
    "verified",
    "actual_listing_open",
    "actual_open_return_pct",
    "actual_is_clean_pop",
    "verified_at",
    "allotment_notified",
    "allotment_notified_at",
]

_MONTHS = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)


def scrape_timestamps(now: datetime | None = None) -> dict[str, str]:
    """UTC ISO + locale-stable IST display stamp for cards and the audit log."""
    utc = now if now is not None else datetime.now(timezone.utc)
    if utc.tzinfo is None:
        utc = utc.replace(tzinfo=timezone.utc)
    else:
        utc = utc.astimezone(timezone.utc)
    ist = utc.astimezone(ZoneInfo("Asia/Kolkata"))
    return {
        "scraped_at_utc": utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "scraped_at_ist": f"{ist.day:02d}-{_MONTHS[ist.month - 1]} {ist.strftime('%H:%M')} IST",
    }


def _iso(value: Any) -> Any:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def to_score_row(master: dict[str, Any], close_date: str | None = None) -> dict[str, Any]:
    """Map a live scrape master row onto score_features() inputs. S1/S2 schema unchanged."""
    row = dict(master)
    if row.get("gmp_at_close") is None or (isinstance(row.get("gmp_at_close"), float) and pd.isna(row["gmp_at_close"])):
        row["gmp_at_close"] = row.get("gmp_rs")
    if not row.get("listing_year"):
        raw = close_date or row.get("ipo_close") or row.get("listing_date")
        text = str(raw)[:4] if raw else ""
        row["listing_year"] = int(text) if text.isdigit() else datetime.now(timezone.utc).year
    return row


def build_alert_record(
    master: dict[str, Any],
    score: dict[str, Any] | None,
    discovery: dict[str, Any],
    error: str | None = None,
) -> dict[str, Any]:
    breakdown = (score or {}).get("quality_breakdown") or []
    rec = {col: None for col in AUDIT_COLUMNS}
    rec.update(
        {
            "timestamp_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "scraped_at_utc": master.get("scraped_at_utc"),
            "scraped_at_ist": master.get("scraped_at_ist"),
            "ipo_id": str(discovery.get("ipo_id") or master.get("ipo_id") or ""),
            "company_name": discovery.get("company_name") or master.get("company_name"),
            "board": discovery.get("exchange_type") or master.get("exchange_type"),
            "close_date": discovery.get("close_date") or _iso(master.get("ipo_close")),
            "url": discovery.get("url") or master.get("url"),
            "price_band_high": master.get("price_band_high"),
            "issue_price": master.get("issue_price"),
            "lot_size": master.get("lot_size"),
            "gmp_rs": master.get("gmp_rs"),
            "gmp_pct": master.get("gmp_pct"),
            "gmp_as_of": master.get("gmp_as_of") or master.get("gmp_close_date"),
            "gmp_date_raw": master.get("gmp_date_raw"),
            "sub_total_x": master.get("sub_total_x"),
            "sub_ig_x": master.get("sub_ig_x"),
            "sub_qib_x": master.get("sub_qib_x"),
            "registrar": master.get("registrar"),
            "listing_date_expected": _iso(master.get("listing_date")),
            "allotment_date": _iso(master.get("allotment_date")),
            "error": error,
            "verified": False,
            "allotment_notified": False,
        }
    )
    if score:
        rec.update(
            {
                "p_pop": score.get("p_pop"),
                "p_allot": score.get("p_allot"),
                "ev_retail": score.get("ev_retail"),
                "apply_s1": score.get("apply_s1"),
                "quality_score": score.get("quality_score"),
                "apply_s2": score.get("apply_s2"),
                "quality_breakdown_json": json.dumps(breakdown, default=str),
                "s2_model_exret_pred": score.get("s2_model_exret_pred"),
            }
        )
    return rec


def read_audit(path: Path) -> pd.DataFrame:
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame(columns=AUDIT_COLUMNS)
    frame = pd.read_csv(path, dtype=str, keep_default_na=False)
    for col in AUDIT_COLUMNS:
        if col not in frame.columns:
            frame[col] = ""
    return frame[AUDIT_COLUMNS]


def upsert_audit(path: Path, records: list[dict[str, Any]]) -> pd.DataFrame:
    """Replace existing (ipo_id, close_date) rows; keep prior verification if re-scored same day."""
    path.parent.mkdir(parents=True, exist_ok=True)
    current = read_audit(path)
    incoming = pd.DataFrame(records)
    for col in AUDIT_COLUMNS:
        if col not in incoming.columns:
            incoming[col] = None
    incoming = incoming[AUDIT_COLUMNS]
    if current.empty:
        incoming.to_csv(path, index=False)
        return incoming
    current["_key"] = current["ipo_id"].astype(str) + "|" + current["close_date"].astype(str)
    incoming["_key"] = incoming["ipo_id"].astype(str) + "|" + incoming["close_date"].astype(str)
    # Defensive: a hand-edited or previously-buggy CSV could contain duplicate
    # keys, which would make prior.loc[key] return a DataFrame instead of a
    # Series below and crash the scalar assignment. Keep the last occurrence.
    current = current.drop_duplicates(subset="_key", keep="last")
    prior = current.set_index("_key")
    for key, row in incoming.iterrows():
        old = prior.loc[row["_key"]] if row["_key"] in prior.index else None
        if old is not None:
            if str(old.get("verified", "")).lower() in {"true", "1", "yes"}:
                incoming.at[key, "verified"] = old["verified"]
                incoming.at[key, "actual_listing_open"] = old["actual_listing_open"]
                incoming.at[key, "actual_open_return_pct"] = old["actual_open_return_pct"]
                incoming.at[key, "actual_is_clean_pop"] = old["actual_is_clean_pop"]
                incoming.at[key, "verified_at"] = old["verified_at"]
            if str(old.get("allotment_notified", "")).lower() in {"true", "1", "yes"}:
                incoming.at[key, "allotment_notified"] = old["allotment_notified"]
                incoming.at[key, "allotment_notified_at"] = old["allotment_notified_at"]
    kept = current[~current["_key"].isin(set(incoming["_key"]))].drop(columns=["_key"])
    fresh = incoming.drop(columns=["_key"])
    parts = [p for p in (kept, fresh) if not p.empty]
    out = pd.concat(parts, ignore_index=True) if parts else fresh
    out.to_csv(path, index=False)
    return out


def write_audit(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    for col in AUDIT_COLUMNS:
        if col not in frame.columns:
            frame[col] = None
    frame[AUDIT_COLUMNS].to_csv(path, index=False)


def listing_open_price(master: dict[str, Any]) -> float | None:
    for key in ("listing_nse_open", "listing_bse_open", "list_open"):
        val = pd.to_numeric(master.get(key), errors="coerce")
        if pd.notna(val):
            return float(val)
    return None


def listing_low_price(master: dict[str, Any]) -> float | None:
    for key in ("listing_nse_low", "listing_bse_low", "list_low"):
        val = pd.to_numeric(master.get(key), errors="coerce")
        if pd.notna(val):
            return float(val)
    return None


def compute_actuals(master: dict[str, Any]) -> dict[str, Any] | None:
    """Return realized S1 fields once listing OHLC is on the detail page; else None.

    actual_is_clean_pop is computed from the OPEN-price return, not the
    close-day tracker gain (`listing_day_gain_pct`). That field is only ever
    populated when a `tracker` dict is passed into `parse_ipo_html()`, which
    a live re-fetch of the bare detail URL never does -- it would otherwise
    stay None forever and silently lock every row as "verified" with a null
    outcome. The open-price basis also matches the EV framework's own
    "exit at listing open" assumption (see docs/codebase/live_alerts.md).
    """
    issue = pd.to_numeric(master.get("issue_price"), errors="coerce")
    open_px = listing_open_price(master)
    if pd.isna(issue) or issue <= 0 or open_px is None:
        return None
    low = listing_low_price(master)
    open_ret = (open_px / float(issue) - 1.0) * 100.0
    low_ok = low is None or low > float(issue)
    clean = bool(open_ret >= 15 and low_ok)
    return {
        "actual_listing_open": open_px,
        "actual_open_return_pct": open_ret,
        "actual_is_clean_pop": clean,
    }


def performance_summary(frame: pd.DataFrame) -> dict[str, Any]:
    if frame.empty:
        return {"n_alerts": 0, "n_verified": 0}
    verified = frame[frame["verified"].astype(str).str.lower().isin({"true", "1", "yes"})].copy()
    applied = verified[verified["apply_s1"].astype(str).str.lower().isin({"true", "1", "yes"})]
    pops = pd.to_numeric(applied["actual_is_clean_pop"], errors="coerce")
    ev_pred = pd.to_numeric(applied["ev_retail"], errors="coerce")
    ret = pd.to_numeric(applied["actual_open_return_pct"], errors="coerce")
    p_allot = pd.to_numeric(applied["p_allot"], errors="coerce")
    issue = pd.to_numeric(applied["issue_price"], errors="coerce")
    lot = pd.to_numeric(applied["lot_size"], errors="coerce")
    realized_ev = p_allot * (ret / 100.0) * lot * issue
    q = pd.to_numeric(verified["quality_score"], errors="coerce")
    s2_pass = verified[q >= 3]
    s2_ret = pd.to_numeric(s2_pass["actual_open_return_pct"], errors="coerce")
    return {
        "n_alerts": int(len(frame)),
        "n_verified": int(len(verified)),
        "n_apply_s1_verified": int(len(applied)),
        "realized_precision_apply_s1": None if pops.dropna().empty else float(pops.mean()),
        "mean_ev_predicted_apply_s1": None if ev_pred.dropna().empty else float(ev_pred.mean()),
        "mean_realized_ev_proxy_apply_s1": None if realized_ev.dropna().empty else float(realized_ev.mean()),
        "n_s2_pass_verified": int(len(s2_pass)),
        "s2_pass_mean_open_return_pct": None if s2_ret.dropna().empty else float(s2_ret.mean()),
        "note": "Live forward-test, not investment advice. S2 listing-day return is not the 6-month target.",
    }


def _as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes"}
    return bool(value)


def _safe_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if num != num:
        return None
    return num


def capital_required(record: dict[str, Any]) -> float | None:
    price = _safe_float(record.get("price_band_high"))
    if price is None:
        price = _safe_float(record.get("issue_price"))
    lot = _safe_float(record.get("lot_size"))
    if price is None or lot is None or price <= 0 or lot <= 0:
        return None
    return price * lot


def ev_capital_ratio(record: dict[str, Any], capital: float | None = None) -> float | None:
    cap = capital if capital is not None else capital_required(record)
    ev = _safe_float(record.get("ev_retail"))
    if cap is None or cap <= 0 or ev is None:
        return None
    return ev / cap


def records_needing_alert(
    records: list[dict[str, Any]],
    existing: pd.DataFrame | None,
) -> list[dict[str, Any]]:
    """Keep only records for which the 3:30 run has no prior audit row — this is the 4:00 catch-up gate; it does not compare field values."""
    if existing is None or existing.empty:
        return list(records)
    keyed = existing.copy()
    keyed["_key"] = keyed["ipo_id"].astype(str) + "|" + keyed["close_date"].astype(str)
    keyed = keyed.drop_duplicates(subset="_key", keep="last").set_index("_key")
    out: list[dict[str, Any]] = []
    for rec in records:
        key = f"{rec.get('ipo_id') or ''}|{rec.get('close_date') or ''}"
        if key not in keyed.index:
            out.append(rec)
    return out


def rank_same_day_candidates(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rank APPLY-S1 names that share a close_date by EV/capital, then QIB.

    A lone same-day applicant is left unranked (no banner). Missing ratio or
    QIB sorts as worst, not as zero. Does not change apply_s1 / apply_s2.
    """
    out = [dict(rec) for rec in records]
    for rec in out:
        cap = capital_required(rec)
        rec["capital_required"] = cap
        rec["ev_capital_ratio"] = ev_capital_ratio(rec, cap)
        rec["rank_of_day"] = None
        rec["rank_total_of_day"] = None

    groups: dict[str, list[int]] = {}
    for i, rec in enumerate(out):
        if rec.get("error"):
            continue
        if not _as_bool(rec.get("apply_s1")):
            continue
        day = str(rec.get("close_date") or "")
        groups.setdefault(day, []).append(i)

    for idxs in groups.values():
        if len(idxs) < 2:
            continue

        def sort_key(i: int) -> tuple[float, float]:
            ratio = out[i].get("ev_capital_ratio")
            qib = _safe_float(out[i].get("sub_qib_x"))
            return (
                ratio if ratio is not None else float("-inf"),
                qib if qib is not None else float("-inf"),
            )

        ranked = sorted(idxs, key=sort_key, reverse=True)
        total = len(ranked)
        for pos, i in enumerate(ranked, 1):
            out[i]["rank_of_day"] = pos
            out[i]["rank_total_of_day"] = total
    return out
