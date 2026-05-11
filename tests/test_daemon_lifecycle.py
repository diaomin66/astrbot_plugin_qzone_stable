import asyncio
import socket
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

from qzone_bridge.controller import QzoneDaemonController, _port_is_free
from qzone_bridge.daemon import QzoneDaemonService
from qzone_bridge.errors import QzoneNeedsRebind, QzoneRequestError
from qzone_bridge.models import FeedEntry, SessionState
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

            service._set_error(QzoneRequestError("无权限访问", status_code=403))

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
                    new=AsyncMock(return_value={"msglist": [{"tid": "fid-1", "uin": 123456, "content": "hello"}]}),
                ) as legacy:
                    payload = await service.list_feeds(limit=1)
                index.assert_awaited_once()
                legacy.assert_awaited_once()
                self.assertEqual(payload["items"][0]["fid"], "fid-1")
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
