"""Best-effort PAN allotment lookup on KFintech and MUFG Intime.

WHAT THIS FILE DOES
--------------------
After Chittorgarh shows that allotment is out, this file (optionally) asks
the registrar's own portal whether a given PAN got shares. The only
production caller is `scripts/check_allotment.py`, which passes in a
Playwright `Page` from `browser.chromium_session` — this module does not
launch a browser. It imports `parse_number` from `normalize.py` for share
counts. Covered by `tests/test_registrar_allotment.py`.

Registrar portals require a captcha. OCR can miss; callers must fall back
to a manual-check reminder. Never log or persist a full PAN.

KEY TERMS USED HERE
--------------------
- Allotment: the lottery that decides who receives IPO shares when the
  issue is oversubscribed. "Allotted" / "not allotted" / "no application"
  are the statuses this file returns.
- Registrar: the company that runs that lottery and the PAN lookup portal.
  Automated here: KFin Technologies (`kfin` / legacy `karvy`) and MUFG
  Intime (also branded Link Intime). Bigshare, Cameo, Skyline, and Purva
  are recognized but return `None` so the caller sends a manual-check note.
- PAN: India's Permanent Account Number (personal tax ID), format
  `ABCDE1234F`. Used to look up one person's application. Never put a full
  PAN in a log line, Telegram message, or audit CSV — use `mask_pan`.
- Captcha: the image the portal shows before search. Solved with Tesseract
  OCR when Pillow/pytesseract are installed; otherwise status is
  `captcha_failed` and the caller reminds the user to check by hand.

FUNCTIONS / CLASSES IN THIS FILE
---------------------------------
- `mask_pan(pan)`: `ABCDE1234F` → `ABCDE***4F`. Safe for logs.
- `load_pan_profiles(raw)`: parse `PAN_PROFILES` JSON (or the env var).
  Drops malformed rows; never crashes the run.
- `find_company_option(options, company_name)`: fuzzy-match the portal
  dropdown to our `company_name` (strips Ltd/IPO/SME suffixes).
- `solve_captcha(image_bytes)`: OCR; empty string on missing deps or fail.
- `parse_result_blob(text)`: map visible page text to
  allotted / not_allotted / no_application / captcha_failed / lookup_failed.
- `assess_lookup_batch(results)` / `raise_if_systematic_lookup_failure(results)`:
  distinguish a one-off captcha/company miss from "every lookup in this
  run failed" (page-structure or OCR break). Callers such as
  `scripts/check_allotment.py` should invoke this after a batch.
- `RegistrarLookupBatchError`: raised only on that total-failure case.
- `checker_for_registrar(name)`: pick `check_kfintech`, `check_mufg`, or
  None from the registrar string `parse_ipo` stored on the master row.
- `check_kfintech(page, company_name, pan)` / `check_mufg(...)`: public
  lookups with up to `CAPTCHA_ATTEMPTS` retries.
- Private page drivers (`_once_kfintech`, `_once_mufg`, `_pick_company_select`,
  `_fill_pan_input`, `_captcha_png`, `_submit`, …): one attempt at a portal.
"""

from __future__ import annotations

import io
import json
import os
import re
from difflib import SequenceMatcher
from typing import Any, Callable, Optional, Sequence

from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from chittorgarh.normalize import parse_number

PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
CAPTCHA_ATTEMPTS = 4
KFINTECH_URL = "https://kcas.kfintech.com/ipostatus/"
MUFG_URL = "https://in.mpms.mufg.com/Initial_Offer/public-issues.html"

_SUFFIXES = (
    " limited",
    " ltd.",
    " ltd",
    " private",
    " pvt.",
    " pvt",
    " ipo",
    " sme",
)


def mask_pan(pan: str) -> str:
    """ABCDE1234F -> ABCDE***4F. Never log the middle of a PAN."""
    text = re.sub(r"[^A-Za-z0-9]", "", str(pan or "")).upper()
    if len(text) < 6:
        return "****"
    return f"{text[:5]}***{text[-2:]}"


def _clean_secret(value: str) -> str:
    raw = (value or "").strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in {"'", '"'}:
        raw = raw[1:-1].strip()
    return raw


def load_pan_profiles(raw: Optional[str] = None) -> list[dict[str, str]]:
    """Parse PAN_PROFILES JSON. Drop malformed rows; never crash the run."""
    text = _clean_secret(raw if raw is not None else (os.environ.get("PAN_PROFILES") or ""))
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        print("[allotment] PAN_PROFILES is not valid JSON; ignoring")
        return []
    if not isinstance(data, list):
        print("[allotment] PAN_PROFILES must be a JSON array; ignoring")
        return []
    out: list[dict[str, str]] = []
    for item in data:
        if not isinstance(item, dict):
            print("[allotment] skipped a non-object PAN profile")
            continue
        pan = re.sub(r"[^A-Za-z0-9]", "", str(item.get("pan") or "")).upper()
        email = str(item.get("email") or "").strip()
        label = str(item.get("label") or email or "investor").strip()
        if not PAN_RE.match(pan):
            print(f"[allotment] skipped malformed PAN {mask_pan(pan)}")
            continue
        if not email or "@" not in email:
            print(f"[allotment] skipped {mask_pan(pan)} (missing email)")
            continue
        out.append({"label": label, "pan": pan, "email": email})
    return out


def _norm_name(value: str) -> str:
    text = str(value or "").lower()
    text = re.sub(r"&amp;", "&", text)
    for suffix in _SUFFIXES:
        text = text.replace(suffix, " ")
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def find_company_option(options: list[str], company_name: str) -> Optional[str]:
    """Fuzzy-match a registrar dropdown label to our company_name."""
    wanted = _norm_name(company_name)
    cleaned = [(opt, _norm_name(opt)) for opt in options if str(opt).strip() and _norm_name(opt)]
    if not wanted or not cleaned:
        return None
    for opt, normed in cleaned:
        if normed == wanted:
            return opt
    best_opt = None
    best_score = 0.0
    for opt, normed in cleaned:
        score = SequenceMatcher(None, wanted, normed).ratio()
        if wanted in normed or normed in wanted:
            score = max(score, 0.86)
        if score > best_score:
            best_score = score
            best_opt = opt
    if best_score < 0.6:
        return None
    return best_opt


def solve_captcha(image_bytes: bytes) -> str:
    """OCR a captcha image. Empty string if Tesseract/Pillow fail."""
    if not image_bytes:
        return ""
    try:
        from PIL import Image, ImageOps
        import pytesseract
    except ImportError:
        print("[allotment] pytesseract/Pillow not installed; captcha OCR skipped")
        return ""
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("L")
        img = ImageOps.autocontrast(img)
        img = img.resize((max(img.width * 3, 1), max(img.height * 3, 1)), Image.Resampling.LANCZOS)
        img = img.point(lambda p: 255 if p > 140 else 0)
        raw = pytesseract.image_to_string(
            img,
            config="--psm 7 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
        )
    except Exception as exc:
        print(f"[allotment] captcha OCR failed: {exc}")
        return ""
    return re.sub(r"[^A-Z0-9]", "", (raw or "").upper())


def parse_result_blob(text: str) -> dict[str, Any]:
    """Map registrar page text to allotted / not_allotted / no_application / captcha_failed / lookup_failed."""
    blob = re.sub(r"\s+", " ", str(text or "")).strip().lower()
    if not blob:
        return {"status": "captcha_failed", "shares": None}
    if any(tok in blob for tok in ("invalid captcha", "incorrect captcha", "wrong captcha", "enter captcha", "captcha mismatch")):
        return {"status": "captcha_failed", "shares": None}
    if any(tok in blob for tok in ("no application", "no record", "not found", "no data found", "no details found")):
        return {"status": "no_application", "shares": None}
    if "not allotted" in blob or "not alloted" in blob:
        return {"status": "not_allotted", "shares": 0}
    shares = None
    share_match = re.search(r"(?:allotted|allotment)\D{0,24}(\d[\d,]*)", blob)
    if share_match:
        parsed = parse_number(share_match.group(1))
        if parsed is not None:
            shares = int(parsed)
    # "0 share" as a substring matches "1600 shares" / "10 shares"; require a
    # true zero count (no digit immediately before the 0).
    zero_shares = re.search(r"(?<!\d)0\s*shares?\b", blob) is not None
    if shares == 0 or zero_shares:
        return {"status": "not_allotted", "shares": 0}
    if "allotted" in blob or "allotment of" in blob:
        return {"status": "allotted", "shares": shares}
    return {"status": "lookup_failed", "shares": None}


def checker_for_registrar(name: Any) -> Optional[Callable[..., dict[str, Any]]]:
    blob = str(name or "").lower()
    for keys, fn in REGISTRAR_CHECKERS:
        if any(key in blob for key in keys):
            return fn
    return None


def _select_options(page: Page) -> list[str]:
    labels: list[str] = []
    for select in page.locator("select").all():
        for opt in select.locator("option").all():
            text = (opt.inner_text() or "").strip()
            value = (opt.get_attribute("value") or "").strip()
            if text and text.lower() not in {"--select--", "select", "please select"}:
                labels.append(text)
            elif value and value not in {"0", "-1", ""}:
                labels.append(value)
    return labels


def _pick_company_select(page: Page, company_name: str) -> Optional[str]:
    options = _select_options(page)
    return find_company_option(options, company_name)


def _fill_select_by_label(page: Page, label: str) -> bool:
    for select in page.locator("select").all():
        try:
            select.select_option(label=label, timeout=2000)
            return True
        except Exception:
            try:
                select.select_option(value=label, timeout=1000)
                return True
            except Exception:
                continue
    return False


def _captcha_png(page: Page) -> bytes:
    loc = page.locator("img[src*='captcha' i], img[id*='captcha' i], img[alt*='captcha' i]").first
    try:
        loc.wait_for(state="visible", timeout=8000)
        return loc.screenshot()
    except Exception:
        return b""


def _fill_captcha(page: Page, code: str) -> None:
    box = page.locator(
        "input[name*='captcha' i], input[id*='captcha' i], input[placeholder*='captcha' i]"
    ).first
    box.fill(code)


def _click_pan_mode(page: Page) -> None:
    for loc in (
        page.get_by_text("PAN", exact=True),
        page.locator("input[type='radio'][value*='PAN' i]"),
        page.locator("label:has-text('PAN')"),
        page.get_by_role("radio", name=re.compile(r"PAN", re.I)),
    ):
        try:
            loc.first.click(timeout=1500)
            return
        except Exception:
            continue


def _fill_pan_input(page: Page, pan: str) -> None:
    box = page.locator(
        "input[name*='pan' i], input[id*='pan' i], input[placeholder*='PAN' i]"
    ).first
    try:
        box.fill(pan, timeout=3000)
        return
    except Exception:
        pass
    page.locator("input[type='text']").last.fill(pan)


def _submit(page: Page) -> None:
    for loc in (
        page.get_by_role("button", name=re.compile(r"submit|search|check", re.I)),
        page.locator("input[type='submit']"),
        page.locator("a:has-text('Submit')"),
    ):
        try:
            loc.first.click(timeout=2000)
            return
        except Exception:
            continue
    page.keyboard.press("Enter")


def _visible_blob(page: Page) -> str:
    try:
        return page.inner_text("body", timeout=8000)
    except Exception:
        return ""


def _once_kfintech(page: Page, company_name: str, pan: str) -> dict[str, Any]:
    page.goto(KFINTECH_URL, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(1200)
    label = _pick_company_select(page, company_name)
    if not label:
        return {"status": "company_not_found", "shares": None}
    _fill_select_by_label(page, label)
    page.wait_for_timeout(400)
    _click_pan_mode(page)
    page.wait_for_timeout(400)
    _fill_pan_input(page, pan)
    png = _captcha_png(page)
    code = solve_captcha(png)
    if not code:
        return {"status": "captcha_failed", "shares": None}
    _fill_captcha(page, code)
    _submit(page)
    page.wait_for_timeout(1800)
    return parse_result_blob(_visible_blob(page))


def _once_mufg(page: Page, company_name: str, pan: str) -> dict[str, Any]:
    page.goto(MUFG_URL, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(1200)
    label = _pick_company_select(page, company_name)
    if not label:
        return {"status": "company_not_found", "shares": None}
    _fill_select_by_label(page, label)
    page.wait_for_timeout(400)
    _click_pan_mode(page)
    page.wait_for_timeout(400)
    _fill_pan_input(page, pan)
    png = _captcha_png(page)
    code = solve_captcha(png)
    if not code:
        return {"status": "captcha_failed", "shares": None}
    _fill_captcha(page, code)
    _submit(page)
    page.wait_for_timeout(1800)
    return parse_result_blob(_visible_blob(page))


def _with_retries(once: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    last = {"status": "captcha_failed", "shares": None}
    for _ in range(CAPTCHA_ATTEMPTS):
        try:
            last = once()
        except PlaywrightTimeout:
            # Transient: the portal was slow. Retry like a captcha miss.
            last = {"status": "captcha_failed", "shares": None}
        except Exception as exc:
            # Selector/page-structure break, not an OCR miss. Do not retry as
            # captcha_failed — that made a systematically broken portal look
            # like a normal one-off captcha failure.
            print(f"[allotment] registrar lookup error: {exc}")
            last = {"status": "lookup_failed", "shares": None}
        if last.get("status") != "captcha_failed":
            return last
    return last


_RESOLVED_LOOKUP_STATUSES = frozenset({"allotted", "not_allotted", "no_application"})
_FAILED_LOOKUP_STATUSES = frozenset({"captcha_failed", "lookup_failed", "company_not_found"})


class RegistrarLookupBatchError(RuntimeError):
    """Every attempted registrar lookup in a run failed; likely a site/OCR break."""


def assess_lookup_batch(
    results: Sequence[dict[str, Any] | None],
    *,
    min_attempts: int = 2,
) -> dict[str, Any]:
    """Detect 'every lookup failed' vs a documented per-item miss.

    A single `captcha_failed` is a normal OCR miss. The same status on every
    attempt in a run (2+ lookups, zero resolved answers) means Tesseract, the
    captcha widget, or the page structure is broken — indistinguishable from a
    one-off miss unless we count. `no_application` is a real registrar answer.

    Does not send alerts; callers should raise on `escalate` (see
    `raise_if_systematic_lookup_failure`). Empty input does not escalate
    (nothing was attempted — unconfigured PAN list / unsupported registrar).
    """
    statuses = [str((row or {}).get("status") or "") for row in results]
    n = len(statuses)
    n_ok = sum(1 for status in statuses if status in _RESOLVED_LOOKUP_STATUSES)
    n_fail = sum(1 for status in statuses if status in _FAILED_LOOKUP_STATUSES)
    escalate = n >= min_attempts and n_ok == 0 and n_fail == n
    counts = ", ".join(f"{s}={statuses.count(s)}" for s in sorted(set(statuses))) or "none"
    reason = (
        f"All {n} registrar lookup(s) failed ({counts}); "
        "page structure or captcha OCR is likely broken"
        if escalate
        else ""
    )
    return {
        "n": n,
        "n_ok": n_ok,
        "n_fail": n_fail,
        "escalate": escalate,
        "reason": reason,
    }


def raise_if_systematic_lookup_failure(
    results: Sequence[dict[str, Any] | None],
    *,
    min_attempts: int = 2,
) -> None:
    """No-op unless every attempted lookup failed. See `assess_lookup_batch`."""
    verdict = assess_lookup_batch(results, min_attempts=min_attempts)
    if verdict["escalate"]:
        print(f"[allotment] {verdict['reason']}")
        raise RegistrarLookupBatchError(verdict["reason"])


def check_kfintech(page: Page, company_name: str, pan: str) -> dict[str, Any]:
    return _with_retries(lambda: _once_kfintech(page, company_name, pan))


def check_mufg(page: Page, company_name: str, pan: str) -> dict[str, Any]:
    return _with_retries(lambda: _once_mufg(page, company_name, pan))


# Same name keys as scripts.notify.REGISTRAR_PORTALS; None means unsupported (manual fallback).
REGISTRAR_CHECKERS: tuple[tuple[tuple[str, ...], Optional[Callable[..., dict[str, Any]]]], ...] = (
    (("mufg", "link intime", "linkintime"), check_mufg),
    (("kfin", "karvy"), check_kfintech),
    (("bigshare",), None),
    (("cameo",), None),
    (("skyline",), None),
    (("purva",), None),
)
