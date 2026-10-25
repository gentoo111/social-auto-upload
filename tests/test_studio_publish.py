import asyncio
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import studio_publish as sp


class StudioPublishTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.video = Path(self.tmp.name) / "clip.mp4"
        self.video.write_bytes(b"x")
        self.env = mock.patch.dict(os.environ, {"SAU_PROFILE_ROOT": self.tmp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def job(self, **extra):
        return {"platform": "youtube", "account": "油管", "video": str(self.video), "title": "T",
                "description": "D", "tags": ["#dog", "bulldog"], "visibility": "private", "ai": True, **extra}

    def test_bad_requests_never_reach_a_browser(self):
        for job in ({}, self.job(platform="facebook"), self.job(video="/nope.mp4"), self.job(visibility="friends")):
            result = asyncio.run(sp.run(job))
            self.assertEqual(result["code"], "BAD_REQUEST")

    def test_youtube_job_maps_privacy_and_ai_onto_the_uploader(self):
        seen = {}

        class FakeYouTube:
            def __init__(self, title, video, tags, account_file, **kw):
                seen.update(title=title, video=video, tags=tags, account_file=account_file, **kw)
                self.video_url = "https://youtu.be/abc"

            async def main(self):
                pass

        module = types.SimpleNamespace(YouTubeVideo=FakeYouTube)
        with mock.patch.dict(sys.modules, {"uploader.youtube_uploader.main": module}):
            result = asyncio.run(sp.run(self.job()))
        self.assertEqual(result, {"ok": True, "code": "PUBLISHED", "message": "published", "url": "https://youtu.be/abc"})
        self.assertEqual(seen["visibility"], "private")
        self.assertTrue(seen["altered_content"])
        self.assertEqual(seen["tags"], ["dog", "bulldog"])
        self.assertEqual(Path(seen["account_file"]).name, "youtube_油管.json")

    def test_failures_are_classified(self):
        class VerificationRequired(RuntimeError):
            pass

        self.assertEqual(sp.classify(VerificationRequired("x")), "VERIFY_REQUIRED")
        self.assertEqual(sp.classify(TimeoutError("已点击 Post，但未确认")), "PUBLISH_UNCONFIRMED")
        self.assertEqual(sp.classify(RuntimeError("YouTube 登录态失效，请重新执行 login")), "LOGIN_REQUIRED")
        self.assertEqual(sp.classify(RuntimeError("未找到抖音「仅自己可见」，拒绝继续发布")), "FAILED")

    def test_main_prints_one_json_line(self):
        with mock.patch.object(sys, "stdin", types.SimpleNamespace(read=lambda: "not json")), \
                mock.patch("builtins.print") as out:
            self.assertEqual(sp.main(), 1)
        self.assertEqual(json.loads(out.call_args.args[0])["code"], "BAD_REQUEST")


if __name__ == "__main__":
    unittest.main()
