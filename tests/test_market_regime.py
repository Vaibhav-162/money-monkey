from pathlib import Path

import pandas as pd

from analysis.market_regime import fetch_market_regime, regime_from_closes


def test_regime_from_closes_bearish_bullish_neutral() -> None:
    assert regime_from_closes([100, 100, 100, 100, 100, 98]) == "BEARISH"
    assert regime_from_closes([100, 100, 100, 100, 100, 101]) == "BULLISH"
    assert regime_from_closes([100, 101]) == "NEUTRAL"
    assert regime_from_closes([]) == "NEUTRAL"


def test_fetch_market_regime_mocked_drop(tmp_path: Path) -> None:
    def fake(_ticker, _start, _end):
        return pd.DataFrame({"close": [100, 99, 99, 98, 98, 97]})

    assert fetch_market_regime(cache_path=tmp_path / "r.json", history_fn=fake) == "BEARISH"


def test_fetch_market_regime_mocked_rise(tmp_path: Path) -> None:
    def fake(_ticker, _start, _end):
        return pd.DataFrame({"close": [100, 100, 100, 100, 100, 102]})

    assert fetch_market_regime(cache_path=tmp_path / "r.json", history_fn=fake) == "BULLISH"


def test_yahoo_failure_uses_fresh_cache(tmp_path: Path) -> None:
    cache = tmp_path / "r.json"

    def good(_ticker, _start, _end):
        return pd.DataFrame({"close": [100, 100, 100, 100, 100, 102]})

    assert fetch_market_regime(cache_path=cache, history_fn=good) == "BULLISH"

    def boom(_ticker, _start, _end):
        raise RuntimeError("yahoo down")

    assert fetch_market_regime(cache_path=cache, history_fn=boom) == "BULLISH"


def test_yahoo_failure_without_cache_is_neutral(tmp_path: Path) -> None:
    def boom(_ticker, _start, _end):
        raise RuntimeError("yahoo down")

    assert fetch_market_regime(cache_path=tmp_path / "missing.json", history_fn=boom) == "NEUTRAL"
