import unittest

from qzone_bridge.parser import (
    extract_feed_entry,
    parse_cookie_text,
    parse_index_html,
    parse_profile_html,
)
from qzone_bridge.utils import gtk


class ParserTests(unittest.TestCase):
    def test_parse_cookie_text_and_gtk(self):
        cookies = parse_cookie_text("p_skey=abc; p_uin=o123456; uin=o123456; skey=def")
        self.assertEqual(cookies["p_skey"], "abc")
        self.assertEqual(cookies["p_uin"], "o123456")
        self.assertEqual(gtk("abc"), 193485963)

    def test_parse_index_html(self):
        html = """
        <html><body>
          <script type="application/javascript">
            window.shine0callback = function(){ return "abcdef"; };
            var FrontPage = { data: { hasmore: false, attachinfo: "next", newcnt: 0, vFeeds: [] } };
          </script>
        </body></html>
        """
        payload = parse_index_html(html)
        self.assertEqual(payload["qzonetoken"], "abcdef")
        self.assertEqual(payload["attachinfo"], "next")

    def test_parse_profile_html(self):
        html = """
        <html><body>
          <script type="application/javascript">
            window.shine0callback = function(){ return "123abc"; };
            var FrontPage = { data: [
              { code: 0, data: { profile: { nickname: "Alice" } } },
              { code: 0, data: { hasmore: false, attachinfo: "", newcnt: 0, vFeeds: [] } }
            ] };
          </script>
        </body></html>
        """
        payload = parse_profile_html(html)
        self.assertEqual(payload["qzonetoken"], "123abc")
        self.assertIn("feedpage", payload)

    def test_extract_feed_entry(self):
        raw = {
            "id": {"cellid": "fid-1"},
            "common": {
                "appid": 311,
                "time": 1715330000,
                "curlikekey": "cur-key",
                "ugcrightkey": "fid-1",
            },
            "userinfo": {"uin": 123456, "nickname": "Alice"},
            "summary": {"summary": "hello qzone"},
            "like": {"num": 3, "isliked": True},
            "comment": {"num": 2},
            "operation": {"busi_param": {"x": 1}},
        }
        entry = extract_feed_entry(raw)
        self.assertEqual(entry.hostuin, 123456)
        self.assertEqual(entry.fid, "fid-1")
        self.assertEqual(entry.curkey, "cur-key")
        self.assertEqual(entry.summary, "hello qzone")
        self.assertEqual(entry.unikey, "https://user.qzone.qq.com/123456/mood/fid-1")


if __name__ == "__main__":
    unittest.main()
