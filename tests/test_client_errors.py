import unittest
from urllib.parse import parse_qs

import httpx

from qzone_bridge.client import QzoneClient
from qzone_bridge.errors import QzoneNeedsRebind, QzoneParseError, QzoneRequestError
from qzone_bridge.models import FeedEntry, SessionState


class ClientErrorMappingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        session = SessionState(
            uin=123456,
            cookies={"uin": "o123456", "p_uin": "o123456", "p_skey": "abc", "skey": "def"},
        )
        self.client = QzoneClient(session, timeout=1, max_retries=1)

    async def asyncTearDown(self):
        await self.client.close()

    async def _use_response(self, response_factory):
        await self.client._client.aclose()

        def handler(request: httpx.Request) -> httpx.Response:
            return response_factory(request)

        self.client._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            timeout=httpx.Timeout(1),
            follow_redirects=False,
            trust_env=False,
        )

    async def test_302_is_login_expired(self):
        async def setup():
            await self._use_response(
                lambda request: httpx.Response(
                    302,
                    headers={"location": "https://ui.ptlogin2.qq.com/"},
                    request=request,
                )
            )

        await setup()
        with self.assertRaises(QzoneNeedsRebind) as caught:
            await self.client._request_text("GET", "https://h5.qzone.qq.com/mqzone/index")
        self.assertEqual(caught.exception.detail["status_code"], 302)
        self.assertTrue(
            str(caught.exception.detail["url"]).startswith("https://h5.qzone.qq.com/mqzone/index")
        )

    async def test_qzone_home_302_is_not_login_expired(self):
        await self._use_response(
            lambda request: httpx.Response(
                302,
                headers={"location": "https://user.qzone.qq.com/123456"},
                request=request,
            )
        )
        with self.assertRaises(QzoneRequestError) as caught:
            await self.client._request_text("GET", "https://h5.qzone.qq.com/mqzone/index")
        self.assertNotIsInstance(caught.exception, QzoneNeedsRebind)
        self.assertEqual(caught.exception.status_code, 302)

    async def test_403_is_permission_error(self):
        await self._use_response(lambda request: httpx.Response(403, text="forbidden", request=request))
        with self.assertRaises(QzoneRequestError) as caught:
            await self.client._request_text("GET", "https://h5.qzone.qq.com/mqzone/profile")
        self.assertEqual(caught.exception.status_code, 403)
        self.assertIn("??", caught.exception.message)

    async def test_payload_login_code_requires_rebind(self):
        await self._use_response(
            lambda request: httpx.Response(200, json={"code": -3000, "message": "?????"}, request=request)
        )
        with self.assertRaises(QzoneNeedsRebind):
            await self.client._request_json("GET", "https://mobile.qzone.qq.com/feeds/mfeeds_get_count")

    async def test_payload_login_code_inside_data_requires_rebind(self):
        await self._use_response(
            lambda request: httpx.Response(200, json={"data": {"code": -3000, "message": "?????"}}, request=request)
        )
        with self.assertRaises(QzoneNeedsRebind):
            await self.client._request_json("GET", "https://mobile.qzone.qq.com/feeds/mfeeds_get_count")

    async def test_generic_negative_code_is_not_forced_to_rebind(self):
        await self._use_response(
            lambda request: httpx.Response(200, json={"code": -10000, "message": "????"}, request=request)
        )
        self.assertFalse(self.client._payload_needs_rebind(-10000, "????"))
        with self.assertRaises(QzoneRequestError) as caught:
            await self.client._request_json("GET", "https://mobile.qzone.qq.com/feeds/mfeeds_get_count")
        self.assertNotIsInstance(caught.exception, QzoneNeedsRebind)

    async def test_jsonish_qzone_payload_falls_back_to_js_literal_parser(self):
        text = """
        {
            "code":0,
            "subcode":0,
            "message":"",
            "default":0,
            "data": {
                main:{
                    attach:'',
                    hasMoreFeeds:true,
                    pagenum:'2',
                    externparam:'basetime=1778625269&pagenum=2',
                    data:[{uin:123456, tid:'fid-1', content:'hello'}]
                }
            }
        }
        """
        await self._use_response(lambda request: httpx.Response(200, text=text, request=request))

        payload = await self.client._request_json(
            "GET",
            "https://user.qzone.qq.com/proxy/domain/ic2.qzone.qq.com/cgi-bin/feeds/feeds3_html_more",
        )

        self.assertTrue(payload["main"]["hasMoreFeeds"])
        self.assertEqual(payload["main"]["data"][0]["tid"], "fid-1")

    async def test_plain_non_json_text_still_fails_as_parse_error(self):
        await self._use_response(lambda request: httpx.Response(200, text="ok", request=request))

        with self.assertRaises(QzoneParseError):
            await self.client._request_json("GET", "https://mobile.qzone.qq.com/feeds/mfeeds_get_count")

    async def test_like_uses_cached_legacy_unikey_and_curkey(self):
        seen_form = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen_form.update(parse_qs(request.content.decode()))
            return httpx.Response(200, json={"code": 0, "message": "ok"}, request=request)

        await self._use_response(handler)
        self.client.feed_cache[(123456, "fid-1")] = FeedEntry(
            hostuin=123456,
            fid="fid-1",
            appid=311,
            summary="hello",
            curkey="cached-curkey",
            unikey="cached-unikey",
        )

        payload = await self.client.like_post(123456, "fid-1")

        self.assertEqual(payload["message"], "ok")
        self.assertEqual(seen_form["curkey"][0], "cached-curkey")
        self.assertEqual(seen_form["unikey"][0], "cached-unikey")
        self.assertEqual(seen_form["hostuin"][0], "123456")
        self.assertEqual(seen_form["fid"][0], "fid-1")
        self.assertEqual(seen_form["uin"][0], "123456")
        self.assertEqual(seen_form["from"][0], "1")
        self.assertEqual(seen_form["fupdate"][0], "1")

    async def test_like_tries_proxy_endpoint_first(self):
        seen_requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen_requests.append(request)
            return httpx.Response(200, json={"code": 0, "message": "ok"}, request=request)

        await self._use_response(handler)

        payload = await self.client.like_post(123456, "fid-1")

        self.assertEqual(payload["message"], "ok")
        self.assertEqual(len(seen_requests), 1)
        self.assertEqual(seen_requests[0].url.host, "user.qzone.qq.com")
        self.assertEqual(
            seen_requests[0].url.path,
            "/proxy/domain/w.qzone.qq.com/cgi-bin/likes/internal_dolike_app",
        )

    async def test_like_falls_back_to_direct_when_proxy_is_unavailable(self):
        seen_hosts = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen_hosts.append(request.url.host)
            if request.url.host == "user.qzone.qq.com":
                return httpx.Response(503, text="proxy unavailable", request=request)
            return httpx.Response(200, json={"code": 0, "message": "ok"}, request=request)

        await self._use_response(handler)

        payload = await self.client.like_post(123456, "fid-1")

        self.assertEqual(payload["message"], "ok")
        self.assertEqual(seen_hosts, ["user.qzone.qq.com", "w.qzone.qq.com"])

    async def test_like_reports_all_endpoint_attempts_when_both_fail(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, text=f"{request.url.host} unavailable", request=request)

        await self._use_response(handler)

        with self.assertRaises(QzoneRequestError) as caught:
            await self.client.like_post(123456, "fid-1")

        self.assertIn("?????????", caught.exception.message)
        self.assertEqual(caught.exception.status_code, 503)
        attempts = caught.exception.detail["attempts"]
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0]["status_code"], 503)
        self.assertEqual(attempts[0]["detail"]["text"], "user.qzone.qq.com unavailable")
        self.assertEqual(attempts[1]["detail"]["text"], "w.qzone.qq.com unavailable")

    async def test_like_accepts_post_action_redirect_without_following_page(self):
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            return httpx.Response(
                302,
                headers={"location": "https://user.qzone.qq.com/123456/mood/fid-1"},
                request=request,
            )

        await self._use_response(handler)

        payload = await self.client.like_post(123456, "fid-1")

        self.assertEqual(payload["message"], "accepted redirect")
        self.assertEqual(payload["redirect"]["status_code"], 302)
        self.assertEqual(len(seen), 1)

    async def test_like_follows_qzone_redirect_preserving_post_body(self):
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            form = parse_qs(request.content.decode())
            seen.append((request.method, str(request.url), form))
            if len(seen) == 1:
                return httpx.Response(
                    302,
                    headers={
                        "location": "https://w.qzone.qq.com/cgi-bin/likes/internal_dolike_app?redirected=1"
                    },
                    request=request,
                )
            return httpx.Response(200, json={"code": 0, "message": "ok"}, request=request)

        await self._use_response(handler)

        payload = await self.client.like_post(123456, "fid-1")

        self.assertEqual(payload["message"], "ok")
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[1][0], "POST")
        self.assertIn("redirected=1", seen[1][1])
        self.assertEqual(seen[1][2]["hostuin"][0], "123456")
        self.assertEqual(seen[1][2]["fid"][0], "fid-1")

    async def test_legacy_feed_follows_qq_domain_redirect(self):
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            if len(seen) == 1:
                return httpx.Response(
                    302,
                    headers={
                        "location": (
                            "https://taotao.qq.com/cgi-bin/emotion_cgi_msglist_v6"
                            "?redirected=1"
                        )
                    },
                    request=request,
                )
            return httpx.Response(
                200,
                text='_Callback({"msglist":[{"tid":"fid-1","uin":123456,"content":"ok"}]})',
                request=request,
            )

        await self._use_response(handler)

        payload = await self.client.legacy_feeds(123456, page=1, num=1)

        self.assertEqual(payload["msglist"][0]["tid"], "fid-1")
        self.assertEqual(len(seen), 2)
        self.assertIn("taotao.qq.com", seen[1])
        self.assertIn("redirected=1", seen[1])
        self.assertIn("g_tk=", seen[1])

    async def test_like_login_redirect_requires_rebind(self):
        await self._use_response(
            lambda request: httpx.Response(
                302,
                headers={"location": "https://ptlogin2.qq.com/login"},
                request=request,
            )
        )

        with self.assertRaises(QzoneNeedsRebind):
            await self.client.like_post(123456, "fid-1")


if __name__ == "__main__":
    unittest.main()
