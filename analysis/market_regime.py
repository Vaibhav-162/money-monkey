"""Nifty-50 5-session regime flag for Strategy 2 card copy.

Network is optional. A failed Yahoo fetch returns the last cached regime
when it is still fresh enough, otherwise NEUTRAL. Never raises.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from analysis.prices import _yf_history

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CACHE = ROOT / "data" / "analysis" / "market_regime.json"
BEARISH_THRESHOLD_PCT = -1.5
REGIMES = ("BULLISH", "BEARISH", "NEUTRAL")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _read_cache(path: Path, max_age_hours: int) -> Optional[str]:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return None
    regime = str(payload.get("regime") or "").upper()
    if regime not in REGIMES:
        return None
    raw = payload.get("fetched_at")
    if not raw:
        return None
    try:
        fetched = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=timezone.utc)
    if _now() - fetched > timedelta(hours=max_age_hours):
        return None
    return regime


def _write_cache(path: Path, regime: str, extra: dict[str, Any] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "regime": regime,
        "fetched_at": _now().strftime("%Y-%m-%dT%H:%M:%SZ"),
        **(extra or {}),
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _close_col(frame) -> Optional[str]:
    for name in ("close", "adj close", "adj_close"):
        if name in frame.columns:
            return name
    return None


def regime_from_closes(closes: list[float], *, threshold_pct: float = BEARISH_THRESHOLD_PCT) -> str:
    """Need latest close plus the close 5 sessions earlier (6 points)."""
    clean = [float(x) for x in closes if x is not None]
    if len(clean) < 6 or clean[-6] <= 0:
        return "NEUTRAL"
    change_pct = (clean[-1] / clean[-6] - 1.0) * 100.0
    if change_pct <= threshold_pct:
        return "BEARISH"
    return "BULLISH"


def fetch_market_regime(
    cache_path: Path | None = None,
    max_age_hours: int = 6,
    history_fn=None,
) -> str:
    """Return BULLISH / BEARISH / NEUTRAL. Never raises."""
    path = Path(cache_path) if cache_path else DEFAULT_CACHE
    fetcher = history_fn if history_fn is not None else _yf_history
    try:
        end = _now().date() + timedelta(days=1)
        start = end - timedelta(days=21)
        hist = fetcher("^NSEI", start.isoformat(), end.isoformat())
        if hist is None or getattr(hist, "empty", True):
            return _read_cache(path, max_age_hours) or "NEUTRAL"
        col = _close_col(hist)
        if col is None:
            return _read_cache(path, max_age_hours) or "NEUTRAL"
        import pandas as pd

        px = pd.to_numeric(hist[col], errors="coerce").dropna().tolist()
        regime = regime_from_closes(px)
        extra = {"n_closes": len(px), "change_pct": None}
        if len(px) >= 6 and px[-6] > 0:
            extra["change_pct"] = (px[-1] / px[-6] - 1.0) * 100.0
        if regime != "NEUTRAL":
            _write_cache(path, regime, extra)
        return regime
    except Exception:
        return _read_cache(path, max_age_hours) or "NEUTRAL"
