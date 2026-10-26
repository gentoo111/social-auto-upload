"""X (Twitter) web posting (fork addition; upstream has no X uploader).

Posts through x.com's own composer in the account's Chrome profile, the same way the other
fork uploaders run: no API key. One post per run: text, plus up to 4 images or 1 video. A post
with only text and a link gets X's link card (an App Store link shows the app's icon and name).

The post is confirmed by X's own CreateTweet response, which also gives the post id. A click
without that response is reported as unconfirmed, never retried.
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

from patchright.async_api import Page

from utils.log import x_logger
from utils.profile_browser import async_playwright

COMPOSE_URL = "https://x.com/compose/post"
TEXTBOX = '[data-testid="tweetTextarea_0"]'
POST_BUTTON = '[data-testid="tweetButton"]'
FILE_INPUT = 'input[data-testid="fileInput"]'
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm"}
MAX_WEIGHT = 280  # non-Premium accounts
URL = re.compile(r"https?://\S+")
# twitter-text v3: these code points weigh 1, everything else 2; every URL weighs 23.
_LIGHT = ((0, 4351), (8192, 8205), (8208, 8223), (8242, 8247))
UPLOAD_TIMEOUT_S = 600
CONFIRM_TIMEOUT_S = 60


class VerificationRequired(RuntimeError):
    """X asked to confirm the account (locked / unusual activity); left to the owner."""


def weighted_length(text: str) -> int:
    """X's character count: URLs 23, CJK and emoji 2 (slightly over-counts some emoji)."""
    total = 0
    for part in URL.split(text):
        for ch in part:
            code = ord(ch)
            if code == 0xFE0F:  # variation selector, not counted by X
                continue
            total += 1 if any(lo <= code <= hi for lo, hi in _LIGHT) else 2
    return total + 23 * len(URL.findall(text))


def check_media(files: list[str]) -> list[str]:
    paths = [Path(f) for f in files]
    for p in paths:
        if not p.is_file():
            raise ValueError(f"媒体文件不存在: {p}")
    kinds = {"image" if p.suffix.lower() in IMAGE_EXT else "video" if p.suffix.lower() in VIDEO_EXT
             else "other" for p in paths}
    if "other" in kinds:
        raise ValueError("X 只支持图片（jpg/png/gif/webp）或视频（mp4/mov/m4v/webm）")
    if "video" in kinds and len(paths) > 1:
        raise ValueError("X 一条帖子只能带 1 个视频，且不能和图片混发")
    if len(paths) > 4:
        raise ValueError("X 一条帖子最多 4 张图片")
    return [str(p.resolve()) for p in paths]


def _normal(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


class XPost:
    def __init__(self, text: str, account_file: str, media: list[str] | None = None):
        self.text = text.strip()
        self.account_file = account_file
        if not self.text and not media:
            raise ValueError("X 帖子需要正文或媒体")
        if weighted_length(self.text) > MAX_WEIGHT:
            raise ValueError(f"X 正文超过 {MAX_WEIGHT} 字符（中文和表情按 2 计，链接按 23 计）："
                             f"现在 {weighted_length(self.text)}")
        self.media = check_media(media or [])
        self.post_id: str | None = None

    @property
    def url(self) -> str | None:
        return f"https://x.com/i/status/{self.post_id}" if self.post_id else None

    async def _assert_session(self, page: Page) -> None:
        if re.search(r"/(i/flow/)?login|/logout", page.url):
            raise RuntimeError("X 登录态失效，请重新执行 login")
        if "/account/access" in page.url or await page.get_by_text(
                re.compile(r"Your account (is|has been) locked|unusual activity")).count():
            raise VerificationRequired("X 要求验证账号，未发布；请主人在该账号的 Chrome 里完成验证")

    async def _type(self, page: Page) -> None:
        box = page.locator(TEXTBOX).first
        await box.click()
        for index, line in enumerate(self.text.split("\n")):
            if index:
                await page.keyboard.press("Enter")
            if line:
                await page.keyboard.insert_text(line)
            await page.wait_for_timeout(150)
        # A trailing #hashtag or @mention leaves X's suggestion list open, and its invisible layer
        # covers the Post button; a space closes it (X trims trailing whitespace).
        await page.keyboard.press("Space")
        await page.wait_for_timeout(800)
        typed = await box.inner_text()
        if _normal(typed) != _normal(self.text):
            raise RuntimeError(f"X 正文没有完整写入（写入的是 {typed[:80]!r}），未发布")

    async def _wait_ready(self, page: Page) -> None:
        button = page.locator(POST_BUTTON).first
        deadline = asyncio.get_running_loop().time() + UPLOAD_TIMEOUT_S
        while asyncio.get_running_loop().time() < deadline:
            if await button.count() and await button.get_attribute("aria-disabled") != "true":
                return
            await page.wait_for_timeout(2000)
        raise RuntimeError("X 媒体上传超时，发布按钮一直不可用，未发布")

    async def _publish(self, page: Page) -> None:
        responses: asyncio.Queue = asyncio.Queue()

        def on_response(response):
            if "/CreateTweet" in response.url:
                responses.put_nowait(response)

        page.on("response", on_response)
        try:
            await page.locator(POST_BUTTON).first.click(timeout=15000)
        except Exception:
            # The click never fired (something still covers the button): post with X's own
            # shortcut from the text box instead.
            x_logger.info("[x] Post button covered; using Cmd/Ctrl+Enter")
            await page.locator(TEXTBOX).first.focus()
            await page.keyboard.press("ControlOrMeta+Enter")
        try:
            response = await asyncio.wait_for(responses.get(), CONFIRM_TIMEOUT_S)
        except asyncio.TimeoutError:
            raise RuntimeError("已点击 X 发布，但未确认（没有收到 CreateTweet 响应），不要重发") from None
        try:
            body = json.loads(await response.text())
        except Exception:
            raise RuntimeError(f"已点击 X 发布，但未确认（响应 {response.status} 无法解析），不要重发") from None
        errors = body.get("errors") or []
        result = (((body.get("data") or {}).get("create_tweet") or {}).get("tweet_results") or {}).get("result") or {}
        if result.get("rest_id"):
            self.post_id = result["rest_id"]
            return
        if errors:
            # X rejected the post (duplicate text 187, automation 226, …): nothing was published.
            detail = "; ".join(f"{e.get('code')}: {e.get('message')}" for e in errors)[:300]
            raise RuntimeError(f"X 拒绝了这条帖子，未发布：{detail}")
        raise RuntimeError(f"已点击 X 发布，但未确认（响应 {response.status} 没有帖子 id），不要重发")

    async def main(self) -> None:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=False)
            context = await browser.new_context(storage_state=self.account_file)
            try:
                page = context.pages[0] if context.pages else await context.new_page()
                await page.goto(COMPOSE_URL, wait_until="domcontentloaded")
                await page.wait_for_timeout(4000)
                await self._assert_session(page)
                await page.locator(TEXTBOX).first.wait_for(state="visible", timeout=30000)
                if self.media:
                    x_logger.info(f"[x] uploading {len(self.media)} media file(s)")
                    await page.locator(FILE_INPUT).first.set_input_files(self.media)
                    await page.wait_for_timeout(2000)
                await self._type(page)
                await self._wait_ready(page)
                await self._assert_session(page)
                await self._publish(page)
                x_logger.success(f"[x] published {self.url}")
                await page.wait_for_timeout(3000)
            finally:
                await browser.close()
