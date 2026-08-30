"""Resumable scrape pipeline: tracker index → detail pages → GMP tab → CSVs."""

from __future__ import annotations

import traceback
from pathlib import Path
from typing import Any, Optional

from chittorgarh.export import (
    append_master,
    append_or_replace,
    coverage_report,
    flatten_into_master,
    load_done_ids,
    read_master,
    rebuild_master_xlsx,
    remove_legacy_outputs,
)
from chittorgarh.gmp import last_gmp_close, scrape_gmp, scrape_gmp_with_page
from chittorgarh.http import HttpClient
from chittorgarh.parse_ipo import parse_ipo_html
from chittorgarh.tracker import scrape_tracker

EXCHANGES = ("mainline", "sme")


def scrape_index(
    from_year: int,
    to_year: int,
    exchanges: tuple[str, ...] = EXCHANGES,
    ipo_id: Optional[str] = None,
) -> list[dict[str, Any]]:
    index: list[dict[str, Any]] = []
    for year in range(from_year, to_year + 1):
        for exchange in exchanges:
            print(f"[index] {exchange} {year}", flush=True)
            rows = scrape_tracker(exchange, year)
            print(f"        {len(rows)} listed IPOs", flush=True)
            index.extend(rows)
    if ipo_id:
        index = [r for r in index if str(r.get("ipo_id")) == str(ipo_id)]
        if not index:
            raise SystemExit(f"ipo_id={ipo_id} not found on tracker for {from_year}-{to_year} {exchanges}")
    return index


def scrape_one(
    client: HttpClient,
    row: dict[str, Any],
    fetch_gmp: bool = True,
    headless: bool = True,
    use_cache: bool = True,
    gmp_page: Any = None,
) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    url = row["url"]
    ipo_id = str(row["ipo_id"])
    html = client.get_text(
        url,
        cache_name=f"{ipo_id}.html" if use_cache else None,
        use_cache=use_cache,
    )
    parsed = parse_ipo_html(
        html,
        url=url,
        exchange_type=row.get("exchange_type"),
        listing_year=row.get("listing_year"),
        tracker=row,
    )
    master = parsed["master"]
    sats = parsed["satellites"]
    gmp_history: list[dict[str, Any]] = []
    if fetch_gmp:
        try:
            if gmp_page is not None:
                gmp_history = scrape_gmp_with_page(gmp_page, url, ipo_id)
            else:
                gmp_history = scrape_gmp(url, ipo_id, headless=headless)
        except Exception as exc:
            warnings = master.get("parse_warnings") or ""
            extra = f"gmp_error:{exc}"
            master["parse_warnings"] = f"{warnings}; {extra}".strip("; ")
            gmp_history = []
        if gmp_history:
            master.update(last_gmp_close(gmp_history, master.get("listing_date")))
        else:
            warnings = master.get("parse_warnings") or ""
            if "gmp_error" not in warnings:
                master["parse_warnings"] = f"{warnings}; gmp_missing".strip("; ")
    sats["gmp_history"] = gmp_history
    master = flatten_into_master(master, sats)
    return master, sats


def persist_gmp_history(
    out_dir: Path,
    ipo_id: str,
    history: list[dict[str, Any]],
    dest: Path | None = None,
) -> None:
    if not history:
        return
    rows = [{**rec, "ipo_id": ipo_id} for rec in history]
    append_or_replace(dest if dest is not None else out_dir / "gmp_history.csv", rows, key="ipo_id")


def persist_one(out_dir: Path, master: dict[str, Any], sats: dict[str, list[dict[str, Any]]]) -> None:
    row = flatten_into_master(master, sats)
    append_master(out_dir, [row])
    persist_gmp_history(out_dir, str(row.get("ipo_id")), sats.get("gmp_history") or [])


def log_failure(out_dir: Path, row: dict[str, Any], error: str) -> None:
    append_or_replace(
        out_dir / "failed.csv",
        [{
            "ipo_id": row.get("ipo_id"),
            "url": row.get("url"),
            "company_name": row.get("company_name"),
            "error": error[:2000],
        }],
    )


def run_pipeline(
    out_dir: Path,
    from_year: int = 2016,
    to_year: int = 2026,
    exchanges: tuple[str, ...] = EXCHANGES,
    delay: float = 1.5,
    resume: bool = False,
    retry_failed: bool = False,
    ipo_id: Optional[str] = None,
    fetch_gmp: bool = True,
    headless: bool = True,
) -> list[dict[str, Any]]:
    out_dir.mkdir(parents=True, exist_ok=True)
    remove_legacy_outputs(out_dir)
    cache_dir = out_dir / "cache"
    index = scrape_index(from_year, to_year, exchanges, ipo_id=ipo_id)

    done = load_done_ids(out_dir / "ipos.csv") if resume else set()
    failed_ids = load_done_ids(out_dir / "failed.csv") if (out_dir / "failed.csv").exists() else set()
    if retry_failed:
        done -= failed_ids

    masters: list[dict[str, Any]] = []
    saved = 0
    with HttpClient(cache_dir=cache_dir, delay=delay) as client:
        for i, row in enumerate(index, 1):
            rid = str(row["ipo_id"])
            if rid in done:
                print(f"[{i}/{len(index)}] skip {rid} (resume)", flush=True)
                continue
            print(f"[{i}/{len(index)}] {row.get('company_name')} ({rid})", flush=True)
            try:
                master, sats = scrape_one(client, row, fetch_gmp=fetch_gmp, headless=headless)
                persist_one(out_dir, master, sats)
                masters.append(master)
                done.add(rid)
                saved += 1
                if saved % 50 == 0:
                    rebuild_master_xlsx(out_dir)
            except Exception as extra:
                print(f"        FAIL {extra}", flush=True)
                log_failure(out_dir, row, f"{extra}\n{traceback.format_exc()}")
    csv_path = out_dir / "ipos.csv"
    if csv_path.exists() and csv_path.stat().st_size > 0:
        report_src = read_master(csv_path).to_dict(orient="records")
        rebuild_master_xlsx(out_dir)
    else:
        report_src = masters
    report = coverage_report(report_src)
    print(report)
    (out_dir / "coverage.txt").write_text(report, encoding="utf-8")
    return masters
