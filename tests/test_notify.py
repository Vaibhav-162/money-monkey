from scripts.notify import _redact, format_card, send_email, send_telegram


def _record(**extra):
    rec = {
        "company_name": "Lumino Industries",
        "board": "mainboard",
        "close_date": "2026-08-31",
        "price_band_high": 216,
        "lot_size": 69,
        "gmp_rs": 18,
        "gmp_pct": 8.3,
        "sub_total_x": 12.4,
        "apply_s1": True,
        "ev_retail": 140.0,
        "p_allot": 0.081,
        "p_pop": 0.62,
        "apply_s2": False,
        "quality_score": 2,
        "quality_breakdown": [
            {"name": "subscription", "status": "fail", "awarded": False},
            {"name": "ofs_ratio", "status": "pass", "awarded": True},
            {"name": "roe", "status": "fail", "awarded": False},
            {"name": "debt_equity", "status": "not_disclosed", "awarded": True},
        ],
    }
    rec.update(extra)
    return rec


def test_format_card_apply_and_checklist_labels() -> None:
    card = format_card(_record())
    assert "Lumino Industries" in card
    assert "Strategy 1 (listing pop): APPLY" in card
    assert "Strategy 2 (quality): FLIP ONLY" in card
    assert "sub_gt_20: FAIL" in card
    assert "ofs_lt_50: PASS" in card
    assert "roe_gt_15: FAIL" in card
    assert "de_le_05: NOT_DISCLOSED" in card
    assert "not investment advice" in card.lower()


def test_format_card_missing_gmp_is_explicit() -> None:
    card = format_card(_record(gmp_rs=None, gmp_pct=None, apply_s1=False))
    assert "GMP: not available" in card
    assert "Strategy 1 (listing pop): SKIP" in card
    assert "HOLD CANDIDATE" not in card


def test_telegram_and_email_noop_without_secrets(monkeypatch) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.delenv("GMAIL_USER", raising=False)
    monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)
    monkeypatch.delenv("GMAIL_PASS", raising=False)
    assert send_telegram("hello") is False
    assert send_email("sub", "body") is False


def test_format_card_error_is_distinct_from_skip() -> None:
    """A scrape/score failure must never render as an ordinary SKIP card --
    that would look like a real model decision instead of a broken pipeline."""
    card = format_card(_record(error="scrape:Timeout 45000ms exceeded", apply_s1=False))
    assert "SCAN ERROR" in card
    assert "Timeout" in card
    assert "Strategy 1" not in card
    assert "Strategy 1 (listing pop): SKIP" not in card


def test_redact_strips_telegram_token_from_urls() -> None:
    msg = "404 Not Found for url 'https://api.telegram.org/bot123456:ABC-DEF_ghi/sendMessage'"
    out = _redact(msg)
    assert "123456:ABC-DEF_ghi" not in out
    assert "REDACTED" in out


def test_send_telegram_posts_when_secrets_set(monkeypatch) -> None:
    calls = []

    class _Resp:
        def raise_for_status(self):
            return None

    def fake_post(url, data=None, timeout=None):
        calls.append((url, data, timeout))
        return _Resp()

    monkeypatch.setattr("scripts.notify.httpx.post", fake_post)
    assert send_telegram("card", token="tok", chat_id="42") is True
    assert calls[0][0] == "https://api.telegram.org/bottok/sendMessage"
    assert calls[0][1]["chat_id"] == "42"
    assert calls[0][1]["text"] == "card"
