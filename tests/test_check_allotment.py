from datetime import date
from pathlib import Path

import pandas as pd
from bs4 import BeautifulSoup

from analysis.live_audit import AUDIT_COLUMNS, write_audit
from chittorgarh.parse_ipo import parse_allotment_published
from scripts.check_allotment import is_allotment_due, run_check


PUBLISHED_HTML = """
<html><body>
<h1>Test Co IPO</h1>
<a href="/ipo/test-co/1/basis-of-allotment">Basis of Allotment</a>
<p>Registrar: KFin Technologies Limited</p>
</body></html>
"""

PENDING_HTML = """
<html><body>
<h1>Test Co IPO</h1>
<p>IPO Close: August 27, 2026</p>
<p>Allotment: September 05, 2026</p>
</body></html>
"""


class FakeClient:
    def __init__(self, html: str):
        self.html = html
        self.calls = 0

    def get_text(self, url, cache_name=None, use_cache=True):
        self.calls += 1
        return self.html

    def close(self):
        return None


def _row(**extra):
    rec = {col: "" for col in AUDIT_COLUMNS}
    rec.update(
        {
            "ipo_id": "1",
            "company_name": "Test Co",
            "board": "mainboard",
            "close_date": "2026-08-27",
            "url": "https://www.chittorgarh.com/ipo/test-co/1/",
            "allotment_notified": False,
        }
    )
    rec.update(extra)
    return rec


def test_parse_allotment_published_detects_basis_link() -> None:
    assert parse_allotment_published(BeautifulSoup(PUBLISHED_HTML, "lxml")) is True
    assert parse_allotment_published(BeautifulSoup(PENDING_HTML, "lxml")) is False


def test_is_allotment_due_window() -> None:
    as_of = date(2026, 8, 30)
    assert is_allotment_due(_row(close_date="2026-08-27"), as_of) is True
    assert is_allotment_due(_row(close_date="2026-08-30"), as_of) is False
    assert is_allotment_due(_row(close_date="2026-08-20"), as_of) is False
    assert is_allotment_due(_row(close_date="2026-08-27", allotment_notified=True), as_of) is False


def test_run_check_notifies_once(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "live_audit_log.csv"
    write_audit(path, pd.DataFrame([_row()]))
    sent = []
    monkeypatch.setattr("scripts.check_allotment.dispatch_allotment", lambda rec, dry_run=False: sent.append(rec["company_name"]))
    client = FakeClient(PUBLISHED_HTML)
    first = run_check(
        out_dir=tmp_path,
        audit_path=path,
        client=client,
        as_of=date(2026, 8, 30),
    )
    assert [r["company_name"] for r in first] == ["Test Co"]
    assert sent == ["Test Co"]
    assert client.calls == 1

    client2 = FakeClient(PUBLISHED_HTML)
    second = run_check(
        out_dir=tmp_path,
        audit_path=path,
        client=client2,
        as_of=date(2026, 8, 30),
    )
    assert second == []
    assert client2.calls == 0
    assert sent == ["Test Co"]


def test_run_check_pending_page_does_not_notify(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "live_audit_log.csv"
    write_audit(path, pd.DataFrame([_row()]))
    sent = []
    monkeypatch.setattr("scripts.check_allotment.dispatch_allotment", lambda rec, dry_run=False: sent.append(1))
    newly = run_check(
        out_dir=tmp_path,
        audit_path=path,
        client=FakeClient(PENDING_HTML),
        as_of=date(2026, 8, 30),
    )
    assert newly == []
    assert sent == []
