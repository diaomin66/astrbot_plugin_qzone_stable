import unittest
from tempfile import TemporaryDirectory
from pathlib import Path

from qzone_bridge.models import BridgeState, SessionState
from qzone_bridge.settings import PluginSettings
from qzone_bridge.storage import StateStore


class StorageSettingsTests(unittest.TestCase):
    def test_state_store_roundtrip(self):
        with TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp))
            state = BridgeState()
            state.session = SessionState(uin=123, cookies={"p_skey": "abc"}, revision=1)
            store.write(state)
            loaded = store.read()
            self.assertEqual(loaded.session.uin, 123)
            self.assertEqual(loaded.session.cookies["p_skey"], "abc")

    def test_settings_from_mapping(self):
        settings = PluginSettings.from_mapping(
            {
                "daemon_port": 19001,
                "admin_uins": "123, 456",
                "public_feed_limit": 7,
                "auto_bind_cookie": False,
                "cookie_domain": "https://user.qzone.qq.com/",
                "render_remote_timeout": 0.25,
            }
        )
        self.assertEqual(settings.daemon_port, 19001)
        self.assertEqual(settings.admin_uins, [123, 456])
        self.assertEqual(settings.public_feed_limit, 7)
        self.assertFalse(settings.auto_bind_cookie)
        self.assertEqual(settings.cookie_domain, "https://user.qzone.qq.com/")
        self.assertEqual(settings.render_remote_timeout, 0.25)


if __name__ == "__main__":
    unittest.main()
