"""Per-account persistent Chrome profiles (fork addition).

Upstream opens a fresh browser for every run and loads the account's cookie file through
``browser.new_context(storage_state=...)``. With ``SAU_PROFILE_ROOT`` set, this module keeps
that call shape but opens each account's own Chrome profile directory instead, the setup the
patchright authors recommend for staying undetected: the installed Google Chrome
(``channel="chrome"``), a persistent ``user_data_dir``, a visible window, no viewport override
and no injected scripts. The browser still starts for each run and closes afterwards; cache,
history and site data stay in the profile between runs.

Uploaders opt in by importing ``async_playwright`` from here instead of from patchright. Without
``SAU_PROFILE_ROOT`` the real patchright object is returned unchanged, so upstream behaviour is
kept.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from patchright.async_api import async_playwright as _patchright

# Launch options that would make the browser differ from the owner's ordinary Chrome.
_DROPPED_CONTEXT_OPTIONS = {"storage_state", "viewport", "user_agent", "locale", "timezone_id"}


def enabled() -> bool:
    return bool(os.environ.get("SAU_PROFILE_ROOT"))


def profile_dir(account_file: Any) -> Path:
    """``cookies/douyin_pets.json`` → ``$SAU_PROFILE_ROOT/douyin_pets``."""
    stem = Path(str(account_file)).stem if account_file else "default"
    stem = re.sub(r"[^\w.-]", "_", stem) or "default"
    root = Path(os.environ["SAU_PROFILE_ROOT"]).expanduser()
    path = root / stem
    path.mkdir(parents=True, exist_ok=True)
    return path


class _ProfileBrowser:
    """Stands in for a launched Browser; each ``new_context`` opens one account profile."""

    def __init__(self, chromium, launch_options: dict):
        self._chromium = chromium
        self._proxy = launch_options.get("proxy")
        self._contexts: list = []

    @property
    def contexts(self) -> list:
        return list(self._contexts)

    async def new_context(self, storage_state=None, **options):
        extra = {k: v for k, v in options.items() if k not in _DROPPED_CONTEXT_OPTIONS}
        if self._proxy and "proxy" not in extra:
            extra["proxy"] = self._proxy
        context = await self._chromium.launch_persistent_context(
            str(profile_dir(storage_state)),
            channel="chrome",
            headless=False,
            no_viewport=True,
            # Playwright launches with --no-sandbox unless told otherwise; keep Chrome's sandbox
            # on like an ordinary Chrome (no "unsupported command-line flag" bar, no weaker isolation).
            chromium_sandbox=True,
            **extra,
        )
        self._contexts.append(context)
        return context

    async def new_page(self, **options):
        return await (await self.new_context(**options)).new_page()

    def is_connected(self) -> bool:
        return bool(self._contexts)

    async def close(self) -> None:
        while self._contexts:
            context = self._contexts.pop()
            try:
                await context.close()
            except Exception:
                pass


class _ProfileChromium:
    def __init__(self, chromium):
        self._chromium = chromium

    async def launch(self, **options):
        return _ProfileBrowser(self._chromium, options)

    def __getattr__(self, name):
        # connect_over_cdp, launch_persistent_context and the rest stay native.
        return getattr(self._chromium, name)


class _ProfilePlaywright:
    def __init__(self, playwright):
        self._playwright = playwright
        self.chromium = _ProfileChromium(playwright.chromium)
        # Upstream TikTok launches Firefox; every account runs on Chrome here.
        self.firefox = self.chromium

    def __getattr__(self, name):
        return getattr(self._playwright, name)


class _Manager:
    def __init__(self):
        self._inner = _patchright()

    async def __aenter__(self):
        playwright = await self._inner.__aenter__()
        return _ProfilePlaywright(playwright) if enabled() else playwright

    async def __aexit__(self, *exc):
        return await self._inner.__aexit__(*exc)


def async_playwright():
    return _Manager()
