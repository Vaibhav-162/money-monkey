"""Playwright Chromium helper that always closes the browser."""

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
