import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from qzone_bridge.controller import QzoneDaemonController
from qzone_bridge.models import BridgeState


class RecordingController(QzoneDaemonController):
    def __init__(self, *args, wait_results=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.events = []
        self.wait_results = list(wait_results or [])

    async def _daemon_accepts_secret(self, port: int, secret: str) -> bool:
        self.events.append(("probe", port, secret))
        return True

    async def _request_daemon_shutdown(self, port: int, secret: str) -> bool:
        self.events.append(("shutdown", port, secret))
        return True

    async def _wait_for_port_release(self, port: int, timeout: float = 3.0) -> bool:
        self.events.append(("wait", port, timeout))
        if self.wait_results:
            return self.wait_results.pop(0)
        return True

    async def _terminate_tracked_process(self) -> None:
        self.events.append(("tracked",))

    async def _kill_plugin_port_owners(
        self,
        port: int,
        *,
        expected_pids: set[int],
        trusted_by_secret: bool,
    ) -> set[int]:
        self.events.append(("kill-port", port, tuple(sorted(expected_pids)), trusted_by_secret))
        return set(expected_pids)


class ControllerShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_close_requests_shutdown_before_clearing_runtime(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            controller = RecordingController(plugin_root=root, data_dir=root / "data")
            state = BridgeState()
            state.runtime.daemon_pid = 12345
            state.runtime.daemon_port = 19005
            state.runtime.secret = "secret"
            state.runtime.started_at = "2026-05-11T12:00:00Z"
            state.runtime.last_seen_at = "2026-05-11T12:00:01Z"
            controller.store.write(state)

            await controller.close()

            self.assertEqual(
                controller.events[:4],
                [
                    ("probe", 19005, "secret"),
                    ("shutdown", 19005, "secret"),
                    ("wait", 19005, 3.0),
                    ("tracked",),
                ],
            )
            runtime = controller.store.read().runtime
            self.assertEqual(runtime.daemon_pid, 0)
            self.assertEqual(runtime.daemon_port, 19005)
            self.assertEqual(runtime.secret, "secret")
            self.assertEqual(runtime.started_at, "")
            self.assertEqual(runtime.last_seen_at, "")

    async def test_stop_daemon_kills_trusted_port_owner_when_shutdown_does_not_release_port(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            controller = RecordingController(
                plugin_root=root,
                data_dir=root / "data",
                wait_results=[False, False, True],
            )
            state = BridgeState()
            state.runtime.daemon_pid = 23456
            state.runtime.daemon_port = 19006
            state.runtime.secret = "secret"
            controller.store.write(state)

            await controller.close()

            self.assertIn(("kill-port", 19006, (23456,), True), controller.events)

    async def test_stop_daemon_kills_secret_trusted_port_owner_without_saved_pid(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            controller = RecordingController(
                plugin_root=root,
                data_dir=root / "data",
                wait_results=[False, False, True],
            )
            state = BridgeState()
            state.runtime.daemon_port = 19007
            state.runtime.secret = "secret"
            controller.store.write(state)

            await controller.close()

            self.assertIn(("kill-port", 19007, (), True), controller.events)


if __name__ == "__main__":
    unittest.main()
