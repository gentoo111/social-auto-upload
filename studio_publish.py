"""Publisher Studio entry point (fork addition).

One job per run, read as JSON from stdin; one JSON result printed as the last stdout line.
Runs the upstream uploaders in per-account Chrome profiles (``SAU_PROFILE_ROOT`` is required).

    {"action": "publish", "platform": "douyin|tiktok|youtube|instagram", "account": "pets",
     "video": "/abs/clip.mp4", "title": "...", "description": "...", "tags": ["a"],
     "visibility": "private", "ai": true, "timeout_s": 1800}
    {"action": "login", "platform": "youtube", "account": "pets"}

Result: {"ok": bool, "code": "PUBLISHED|PUBLISH_UNCONFIRMED|LOGIN_REQUIRED|VERIFY_REQUIRED|
FAILED|BAD_REQUEST|LOGGED_IN", "message": str, "url": str|null}. ``PUBLISH_UNCONFIRMED`` means the
post may exist: check the platform, never repost automatically.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from pathlib import Path

PLATFORMS = ("douyin", "tiktok", "youtube", "instagram")
LOGIN_URLS = {
    "douyin": "https://creator.douyin.com/",
    "tiktok": "https://www.tiktok.com/tiktokstudio/upload?lang=en",
    "youtube": "https://studio.youtube.com/",
    "instagram": "https://www.instagram.com/",
}
LOGIN_HINT = re.compile(r"登录态失效|cookie|login|Log in|未登录|sign ?in", re.I)
UNCONFIRMED_HINT = re.compile(r"未确认|不要重发|submission_unknown|已提交")


def account_file(platform: str, account: str) -> Path:
    """Upstream keys accounts by a cookie-file path; with profiles only its stem is used."""
    safe = re.sub(r"[^\w.-]", "_", account)
    return Path(os.environ["SAU_PROFILE_ROOT"]) / f"{platform}_{safe}.json"


def caption(job: dict) -> str:
    return "\n".join(part for part in (job.get("title", "").strip(), job.get("description", "").strip()) if part)


async def publish(job: dict) -> dict:
    platform, file = job["platform"], account_file(job["platform"], job["account"])
    private, ai = job.get("visibility") == "private", bool(job.get("ai"))
    tags = [t.lstrip("#") for t in job.get("tags") or [] if t.strip("# ")]
    video = str(Path(job["video"]).resolve())
    if platform == "douyin":
        from uploader.douyin_uploader.main import DouYinVideo

        app = DouYinVideo(job["title"], video, tags, 0, str(file), desc=job.get("description", ""),
                          declaration="内容由AI生成" if ai else None,
                          visibility="private" if private else "public")
        await app.douyin_upload_video()
        return {"url": None}
    if platform == "tiktok":
        from uploader.tk_uploader.main_chrome import TiktokVideo

        app = TiktokVideo(caption(job), video, tags, 0, str(file),
                          visibility="private" if private else "public", ai_generated=ai)
        await app.main()
        return {"url": f"https://www.tiktok.com/video/{app.video_id}" if app.video_id else None}
    if platform == "youtube":
        from uploader.youtube_uploader.main import YouTubeVideo

        app = YouTubeVideo(job["title"], video, tags, str(file), description=job.get("description", ""),
                           visibility="private" if private else "public", altered_content=ai)
        await app.main()
        return {"url": app.video_url or None}
    if platform == "instagram":
        from uploader.meta_uploader.insgraph_uploader.main import InstagramUploader

        result_file = Path(os.environ["SAU_PROFILE_ROOT"]) / "results" / f"instagram-{os.getpid()}.json"
        app = InstagramUploader(str(file), title=job["title"], description=job.get("description", ""),
                                tags=tags, video_file=video, ai_label=ai, result_file=result_file)
        result = await app.upload()
        return {"url": result.get("post_url")}
    raise ValueError(f"unsupported platform: {platform}")


async def login(job: dict) -> dict:
    """Open the account's profile on the platform and wait until the owner closes the window."""
    from utils.profile_browser import async_playwright

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context(storage_state=str(account_file(job["platform"], job["account"])))
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto(LOGIN_URLS[job["platform"]])
        closed = asyncio.Event()
        context.on("close", lambda *_: closed.set())
        await closed.wait()
    return {"url": None}


def classify(error: BaseException) -> str:
    name, text = type(error).__name__, str(error)
    if name == "VerificationRequired" or "Verify it" in text:
        return "VERIFY_REQUIRED"
    if isinstance(error, (TimeoutError, asyncio.TimeoutError)) or UNCONFIRMED_HINT.search(text):
        return "PUBLISH_UNCONFIRMED"
    if LOGIN_HINT.search(text):
        return "LOGIN_REQUIRED"
    return "FAILED"


def validate(job: dict) -> str | None:
    if not os.environ.get("SAU_PROFILE_ROOT"):
        return "SAU_PROFILE_ROOT is not set"
    if job.get("platform") not in PLATFORMS:
        return f"platform must be one of {', '.join(PLATFORMS)}"
    if not isinstance(job.get("account"), str) or not job["account"].strip():
        return "account is required"
    if job.get("action", "publish") == "publish":
        if not job.get("title") or not job.get("video") or not Path(job["video"]).is_file():
            return "title and an existing video file are required"
        if job.get("visibility", "public") not in ("public", "private"):
            return "visibility must be public or private"
    return None


async def run(job: dict) -> dict:
    problem = validate(job)
    if problem:
        return {"ok": False, "code": "BAD_REQUEST", "message": problem, "url": None}
    action = job.get("action", "publish")
    try:
        if action == "login":
            await login(job)
            return {"ok": True, "code": "LOGGED_IN", "message": "login window closed", "url": None}
        result = await asyncio.wait_for(publish(job), timeout=float(job.get("timeout_s") or 1800))
        return {"ok": True, "code": "PUBLISHED", "message": "published", "url": result.get("url")}
    except BaseException as error:  # noqa: BLE001 — every failure becomes one result line
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        return {"ok": False, "code": classify(error), "message": str(error)[:500] or type(error).__name__,
                "url": None}


def main() -> int:
    try:
        job = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError as error:
        job, early = {}, {"ok": False, "code": "BAD_REQUEST", "message": f"invalid JSON: {error}", "url": None}
    else:
        early = None
    result = early or asyncio.run(run(job))
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
