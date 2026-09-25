"""Fill blank close-day GMP on the live audit log from InvestorGain history.

Does not re-score and does not send alerts. p_pop / apply_s1 stay as mailed.
Only gmp_rs, gmp_pct, gmp_as_of, gmp_date_raw, sub_ig_x are filled, and only
from a history row dated on or before that IPO's close_date.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from analysis.live_audit import read_audit, write_audit
from chittorgarh.browser import chromium_session
from chittorgarh.gmp import last_gmp_on_or_before, scrape_gmp_with_page

BLANK_FROM = "2026-09-21"
BLANK_TO = "2026-09-25"


def _blank(value: object) -> bool:
    text = "" if value is None else str(value).strip()
    return text == "" or text.lower() == "nan"


def main() -> int:
    path = ROOT / "data" / "live_audit_log.csv"
    frame = read_audit(path)
    pending = frame[
        (frame["close_date"] >= BLANK_FROM)
        & (frame["close_date"] <= BLANK_TO)
        & frame["gmp_rs"].map(_blank)
    ]
    print(f"[backfill] rows={len(pending)}")
    if pending.empty:
        return 0
    with chromium_session() as context:
        page = context.new_page()
        try:
            for idx, row in pending.iterrows():
                url = str(row.get("url") or "")
                ipo_id = str(row.get("ipo_id") or "")
                close_date = str(row.get("close_date") or "")
                name = row.get("company_name")
                try:
                    history = scrape_gmp_with_page(page, url, ipo_id)
                    stamped = last_gmp_on_or_before(history, close_date)
                except Exception as exc:
                    frame.at[idx, "error"] = f"gmp_backfill:{exc}"[:500]
                    print(f"[backfill] FAIL {name}: {exc}")
                    continue
                if stamped.get("gmp_rs") is None:
                    note = "gmp_backfill:no row on or before close_date"
                    prior = str(row.get("error") or "").strip()
                    frame.at[idx, "error"] = f"{prior}; {note}".strip("; ") if prior else note
                    print(f"[backfill] MISS {name} history={len(history)}")
                    continue
                frame.at[idx, "gmp_rs"] = stamped.get("gmp_rs")
                frame.at[idx, "gmp_pct"] = stamped.get("gmp_pct")
                frame.at[idx, "gmp_as_of"] = stamped.get("gmp_close_date")
                frame.at[idx, "gmp_date_raw"] = stamped.get("gmp_date_raw")
                if _blank(row.get("sub_ig_x")) and stamped.get("sub_ig_x") is not None:
                    frame.at[idx, "sub_ig_x"] = stamped.get("sub_ig_x")
                frame.at[idx, "gmp_backfilled"] = True
                print(f"[backfill] {name} gmp={stamped.get('gmp_rs')} as_of={stamped.get('gmp_close_date')}")
        finally:
            page.close()
    write_audit(path, frame)
    print(f"[backfill] wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
