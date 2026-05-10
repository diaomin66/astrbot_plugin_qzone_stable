import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

from qzone_bridge.controller import QzoneDaemonController
from qzone_bridge.errors import DaemonUnavailableError


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


if __name__ == "__main__":
    unittest.main()
