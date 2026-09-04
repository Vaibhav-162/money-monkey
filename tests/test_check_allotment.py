from datetime import date
from pathlib import Path

import pandas as pd
import pytest
import smtplib
from bs4 import BeautifulSoup

from analysis.live_audit import AUDIT_COLUMNS, read_audit, write_audit
from chittorgarh.parse_ipo import parse_allotment_published
from chittorgarh.registrar_allotment import RegistrarLookupBatchError
from scripts.check_allotment import (
    dispatch_allotment,
    dispatch_pan_results,
    is_allotment_due,
    run_check,
)
from scripts.notify import NotificationDeliveryError


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
        profiles=[],
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
        profiles=[],
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
        profiles=[],
    )
    assert newly == []
    assert sent == []


def test_run_check_hybrid_emails_and_masked_telegram(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "live_audit_log.csv"
    write_audit(
        path,
        pd.DataFrame([_row(registrar="KFin Technologies Limited", company_name="Test Co")]),
    )
    emails: list[tuple[str, str, str]] = []
    telegrams: list[str] = []

    def fake_email(subject, body, to_addr=None, **kw):
        emails.append((subject, body, to_addr or ""))
        return True

    monkeypatch.setattr("scripts.check_allotment.send_email", fake_email)
    monkeypatch.setattr("scripts.check_allotment.send_telegram", lambda text, **kw: telegrams.append(text) or True)

    def fake_checker(page, company, pan):
        if pan == "ABCDE1234F":
            return {"status": "allotted", "shares": 100}
        return {"status": "captcha_failed", "shares": None}

    monkeypatch.setattr("scripts.check_allotment.checker_for_registrar", lambda name: fake_checker)
    profiles = [
        {"label": "Dad", "pan": "ABCDE1234F", "email": "dad@example.com"},
        {"label": "Me", "pan": "PQRST5678G", "email": "me@example.com"},
    ]
    newly = run_check(
        out_dir=tmp_path,
        audit_path=path,
        client=FakeClient(PUBLISHED_HTML),
        as_of=date(2026, 8, 30),
        profiles=profiles,
        page=object(),
    )
    assert [r["company_name"] for r in newly] == ["Test Co"]
    assert len(emails) == 1
    assert emails[0][2] == "dad@example.com"
    dad = emails[0]
    assert "ALLOTTED" in dad[1]
    assert "100" in dad[1]
    assert len(telegrams) == 1
    summary = telegrams[0]
    assert "1 emailed" in summary
    assert "1 skipped" in summary
    assert "ABCDE1234F" not in summary
    assert "PQRST5678G" not in summary
    assert "dad@example.com" not in summary
    assert "Dad" not in summary
    assert "ALLOTTED" not in summary
    assert ">100<" not in summary and "Shares allotted: 100" not in summary
    saved = (tmp_path / "live_audit_log.csv").read_text(encoding="utf-8")
    assert "ABCDE1234F" not in saved
    assert "PQRST5678G" not in saved
    assert "dad@example.com" not in saved
    from analysis.live_audit import AUDIT_COLUMNS, read_audit

    cols = list(read_audit(path).columns)
    assert cols == AUDIT_COLUMNS


def test_run_check_unsupported_registrar_is_silent(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "live_audit_log.csv"
    write_audit(
        path,
        pd.DataFrame([_row(registrar="Bigshare Services Pvt.Ltd.", company_name="Test Co")]),
    )
    emails: list[tuple[str, str, str]] = []
    telegrams: list[str] = []
    calls = []

    def boom_checker(*args, **kwargs):
        calls.append(1)
        raise AssertionError("unsupported registrar must not call a checker")

    def capture_email(subject, body, to_addr=None, **kw):
        emails.append((subject, body, to_addr or ""))
        return True

    monkeypatch.setattr("scripts.check_allotment.checker_for_registrar", lambda name: None)
    monkeypatch.setattr("scripts.check_allotment.send_email", capture_email)
    monkeypatch.setattr("scripts.check_allotment.send_telegram", lambda text, **kw: telegrams.append(text) or True)
    monkeypatch.setattr("chittorgarh.registrar_allotment.check_kfintech", boom_checker)

    run_check(
        out_dir=tmp_path,
        audit_path=path,
        client=FakeClient(PUBLISHED_HTML),
        as_of=date(2026, 8, 30),
        profiles=[{"label": "Me", "pan": "ABCDE1234F", "email": "me@example.com"}],
        page=object(),
    )
    assert emails == []
    assert telegrams == []
    assert calls == []
    saved = (tmp_path / "live_audit_log.csv").read_text(encoding="utf-8")
    assert "ABCDE1234F" not in saved


def test_run_check_reads_pan_profiles_from_env(tmp_path: Path, monkeypatch) -> None:
    import json

    path = tmp_path / "live_audit_log.csv"
    write_audit(
        path,
        pd.DataFrame([_row(registrar="KFin Technologies Limited", company_name="Test Co")]),
    )
    monkeypatch.setenv(
        "PAN_PROFILES",
        json.dumps([{"label": "Me", "pan": "ABCDE1234F", "email": "me@example.com"}]),
    )
    emails: list[str] = []
    telegrams: list[str] = []
    monkeypatch.setattr(
        "scripts.check_allotment.send_email",
        lambda subject, body, to_addr=None, **kw: emails.append(to_addr or "") or True,
    )
    monkeypatch.setattr(
        "scripts.check_allotment.send_telegram",
        lambda text, **kw: telegrams.append(text) or True,
    )
    monkeypatch.setattr(
        "scripts.check_allotment.checker_for_registrar",
        lambda name: (lambda page, company, pan: {"status": "company_not_found", "shares": None}),
    )
    run_check(
        out_dir=tmp_path,
        audit_path=path,
        client=FakeClient(PUBLISHED_HTML),
        as_of=date(2026, 8, 30),
        page=object(),
    )
    assert emails == []
    assert telegrams == []


def test_run_check_lookup_failed_is_silent(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "live_audit_log.csv"
    write_audit(
        path,
        pd.DataFrame([_row(registrar="KFin Technologies Limited", company_name="Test Co")]),
    )
    emails: list[tuple[str, str, str]] = []
    telegrams: list[str] = []
    monkeypatch.setattr(
        "scripts.check_allotment.send_email",
        lambda subject, body, to_addr=None, **kw: emails.append((subject, body, to_addr or "")) or True,
    )
    monkeypatch.setattr("scripts.check_allotment.send_telegram", lambda text, **kw: telegrams.append(text) or True)
    monkeypatch.setattr(
        "scripts.check_allotment.checker_for_registrar",
        lambda name: (lambda page, company, pan: {"status": "lookup_failed", "shares": None}),
    )
    run_check(
        out_dir=tmp_path,
        audit_path=path,
        client=FakeClient(PUBLISHED_HTML),
        as_of=date(2026, 8, 30),
        profiles=[{"label": "Me", "pan": "ABCDE1234F", "email": "me@example.com"}],
        page=object(),
    )
    assert emails == []
    assert telegrams == []


def test_run_check_no_application_is_silent_but_marks_notified(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "live_audit_log.csv"
    write_audit(
        path,
        pd.DataFrame([_row(registrar="KFin Technologies Limited", company_name="Test Co")]),
    )
    emails: list[tuple[str, str, str]] = []
    telegrams: list[str] = []
    monkeypatch.setattr(
        "scripts.check_allotment.send_email",
        lambda subject, body, to_addr=None, **kw: emails.append((subject, body, to_addr or "")) or True,
    )
    monkeypatch.setattr("scripts.check_allotment.send_telegram", lambda text, **kw: telegrams.append(text) or True)
    monkeypatch.setattr(
        "scripts.check_allotment.checker_for_registrar",
        lambda name: (lambda page, company, pan: {"status": "no_application", "shares": None}),
    )
    newly = run_check(
        out_dir=tmp_path,
        audit_path=path,
        client=FakeClient(PUBLISHED_HTML),
        as_of=date(2026, 8, 30),
        profiles=[{"label": "Me", "pan": "ABCDE1234F", "email": "me@example.com"}],
        page=object(),
    )
    assert [r["company_name"] for r in newly] == ["Test Co"]
    assert emails == []
    assert telegrams == []
    saved = read_audit(path)
    assert str(saved.iloc[0]["allotment_notified"]).strip().lower() in {"true", "1", "yes"}


def test_run_check_allotted_and_no_application_emails_one(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "live_audit_log.csv"
    write_audit(
        path,
        pd.DataFrame([_row(registrar="KFin Technologies Limited", company_name="Test Co")]),
    )
    emails: list[tuple[str, str, str]] = []
    telegrams: list[str] = []
    monkeypatch.setattr(
        "scripts.check_allotment.send_email",
        lambda subject, body, to_addr=None, **kw: emails.append((subject, body, to_addr or "")) or True,
    )
    monkeypatch.setattr("scripts.check_allotment.send_telegram", lambda text, **kw: telegrams.append(text) or True)

    def fake_checker(page, company, pan):
        if pan == "ABCDE1234F":
            return {"status": "allotted", "shares": 100}
        return {"status": "no_application", "shares": None}

    monkeypatch.setattr("scripts.check_allotment.checker_for_registrar", lambda name: fake_checker)
    run_check(
        out_dir=tmp_path,
        audit_path=path,
        client=FakeClient(PUBLISHED_HTML),
        as_of=date(2026, 8, 30),
        profiles=[
            {"label": "Dad", "pan": "ABCDE1234F", "email": "dad@example.com"},
            {"label": "Me", "pan": "PQRST5678G", "email": "me@example.com"},
        ],
        page=object(),
    )
    assert len(emails) == 1
    assert emails[0][2] == "dad@example.com"
    assert "ALLOTTED" in emails[0][1]
    assert len(telegrams) == 1
    summary = telegrams[0]
    assert "1 emailed" in summary
    assert "1 skipped" in summary
    assert "ABCDE1234F" not in summary
    assert "PQRST5678G" not in summary
    assert "dad@example.com" not in summary
    assert "Dad" not in summary
    assert "ALLOTTED" not in summary


def test_allotment_manual_dispatch_defaults_to_dry_run() -> None:
    # Same rationale as the daily scanner: a stray manual test must not
    # write real notification flags before the real 12:00 PM IST tick runs.
    text = Path(".github/workflows/check_allotment.yml").read_text(encoding="utf-8")
    assert "workflow_dispatch:" in text
    assert "dry_run:" in text
    assert "default: true" in text
    assert "--dry-run" in text
    assert "github.event_name == 'workflow_dispatch' && inputs.dry_run" in text


def test_allotment_has_no_github_schedule() -> None:
    text = Path(".github/workflows/check_allotment.yml").read_text(encoding="utf-8")
    assert "schedule:" not in text
    assert 'cron: "30 6 * * 1-5"' not in text
    assert "repository_dispatch:" in text
    assert "types: [trigger-check-allotment]" in text


def test_dispatch_allotment_raises_when_telegram_and_email_both_fail(monkeypatch) -> None:
    def telegram_boom(text, **kw):
        raise RuntimeError("telegram down")

    def email_boom(subject, body, **kw):
        raise smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted")

    monkeypatch.setattr("scripts.check_allotment.send_telegram", telegram_boom)
    monkeypatch.setattr("scripts.check_allotment.send_email", email_boom)
    record = {"company_name": "Test Co", "registrar": "KFin"}
    with pytest.raises(NotificationDeliveryError):
        dispatch_allotment(record, dry_run=False)


def test_dispatch_allotment_does_not_raise_when_both_unconfigured(monkeypatch) -> None:
    monkeypatch.setattr("scripts.check_allotment.send_telegram", lambda text, **kw: False)
    monkeypatch.setattr("scripts.check_allotment.send_email", lambda *a, **kw: False)
    dispatch_allotment({"company_name": "Test Co"}, dry_run=False)


def test_dispatch_pan_results_raises_when_all_profile_emails_fail(monkeypatch) -> None:
    profiles = [
        {"label": "Dad", "pan": "ABCDE1234F", "email": "dad@example.com"},
        {"label": "Me", "pan": "PQRST5678G", "email": "me@example.com"},
    ]

    def email_boom(subject, body, to_addr=None, **kw):
        raise smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted")

    monkeypatch.setattr("scripts.check_allotment.send_email", email_boom)
    monkeypatch.setattr(
        "scripts.check_allotment.checker_for_registrar",
        lambda name: (lambda page, company, pan: {"status": "allotted", "shares": 100}),
    )
    record = {"company_name": "Test Co", "registrar": "KFin Technologies"}
    with pytest.raises(NotificationDeliveryError):
        dispatch_pan_results(record, profiles, page=object(), dry_run=False)


def test_dispatch_pan_results_raises_when_every_lookup_fails(monkeypatch) -> None:
    profiles = [
        {"label": "Dad", "pan": "ABCDE1234F", "email": "dad@example.com"},
        {"label": "Me", "pan": "PQRST5678G", "email": "me@example.com"},
    ]
    monkeypatch.setattr(
        "scripts.check_allotment.checker_for_registrar",
        lambda name: (lambda page, company, pan: {"status": "captcha_failed", "shares": None}),
    )
    record = {"company_name": "Test Co", "registrar": "KFin Technologies"}
    with pytest.raises(RegistrarLookupBatchError):
        dispatch_pan_results(record, profiles, page=object(), dry_run=False)


def test_dispatch_pan_results_does_not_raise_when_one_email_succeeds(monkeypatch) -> None:
    profiles = [
        {"label": "Dad", "pan": "ABCDE1234F", "email": "dad@example.com"},
        {"label": "Me", "pan": "PQRST5678G", "email": "me@example.com"},
    ]
    calls: list[str] = []

    def mixed_email(subject, body, to_addr=None, **kw):
        calls.append(to_addr or "")
        if to_addr == "dad@example.com":
            return True
        raise smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted")

    telegrams: list[str] = []
    monkeypatch.setattr("scripts.check_allotment.send_email", mixed_email)
    monkeypatch.setattr(
        "scripts.check_allotment.send_telegram",
        lambda text, **kw: telegrams.append(text) or True,
    )
    monkeypatch.setattr(
        "scripts.check_allotment.checker_for_registrar",
        lambda name: (lambda page, company, pan: {"status": "not_allotted", "shares": None}),
    )
    record = {"company_name": "Test Co", "registrar": "KFin Technologies"}
    out = dispatch_pan_results(record, profiles, page=object(), dry_run=False)
    assert out["n_emailed"] == 1
    assert len(telegrams) == 1
    assert "1 emailed" in telegrams[0]


def test_run_check_does_not_mark_notified_when_dispatch_fails(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "live_audit_log.csv"
    write_audit(path, pd.DataFrame([_row()]))
    monkeypatch.setattr(
        "scripts.check_allotment.dispatch_allotment",
        lambda rec, dry_run=False: (_ for _ in ()).throw(
            NotificationDeliveryError("both channels down")
        ),
    )
    with pytest.raises(NotificationDeliveryError):
        run_check(
            out_dir=tmp_path,
            audit_path=path,
            client=FakeClient(PUBLISHED_HTML),
            as_of=date(2026, 8, 30),
            profiles=[],
        )
    saved = read_audit(path)
    assert str(saved.iloc[0]["allotment_notified"]).strip().lower() not in {"true", "1", "yes"}

    # Retry after fixing credentials should still attempt dispatch.
    sent: list[str] = []
    monkeypatch.setattr(
        "scripts.check_allotment.dispatch_allotment",
        lambda rec, dry_run=False: sent.append(rec["company_name"]),
    )
    first = run_check(
        out_dir=tmp_path,
        audit_path=path,
        client=FakeClient(PUBLISHED_HTML),
        as_of=date(2026, 8, 30),
        profiles=[],
    )
    assert sent == ["Test Co"]
    assert [r["company_name"] for r in first] == ["Test Co"]

