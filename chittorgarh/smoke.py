"""End-to-end smoke test for Lohia Corp (ipo_id=2574)."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Any

from chittorgarh.export import read_master
from chittorgarh.pipeline import run_pipeline

SMOKE_IPO_ID = "2574"
SMOKE_YEAR = 2026
SMOKE_EXCHANGE = "mainline"

# Stable fields from the live Lohia Corp page. current_price is NOT asserted.
GOLDEN = {
    "ipo_id": "2574",
    "nse_symbol": "LCL",
    "bse_code": "544839",
    "isin": "INE0QJW01029",
    "issue_price": 425,
    "face_value": 1,
    "listing_date": "2026-07-30",
    "sale_type_contains": "OFS",
    "listing_at_contains": ("BSE", "NSE"),
    "industry": "Industrial Products",
    "issue_size_cr": 1101,
    "roce": 40.92,
    "roe": 36.80,
    "sub_total_x": 7.26,
    "sub_qib_x": 9.11,
    "sub_nii_x": 6.82,
    "sub_retail_x": 2.78,
    "list_open": 461,
    "list_last": 494.60,
    "promoter_pre_pct": 89.04,
    "promoter_post_pct": 68.66,
    "registrar_contains": "MUFG Intime",
    "lead_contains": ("Equirus", "Motilal"),
}


def _num(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", ""))
    except ValueError:
        return None


def close_enough(actual: Any, expected: float, rel: float = 0.01, abs_tol: float = 0.02) -> bool:
    got = _num(actual)
    if got is None:
        return False
    return abs(got - expected) <= max(abs_tol, abs(expected) * rel)


def check_golden(
    master: dict[str, Any],
    sats: dict[str, Any] | None = None,
    require_gmp: bool = False,
) -> list[tuple[str, bool, str]]:
    results: list[tuple[str, bool, str]] = []

    def eq(field: str, expected: Any) -> None:
        got = master.get(field)
        ok = str(got).strip() == str(expected)
        results.append((field, ok, f"got {got!r} expected {expected!r}"))

    def approx(field: str, expected: float) -> None:
        got = master.get(field)
        ok = close_enough(got, expected)
        results.append((field, ok, f"got {got!r} expected ~{expected}"))

    def contains(field: str, needle: str) -> None:
        got = str(master.get(field) or "")
        ok = needle.lower() in got.lower()
        results.append((field, ok, f"got {got!r} should contain {needle!r}"))

    eq("ipo_id", GOLDEN["ipo_id"])
    eq("nse_symbol", GOLDEN["nse_symbol"])
    eq("bse_code", GOLDEN["bse_code"])
    eq("isin", GOLDEN["isin"])
    approx("issue_price", GOLDEN["issue_price"])
    approx("face_value", GOLDEN["face_value"])
    eq("listing_date", GOLDEN["listing_date"])
    contains("sale_type", GOLDEN["sale_type_contains"])
    for part in GOLDEN["listing_at_contains"]:
        contains("listing_at", part)
    eq("industry", GOLDEN["industry"])
    contains("company_name", "Lohia")
    approx("issue_size_cr", GOLDEN["issue_size_cr"])
    approx("roce", GOLDEN["roce"])
    approx("roe", GOLDEN["roe"])
    approx("sub_total_x", GOLDEN["sub_total_x"])
    approx("sub_qib_x", GOLDEN["sub_qib_x"])
    approx("sub_nii_x", GOLDEN["sub_nii_x"])
    approx("sub_retail_x", GOLDEN["sub_retail_x"])
    approx("list_open", GOLDEN["list_open"])
    approx("list_last", GOLDEN["list_last"])
    approx("promoter_pre_pct", GOLDEN["promoter_pre_pct"])
    approx("promoter_post_pct", GOLDEN["promoter_post_pct"])
    contains("registrar", GOLDEN["registrar_contains"])
    for part in GOLDEN["lead_contains"]:
        contains("lead_managers", part)

    if sats is not None:
        results.append(("fy1_pat", close_enough(master.get("fy1_pat"), 193.45), f"got {master.get('fy1_pat')!r}"))
        results.append(("fy2_pat", close_enough(master.get("fy2_pat"), 117.84), f"got {master.get('fy2_pat')!r}"))
        results.append(("listing_nse_open", close_enough(master.get("listing_nse_open"), 461), f"got {master.get('listing_nse_open')!r}"))
        results.append(("listing_bse_last", close_enough(master.get("listing_bse_last"), 494.65), f"got {master.get('listing_bse_last')!r}"))
        results.append(("ipos.csv", True, "1 row (checked by caller)"))
        if require_gmp:
            results.append(("gmp_rs", close_enough(master.get("gmp_rs"), 17), f"got {master.get('gmp_rs')!r}"))
            results.append(("gmp_est_listing_price", close_enough(master.get("gmp_est_listing_price"), 442), f"got {master.get('gmp_est_listing_price')!r}"))
            results.append(("gmp_close_date", str(master.get("gmp_close_date") or "") == "2026-07-30", f"got {master.get('gmp_close_date')!r}"))
            gmp_n = master.get("gmp_obs_count")
            results.append(("gmp_obs_count", bool(gmp_n), f"got {gmp_n!r}"))
    return results


def print_results(results: list[tuple[str, bool, str]]) -> bool:
    width = max(len(name) for name, _, _ in results)
    failed = 0
    print("\n=== SMOKE Lohia Corp 2574 ===")
    for name, ok, detail in results:
        mark = "PASS" if ok else "FAIL"
        if not ok:
            failed += 1
        print(f"  {mark:4}  {name.ljust(width)}  {detail}")
    print(f"\n{len(results) - failed}/{len(results)} checks passed")
    return failed == 0


def run_smoke(out_dir: Path | None = None, headed: bool = False) -> int:
    """Run the live Lohia check. Default: temp folder, deleted afterwards."""
    keep = out_dir is not None
    if out_dir is None:
        out_dir = Path(tempfile.mkdtemp(prefix="ipo-smoke-"))
    try:
        return _run_smoke(out_dir, headed=headed)
    finally:
        if not keep:
            shutil.rmtree(out_dir, ignore_errors=True)


def _run_smoke(out_dir: Path, headed: bool = False) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    masters = run_pipeline(
        out_dir=out_dir,
        from_year=SMOKE_YEAR,
        to_year=SMOKE_YEAR,
        exchanges=(SMOKE_EXCHANGE,),
        delay=1.0,
        resume=False,
        ipo_id=SMOKE_IPO_ID,
        fetch_gmp=True,
        headless=not headed,
    )
    if not masters:
        print("SMOKE FAIL: no master row written")
        return 1
    ipos_path = out_dir / "ipos.csv"
    if not ipos_path.exists():
        print("SMOKE FAIL: ipos.csv missing")
        return 1
    ipos = read_master(ipos_path)
    if len(ipos) != 1:
        print(f"SMOKE FAIL: ipos.csv has {len(ipos)} rows, expected 1")
        return 1
    first_line = ipos_path.read_text(encoding="utf-8-sig").splitlines()[0]
    if not first_line.startswith("Identity"):
        print("SMOKE FAIL: ipos.csv is missing the group header row")
        return 1
    xlsx = out_dir / "ipos.xlsx"
    if not xlsx.exists():
        print("SMOKE FAIL: ipos.xlsx missing")
        return 1
    master = masters[0]
    results = check_golden(master, sats={}, require_gmp=True)
    results.append(("group header", first_line.startswith("Identity"), first_line[:40]))
    results.append(("xlsx", xlsx.exists(), str(xlsx.name)))
    leftover = sorted(
        p.name
        for p in out_dir.glob("*.csv")
        if p.name not in {"ipos.csv", "failed.csv"}
    )
    leftover += sorted(p.name for p in out_dir.glob("*.xlsx") if p.name != "ipos.xlsx")
    results.append(("no satellite files", not leftover, leftover or "clean"))
    ok = print_results(results)
    cache = out_dir / "cache" / f"{SMOKE_IPO_ID}.html"
    fixture = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "lohia_2574.html"
    if cache.exists() and not fixture.exists():
        fixture.parent.mkdir(parents=True, exist_ok=True)
        fixture.write_bytes(cache.read_bytes())
        print(f"Wrote parser fixture {fixture}")
    return 0 if ok else 1
