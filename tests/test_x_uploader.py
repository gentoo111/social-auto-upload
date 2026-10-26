import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from uploader.x_uploader import main as x


class FakeResponse:
    def __init__(self, body, status=200):
        self.url = "https://x.com/i/api/graphql/abc/CreateTweet"
        self.status = status
        self._body = body

    async def text(self):
        return json.dumps(self._body)


class FakeButton:
    def __init__(self, page):
        self.page = page
        self.first = self

    async def click(self):
        if self.page.reply is not None:
            self.page.handler(self.page.reply)


class FakePage:
    def __init__(self, reply):
        self.reply = reply
        self.handler = None

    def on(self, event, handler):
        self.handler = handler

    def locator(self, selector):
        return FakeButton(self)


class XUploaderTests(unittest.TestCase):
    def test_weighted_length_follows_x_counting(self):
        self.assertEqual(x.weighted_length("hello"), 5)
        self.assertEqual(x.weighted_length("你好"), 4)
        self.assertEqual(x.weighted_length("try https://apps.apple.com/app/id123456789 now"), 4 + 23 + 4)
        self.assertEqual(x.weighted_length("❤️"), 2)

    def test_long_text_and_bad_media_are_refused_before_a_browser(self):
        with self.assertRaises(ValueError):
            x.XPost("字" * 141, "a.json")
        with self.assertRaises(ValueError):
            x.XPost("", "a.json")
        with tempfile.TemporaryDirectory() as root:
            files = []
            for name in ("a.png", "b.png", "c.mp4", "d.txt"):
                (Path(root) / name).write_bytes(b"x")
                files.append(str(Path(root) / name))
            self.assertEqual(len(x.XPost("ok", "a.json", files[:2]).media), 2)
            for bad in ([files[0], files[2]], [files[3]], [files[0]] * 5, [root + "/missing.png"]):
                with self.assertRaises(ValueError):
                    x.XPost("ok", "a.json", bad)

    def test_create_tweet_response_decides_the_outcome(self):
        post = x.XPost("hi", "a.json")
        ok = {"data": {"create_tweet": {"tweet_results": {"result": {"rest_id": "1849"}}}}}
        asyncio.run(post._publish(FakePage(FakeResponse(ok))))
        self.assertEqual(post.url, "https://x.com/i/status/1849")

        duplicate = {"errors": [{"code": 187, "message": "Status is a duplicate."}]}
        with self.assertRaisesRegex(RuntimeError, "未发布"):
            asyncio.run(x.XPost("hi", "a.json")._publish(FakePage(FakeResponse(duplicate))))

        with self.assertRaisesRegex(RuntimeError, "不要重发"):
            asyncio.run(x.XPost("hi", "a.json")._publish(FakePage(FakeResponse({"data": {}}))))


if __name__ == "__main__":
    unittest.main()
