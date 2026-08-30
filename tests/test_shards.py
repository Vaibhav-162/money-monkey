from pathlib import Path

from chittorgarh.shards import (
    ids_from_paths,
    merge_csv_replace_by_ipo_id,
    shard_slice,
)


def test_shard_slice_is_partition():
    items = [f"id{i}" for i in range(10)]
    parts = [shard_slice(items, s, 3) for s in range(3)]
    flat = [x for part in parts for x in part]
    assert sorted(flat) == sorted(items)
    assert len(set(flat)) == len(items)
    assert parts[0] == ["id0", "id3", "id6", "id9"]
    assert parts[1] == ["id1", "id4", "id7"]
    assert parts[2] == ["id2", "id5", "id8"]
    for a in range(3):
        for b in range(a + 1, 3):
            assert set(parts[a]).isdisjoint(set(parts[b]))


def test_merge_replaces_whole_ipo_block_not_one_row(tmp_path: Path):
    dest = tmp_path / "gmp_history.csv"
    dest.write_text(
        "ipo_id,gmp_date,gmp_rs\nA,2024-01-01,10\nA,2024-01-02,12\nB,2024-01-01,5\n",
        encoding="utf-8",
    )
    shard = tmp_path / "shard_00.csv"
    shard.write_text(
        "ipo_id,gmp_date,gmp_rs\nA,2024-01-03,20\nA,2024-01-04,21\nA,2024-01-05,22\n",
        encoding="utf-8",
    )
    n = merge_csv_replace_by_ipo_id(dest, [shard])
    assert n == 1
    text = dest.read_text(encoding="utf-8")
    assert text.count("\nA,") == 3
    assert "A,2024-01-01" not in text
    assert "A,2024-01-03,20" in text
    assert "B,2024-01-01,5" in text


def test_resume_union_of_merged_and_parts(tmp_path: Path):
    hist = tmp_path / "gmp_history.csv"
    hist.write_text("ipo_id,gmp_date\n111,2024-01-01\n111,2024-01-02\n", encoding="utf-8")
    part = tmp_path / "shard_00.csv"
    part.write_text("ipo_id,gmp_date\n222,2024-02-01\n", encoding="utf-8")
    done = ids_from_paths([hist, part])
    assert done == {"111", "222"}
