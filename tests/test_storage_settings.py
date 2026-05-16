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

    def test_settings_accept_target_plugin_sections(self):
        settings = PluginSettings.from_mapping(
            {
                "manage_group": "12345",
                "cookies_str": "uin=o1; p_skey=abc",
                "llm": {
                    "post_provider_id": "post-provider",
                    "comment_prompt": "comment prompt",
                },
                "source": {
                    "ignore_groups": ["100"],
                    "ignore_users": "200, 300",
                    "post_max_msg": 250,
                },
                "trigger": {
                    "publish_cron": "30 23 * * *",
                    "publish_offset": 600,
                    "comment_cron": "0 8 * * *",
                    "like_when_comment": True,
                },
            }
        )

        self.assertEqual(settings.manage_group, 12345)
        self.assertEqual(settings.cookies_str, "uin=o1; p_skey=abc")
        self.assertEqual(settings.post_provider_id, "post-provider")
        self.assertEqual(settings.comment_prompt, "comment prompt")
        self.assertEqual(settings.ignore_groups, ["100"])
        self.assertEqual(settings.ignore_users, ["200", "300"])
        self.assertEqual(settings.post_max_msg, 250)
        self.assertEqual(settings.publish_cron, "30 23 * * *")
        self.assertEqual(settings.publish_offset, 600)
        self.assertTrue(settings.like_when_comment)


if __name__ == "__main__":
    unittest.main()
