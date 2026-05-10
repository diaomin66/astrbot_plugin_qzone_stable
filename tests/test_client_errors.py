import unittest

import httpx

from qzone_bridge.client import QzoneClient
from qzone_bridge.errors import QzoneNeedsRebind, QzoneRequestError
from qzone_bridge.models import SessionState


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

    async def test_403_is_permission_error(self):
        await self._use_response(lambda request: httpx.Response(403, text="forbidden", request=request))
        with self.assertRaises(QzoneRequestError) as caught:
            await self.client._request_text("GET", "https://h5.qzone.qq.com/mqzone/profile")
        self.assertEqual(caught.exception.status_code, 403)
        self.assertIn("权限", caught.exception.message)

    async def test_payload_login_code_requires_rebind(self):
        await self._use_response(
            lambda request: httpx.Response(200, json={"code": -3000, "message": "登录态失效"}, request=request)
        )
        with self.assertRaises(QzoneNeedsRebind):
            await self.client._request_json("GET", "https://mobile.qzone.qq.com/feeds/mfeeds_get_count")

    async def test_generic_negative_code_is_not_forced_to_rebind(self):
        await self._use_response(
            lambda request: httpx.Response(200, json={"code": -10000, "message": "系统繁忙"}, request=request)
        )
        with self.assertRaises(QzoneRequestError) as caught:
            await self.client._request_json("GET", "https://mobile.qzone.qq.com/feeds/mfeeds_get_count")
        self.assertNotIsInstance(caught.exception, QzoneNeedsRebind)


if __name__ == "__main__":
    unittest.main()
