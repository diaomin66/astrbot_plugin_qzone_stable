import unittest

from qzone_bridge.parser import (
    extract_feed_entry,
    extract_feed_page,
    feed_page_cursor,
    feed_page_has_more,
    parse_cookie_text,
    parse_index_html,
    parse_profile_html,
    unwrap_payload,
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

    def test_extract_legacy_feed_entry(self):
        raw = {
            "tid": "legacy-fid",
            "uin": 123456,
            "name": "Alice",
            "content": "legacy hello",
            "created_time": 1715330001,
            "commentlist": [{"content": "x"}],
        }
        entry = extract_feed_entry(raw)
        self.assertEqual(entry.hostuin, 123456)
        self.assertEqual(entry.fid, "legacy-fid")
        self.assertEqual(entry.summary, "legacy hello")
        self.assertEqual(entry.curkey, "https://user.qzone.qq.com/123456/mood/legacy-fid")

    def test_extract_feed_page_accepts_msglist(self):
        feedpage, items = extract_feed_page({"msglist": [{"tid": "fid-1", "uin": 123456, "content": "hello"}]})
        self.assertEqual(feedpage["msglist"][0]["tid"], "fid-1")
        self.assertEqual(items[0].fid, "fid-1")

    def test_extract_feed_page_accepts_legacy_main_container(self):
        feedpage, items = extract_feed_page(
            {
                "main": {
                    "hasMoreFeeds": True,
                    "attach": "",
                    "externparam": "basetime=1778625269&pagenum=2",
                    "data": [
                        {
                            "key": "fid-legacy",
                            "nickname": "Alice",
                            "html": (
                                "<div data-uin='123456' data-fid='fid-legacy' "
                                "data-appid='311' data-curkey='cur-key' "
                                "data-unikey='uni-key'>hello<br>legacy</div>"
                            ),
                        }
                    ],
                }
            }
        )
        self.assertTrue(feed_page_has_more(feedpage))
        self.assertEqual(feed_page_cursor(feedpage), "basetime=1778625269&pagenum=2")
        self.assertEqual(items[0].fid, "fid-legacy")
        self.assertEqual(items[0].hostuin, 123456)
        self.assertEqual(items[0].curkey, "cur-key")
        self.assertEqual(items[0].unikey, "uni-key")
        self.assertIn("hello", items[0].summary)

    def test_unwrap_payload_keeps_legacy_data_behavior(self):
        payload = {"data": {"code": -3000, "message": "登录态失效"}}
        self.assertEqual(unwrap_payload(payload), payload["data"])
        payload = {"code": -3000, "data": {"x": 1}, "message": "登录态失效"}
        self.assertEqual(unwrap_payload(payload), {"x": 1})


if __name__ == "__main__":
    unittest.main()
