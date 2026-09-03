"""Playwright Chromium helper that always closes the browser.

WHAT THIS FILE DOES
--------------------
The single place that launches Chromium so every JS-rendered scrape shares
the same User-Agent and the same guaranteed teardown. `tracker.py` and
`gmp.py` use `chromium_page` (one page, then close). Long-running jobs that
hit many URLs — `scripts/live_scanner.py`, `scripts/check_allotment.py`, and
`scripts/rescrape_gmp_history.py` — use `chromium_session` so they can open
and close pages on one shared context. This file only imports `USER_AGENT`
from `http.py`; it does not fetch URLs itself.

KEY TERMS USED HERE
--------------------
- Playwright / Chromium: a real browser used when the target page is built
  in JavaScript (the IPO tracker DataTables, InvestorGain GMP tables) or
  needs captcha interaction (registrar portals). Static HTML uses
  `http.HttpClient` instead.
- Headless: browser runs without a visible window. `scrape_ipos.py --headed`
  and `--headed` on the GMP rescrape flip this off for debugging.
- User-Agent: the Chrome-on-Windows string from `http.USER_AGENT`, applied
  to the browser context so Chittorgarh/InvestorGain see the same client
  as the HTTP session.

FUNCTIONS / CLASSES IN THIS FILE
---------------------------------
- `_close_quietly(obj)`: `close()` that swallows errors so teardown cannot
  mask the original exception.
- `chromium_page(headless)`: context manager that yields one `Page` and
  always closes page, context, and browser.
- `chromium_session(headless)`: context manager that yields a
  `BrowserContext`. The caller opens/closes individual pages (e.g. one GMP
  history URL after another) and this still closes the browser at the end.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from playwright.sync_api import BrowserContext, Page, sync_playwright

from chittorgarh.http import USER_AGENT


def _close_quietly(obj: object) -> None:
    if obj is None:
        return
    try:
        obj.close()
    except Exception:
        pass


@contextmanager
def chromium_page(headless: bool = True) -> Iterator[Page]:
    browser = None
    context = None
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(headless=headless)
            context = browser.new_context(user_agent=USER_AGENT, locale="en-US")
            yield context.new_page()
        finally:
            _close_quietly(context)
            _close_quietly(browser)


@contextmanager
def chromium_session(headless: bool = True) -> Iterator[BrowserContext]:
    """One browser + context for many pages. Caller opens/closes pages."""
    browser = None
    context = None
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(headless=headless)
            context = browser.new_context(user_agent=USER_AGENT, locale="en-US")
            yield context
        finally:
            _close_quietly(context)
            _close_quietly(browser)
