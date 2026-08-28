"""Playwright Chromium helper that always closes the browser."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from playwright.sync_api import Page, sync_playwright

from chittorgarh.http import USER_AGENT


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
            if context is not None:
                try:
                    context.close()
                except Exception:
                    pass
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    pass
