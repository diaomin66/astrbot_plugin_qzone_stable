import unittest

from qzone_bridge.client import QzoneClient
from qzone_bridge.models import SessionState


class ClientCookieTests(unittest.IsolatedAsyncioTestCase):
    async def test_client_gtk_uses_direct_onebot_token(self):
        client = QzoneClient(SessionState(uin=123456, cookies={"uin": "o123456", "g_tk": "123456789"}))
        try:
            self.assertEqual(client.gtk, 123456789)
        finally:
            await client.close()

    async def test_client_gtk_uses_pskey_alias(self):
        client = QzoneClient(SessionState(uin=123456, cookies={"uin": "o123456", "pskey": "domain-secret"}))
        try:
            self.assertGreater(client.gtk, 0)
            self.assertEqual(client.session.cookies["p_skey"], "domain-secret")
        finally:
            await client.close()


if __name__ == "__main__":
    unittest.main()
