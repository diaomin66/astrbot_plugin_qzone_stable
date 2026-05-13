import asyncio
import unittest
from urllib.parse import parse_qs

import httpx

from qzone_bridge.client import QzoneClient
from qzone_bridge.errors import QzoneRequestError
from qzone_bridge.models import SessionState


class ClientMediaTests(unittest.IsolatedAsyncioTestCase):
    async def test_publish_mood_uploads_image_and_sends_rich_fields(self):
        seen_upload = False
        seen_publish = False

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal seen_upload, seen_publish
            form = parse_qs(request.content.decode())
            if request.url.path.endswith("/cgi_upload_image"):
                seen_upload = True
                self.assertEqual(form["base64"][0], "1")
                self.assertEqual(form["filename"][0], "photo.jpg")
                self.assertEqual(form["uin"][0], "123456")
                self.assertTrue(form["picfile"][0])
                return httpx.Response(
                    200,
                    text=(
                        '_Callback({"ret":0,"data":{"albumid":"album-1","lloc":"lloc-1",'
                        '"sloc":"sloc-1","type":1,"height":10,"width":20,'
                        '"url":"https://qzone.qq.com/photo?bo=bo-token!!x"}})'
                    ),
                    request=request,
                )

            seen_publish = True
            self.assertEqual(form["con"][0], "hello")
            self.assertEqual(form["richtype"][0], "1")
            self.assertEqual(form["subrichtype"][0], "1")
            self.assertEqual(form["pic_bo"][0], "bo-token")
            self.assertEqual(form["richval"][0], ",album-1,lloc-1,sloc-1,1,10,20,,10,20")
            return httpx.Response(200, json={"code": 0, "tid": "fid-1"}, request=request)

        client = QzoneClient(
            SessionState(
                uin=123456,
                cookies={"uin": "o123456", "p_uin": "o123456", "p_skey": "abc"},
            )
        )
        await client._client.aclose()
        client._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            trust_env=False,
            headers={"User-Agent": client.user_agent},
        )
        try:
            payload = await client.publish_mood(
                "hello",
                photos=[{"kind": "image", "source": "base64://aGVsbG8=", "name": "photo.jpg"}],
            )
        finally:
            await client.close()

        self.assertTrue(seen_upload)
        self.assertTrue(seen_publish)
        self.assertEqual(payload["tid"], "fid-1")

    async def test_publish_mood_reuses_prepared_photo_payload(self):
        upload_called = False

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal upload_called
            if request.url.path.endswith("/cgi_upload_image"):
                upload_called = True
            form = parse_qs(request.content.decode())
            self.assertEqual(form["richval"][0], ",album,lloc,sloc,1,1,1,,1,1")
            self.assertEqual(form["pic_bo"][0], "bo")
            return httpx.Response(200, json={"code": 0, "tid": "fid-2"}, request=request)

        client = QzoneClient(
            SessionState(
                uin=123456,
                cookies={"uin": "o123456", "p_uin": "o123456", "p_skey": "abc"},
            )
        )
        await client._client.aclose()
        client._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            trust_env=False,
            headers={"User-Agent": client.user_agent},
        )
        try:
            payload = await client.publish_mood(
                "hello",
                photos=[{"richval": ",album,lloc,sloc,1,1,1,,1,1", "pic_bo": "bo"}],
            )
        finally:
            await client.close()

        self.assertFalse(upload_called)
        self.assertEqual(payload["tid"], "fid-2")

    async def test_prepare_publish_photos_uploads_concurrently_and_preserves_order(self):
        client = QzoneClient(
            SessionState(
                uin=123456,
                cookies={"uin": "o123456", "p_uin": "o123456", "p_skey": "abc"},
            )
        )
        active = 0
        max_active = 0

        async def upload_photo(photo):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.01)
            active -= 1
            return {"richval": f"rich-{photo['name']}", "pic_bo": photo["name"]}

        client.upload_photo = upload_photo
        try:
            payload = await client._prepare_publish_photos(
                [
                    {"kind": "image", "source": "base64://MQ==", "name": "a.jpg"},
                    {"kind": "image", "source": "base64://Mg==", "name": "b.jpg"},
                    {"kind": "image", "source": "base64://Mw==", "name": "c.jpg"},
                ]
            )
        finally:
            await client.close()

        self.assertGreater(max_active, 1)
        self.assertEqual([item["richval"] for item in payload], ["rich-a.jpg", "rich-b.jpg", "rich-c.jpg"])

    async def test_prepare_publish_photos_retries_only_failed_parallel_uploads(self):
        client = QzoneClient(
            SessionState(
                uin=123456,
                cookies={"uin": "o123456", "p_uin": "o123456", "p_skey": "abc"},
            )
        )
        attempts = {}

        async def upload_photo(photo):
            name = photo["name"]
            attempts[name] = attempts.get(name, 0) + 1
            if name == "b.jpg" and attempts[name] == 1:
                raise QzoneRequestError("temporary upload failure")
            return {"richval": f"rich-{name}", "pic_bo": name}

        client.upload_photo = upload_photo
        try:
            payload = await client._prepare_publish_photos(
                [
                    {"kind": "image", "source": "base64://MQ==", "name": "a.jpg"},
                    {"kind": "image", "source": "base64://Mg==", "name": "b.jpg"},
                    {"kind": "image", "source": "base64://Mw==", "name": "c.jpg"},
                ]
            )
        finally:
            await client.close()

        self.assertEqual(attempts, {"a.jpg": 1, "b.jpg": 2, "c.jpg": 1})
        self.assertEqual([item["richval"] for item in payload], ["rich-a.jpg", "rich-b.jpg", "rich-c.jpg"])

    async def test_upload_photo_reuses_downloaded_remote_image_bytes(self):
        download_calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal download_calls
            if request.url.path == "/photo.jpg":
                download_calls += 1
                return httpx.Response(
                    200,
                    content=b"image-bytes",
                    headers={"content-type": "image/jpeg"},
                    request=request,
                )
            return httpx.Response(
                200,
                text=(
                    '_Callback({"ret":0,"data":{"albumid":"album","lloc":"lloc",'
                    '"sloc":"sloc","type":1,"height":1,"width":1}})'
                ),
                request=request,
            )

        client = QzoneClient(
            SessionState(
                uin=123456,
                cookies={"uin": "o123456", "p_uin": "o123456", "p_skey": "abc"},
            )
        )
        await client._client.aclose()
        client._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            trust_env=False,
            headers={"User-Agent": client.user_agent},
        )
        try:
            media = {"kind": "image", "source": "https://example.com/photo.jpg", "name": "photo.jpg"}
            await client.upload_photo(media)
            await client.upload_photo(media)
        finally:
            await client.close()

        self.assertEqual(download_calls, 1)


if __name__ == "__main__":
    unittest.main()
