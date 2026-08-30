"""Re-scrape daily InvestorGain GMP history for listed IPOs (2020+).

Run this yourself. The agent does not execute it.

  python scripts/rescrape_gmp_history.py --out data --resume --workers 4 --delay 1.5
  python scripts/rescrape_gmp_history.py --out data --merge

Workers write data/gmp_parts/shard_XX.csv, then the parent merges into
data/gmp_history.csv (many rows per ipo_id). Does not rewrite ipos.csv.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from chittorgarh.browser import chromium_session
from chittorgarh.export import append_or_replace, read_master
from chittorgarh.gmp import scrape_gmp_with_page
from chittorgarh.pipeline import persist_gmp_history
from chittorgarh.shards import (
    delete_paths,
    ids_from_paths,
    launch_worker_processes,
    merge_csv_replace_by_ipo_id,
    shard_slice,
)

SCRIPT = Path(__file__).resolve()


def _parts_dir(out_dir: Path) -> Path:
    return out_dir / "gmp_parts"


def _shard_csv(out_dir: Path, shard: int) -> Path:
    return _parts_dir(out_dir) / f"shard_{shard:02d}.csv"


def _failed_csv(out_dir: Path, shard: int | None = None) -> Path:
    if shard is None:
        return out_dir / "gmp_failed.csv"
    return _parts_dir(out_dir) / f"failed_{shard:02d}.csv"


def _resume_ids(out_dir: Path) -> set[str]:
    hist = out_dir / "gmp_history.csv"
    parts = sorted(_parts_dir(out_dir).glob("shard_*.csv"))
    return ids_from_paths([hist, *parts])


def merge_gmp_parts(out_dir: Path) -> int:
    parts_dir = _parts_dir(out_dir)
    shards = sorted(parts_dir.glob("shard_*.csv"))
    failed = sorted(parts_dir.glob("failed_*.csv"))
    n = merge_csv_replace_by_ipo_id(out_dir / "gmp_history.csv", shards)
    if failed:
        merge_csv_replace_by_ipo_id(out_dir / "gmp_failed.csv", failed)
    delete_paths([*shards, *failed])
    if parts_dir.exists() and not any(parts_dir.iterdir()):
        parts_dir.rmdir()
    print(f"[merge] {n} ipo_ids into {out_dir / 'gmp_history.csv'}", flush=True)
    return n


def run_shard(
    work,
    dest: Path,
    failed_dest: Path,
    delay: float,
    headed: bool,
    tag: str,
) -> tuple[int, int, int]:
    ok = empty = fail = 0
    n = len(work)
    if n == 0:
        return ok, empty, fail
    with chromium_session(headless=not headed) as context:
        for i, row in enumerate(work, 1):
            ipo_id = str(row["ipo_id"])
            print(
                f"{tag} [{i}/{n}] {row.get('company_name')} ({ipo_id}) year={row.get('listing_year')}",
                flush=True,
            )
            page = context.new_page()
            try:
                try:
                    history = scrape_gmp_with_page(page, str(row["url"]), ipo_id)
                    if history:
                        persist_gmp_history(dest.parent, ipo_id, history, dest=dest)
                        ok += 1
                        print(f"{tag}         {len(history)} GMP rows", flush=True)
                    else:
                        append_or_replace(
                            dest,
                            [{"ipo_id": ipo_id, "gmp_date": "", "gmp_rs": "", "gmp_missing": "1"}],
                            key="ipo_id",
                        )
                        empty += 1
                        print(f"{tag}         no GMP archive", flush=True)
                except Exception as exc:
                    fail += 1
                    print(f"{tag}         FAIL {exc}", flush=True)
                    append_or_replace(
                        failed_dest,
                        [{"ipo_id": ipo_id, "url": row.get("url"), "error": str(exc)[:2000]}],
                    )
            finally:
                try:
                    page.close()
                except Exception:
                    pass
            time.sleep(max(0.0, delay))
    return ok, empty, fail


def _work_frame(out_dir: Path, from_year: int, to_year: int):
    master_path = out_dir / "ipos.csv"
    if not master_path.exists():
        return None
    df = read_master(master_path)
    years = df["listing_year"].astype(str)
    mask = years.apply(lambda y: y.isdigit() and from_year <= int(y) <= to_year)
    return df.loc[mask, ["ipo_id", "url", "company_name", "listing_year"]].copy()


def _remaining_rows(work, done: set[str], limit: int):
    rows = [row for _, row in work.iterrows() if str(row["ipo_id"]) not in done]
    if limit:
        rows = rows[:limit]
    return rows


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Re-scrape daily GMP history from InvestorGain (2020+).")
    p.add_argument("--out", default="data")
    p.add_argument("--from-year", type=int, default=2020)
    p.add_argument("--to-year", type=int, default=2026)
    p.add_argument("--delay", type=float, default=1.5)
    p.add_argument("--resume", action="store_true", help="Skip ipo_ids already in gmp_history.csv or gmp_parts/")
    p.add_argument("--headed", action="store_true")
    p.add_argument("--limit", type=int, default=0, help="Cap remaining IPOs (applied before sharding).")
    p.add_argument("--workers", type=int, default=1, help="Parallel processes. Each writes its own shard CSV.")
    p.add_argument("--shard", type=int, default=None, help="This process's shard index (0-based).")
    p.add_argument("--shards", type=int, default=None, help="Total shard count. Required with --shard.")
    p.add_argument("--merge", action="store_true", help="Merge gmp_parts/ into gmp_history.csv and exit.")
    args = p.parse_args(argv)

    out_dir = ROOT / args.out if not Path(args.out).is_absolute() else Path(args.out)
    hist_path = out_dir / "gmp_history.csv"

    if args.merge:
        if not _parts_dir(out_dir).exists():
            print(f"nothing to merge under {_parts_dir(out_dir)}")
            return 0
        merge_gmp_parts(out_dir)
        print("Next: python run_analysis.py --out data")
        return 0

    if args.workers < 1:
        print("--workers must be >= 1")
        return 1
    if args.shard is not None and args.shards is None:
        print("--shard requires --shards")
        return 1
    if args.shards is not None and args.shard is None:
        print("--shards requires --shard")
        return 1

    if args.workers > 1 and args.shard is None:
        extra = [
            "--out", str(out_dir),
            "--from-year", str(args.from_year),
            "--to-year", str(args.to_year),
            "--delay", str(args.delay),
        ]
        if args.resume:
            extra.append("--resume")
        if args.headed:
            extra.append("--headed")
        if args.limit:
            extra.extend(["--limit", str(args.limit)])
        codes = launch_worker_processes(SCRIPT, args.workers, extra, cwd=ROOT)
        merge_gmp_parts(out_dir)
        fail_n = sum(1 for c in codes if c != 0)
        print(f"[parent] workers exit={codes} merged={hist_path}")
        print("Next: python run_analysis.py --out data")
        return 0 if fail_n == 0 else 2

    work = _work_frame(out_dir, args.from_year, args.to_year)
    if work is None:
        print(f"missing {out_dir / 'ipos.csv'}; scrape the master sheet first")
        return 1

    done = _resume_ids(out_dir) if args.resume else set()
    remaining = _remaining_rows(work, done, args.limit)
    shard = 0 if args.shard is None else args.shard
    shards = 1 if args.shards is None else args.shards
    mine = shard_slice(remaining, shard, shards)

    dest = hist_path if args.shard is None else _shard_csv(out_dir, shard)
    failed_dest = _failed_csv(out_dir) if args.shard is None else _failed_csv(out_dir, shard)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tag = "[serial]" if args.shard is None else f"[shard {shard}]"

    print(f"{tag} remaining={len(remaining)} this_shard={len(mine)} dest={dest}", flush=True)
    ok, empty, fail = run_shard(mine, dest, failed_dest, args.delay, args.headed, tag)
    print(f"{tag} done scraped={ok} empty={empty} fail={fail} dest={dest}")
    if args.shard is None:
        print("Next: python run_analysis.py --out data")
    return 0 if fail == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
