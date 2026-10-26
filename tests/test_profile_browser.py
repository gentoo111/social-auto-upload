import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from utils import profile_browser


class FakeContext:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


class FakeChromium:
    def __init__(self):
        self.calls = []

    async def launch_persistent_context(self, user_data_dir, **options):
        self.calls.append((user_data_dir, options))
        return FakeContext()


class ProfileBrowserTests(unittest.TestCase):
    def test_new_context_opens_the_account_profile_in_real_chrome(self):
        with tempfile.TemporaryDirectory() as root, mock.patch.dict(os.environ, {"SAU_PROFILE_ROOT": root}):
            chromium = FakeChromium()
            proxy = profile_browser._ProfileChromium(chromium)

            async def run():
                browser = await proxy.launch(headless=True, args=["--no-sandbox"], proxy={"server": "http://p"})
                context = await browser.new_context(
                    storage_state="cookies/douyin_宠物.json", viewport={"width": 1}, user_agent="x", locale="zh-CN")
                await browser.close()
                return context

            context = asyncio.run(run())
            [(user_data_dir, options)] = chromium.calls
            self.assertEqual(Path(user_data_dir), Path(root) / "douyin_宠物")
            self.assertTrue(Path(user_data_dir).is_dir())
            self.assertEqual(options, {"channel": "chrome", "headless": False, "no_viewport": True,
                                       "chromium_sandbox": True, "proxy": {"server": "http://p"}})
            self.assertTrue(context.closed)

    def test_disabled_without_profile_root(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(profile_browser.enabled())

    def test_init_script_is_skipped_for_profiles(self):
        from utils.base_social_media import set_init_script

        context = mock.AsyncMock()
        with mock.patch.dict(os.environ, {"SAU_PROFILE_ROOT": "/tmp/x"}):
            self.assertIs(asyncio.run(set_init_script(context)), context)
        context.add_init_script.assert_not_called()


if __name__ == "__main__":
    unittest.main()
