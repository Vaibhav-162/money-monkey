"""HTTP session with retries, polite delay, and on-disk HTML cache."""

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
