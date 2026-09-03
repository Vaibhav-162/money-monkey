"""Fetch post-listing prices and Nifty for Strategy 2.

Run this yourself. The agent does not execute it.

  python scripts/fetch_prices.py --spot-check
  python scripts/fetch_prices.py --out data --workers 4
  python scripts/fetch_prices.py --out data --merge

Spot-check tries 10 mainboard + 10 SME tickers through:
  yfinance {nse_symbol}.NS -> {bse_code}.BO -> jugaad-data (optional)

Workers fetch missing data/prices/daily/{ipo_id}.parquet files. The parent
(or --merge) then builds data/prices/returns.csv locally from those parquets.

WHAT THIS FILE DOES
--------------------
Strategy 2 (quality / hold) is trained on what the stock actually did
after listing — especially ~6-month excess returns vs Nifty. Those
series do not come from Chittorgarh; this script is the only network
entry that downloads them. `analysis/prices.py` owns the Yahoo /
jugaad-data helpers and the returns join; this file is the CLI +
worker orchestration around those helpers (`listed_price_jobs`,
`missing_daily_ids`, `fetch_daily_bars`, `fetch_nifty`, `spot_check`,
`build_returns_table`).

This is a **manual, local** job. There is no GitHub Actions workflow.
README tells you to `--spot-check` first, then `--workers 4`;
`run_analysis.py` notes that until this has been run, Strategy 2
falls back to the quality-ranker (no price model). After a fetch,
re-run `python run_analysis.py --out data`.

`--workers N` re-invokes this same script via
`chittorgarh.shards.launch_worker_processes` so each child fills
missing `data/prices/daily/{ipo_id}.parquet` files. The parent (or a
later `--merge`) builds `data/prices/returns.csv` locally from those
parquets and also fetches Nifty (`^NSEI`) once. `--spot-check` writes
`data/prices/spot_check.json` and warns if the SME hit-rate is too
low to trust an SME price backtest.

KEY TERMS USED HERE
--------------------
- Strategy 2: the "is this worth holding past listing day?" decision.
  Needs post-listing daily bars plus Nifty so the trainer can compute
  excess returns; without this script it uses a fundamentals-only
  quality ranker.
- Mainboard vs SME: NSE/BSE listed issues vs the smaller SME boards.
  Yahoo coverage of SME tickers is patchy; the spot-check's per-board
  hit-rate tells you whether an SME S2 price backtest is honest.
- Nifty: the Nifty-50 index (`^NSEI`), fetched once per parent/serial
  run into `data/prices/nifty.parquet` so stock returns can be compared
  to the market over the same window.
- Daily parquet / `returns.csv`: one `{ipo_id}.parquet` of OHLCV bars
  per listed IPO; the merge step rolls those into the wide returns
  table `run_analysis.py` reads.

FUNCTIONS / CLASSES IN THIS FILE
---------------------------------
- `main(argv)`: CLI. `--spot-check` and `--merge` exit early.
  `--workers N` (parent) fetches Nifty, launches children, then writes
  returns. A child (`--shard`) only fills its slice of missing parquets.
- `_jobs(out_dir)`: listed rows from `ipos.csv` that have a listing date.
- `_write_returns(out_dir)`: rebuild `data/prices/returns.csv` from the
  daily parquet cache (no network).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from analysis.load import sanitize
from analysis.prices import (
    build_returns_table,
    fetch_daily_bars,
    fetch_nifty,
    listed_price_jobs,
    missing_daily_ids,
    spot_check,
)
from chittorgarh.export import read_master
from chittorgarh.shards import launch_worker_processes, shard_slice

SCRIPT = Path(__file__).resolve()


def _jobs(out_dir: Path):
    return listed_price_jobs(sanitize(read_master(out_dir / "ipos.csv")))


def _write_returns(out_dir: Path) -> int:
    prices_dir = out_dir / "prices"
    prices_dir.mkdir(parents=True, exist_ok=True)
    returns = build_returns_table(out_dir)
    dest = prices_dir / "returns.csv"
    returns.to_csv(dest, index=False)
    print(f"[merge] wrote {dest} rows={len(returns)}", flush=True)
    return len(returns)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Fetch Yahoo/jugaad post-listing prices (you run this).")
    p.add_argument("--out", default="data")
    p.add_argument("--spot-check", action="store_true")
    p.add_argument("--delay", type=float, default=0.4)
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--workers", type=int, default=1, help="Parallel processes fetching daily parquets.")
    p.add_argument("--shard", type=int, default=None)
    p.add_argument("--shards", type=int, default=None)
    p.add_argument("--merge", action="store_true", help="Rebuild returns.csv from daily parquets and exit.")
    args = p.parse_args(argv)

    out_dir = ROOT / args.out if not Path(args.out).is_absolute() else Path(args.out)
    prices_dir = out_dir / "prices"
    prices_dir.mkdir(parents=True, exist_ok=True)
    daily_dir = prices_dir / "daily"
    daily_dir.mkdir(parents=True, exist_ok=True)

    if args.spot_check:
        report = spot_check(out_dir, n_each=10)
        path = prices_dir / "spot_check.json"
        path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report["summary"], indent=2))
        print(f"wrote {path}")
        print("Read the summary. If SME hit-rate is under ~30%, Strategy 2 price backtest stays mainboard-only.")
        return 0

    if args.merge:
        _write_returns(out_dir)
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
        nifty = fetch_nifty(prices_dir)
        print(f"[parent] Nifty rows={0 if nifty is None else len(nifty)}", flush=True)
        extra = ["--out", str(out_dir), "--delay", str(args.delay)]
        if args.limit:
            extra.extend(["--limit", str(args.limit)])
        codes = launch_worker_processes(SCRIPT, args.workers, extra, cwd=ROOT)
        _write_returns(out_dir)
        fail_n = sum(1 for c in codes if c != 0)
        print(f"[parent] workers exit={codes}")
        print("Next: python run_analysis.py --out data")
        return 0 if fail_n == 0 else 2

    if args.shard is None:
        nifty = fetch_nifty(prices_dir)
        print(f"Nifty rows={0 if nifty is None else len(nifty)}", flush=True)

    jobs = _jobs(out_dir)
    missing = missing_daily_ids(daily_dir, jobs)
    if args.limit:
        missing = missing[: args.limit]
    shard = 0 if args.shard is None else args.shard
    shards = 1 if args.shards is None else args.shards
    mine_ids = shard_slice(missing, shard, shards)
    idset = set(mine_ids)
    order = {iid: i for i, iid in enumerate(mine_ids)}
    mine = jobs[jobs["ipo_id"].astype(str).isin(idset)].copy()
    if len(mine):
        mine["_ord"] = mine["ipo_id"].astype(str).map(order)
        mine = mine.sort_values("_ord").drop(columns="_ord")
    tag = "[serial]" if args.shard is None else f"[shard {shard}]"
    print(f"{tag} missing={len(missing)} this_shard={len(mine)}", flush=True)
    stats = fetch_daily_bars(mine, daily_dir, delay=args.delay, tag=tag)
    print(f"{tag} {stats}", flush=True)

    if args.shard is None:
        _write_returns(out_dir)
        print("Next: python run_analysis.py --out data")
    return 0


if __name__ == "__main__":
    sys.exit(main())
