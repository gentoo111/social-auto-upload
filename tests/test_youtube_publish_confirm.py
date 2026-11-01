# Language: 中文
import asyncio
import unittest

from studio_publish import classify
from uploader.youtube_uploader import main as yt


class _Loc:
    def __init__(self, page, selector):
        self.page, self.selector = page, selector
        self.first = self

    async def count(self):
        return 1 if self.page.visible(self.selector) else 0

    async def is_visible(self):
        return self.page.visible(self.selector)

    async def wait_for(self, state="visible", timeout=0):
        if not self.page.visible(self.selector):
            raise TimeoutError(self.selector)

    async def click(self):
        self.page.clicked.append(self.selector)


class FakePage:
    """published_after_click: the share dialog appears only after the precheck Publish is clicked."""

    def __init__(self, share=False, prechecks=False):
        self.share, self.prechecks, self.clicked = share, prechecks, []

    def visible(self, selector):
        if selector == yt.PUBLISHED_DIALOGS:
            return False  # a hidden copy: only the visible-filtered locator may confirm
        if selector == yt.PUBLISHED_DIALOGS + " >> visible=true":
            return self.share or (self.prechecks and yt.PRECHECKS_PUBLISH in self.clicked)
        if selector == yt.PRECHECKS_PUBLISH:
            return self.prechecks and yt.PRECHECKS_PUBLISH not in self.clicked
        return False

    def locator(self, selector):
        return _Loc(self, selector)

    async def wait_for_timeout(self, ms):
        return None


class YouTubePublishConfirmTests(unittest.TestCase):
    def test_share_dialog_confirms(self):
        page = FakePage(share=True)
        self.assertTrue(asyncio.run(yt._confirm_published(page)))
        self.assertEqual(page.clicked, [])

    def test_precheck_warning_gets_second_publish(self):
        page = FakePage(prechecks=True)
        self.assertTrue(asyncio.run(yt._confirm_published(page)))
        self.assertEqual(page.clicked, [yt.PRECHECKS_PUBLISH])

    def test_no_confirmation_is_not_success(self):
        self.assertFalse(asyncio.run(yt._confirm_published(FakePage(), timeout_ms=3000)))

    def test_unconfirmed_error_is_never_retried(self):
        err = RuntimeError("已点 YouTube 发布但没看到发布确认，视频可能停在草稿：未确认，不要重发，请到 YouTube Studio 核对")
        self.assertEqual(classify(err), "PUBLISH_UNCONFIRMED")


if __name__ == "__main__":
    unittest.main()
