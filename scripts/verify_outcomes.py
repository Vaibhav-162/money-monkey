"""Fill realized listing-day outcomes on unverified audit rows."""

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
