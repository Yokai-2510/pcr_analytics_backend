"""Upstox access-token refresh via Playwright + TOTP.

Adapted from the standalone `upstox/auth.py` reference module. Self-contained:
the only project imports are `utilities` (for credentials I/O) and `broker_api`
(to invalidate the cached token after a refresh).

Public entry point: ``refresh_access_token()`` runs the full flow — Playwright
login → auth_code → token exchange → persist to credentials.json. Returns a
result dict for the API / worker to surface.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

import requests

import utilities as utils

logger = logging.getLogger(__name__)

_LOGIN_URL = "https://api.upstox.com/v2/login/authorization/dialog"
_TOKEN_URL = "https://api-v2.upstox.com/login/authorization/token"
_VALIDATE_URL = "https://api.upstox.com/v2/user/profile"

# System Chrome is used because Playwright doesn't ship a chromium build
# catalogued for Ubuntu 26.04. Override via the configs if you move hosts.
_CHROME_EXECUTABLE = "/usr/bin/google-chrome"


class UpstoxAuthError(RuntimeError):
    """Raised when any step of the login / exchange flow fails."""


# ── Remote validity probe ───────────────────────────────────────────────


def is_token_valid_remote(token: str, *, timeout: int = 5) -> bool:
    """True iff Upstox accepts the token on /user/profile."""
    if not token:
        return False
    try:
        r = requests.get(
            _VALIDATE_URL,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=timeout,
        )
    except requests.RequestException:
        return False
    if r.status_code != 200:
        return False
    try:
        return r.json().get("status") == "success"
    except ValueError:
        return False


# ── auth_code → access_token exchange ───────────────────────────────────


def exchange_code_for_token(creds: dict[str, Any], auth_code: str, *, timeout: int = 30) -> str:
    required = ("api_key", "api_secret", "redirect_uri")
    missing = [k for k in required if not creds.get(k)]
    if missing:
        raise UpstoxAuthError(f"missing required credentials: {', '.join(missing)}")
    r = requests.post(
        _TOKEN_URL,
        data={
            "code": auth_code,
            "client_id": creds["api_key"],
            "client_secret": creds["api_secret"],
            "redirect_uri": creds["redirect_uri"],
            "grant_type": "authorization_code",
        },
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "Api-Version": "2.0",
        },
        timeout=timeout,
    )
    if r.status_code != 200:
        raise UpstoxAuthError(f"token exchange HTTP {r.status_code}: {r.text[:400]}")
    try:
        parsed = r.json()
    except ValueError:
        raise UpstoxAuthError(f"token exchange returned non-JSON: {r.text[:200]}")
    token = parsed.get("access_token") or (parsed.get("data") or {}).get("access_token")
    if not token:
        raise UpstoxAuthError(f"token exchange response missing access_token: {parsed}")
    return str(token)


# ── Playwright login flow ───────────────────────────────────────────────


def fetch_auth_code(creds: dict[str, Any], *, headless: bool = True, max_retries: int = 3) -> str:
    """Drive Upstox login via Playwright + TOTP + PIN. Returns the auth_code."""
    try:
        import pyotp  # type: ignore
        from playwright.sync_api import sync_playwright  # type: ignore
    except ImportError as exc:
        raise UpstoxAuthError(f"playwright/pyotp not installed: {exc}") from exc

    required = ("api_key", "redirect_uri", "mobile_no", "totp_key", "pin")
    missing = [k for k in required if not creds.get(k)]
    if missing:
        raise UpstoxAuthError(f"missing required credentials: {', '.join(missing)}")

    last_error: BaseException | None = None
    for attempt in range(1, max_retries + 1):
        try:
            return _do_fetch_auth_code(creds, attempt, headless, sync_playwright, pyotp)
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            logger.warning("auth_code attempt %d/%d failed: %s", attempt, max_retries, exc)
            if attempt < max_retries:
                import time

                time.sleep(3 * attempt)
    raise UpstoxAuthError(f"auth_code fetch failed after {max_retries} attempts: {last_error}")


def _do_fetch_auth_code(
    creds: dict[str, Any],
    attempt: int,
    headless: bool,
    sync_playwright: Any,
    pyotp: Any,
) -> str:
    api_key = creds["api_key"]
    redirect_uri = creds["redirect_uri"]
    auth_url = (
        f"{_LOGIN_URL}?response_type=code"
        f"&client_id={api_key}"
        f"&redirect_uri={quote(redirect_uri, safe='')}"
    )

    captured: dict[str, str | None] = {"code": None}

    def on_request(req: Any) -> None:
        if captured["code"] is None and redirect_uri in req.url and "code=" in req.url:
            captured["code"] = parse_qs(urlparse(req.url).query).get("code", [None])[0]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless, executable_path=_CHROME_EXECUTABLE)
        context = browser.new_context(ignore_https_errors=True)
        page = context.new_page()
        page.on("request", on_request)
        try:
            page.goto(auth_url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_selector("#mobileNum", state="visible", timeout=30000)
            page.locator("#mobileNum").fill(creds["mobile_no"])
            page.get_by_role("button", name="Get OTP").click()
            page.wait_for_selector("#otpNum", timeout=30000)
            otp = pyotp.TOTP(creds["totp_key"]).now()
            page.locator("#otpNum").fill(otp)
            page.get_by_role("button", name="Continue").click()
            page.wait_for_selector("input[type='password']", timeout=30000)
            page.get_by_label("Enter 6-digit PIN").fill(creds["pin"])
            page.get_by_role("button", name="Continue").click()
            page.wait_for_timeout(5000)
            if captured["code"] is None and redirect_uri in page.url and "code=" in page.url:
                captured["code"] = parse_qs(urlparse(page.url).query).get("code", [None])[0]
        finally:
            context.close()
            browser.close()

    if not captured["code"]:
        raise UpstoxAuthError(f"no auth_code captured (attempt {attempt})")
    return captured["code"]


# ── Orchestrator ────────────────────────────────────────────────────────


def refresh_access_token(*, force: bool = False, headless: bool = True) -> dict[str, Any]:
    """Fetch a fresh Upstox access_token end-to-end and persist it.

    When ``force`` is False, the currently-saved access_token is probed
    against /user/profile first; if Upstox still accepts it, the existing
    token is kept and no Playwright run is needed.
    """
    creds = utils.upstox_config()
    if not force:
        existing = (creds.get("access_token") or "").strip()
        if existing and is_token_valid_remote(existing):
            logger.info("Existing access_token still valid — skipping refresh")
            return {"refreshed": False, "reason": "existing token still valid", "token_preview": _preview(existing)}

    logger.info("Refreshing Upstox access_token via Playwright login flow")
    auth_code = fetch_auth_code(creds, headless=headless)
    token = exchange_code_for_token(creds, auth_code)
    utils.update_credentials_section("upstox", {"access_token": token})

    # Best-effort cache invalidation so the worker picks up the new token on
    # its next call. Imported lazily to avoid a circular import at module load.
    try:
        import broker_api

        broker_api.invalidate_token()
    except Exception:  # noqa: BLE001
        logger.debug("broker_api.invalidate_token unavailable", exc_info=True)

    logger.info("Upstox access_token refreshed and saved")
    return {"refreshed": True, "token_preview": _preview(token)}


def _preview(token: str) -> str:
    if len(token) <= 8:
        return "*" * len(token)
    return f"{token[:4]}...{token[-4:]}"
