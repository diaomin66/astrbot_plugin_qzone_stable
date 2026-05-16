import unittest
import socket
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

import httpx

from qzone_bridge.controller import QzoneDaemonController
from qzone_bridge.errors import DaemonUnavailableError, QzoneRequestError
from qzone_bridge.models import SessionState


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class ControllerBindTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_initialization_persists_secret_and_port(self):
        with TemporaryDirectory() as tmp:
            controller = QzoneDaemonController(
                plugin_root=Path(tmp),
                data_dir=Path(tmp) / "data",
                default_port=19009,
                request_timeout=1.0,
                start_timeout=1.0,
                keepalive_interval=30,
                user_agent="test-agent",
            )
            try:
                runtime = controller._runtime()
                state = controller.store.read()
                self.assertTrue(runtime.secret)
                self.assertEqual(state.runtime.secret, runtime.secret)
                self.assertEqual(state.runtime.daemon_port, 19009)
            finally:
                await controller.close()

    async def test_bind_cookie_local_falls_back_to_store(self):
        with TemporaryDirectory() as tmp:
            controller = QzoneDaemonController(
                plugin_root=Path(tmp),
                data_dir=Path(tmp) / "data",
                default_port=19009,
                request_timeout=1.0,
                start_timeout=1.0,
                keepalive_interval=30,
                user_agent="test-agent",
            )
            try:
                with patch.object(
                    controller,
                    "bind_cookie",
                    new=AsyncMock(side_effect=DaemonUnavailableError("offline")),
                ):
                    payload = await controller.bind_cookie_local(
                        "uin=o123456; p_uin=o123456; skey=abc; p_skey=def",
                        source="aiocqhttp",
                    )

                state = controller.store.read()
                self.assertEqual(state.session.uin, 123456)
                self.assertEqual(state.session.source, "aiocqhttp")
                self.assertEqual(state.session.cookies["p_skey"], "def")
                self.assertGreaterEqual(payload["cookie_count"], 4)
                self.assertEqual(payload["login_uin"], 123456)
                self.assertEqual(payload["session_source"], "aiocqhttp")
            finally:
                await controller.close()

    async def test_auto_start_disabled_refuses_missing_daemon(self):
        with TemporaryDirectory() as tmp:
            controller = QzoneDaemonController(
                plugin_root=Path(tmp),
                data_dir=Path(tmp) / "data",
                default_port=19009,
                request_timeout=1.0,
                start_timeout=1.0,
                keepalive_interval=30,
                user_agent="test-agent",
                auto_start_daemon=False,
            )
            try:
                with patch.object(controller, "_probe_health", new=AsyncMock(return_value=False)), patch.object(
                    controller,
                    "ensure_running",
                    new=AsyncMock(),
                ) as ensure_running:
                    with self.assertRaises(DaemonUnavailableError):
                        await controller.list_feeds()
                    ensure_running.assert_not_awaited()
            finally:
                await controller.close()

    async def test_publish_post_strips_command_prefix_before_daemon_request(self):
        with TemporaryDirectory() as tmp:
            controller = QzoneDaemonController(
                plugin_root=Path(tmp),
                data_dir=Path(tmp) / "data",
                default_port=19009,
                request_timeout=1.0,
                start_timeout=1.0,
                keepalive_interval=30,
                user_agent="test-agent",
            )
            try:
                with patch.object(controller, "_request", new=AsyncMock(return_value={"fid": "fid-1"})) as request:
                    payload = await controller.publish_post(content="!qzone post hello")
                request.assert_awaited_once()
                self.assertEqual(request.await_args.kwargs["json_body"]["content"], "hello")
                self.assertTrue(request.await_args.kwargs["json_body"]["content_sanitized"])
                self.assertEqual(payload["fid"], "fid-1")
            finally:
                await controller.close()

    async def test_publish_post_preserves_content_marked_sanitized(self):
        with TemporaryDirectory() as tmp:
            controller = QzoneDaemonController(
                plugin_root=Path(tmp),
                data_dir=Path(tmp) / "data",
                default_port=19009,
                request_timeout=1.0,
                start_timeout=1.0,
                keepalive_interval=30,
                user_agent="test-agent",
            )
            try:
                with patch.object(controller, "_request", new=AsyncMock(return_value={"fid": "fid-1"})) as request:
                    payload = await controller.publish_post(
                        content="qzone post literal",
                        content_sanitized=True,
                    )
                request.assert_awaited_once()
                self.assertEqual(request.await_args.kwargs["json_body"]["content"], "qzone post literal")
                self.assertTrue(request.await_args.kwargs["json_body"]["content_sanitized"])
                self.assertEqual(payload["fid"], "fid-1")
            finally:
                await controller.close()

    async def test_ensure_running_records_crashed_daemon_start_detail(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "daemon_main.py").write_text(
                "import sys\nprint('daemon boom', file=sys.stderr)\nraise SystemExit(7)\n",
                encoding="utf-8",
            )
            controller = QzoneDaemonController(
                plugin_root=root,
                data_dir=root / "data",
                default_port=free_port(),
                request_timeout=0.5,
                start_timeout=2.0,
                keepalive_interval=30,
                user_agent="test-agent",
            )
            try:
                state = controller.store.read()
                state.session = SessionState(
                    uin=123456,
                    cookies={"uin": "o123456", "p_uin": "o123456", "p_skey": "abc"},
                    needs_rebind=False,
                )
                controller.store.write(state)

                with self.assertRaises(DaemonUnavailableError) as caught:
                    await controller.ensure_running()

                detail = caught.exception.detail
                self.assertEqual(detail["returncode"], 7)
                self.assertIn("daemon.log", detail["log_path"])
                self.assertIn("daemon boom", detail["log_tail"])
                stored_error = controller.store.read().session.last_error
                self.assertEqual(stored_error["type"], "DaemonUnavailableError")
                self.assertIn("daemon boom", stored_error["detail"]["log_tail"])
            finally:
                await controller.close()

    async def test_daemon_request_error_preserves_status_code_from_detail(self):
        with TemporaryDirectory() as tmp:
            controller = QzoneDaemonController(
                plugin_root=Path(tmp),
                data_dir=Path(tmp) / "data",
                default_port=19009,
                request_timeout=1.0,
                start_timeout=1.0,
                keepalive_interval=30,
                user_agent="test-agent",
                auto_start_daemon=False,
            )
            try:
                controller._runtime()
                response = httpx.Response(
                    400,
                    json={
                        "ok": False,
                        "error": {
                            "code": "QZONE_REQUEST",
                            "message": "QQ 空间服务暂时不可用 (503)",
                            "detail": {
                                "status_code": 503,
                                "url": "https://w.qzone.qq.com/cgi-bin/likes/internal_dolike_app",
                                "text": "service unavailable",
                            },
                        },
                    },
                )
                with patch.object(controller, "_probe_health", new=AsyncMock(return_value=True)), patch.object(
                    controller._client,
                    "request",
                    new=AsyncMock(return_value=response),
                ):
                    with self.assertRaises(QzoneRequestError) as caught:
                        await controller.like_post(hostuin=123456, fid="fid-1")
                self.assertEqual(caught.exception.status_code, 503)
                self.assertEqual(caught.exception.detail["text"], "service unavailable")
            finally:
                await controller.close()


if __name__ == "__main__":
    unittest.main()
