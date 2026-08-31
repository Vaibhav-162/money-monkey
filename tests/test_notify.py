import smtplib

from scripts.notify import (
    _app_password,
    _env,
    _redact,
    dispatch,
    format_allotment_card,
    format_card,
    format_email_digest,
    registrar_portal_url,
    send_email,
    send_telegram,
)


def _record(**extra):
    rec = {
        "company_name": "Lumino Industries",
        "board": "mainboard",
        "close_date": "2026-08-31",
        "price_band_high": 82,
        "lot_size": 182,
        "gmp_rs": 63,
        "gmp_pct": 76.8,
        "sub_total_x": 5.14,
        "apply_s1": True,
        "ev_retail": 2489.0,
        "p_allot": 0.195,
        "p_pop": 0.827,
        "apply_s2": False,
        "quality_score": 2,
        "quality_breakdown": [
            {"name": "subscription", "status": "fail", "awarded": False},
            {"name": "ofs_ratio", "status": "pass", "awarded": True},
            {"name": "roe", "status": "pass", "awarded": True},
            {"name": "debt_equity", "status": "fail", "awarded": False},
        ],
    }
    rec.update(extra)
    return rec


def test_format_card_apply_layout_and_human_checklist() -> None:
    cards = format_card(_record())
    html = cards["html"]
    assert "parse_mode" not in html
    assert "🏢" in html
    assert "<b>Lumino Industries</b>" in html
    assert "(Mainboard)" in html
    assert "Closes:</b> 2026-08-31" in html
    assert "₹82 / share" in html
    assert "Lot Size:</b> 182" in html
    assert "Grey Market Premium is currently ₹63" in html
    assert "5.14x" in html
    assert "APPLY FOR LISTING GAINS" in html
    assert "82.7%" in html
    assert "19.5%" in html
    assert "₹2,489" in html
    assert "0.827%" not in html
    assert "QIB not available" in html
    assert "MODERATE (Score 2/4) - MARKET UNCLEAR" in html
    assert "❌ Retail Demand (&gt;20x): FAIL" in html
    assert "✅ Fresh Capital / Low OFS: PASS" in html
    assert "✅ Return on Equity (&gt;15%): PASS" in html
    assert "❌ Low Debt-to-Equity: FAIL" in html
    assert "sub_gt_20" not in html
    assert "ofs_lt_50" not in html
    assert "roe_gt_15" not in html
    assert "de_le_05" not in html


def test_checklist_not_disclosed_uses_question_icon() -> None:
    html = format_card(
        _record(
            quality_breakdown=[
                {"name": "subscription", "status": "fail"},
                {"name": "ofs_ratio", "status": "not_disclosed"},
                {"name": "roe", "status": "pass"},
                {"name": "debt_equity", "status": "fail"},
            ]
        )
    )["html"]
    assert "❌ Retail Demand (&gt;20x): FAIL" in html
    assert "❓ Fresh Capital / Low OFS: NOT DISCLOSED" in html
    assert "✅ Return on Equity (&gt;15%): PASS" in html
    assert "❌ Low Debt-to-Equity: FAIL" in html
    assert "not investment advice" in html.lower()
    assert "<b>" in html
    assert "<i>" in html


def test_format_card_skip_and_partial_hold_copy() -> None:
    html = format_card(
        _record(apply_s1=False, apply_s2=True, quality_score=3, p_pop=0.184, ev_retail=1394)
    )["html"]
    assert "🔴 <b>SKIP</b>" in html
    assert "PARTIAL HOLD (Score 3/4)" in html
    assert "Book 50% profit" in html
    assert "10% trailing stop-loss" in html
    assert "advises against applying" in html
    assert "18.4%" in html
    assert "₹1,394" in html
    assert "justifies locking up the capital" not in html


def test_s2_branches_by_score_and_regime() -> None:
    strong = format_card(_record(quality_score=4, apply_s2=True))["html"]
    assert "PARTIAL HOLD (Score 4/4)" in strong
    assert "50% profit" in strong

    bull = format_card(_record(quality_score=2, market_regime="BULLISH"))["html"]
    assert "MARKET TAILWIND" in bull
    assert "5% stop-loss" in bull

    bear = format_card(_record(quality_score=2, market_regime="BEARISH"))["html"]
    assert "MARKET HEADWIND" in bear
    assert "Sell 100%" in bear

    weak = format_card(_record(quality_score=1, apply_s2=False))["html"]
    assert "FLIP ONLY (Score 1/4)" in weak
    assert "Liquidate 100%" in weak


def test_rank_banner_and_qib() -> None:
    html = format_card(_record(rank_of_day=1, rank_total_of_day=3, sub_qib_x=12.3))["html"]
    assert "Rank 1 of 3 Closing Today" in html
    assert "QIB 12.30x" in html
    assert format_card(_record())["html"].count("Rank") == 0


def test_format_card_stamps_fetch_time_and_dual_sub() -> None:
    html = format_card(
        _record(
            scraped_at_ist="31-Aug 15:41 IST",
            scraped_at_utc="2026-08-31T10:11:00Z",
            gmp_as_of="2026-08-31",
            gmp_date_raw="31-08-2026 Close",
            sub_ig_x=97.92,
            sub_total_x=150.09,
        )
    )["html"]
    assert "Fetched:</b> 31-Aug 15:41 IST" in html
    assert "live snapshot" in html
    assert "Grey Market Premium is currently ₹63 (InvestorGain, as of 31-08-2026)" in html
    assert "150.09x Chittorgarh" in html
    assert "97.92x InvestorGain" in html
    assert "Chittorgarh" in html
    assert format_card(_record())["html"].count("Fetched:") == 0
    only_chit = format_card(_record(sub_total_x=5.14))["html"]
    assert "5.14x Chittorgarh" in only_chit
    assert "InvestorGain" in only_chit  # GMP source
    assert "x InvestorGain" not in only_chit


def test_format_card_missing_gmp_is_explicit() -> None:
    html = format_card(_record(gmp_rs=None, gmp_pct=None, apply_s1=False))["html"]
    assert "not available" in html
    assert "🔴 <b>SKIP</b>" in html
    assert "PARTIAL HOLD" not in html


def test_telegram_and_email_noop_without_secrets(monkeypatch) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.delenv("GMAIL_USER", raising=False)
    monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)
    monkeypatch.delenv("GMAIL_PASS", raising=False)
    assert send_telegram("hello") is False
    assert send_email("sub", "body") is False


def test_format_card_error_is_distinct_from_skip() -> None:
    html = format_card(_record(error="scrape:Timeout 45000ms exceeded", apply_s1=False))["html"]
    assert "SCAN ERROR" in html
    assert "Timeout" in html
    assert "Strategy 1" not in html
    assert "APPLY FOR LISTING GAINS" not in html
    assert "🔴 <b>SKIP</b>" not in html


def test_redact_strips_telegram_token_from_urls() -> None:
    msg = "404 Not Found for url 'https://api.telegram.org/bot123456:ABC-DEF_ghi/sendMessage'"
    out = _redact(msg)
    assert "123456:ABC-DEF_ghi" not in out
    assert "REDACTED" in out


def test_env_strips_quotes_and_app_password_spaces(monkeypatch) -> None:
    monkeypatch.setenv("GMAIL_USER", '"me@gmail.com"')
    monkeypatch.setenv("GMAIL_APP_PASSWORD", '"tasr dzzx ubgi ougf"')
    assert _env("GMAIL_USER") == "me@gmail.com"
    assert _app_password() == "tasrdzzxubgiougf"


def test_send_telegram_uses_html_parse_mode(monkeypatch) -> None:
    calls = []

    class _Resp:
        def raise_for_status(self):
            return None

    def fake_post(url, data=None, timeout=None):
        calls.append((url, data, timeout))
        return _Resp()

    monkeypatch.setattr("scripts.notify.httpx.post", fake_post)
    assert send_telegram("<b>card</b>", token="tok", chat_id="42") is True
    assert calls[0][1]["parse_mode"] == "HTML"
    assert calls[0][1]["text"] == "<b>card</b>"


def test_send_email_uses_html_mime_and_strips_windows_quotes(monkeypatch) -> None:
    captured: dict = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            captured["host"] = host
            captured["port"] = port

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def starttls(self):
            return None

        def login(self, user, password):
            captured["user"] = user
            captured["password"] = password

        def sendmail(self, frm, to, raw):
            captured["raw"] = raw

    monkeypatch.setenv("GMAIL_USER", '"me@gmail.com"')
    monkeypatch.setenv("GMAIL_APP_PASSWORD", '"tasr dzzx ubgi ougf"')
    monkeypatch.setattr("scripts.notify.smtplib.SMTP", FakeSMTP)
    assert send_email("IPO alert", "<p>hi</p>") is True
    assert captured["user"] == "me@gmail.com"
    assert captured["password"] == "tasrdzzxubgiougf"
    assert "text/html" in captured["raw"]


def test_dispatch_sends_one_email_digest(monkeypatch) -> None:
    telegrams: list[str] = []
    emails: list[tuple[str, str]] = []

    monkeypatch.setattr("scripts.notify.send_telegram", lambda text, **kw: telegrams.append(text) or True)

    def capture_email(subject, body, **kw):
        emails.append((subject, body))
        return True

    monkeypatch.setattr("scripts.notify.send_email", capture_email)
    dispatch([_record(company_name="A"), _record(company_name="B")], dry_run=False)
    assert len(telegrams) == 2
    assert len(emails) == 1
    subject, body = emails[0]
    assert subject.startswith("IPO alerts (2)")
    assert "A" in subject and "B" in subject
    assert "A" in body and "B" in body
    assert "<hr>" in body
    assert "<br>" in body


def test_format_email_digest_joins_cards() -> None:
    digest = format_email_digest(["<b>One</b>\nline", "<b>Two</b>"])
    assert digest.startswith("<html>")
    assert "<br>" in digest
    assert "<hr>" in digest
    assert "One" in digest and "Two" in digest


def test_allotment_card_and_registrar_map() -> None:
    card = format_allotment_card({"company_name": "Lumino Industries", "registrar": "MUFG Intime India Pvt.Ltd."})
    assert "ALLOTMENT OUT: Lumino Industries" in card
    assert "MUFG Intime" in card
    assert "linkintime.co.in" in card
    assert registrar_portal_url("KFin Technologies") == "https://kosmic.kfintech.com/ipostatus/"
    assert registrar_portal_url("Unknown House") == "https://www.chittorgarh.com/ipo_allotment_status/"


def test_dispatch_one_email_even_on_auth_failure(monkeypatch) -> None:
    email_subjects: list[str] = []
    monkeypatch.setattr("scripts.notify.send_telegram", lambda text, **kw: True)

    def boom(subject, body, **kw):
        email_subjects.append(subject)
        raise smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted")

    monkeypatch.setattr("scripts.notify.send_email", boom)
    dispatch([_record(company_name="A"), _record(company_name="B")], dry_run=False)
    assert len(email_subjects) == 1
