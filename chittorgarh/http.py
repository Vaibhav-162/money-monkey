"""HTTP session with retries, polite delay, and on-disk HTML cache.

WHAT THIS FILE DOES
--------------------
This is the shared non-browser HTTP layer for Chittorgarh pages that are
static HTML (detail pages, the current-IPO dashboard, the live subscription
URL). `pipeline.py`, `live_dashboard.py`, `live_subscription.py`,
`scripts/live_scanner.py`, `scripts/check_allotment.py`, and
`scripts/verify_outcomes.py` all construct an `HttpClient`. Sibling modules
`tracker.py`, `parse_ipo.py`, `live_dashboard.py`, and `live_subscription.py`
also import `BASE_URL`; `browser.py` reuses `USER_AGENT` so Playwright looks
like the same client. Nothing here launches a browser — JS-heavy pages go
through `browser.py` instead.

KEY TERMS USED HERE
--------------------
- Chittorgarh: the public IPO-data site (`https://www.chittorgarh.com`) this
  project scrapes. Most URLs in the package are built from `BASE_URL`.
- On-disk HTML cache: `get_text(..., cache_name=...)` writes the response
  under `cache_dir` and, on a later run, returns that file instead of hitting
  the network. `pipeline.py --resume` relies on this; live jobs pass
  `use_cache=False` so they never score yesterday's HTML.
- Polite delay: seconds (plus a small random jitter) slept between live
  requests so a full historical scrape does not hammer the site.

FUNCTIONS / CLASSES IN THIS FILE
---------------------------------
- `FetchError`: raised on HTTP 4xx/5xx so tenacity can retry the same GET.
- `HttpClient(cache_dir, delay, timeout)`: context-managed `httpx` session
  with Chrome-like headers. Use `with HttpClient(...) as client:`.
- `HttpClient.get_text(url, cache_name, use_cache)`: the only fetch method.
  Cache hit returns disk HTML; otherwise delay → GET → retry up to 3 times
  → optionally write the cache file.
- `HttpClient._sleep()`: enforces the polite delay; skip if `delay <= 0`.
"""

from __future__ import annotations

import random
import time
from pathlib import Path

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
BASE_URL = "https://www.chittorgarh.com"


class FetchError(Exception):
    pass


class HttpClient:
    def __init__(self, cache_dir: Path, delay: float = 1.5, timeout: float = 45.0):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.delay = delay
        self._last_request = 0.0
        self.session = httpx.Client(
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
            timeout=timeout,
            follow_redirects=True,
        )

    def close(self) -> None:
        self.session.close()

    def __enter__(self) -> "HttpClient":
        return self

    def __exit__(self, *args) -> None:
        self.close()

    def _sleep(self) -> None:
        if self.delay <= 0:
            return
        elapsed = time.time() - self._last_request
        wait = self.delay + random.uniform(0, min(0.5, self.delay * 0.3))
        if elapsed < wait:
            time.sleep(wait - elapsed)
        self._last_request = time.time()

    @retry(
        retry=retry_if_exception_type((httpx.HTTPError, FetchError)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=20),
        reraise=True,
    )
    def get_text(self, url: str, cache_name: str | None = None, use_cache: bool = True) -> str:
        cache_path = self.cache_dir / cache_name if cache_name else None
        if use_cache and cache_path and cache_path.exists():
            return cache_path.read_text(encoding="utf-8", errors="replace")
        self._sleep()
        response = self.session.get(url)
        if response.status_code >= 500:
            raise FetchError(f"{url} returned {response.status_code}")
        if response.status_code >= 400:
            raise FetchError(f"{url} returned {response.status_code}")
        text = response.text
        if cache_path:
            cache_path.write_text(text, encoding="utf-8")
        return text
