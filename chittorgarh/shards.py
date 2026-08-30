"""Process-shard helpers for parallel network jobs. No scraping here."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable, Sequence, TypeVar

import pandas as pd

from chittorgarh.export import append_or_replace

T = TypeVar("T")

EMPTY_IDS = {"", "nan", "None", "none", "<NA>", "<na>"}


def shard_slice(items: Sequence[T], shard: int, shards: int) -> list[T]:
    """Round-robin partition: item i goes to shard i % shards."""
    if shards < 1:
        raise ValueError(f"shards must be >= 1, got {shards}")
    if shard < 0 or shard >= shards:
        raise ValueError(f"shard must be in [0, {shards}), got {shard}")
    return [item for i, item in enumerate(items) if i % shards == shard]


def ids_from_csv(path: Path) -> set[str]:
    if not path.exists() or path.stat().st_size == 0:
        return set()
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    if "ipo_id" not in df.columns:
        return set()
    return {str(v) for v in df["ipo_id"] if str(v).strip() not in EMPTY_IDS}


def ids_from_paths(paths: Iterable[Path]) -> set[str]:
    done: set[str] = set()
    for path in paths:
        done |= ids_from_csv(path)
    return done


def merge_csv_replace_by_ipo_id(dest: Path, parts: Sequence[Path]) -> int:
    """Replace dest rows for every ipo_id present in parts, keeping every date-row.

    gmp_history.csv is many rows per ipo_id. A naive drop_duplicates would
    collapse an IPO's archive to one row. append_or_replace already drops the
    old block for those ids and writes the new block in full.
    """
    frames: list[pd.DataFrame] = []
    for path in parts:
        if not path.exists() or path.stat().st_size == 0:
            continue
        df = pd.read_csv(path, dtype=str, keep_default_na=False)
        if df.empty or "ipo_id" not in df.columns:
            continue
        frames.append(df)
    if not frames:
        return 0
    new = pd.concat(frames, ignore_index=True)
    rows: list[dict[str, Any]] = new.to_dict(orient="records")
    append_or_replace(dest, rows, key="ipo_id")
    return int(new["ipo_id"].nunique())


def delete_paths(paths: Iterable[Path]) -> None:
    for path in paths:
        if path.exists():
            path.unlink()


def launch_worker_processes(script: Path, n: int, extra: Sequence[str], cwd: Path) -> list[int]:
    """Spawn N copies of script with --shard i --shards N plus extra flags. Inherit stdout."""
    procs = []
    for i in range(n):
        cmd = [sys.executable, str(script), "--shard", str(i), "--shards", str(n), *extra]
        print(f"[parent] starting shard {i}/{n}", flush=True)
        procs.append(subprocess.Popen(cmd, cwd=str(cwd)))
    return [p.wait() for p in procs]
