from __future__ import annotations

from pathlib import Path

import pytest

from chittorgarh.export import read_master
from chittorgarh.smoke import SMOKE_IPO_ID, run_smoke


@pytest.mark.smoke
def test_lohia_end_to_end(tmp_path: Path) -> None:
    code = run_smoke(tmp_path, headed=False)
    assert code == 0, "Lohia Corp 2574 smoke test failed (see printed PASS/FAIL table)"
    ipos = read_master(tmp_path / "ipos.csv")
    assert len(ipos) == 1
    assert str(ipos.iloc[0]["ipo_id"]) == SMOKE_IPO_ID
    assert (tmp_path / "ipos.xlsx").exists()
    leftover = [
        p.name
        for p in tmp_path.glob("*.csv")
        if p.name not in {"ipos.csv", "failed.csv"}
    ]
    assert leftover == [], leftover
