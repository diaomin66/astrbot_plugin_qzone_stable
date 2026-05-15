import asyncio
import socket
import warnings
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

import httpx
from aiohttp import web
from aiohttp.web_app import NotAppKeyWarning

from qzone_bridge.controller import QzoneDaemonController, _port_is_free
from qzone_bridge.daemon import QzoneDaemonService, create_app
from qzone_bridge.errors import QzoneNeedsRebind, QzoneRequestError
from qzone_bridge.models import FeedEntry, SessionState
from qzone_bridge.protocol import SECRET_HEADER
from qzone_bridge.storage import StateStore


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class DaemonStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_warmup_uses_json_health_check_instead_of_h5_index(self):
        with TemporaryDirectory() as tmp:
            service = QzoneDaemonService(StateStore(Path(tmp)), secret="secret", port=free_port())
            service.state.session = SessionState(
                uin=123456,
                cookies={"uin": "o123456", "p_uin": "o123456", "p_skey": "abc"},
            )
            service.client.update_session(service.state.session)
            try:
                with patch.object(service.client, "index", new=AsyncMock()) as index, patch.object(
                    service.client,
                    "mfeeds_get_count",
                    new=AsyncMock(return_value={"code": 0}),
                ) as count:
                    await service.warmup()
                    index.assert_not_awaited()
                    count.assert_awaited_once()
                self.assertFalse(service.state.session.needs_rebind)
            finally:
                await service.close()

    async def test_4xx_request_error_does_not_degrade_session_health(self):
        with TemporaryDirectory() as tmp:
            service = QzoneDaemonService(StateStore(Path(tmp)), secret="secret", port=free_port())
            service.state.session = SessionState(uin=123456, cookies={"uin": "o123456", "p_skey": "abc"})
            service.client.update_session(service.state.session)
            service.health_state = "ready"

            service._set_error(QzoneRequestError("?????", status_code=403))

            self.assertEqual(service.health_state, "ready")
            self.assertFalse(service.state.session.needs_rebind)
            await service.close()

    async def test_auth_error_clears_tokens_and_feed_cache(self):
        with TemporaryDirectory() as tmp:
            service = QzoneDaemonService(StateStore(Path(tmp)), secret="secret", port=free_port())
            service.state.session = SessionState(
                uin=123456,
                cookies={"uin": "o123456", "p_skey": "abc"},
                qzonetokens={"123456": "token"},
            )
            service.client.update_session(service.state.session)
            service.client.feed_cache[(123456, "fid-1")] = FeedEntry(
                hostuin=123456,
                fid="fid-1",
                appid=311,
                summary="hello",
            )

            service._set_error(QzoneNeedsRebind())

            self.assertEqual(service.health_state, "needs_rebind")
            self.assertTrue(service.state.session.needs_rebind)
            self.assertEqual(service.state.session.qzonetokens, {})
            self.assertEqual(service.client.feed_cache, {})
            await service.close()

    async def test_list_feeds_falls_back_when_h5_home_redirects(self):
        with TemporaryDirectory() as tmp:
            service = QzoneDaemonService(StateStore(Path(tmp)), secret="secret", port=free_port())
            service.state.session = SessionState(
                uin=123456,
                cookies={"uin": "o123456", "p_uin": "o123456", "p_skey": "abc"},
            )
            service.client.update_session(service.state.session)
            try:
                with patch.object(
                    service.client,
                    "index",
                    new=AsyncMock(side_effect=QzoneRequestError("h5 redirect", status_code=302)),
                ) as index, patch.object(
                    service.client,
                    "legacy_recent_feeds",
                    new=AsyncMock(
                        return_value={
                            "main": {
                                "hasMoreFeeds": False,
                                "data": [{"tid": "fid-1", "uin": 123456, "content": "hello"}],
                            }
                        }
                    ),
                ) as legacy:
                    payload = await service.list_feeds(limit=1)
                index.assert_awaited_once()
                legacy.assert_awaited_once()
                self.assertEqual(payload["items"][0]["fid"], "fid-1")
            finally:
                await service.close()

    async def test_profile_list_feeds_falls_back_when_h5_redirects(self):
        with TemporaryDirectory() as tmp:
            service = QzoneDaemonService(StateStore(Path(tmp)), secret="secret", port=free_port())
            service.state.session = SessionState(
                uin=123456,
                cookies={"uin": "o123456", "p_uin": "o123456", "p_skey": "abc"},
            )
            service.client.update_session(service.state.session)
            try:
                with patch.object(
                    service.client,
                    "profile",
                    new=AsyncMock(side_effect=QzoneRequestError("profile redirect", status_code=302)),
                ) as profile, patch.object(
                    service.client,
                    "legacy_feeds",
                    new=AsyncMock(
                        return_value={
                            "msglist": [
                                {"tid": "fid-latest", "uin": 123456, "content": "latest"}
                            ]
                        }
                    ),
                ) as legacy:
                    payload = await service.list_feeds(hostuin=123456, limit=1, scope="profile")
                profile.assert_awaited_once()
                legacy.assert_awaited_once_with(123456, page=1, num=20)
                self.assertEqual(payload["items"][0]["fid"], "fid-latest")
            finally:
                await service.close()

    async def test_publish_post_does_not_probe_h5_index_for_token(self):
        with TemporaryDirectory() as tmp:
            service = QzoneDaemonService(StateStore(Path(tmp)), secret="secret", port=free_port())
            service.state.session = SessionState(
                uin=123456,
                cookies={"uin": "o123456", "p_uin": "o123456", "p_skey": "abc"},
            )
            service.client.update_session(service.state.session)
            try:
                with patch.object(
                    service.client,
                    "index",
                    new=AsyncMock(side_effect=QzoneRequestError("h5 redirect", status_code=302)),
                ) as index, patch.object(
                    service.client,
                    "publish_mood",
                    new=AsyncMock(return_value={"fid": "fid-1", "message": "ok"}),
                ) as publish:
                    payload = await service.publish_post(content="hello")
                index.assert_not_awaited()
                publish.assert_awaited_once()
                self.assertEqual(payload["fid"], "fid-1")
            finally:
                await service.close()

    async def test_publish_post_strips_command_prefix_before_client_publish(self):
        with TemporaryDirectory() as tmp:
            service = QzoneDaemonService(StateStore(Path(tmp)), secret="secret", port=free_port())
            service.state.session = SessionState(
                uin=123456,
                cookies={"uin": "o123456", "p_uin": "o123456", "p_skey": "abc"},
            )
            service.client.update_session(service.state.session)
            try:
                with patch.object(
                    service.client,
                    "publish_mood",
                    new=AsyncMock(return_value={"fid": "fid-1", "message": "ok"}),
                ) as publish:
                    payload = await service.publish_post(content="!qzone post hello")
                publish.assert_awaited_once()
                self.assertEqual(publish.await_args.args[0], "hello")
                self.assertEqual(payload["fid"], "fid-1")
            finally:
                await service.close()

    async def test_publish_post_preserves_content_marked_sanitized(self):
        with TemporaryDirectory() as tmp:
            service = QzoneDaemonService(StateStore(Path(tmp)), secret="secret", port=free_port())
            service.state.session = SessionState(
                uin=123456,
                cookies={"uin": "o123456", "p_uin": "o123456", "p_skey": "abc"},
            )
            service.client.update_session(service.state.session)
            try:
                with patch.object(
                    service.client,
                    "publish_mood",
                    new=AsyncMock(return_value={"fid": "fid-1", "message": "ok"}),
                ) as publish:
                    payload = await service.publish_post(
                        content="qzone post literal",
                        content_sanitized=True,
                    )
                publish.assert_awaited_once()
                self.assertEqual(publish.await_args.args[0], "qzone post literal")
                self.assertEqual(payload["fid"], "fid-1")
            finally:
                await service.close()

    async def test_post_route_strips_raw_prefix_but_preserves_sanitized_content(self):
        with TemporaryDirectory() as tmp:
            port = free_port()
            service = QzoneDaemonService(StateStore(Path(tmp)), secret="secret", port=port)
            service.state.session = SessionState(
                uin=123456,
                cookies={"uin": "o123456", "p_uin": "o123456", "p_skey": "abc"},
            )
            service.client.update_session(service.state.session)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", NotAppKeyWarning)
                app = create_app(service)
            runner = web.AppRunner(app, access_log=None)
            await runner.setup()
            site = web.TCPSite(runner, host="127.0.0.1", port=port)
            try:
                await site.start()
                with patch.object(
                    service.client,
                    "publish_mood",
                    new=AsyncMock(return_value={"fid": "fid-1", "message": "ok"}),
                ) as publish:
                    async with httpx.AsyncClient(timeout=httpx.Timeout(5.0), trust_env=False) as client:
                        raw_response = await client.post(
                            f"http://127.0.0.1:{port}/post",
                            headers={SECRET_HEADER: "secret"},
                            json={"content": "!qzone post hello"},
                        )
                        sanitized_response = await client.post(
                            f"http://127.0.0.1:{port}/post",
                            headers={SECRET_HEADER: "secret"},
                            json={"content": "qzone post literal", "content_sanitized": True},
                        )
                self.assertEqual(raw_response.status_code, 200)
                self.assertEqual(sanitized_response.status_code, 200)
                self.assertEqual(publish.await_args_list[0].args[0], "hello")
                self.assertEqual(publish.await_args_list[1].args[0], "qzone post literal")
            finally:
                await runner.cleanup()
                await service.close()

    async def test_publish_post_allows_media_only_posts(self):
        with TemporaryDirectory() as tmp:
            service = QzoneDaemonService(StateStore(Path(tmp)), secret="secret", port=free_port())
            service.state.session = SessionState(
                uin=123456,
                cookies={"uin": "o123456", "p_uin": "o123456", "p_skey": "abc"},
            )
            service.client.update_session(service.state.session)
            try:
                with patch.object(
                    service.client,
                    "publish_mood",
                    new=AsyncMock(return_value={"fid": "fid-media", "message": "ok"}),
                ) as publish:
                    payload = await service.publish_post(
                        content="",
                        media=[{"kind": "image", "source": "base64://aGVsbG8=", "name": "photo.jpg"}],
                    )
                publish.assert_awaited_once()
                _, kwargs = publish.await_args
                self.assertEqual(kwargs["photos"][0]["source"], "base64://aGVsbG8=")
                self.assertEqual(payload["fid"], "fid-media")
                self.assertEqual(payload["photo_count"], 1)
            finally:
                await service.close()

    async def test_comment_and_like_do_not_probe_h5_index_for_token(self):
        with TemporaryDirectory() as tmp:
            service = QzoneDaemonService(StateStore(Path(tmp)), secret="secret", port=free_port())
            service.state.session = SessionState(
                uin=123456,
                cookies={"uin": "o123456", "p_uin": "o123456", "p_skey": "abc"},
            )
            service.client.update_session(service.state.session)
            try:
                with patch.object(
                    service.client,
                    "index",
                    new=AsyncMock(side_effect=QzoneRequestError("h5 redirect", status_code=302)),
                ) as index, patch.object(
                    service.client,
                    "add_comment",
                    new=AsyncMock(return_value={"commentid": 7, "message": "ok"}),
                ) as comment, patch.object(
                    service.client,
                    "like_post",
                    new=AsyncMock(return_value={"message": "ok"}),
                ) as like:
                    comment_payload = await service.comment_post(hostuin=123456, fid="fid-1", content="hello")
                    like_payload = await service.like_post(hostuin=123456, fid="fid-1")
                index.assert_not_awaited()
                comment.assert_awaited_once()
                like.assert_awaited_once()
                self.assertEqual(comment_payload["commentid"], 7)
                self.assertEqual(like_payload["action"], "like")
            finally:
                await service.close()

    async def test_like_post_verifies_liked_state_after_request(self):
        with TemporaryDirectory() as tmp:
            service = QzoneDaemonService(StateStore(Path(tmp)), secret="secret", port=free_port())
            service.state.session = SessionState(
                uin=123456,
                cookies={"uin": "o123456", "p_uin": "o123456", "p_skey": "abc"},
            )
            service.client.update_session(service.state.session)
            before = {
                "tid": "fid-1",
                "uin": 123456,
                "content": "hello",
                "like": {"isliked": "0"},
            }
            after = {
                "tid": "fid-1",
                "uin": 123456,
                "content": "hello",
                "like": {"isliked": "1"},
            }
            try:
                with patch.object(service.client, "detail", new=AsyncMock(side_effect=[before, after])) as detail, patch.object(
                    service.client,
                    "like_post",
                    new=AsyncMock(return_value={"message": "ok"}),
                ) as like:
                    payload = await service.like_post(hostuin=123456, fid="fid-1")
                self.assertEqual(detail.await_count, 2)
                like.assert_awaited_once()
                self.assertTrue(payload["verified"])
                self.assertTrue(payload["liked"])
                self.assertFalse(payload["already"])
            finally:
                await service.close()

    async def test_like_post_accepts_scalar_success_response_when_verified(self):
        with TemporaryDirectory() as tmp:
            service = QzoneDaemonService(StateStore(Path(tmp)), secret="secret", port=free_port())
            service.state.session = SessionState(
                uin=123456,
                cookies={"uin": "o123456", "p_uin": "o123456", "p_skey": "abc"},
            )
            service.client.update_session(service.state.session)
            before = {
                "tid": "fid-1",
                "uin": 123456,
                "content": "hello",
                "like": {"isliked": "0"},
            }
            after = {
                "tid": "fid-1",
                "uin": 123456,
                "content": "hello",
                "like": {"isliked": "1"},
            }
            try:
                with patch.object(service.client, "detail", new=AsyncMock(side_effect=[before, after])), patch.object(
                    service.client,
                    "like_post",
                    new=AsyncMock(return_value={"data": 0}),
                ):
                    payload = await service.like_post(hostuin=123456, fid="fid-1")
                self.assertTrue(payload["verified"])
                self.assertTrue(payload["liked"])
                self.assertEqual(payload["raw"]["value"], 0)
            finally:
                await service.close()

    async def test_like_post_accepts_scalar_response_when_verification_unavailable(self):
        with TemporaryDirectory() as tmp:
            service = QzoneDaemonService(StateStore(Path(tmp)), secret="secret", port=free_port())
            service.state.session = SessionState(
                uin=123456,
                cookies={"uin": "o123456", "p_uin": "o123456", "p_skey": "abc"},
            )
            service.client.update_session(service.state.session)
            try:
                with patch.object(
                    service.client,
                    "detail",
                    new=AsyncMock(side_effect=QzoneRequestError("detail unavailable")),
                ), patch.object(
                    service.client,
                    "legacy_feeds",
                    new=AsyncMock(side_effect=QzoneRequestError("feed unavailable")),
                ), patch.object(
                    service.client,
                    "like_post",
                    new=AsyncMock(return_value={"data": 0}),
                ):
                    payload = await service.like_post(hostuin=123456, fid="fid-1")
                self.assertFalse(payload["verified"])
                self.assertTrue(payload["liked"])
                self.assertEqual(payload["raw"]["value"], 0)
            finally:
                await service.close()

    async def test_like_post_retries_http_key_when_first_request_does_not_change_state(self):
        with TemporaryDirectory() as tmp:
            service = QzoneDaemonService(StateStore(Path(tmp)), secret="secret", port=free_port())
            service.state.session = SessionState(
                uin=123456,
                cookies={"uin": "o123456", "p_uin": "o123456", "p_skey": "abc"},
            )
            service.client.update_session(service.state.session)
            false_state = {
                "tid": "fid-1",
                "uin": 123456,
                "content": "hello",
                "like": {"isliked": "0"},
            }
            true_state = {
                "tid": "fid-1",
                "uin": 123456,
                "content": "hello",
                "like": {"isliked": "1"},
            }
            try:
                with patch.object(
                    service.client,
                    "detail",
                    new=AsyncMock(side_effect=[false_state, false_state, true_state]),
                ), patch.object(
                    service.client,
                    "like_post",
                    new=AsyncMock(return_value={"message": "ok"}),
                ) as like:
                    payload = await service.like_post(hostuin=123456, fid="fid-1")
                self.assertEqual(like.await_count, 2)
                _, second_kwargs = like.await_args_list[1]
                self.assertEqual(second_kwargs["curkey"], "http://user.qzone.qq.com/123456/mood/fid-1")
                self.assertEqual(second_kwargs["unikey"], "http://user.qzone.qq.com/123456/mood/fid-1")
                self.assertTrue(payload["verified"])
                self.assertTrue(payload["liked"])
            finally:
                await service.close()

    async def test_like_post_retries_stale_verification_before_marking_unconfirmed(self):
        with TemporaryDirectory() as tmp:
            service = QzoneDaemonService(StateStore(Path(tmp)), secret="secret", port=free_port())
            service.state.session = SessionState(
                uin=123456,
                cookies={"uin": "o123456", "p_uin": "o123456", "p_skey": "abc"},
            )
            service.client.update_session(service.state.session)
            false_state = {
                "tid": "fid-1",
                "uin": 123456,
                "content": "hello",
                "like": {"isliked": "0"},
            }
            true_state = {
                "tid": "fid-1",
                "uin": 123456,
                "content": "hello",
                "like": {"isliked": "1"},
            }
            try:
                with patch("qzone_bridge.daemon.LIKE_VERIFY_RETRY_DELAYS_SECONDS", (0,)), patch.object(
                    service.client,
                    "detail",
                    new=AsyncMock(side_effect=[false_state, false_state, false_state, true_state]),
                ) as detail, patch.object(
                    service.client,
                    "like_post",
                    new=AsyncMock(return_value={"message": "ok"}),
                ) as like:
                    payload = await service.like_post(hostuin=123456, fid="fid-1")
                self.assertEqual(detail.await_count, 4)
                self.assertEqual(like.await_count, 2)
                self.assertTrue(payload["verified"])
                self.assertTrue(payload["liked"])
            finally:
                await service.close()

    async def test_like_post_keeps_success_when_verification_state_stays_stale(self):
        with TemporaryDirectory() as tmp:
            service = QzoneDaemonService(StateStore(Path(tmp)), secret="secret", port=free_port())
            service.state.session = SessionState(
                uin=123456,
                cookies={"uin": "o123456", "p_uin": "o123456", "p_skey": "abc"},
            )
            service.client.update_session(service.state.session)
            false_state = {
                "tid": "fid-1",
                "uin": 123456,
                "content": "hello",
                "like": {"isliked": "0"},
            }
            try:
                with patch("qzone_bridge.daemon.LIKE_VERIFY_RETRY_DELAYS_SECONDS", (0,)), patch.object(
                    service.client,
                    "detail",
                    new=AsyncMock(side_effect=[false_state, false_state, false_state, false_state]),
                ), patch.object(
                    service.client,
                    "like_post",
                    new=AsyncMock(return_value={"message": "ok"}),
                ) as like:
                    payload = await service.like_post(hostuin=123456, fid="fid-1")
                self.assertEqual(like.await_count, 2)
                self.assertFalse(payload["verified"])
                self.assertTrue(payload["liked"])
                self.assertEqual(payload["verification"]["expected_liked"], True)
                self.assertEqual(payload["verification"]["actual_liked"], False)
            finally:
                await service.close()

    async def test_like_post_verification_falls_back_to_legacy_feed_page(self):
        with TemporaryDirectory() as tmp:
            service = QzoneDaemonService(StateStore(Path(tmp)), secret="secret", port=free_port())
            service.state.session = SessionState(
                uin=123456,
                cookies={"uin": "o123456", "p_uin": "o123456", "p_skey": "abc"},
            )
            service.client.update_session(service.state.session)
            before = {"msglist": [{"tid": "fid-1", "uin": 3112333596, "content": "hello", "isliked": "0"}]}
            after = {"msglist": [{"tid": "fid-1", "uin": 3112333596, "content": "hello", "isliked": "1"}]}
            try:
                with patch.object(
                    service.client,
                    "detail",
                    new=AsyncMock(side_effect=QzoneRequestError("detail unavailable")),
                ), patch.object(
                    service.client,
                    "legacy_feeds",
                    new=AsyncMock(side_effect=[before, after]),
                ) as legacy, patch.object(
                    service.client,
                    "like_post",
                    new=AsyncMock(return_value={"message": "ok"}),
                ):
                    payload = await service.like_post(hostuin=3112333596, fid="fid-1")
                self.assertEqual(legacy.await_count, 2)
                self.assertTrue(payload["verified"])
                self.assertTrue(payload["liked"])
            finally:
                await service.close()

    async def test_like_post_accepts_recent_feed_index_reference(self):
        with TemporaryDirectory() as tmp:
            service = QzoneDaemonService(StateStore(Path(tmp)), secret="secret", port=free_port())
            service.state.session = SessionState(
                uin=123456,
                cookies={"uin": "o123456", "p_uin": "o123456", "p_skey": "abc"},
            )
            service.client.update_session(service.state.session)
            service.recent_feed_entries = [
                FeedEntry(
                    hostuin=3112333596,
                    fid="1c7182b96589046ad3380900",
                    appid=311,
                    summary="??????",
                    liked=False,
                    curkey="cached-curkey",
                )
            ]
            before = {
                "tid": "1c7182b96589046ad3380900",
                "uin": 3112333596,
                "content": "??????",
                "like": {"isliked": "0"},
            }
            after = {
                "tid": "1c7182b96589046ad3380900",
                "uin": 3112333596,
                "content": "??????",
                "like": {"isliked": "1"},
            }
            try:
                with patch.object(service.client, "detail", new=AsyncMock(side_effect=[before, after])), patch.object(
                    service.client,
                    "like_post",
                    new=AsyncMock(return_value={"message": "ok"}),
                ) as like:
                    payload = await service.like_post(hostuin=0, fid="1")
                args, kwargs = like.await_args
                self.assertEqual(args[:2], (3112333596, "1c7182b96589046ad3380900"))
                self.assertEqual(kwargs["curkey"], "cached-curkey")
                self.assertTrue(payload["verified"])
                self.assertEqual(payload["summary"], "??????")
            finally:
                await service.close()


    async def test_like_post_resolves_latest_reference_from_self_profile(self):
        with TemporaryDirectory() as tmp:
            service = QzoneDaemonService(StateStore(Path(tmp)), secret="secret", port=free_port())
            service.state.session = SessionState(
                uin=123456,
                cookies={"uin": "o123456", "p_uin": "o123456", "p_skey": "abc"},
            )
            service.client.update_session(service.state.session)
            before = {
                "tid": "fid-latest",
                "uin": 123456,
                "content": "latest self post",
                "like": {"isliked": "0"},
            }
            after = {
                "tid": "fid-latest",
                "uin": 123456,
                "content": "latest self post",
                "like": {"isliked": "1"},
            }
            try:
                with patch.object(
                    service,
                    "list_feeds",
                    new=AsyncMock(
                        return_value={
                            "items": [
                                {
                                    "hostuin": 123456,
                                    "fid": "fid-latest",
                                    "appid": 311,
                                    "summary": "latest self post",
                                    "curkey": "latest-curkey",
                                }
                            ]
                        }
                    ),
                ) as feeds, patch.object(
                    service.client,
                    "detail",
                    new=AsyncMock(side_effect=[before, after]),
                ), patch.object(
                    service.client,
                    "like_post",
                    new=AsyncMock(return_value={"message": "ok"}),
                ) as like:
                    payload = await service.like_post(hostuin=0, fid="", latest=True)
                feeds.assert_awaited_once_with(hostuin=123456, limit=1, scope="profile")
                args, kwargs = like.await_args
                self.assertEqual(args[:2], (123456, "fid-latest"))
                self.assertEqual(kwargs["curkey"], "latest-curkey")
                self.assertTrue(payload["verified"])
                self.assertEqual(payload["summary"], "latest self post")
            finally:
                await service.close()

    async def test_like_post_resolves_latest_reference_from_legacy_when_profile_5xx(self):
        with TemporaryDirectory() as tmp:
            service = QzoneDaemonService(StateStore(Path(tmp)), secret="secret", port=free_port())
            service.state.session = SessionState(
                uin=123456,
                cookies={"uin": "o123456", "p_uin": "o123456", "p_skey": "abc"},
            )
            service.client.update_session(service.state.session)
            legacy = {
                "msglist": [
                    {
                        "tid": "fid-latest",
                        "uin": 123456,
                        "content": "legacy latest post",
                        "isliked": "0",
                    }
                ]
            }
            before = {
                "tid": "fid-latest",
                "uin": 123456,
                "content": "legacy latest post",
                "like": {"isliked": "0"},
            }
            after = {
                "tid": "fid-latest",
                "uin": 123456,
                "content": "legacy latest post",
                "like": {"isliked": "1"},
            }
            try:
                with patch.object(
                    service.client,
                    "profile",
                    new=AsyncMock(side_effect=QzoneRequestError("profile unavailable", status_code=503)),
                ) as profile, patch.object(
                    service.client,
                    "legacy_feeds",
                    new=AsyncMock(return_value=legacy),
                ) as legacy_feeds, patch.object(
                    service.client,
                    "detail",
                    new=AsyncMock(side_effect=[before, after]),
                ), patch.object(
                    service.client,
                    "like_post",
                    new=AsyncMock(return_value={"message": "ok"}),
                ) as like:
                    payload = await service.like_post(hostuin=0, fid="", latest=True)
                profile.assert_awaited_once_with(123456)
                legacy_feeds.assert_awaited_once_with(123456, page=1, num=20)
                args, _ = like.await_args
                self.assertEqual(args[:2], (123456, "fid-latest"))
                self.assertTrue(payload["verified"])
                self.assertEqual(payload["summary"], "legacy latest post")
            finally:
                await service.close()

    async def test_like_post_resolves_named_index_reference_for_specific_host(self):
        with TemporaryDirectory() as tmp:
            service = QzoneDaemonService(StateStore(Path(tmp)), secret="secret", port=free_port())
            service.state.session = SessionState(
                uin=123456,
                cookies={"uin": "o123456", "p_uin": "o123456", "p_skey": "abc"},
            )
            service.client.update_session(service.state.session)
            before = {
                "tid": "fid-2",
                "uin": 3112333596,
                "content": "second friend post",
                "like": {"isliked": "0"},
            }
            after = {
                "tid": "fid-2",
                "uin": 3112333596,
                "content": "second friend post",
                "like": {"isliked": "1"},
            }
            try:
                with patch.object(
                    service,
                    "list_feeds",
                    new=AsyncMock(
                        return_value={
                            "items": [
                                {
                                    "hostuin": 3112333596,
                                    "fid": "fid-1",
                                    "appid": 311,
                                    "summary": "first friend post",
                                    "curkey": "friend-curkey-1",
                                },
                                {
                                    "hostuin": 3112333596,
                                    "fid": "fid-2",
                                    "appid": 311,
                                    "summary": "second friend post",
                                    "curkey": "friend-curkey-2",
                                },
                            ]
                        }
                    ),
                ) as feeds, patch.object(
                    service.client,
                    "detail",
                    new=AsyncMock(side_effect=[before, after]),
                ), patch.object(
                    service.client,
                    "like_post",
                    new=AsyncMock(return_value={"message": "ok"}),
                ) as like:
                    payload = await service.like_post(hostuin=3112333596, fid="?2?")
                feeds.assert_awaited_once_with(hostuin=3112333596, limit=2, scope="profile")
                args, kwargs = like.await_args
                self.assertEqual(args[:2], (3112333596, "fid-2"))
                self.assertEqual(kwargs["curkey"], "friend-curkey-2")
                self.assertTrue(payload["verified"])
                self.assertEqual(payload["summary"], "second friend post")
            finally:
                await service.close()


class ControllerLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_close_shuts_down_daemon_and_releases_port(self):
        with TemporaryDirectory() as tmp:
            port = free_port()
            controller = QzoneDaemonController(
                plugin_root=Path.cwd(),
                data_dir=Path(tmp) / "qzone",
                default_port=port,
                request_timeout=1,
                start_timeout=8,
                keepalive_interval=30,
            )
            actual_port = port
            try:
                status = await controller.ensure_running()
                actual_port = int(status["daemon_port"])
                runtime = controller._runtime()
                self.assertTrue(await controller._probe_health(actual_port, secret=runtime.secret))
            finally:
                await controller.close()

            for _ in range(20):
                if _port_is_free(actual_port):
                    break
                await asyncio.sleep(0.2)
            self.assertTrue(_port_is_free(actual_port))


if __name__ == "__main__":
    unittest.main()
