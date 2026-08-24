"""Polite Playwright session.

The rules here are the ones that keep you both legal and unblocked:
  * public, unauthenticated pages only — never log in, never accept terms
  * one page at a time, with jittered delays
  * an honest user-agent naming the project and a contact address
  * every page archived to disk so you never fetch the same thing twice
"""

import asyncio
import random
import time
import hashlib
from pathlib import Path

import yaml


class Settings:
    def __init__(self, path="config/settings.yaml"):
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        self.contact_email = raw.get("contact_email", "unknown@example.com")
        self.project_url = raw.get("project_url", "")
        self.delay_min = float(raw.get("delay_min", 4))
        self.delay_max = float(raw.get("delay_max", 9))
        self.concurrency = int(raw.get("concurrency", 1))
        self.timeout = int(raw.get("timeout_seconds", 45)) * 1000
        self.max_retries = int(raw.get("max_retries", 3))
        self.archive_html = bool(raw.get("archive_html", True))
        self.archive_dir = Path(raw.get("archive_dir", "data/raw"))
        self.headless = bool(raw.get("headless", True))

    @property
    def user_agent(self) -> str:
        return (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 "
            f"WestEndTracker/0.1 (+{self.project_url}; {self.contact_email})"
        )


class Session:
    """Wraps a single browser. Use as an async context manager."""

    def __init__(self, settings: Settings):
        self.s = settings
        self._pw = None
        self._browser = None
        self._ctx = None
        self._last_hit = 0.0
        self._lock = asyncio.Lock()

    async def __aenter__(self):
        # Imported here rather than at module load so the reporting commands
        # work on a machine where Playwright isn't installed yet.
        from playwright.async_api import async_playwright

        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=self.s.headless)
        self._ctx = await self._browser.new_context(
            user_agent=self.s.user_agent,
            locale="en-GB",
            timezone_id="Europe/London",
            viewport={"width": 1440, "height": 900},
        )
        self._ctx.set_default_timeout(self.s.timeout)
        # Don't waste bandwidth or their capacity on things we never read.
        await self._ctx.route(
            "**/*",
            lambda route: asyncio.ensure_future(
                route.abort()
                if route.request.resource_type in {"image", "media", "font"}
                else route.continue_()
            ),
        )
        return self

    async def __aexit__(self, *exc):
        for closer in (self._ctx, self._browser):
            if closer:
                await closer.close()
        if self._pw:
            await self._pw.stop()

    async def _throttle(self):
        async with self._lock:
            wait = random.uniform(self.s.delay_min, self.s.delay_max)
            elapsed = time.monotonic() - self._last_hit
            if elapsed < wait:
                await asyncio.sleep(wait - elapsed)
            self._last_hit = time.monotonic()

    def _archive(self, url: str, html: str):
        if not self.s.archive_html:
            return
        key = hashlib.sha1(url.encode()).hexdigest()[:16]
        day = time.strftime("%Y-%m-%d")
        out = self.s.archive_dir / day
        out.mkdir(parents=True, exist_ok=True)
        (out / f"{key}.html").write_text(html, encoding="utf-8")

    async def load(self, url: str, wait_for: str | None = None, settle_ms: int = 1200):
        """Load a page and return (page, html). Caller must close the page.

        wait_for: a CSS selector that must appear before we consider the page
                  ready. For seat maps this is what stops you capturing an
                  empty shell — which is exactly the trap this whole project
                  nearly fell into.
        """
        last_err = None
        for attempt in range(1, self.s.max_retries + 1):
            await self._throttle()
            page = await self._ctx.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded")
                if wait_for:
                    await page.wait_for_selector(wait_for, state="attached")
                if settle_ms:
                    await page.wait_for_timeout(settle_ms)
                html = await page.content()
                self._archive(url, html)
                return page, html
            except Exception as e:  # noqa: BLE001
                last_err = e
                await page.close()
                backoff = min(60, 5 * (2 ** (attempt - 1)))
                await asyncio.sleep(backoff)
        raise RuntimeError(f"failed after {self.s.max_retries} attempts: {url}: {last_err}")
