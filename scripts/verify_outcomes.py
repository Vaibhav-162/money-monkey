"""Fill realized listing-day outcomes on unverified audit rows.

WHAT THIS FILE DOES
--------------------
The close-day scanner writes a *prediction* (`apply_s1`, `p_pop`,
`ev_retail`, …) into `data/live_audit_log.csv` before the stock lists.
This script is the next-morning check: re-fetch each unverified row's
Chittorgarh detail URL, and once listing OHLC is on the page, fill
`actual_listing_open`, `actual_open_return_pct`, and
`actual_is_clean_pop`, then flip `verified`. Already-verified rows are
left untouched (idempotent).

`.github/workflows/verify_outcomes.yml` is the production entrypoint.
It runs `python scripts/verify_outcomes.py --out data` on a weekday
`schedule` at 09:45 IST (`15 4 * * 1-5`) and on `workflow_dispatch`
(no dry-run flag — this job does not send alerts). The workflow then
commits the updated audit CSV plus `data/analysis/live_performance.json`.
Nothing else in the package imports `run_verify()` except tests.

This file calls `chittorgarh.http.HttpClient` + `parse_ipo_html` +
`flatten_into_master` for the live re-fetch, and
`analysis.live_audit.compute_actuals` / `performance_summary` /
`read_audit` / `write_audit` for the outcome math and the CSV/JSON
write. It does not talk to Telegram or Gmail.

`compute_actuals` returns None until listing open is published, so a
row stays unverified and will be tried again the next weekday. A clean
pop is defined from the **listing OPEN** return (>= 15% and the day's
low held above issue price), matching the EV framework's "exit at
listing open" assumption — not the historical close-day tracker gain,
which a bare detail-URL re-fetch never has.

KEY TERMS USED HERE
--------------------
- Listing-day outcome / clean pop: whether the stock opened at least
  15% above issue price without trading below it that morning. This is
  the realized label that Strategy 1 (`apply_s1`) was predicting.
- Verified flag: `verified=True` plus `verified_at` means listing OHLC
  was found and actuals were written. The next run skips that row.
  If OHLC is still missing, the row stays unverified for tomorrow.
- Audit log (`data/live_audit_log.csv`): the forward-test ledger the
  close-day scanner and allotment checker also write. This script only
  fills the actuals / verified columns; it does not re-score or re-alert.
- Board (`exchange_type`): passed through to `parse_ipo_html` so
  mainboard vs SME detail pages parse with the right layout.
- `live_performance.json`: aggregate precision / realized-EV snapshot
  over verified APPLY-S1 rows, rewritten every run (even when the audit
  is empty) so the committed JSON cannot go stale.

FUNCTIONS / CLASSES IN THIS FILE
---------------------------------
- `run_verify(...)`: walk the audit, verify unverified rows, write the
  CSV and the performance JSON. Accepts injected `rows` / `client` for
  tests.
- `verify_row(client, row)`: fetch one detail page and merge actuals.
  Returns the row unchanged when listing OHLC is not published yet.
- `_verified_flag(value)` / `_write_summary(out_dir, summary)`: audit-CSV
  bool parse, and the JSON dump to `data/analysis/live_performance.json`.
- `main(argv)`: CLI (`--out`, default `data`).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from analysis.live_audit import (
    compute_actuals,
    performance_summary,
    read_audit,
    write_audit,
)
from chittorgarh.http import HttpClient
from chittorgarh.parse_ipo import parse_ipo_html
from chittorgarh.export import flatten_into_master


def _verified_flag(value: Any) -> bool:
    return str(value).strip().lower() in {"true", "1", "yes"}


def verify_row(client: HttpClient, row: dict[str, Any]) -> dict[str, Any]:
    url = row.get("url")
    ipo_id = str(row.get("ipo_id") or "")
    if not url:
        return row
    html = client.get_text(url, cache_name=None, use_cache=False)
    parsed = parse_ipo_html(
        html,
        url=url,
        exchange_type=row.get("board"),
    )
    master = flatten_into_master(parsed["master"], parsed["satellites"])
    actuals = compute_actuals(master)
    if actuals is None:
        return row
    updated = dict(row)
    updated.update(actuals)
    updated["verified"] = True
    updated["verified_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if not updated.get("listing_date_expected"):
        updated["listing_date_expected"] = master.get("listing_date")
    return updated


def run_verify(
    *,
    out_dir: Path | None = None,
    audit_path: Path | None = None,
    rows: Optional[pd.DataFrame] = None,
    client: Optional[HttpClient] = None,
) -> dict[str, Any]:
    out_dir = Path(out_dir) if out_dir else ROOT / "data"
    path = Path(audit_path) if audit_path else out_dir / "live_audit_log.csv"
    frame = rows if rows is not None else read_audit(path)
    if frame.empty:
        print("[verify] audit log empty")
        summary = performance_summary(frame)
        _write_summary(out_dir, summary)
        return summary

    own = client is None
    http = client or HttpClient(cache_dir=out_dir / "cache_live", delay=1.5)
    updated_rows: list[dict[str, Any]] = []
    try:
        for rec in frame.to_dict(orient="records"):
            if _verified_flag(rec.get("verified")):
                updated_rows.append(rec)
                continue
            try:
                updated_rows.append(verify_row(http, rec))
            except Exception as exc:
                rec = dict(rec)
                rec["error"] = f"verify:{exc}"
                updated_rows.append(rec)
    finally:
        if own:
            http.close()

    out = pd.DataFrame(updated_rows)
    write_audit(path, out)
    summary = performance_summary(out)
    _write_summary(out_dir, summary)
    n_now = int(out["verified"].astype(str).str.lower().isin({"true", "1", "yes"}).sum())
    print(f"[verify] verified={n_now}/{len(out)} wrote {path}")
    return summary


def _write_summary(out_dir: Path, summary: dict[str, Any]) -> None:
    dest = out_dir / "analysis" / "live_performance.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"[verify] {dest}")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Verify listing-day outcomes for prior alerts")
    parser.add_argument("--out", default="data")
    args = parser.parse_args(argv)
    run_verify(out_dir=Path(args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
