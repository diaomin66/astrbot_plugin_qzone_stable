import unittest

from qzone_bridge.onebot_cookie import extract_cookie_text, fetch_cookie_text, iter_cookie_domains
from qzone_bridge.parser import cookie_gtk, parse_cookie_text


class DirectCookieClient:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    async def get_cookies(self, domain=None):
        self.calls.append(("get_cookies", domain))
        return self.payload


class ActionCookieClient:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    async def call_action(self, action, **params):
        self.calls.append((action, params))
        return self.payload


class LoginInfoCookieClient:
    def __init__(self, cookie_payload, login_payload):
        self.cookie_payload = cookie_payload
        self.login_payload = login_payload
        self.calls = []

    async def get_cookies(self, domain=None):
        self.calls.append(("get_cookies", domain))
        return self.cookie_payload

    async def get_login_info(self):
        self.calls.append(("get_login_info", None))
        return self.login_payload


class DomainAwareCookieClient:
    def __init__(self):
        self.calls = []

    async def get_cookies(self, domain=None):
        self.calls.append(("get_cookies", domain))
        if domain == "user.qzone.qq.com":
            return {"cookies": "uin=o123456; p_uin=o123456"}
        return {"cookies": "uin=o123456; p_uin=o123456; pskey=domain-secret"}


class OneBotCookieTests(unittest.IsolatedAsyncioTestCase):
    def test_extract_cookie_text_from_string(self):
        text = extract_cookie_text("uin=o123; p_uin=o123; skey=abc; p_skey=def")
        self.assertIn("p_skey=def", text)

    def test_extract_cookie_text_from_nested_payload(self):
        payload = {"data": {"cookies": {"uin": "o123", "p_uin": "o123", "skey": "abc", "p_skey": "def"}}}
        text = extract_cookie_text(payload)
        self.assertIn("uin=o123", text)
        self.assertIn("p_skey=def", text)

    def test_extract_cookie_text_from_cookie_list(self):
        payload = [
            {"name": "uin", "value": "o123"},
            {"name": "p_uin", "value": "o123"},
            {"name": "skey", "value": "abc"},
            {"name": "p_skey", "value": "def"},
        ]
        text = extract_cookie_text(payload)
        self.assertIn("uin=o123", text)
        self.assertIn("p_skey=def", text)

    def test_iter_cookie_domains(self):
        domains = iter_cookie_domains("https://user.qzone.qq.com/")
        self.assertIn("user.qzone.qq.com", domains)
        self.assertIn("qzone.qq.com", domains)

    async def test_fetch_cookie_text_prefers_direct_method(self):
        client = DirectCookieClient({"cookies": {"uin": "o123", "p_uin": "o123", "skey": "abc", "p_skey": "def"}})
        text = await fetch_cookie_text(client, domain="user.qzone.qq.com")
        self.assertIn("p_skey=def", text)
        self.assertTrue(client.calls)

    async def test_fetch_cookie_text_falls_back_to_call_action(self):
        client = ActionCookieClient({"cookies": "uin=o123; p_uin=o123; skey=abc; p_skey=def"})
        text = await fetch_cookie_text(client, domain="user.qzone.qq.com")
        self.assertIn("p_skey=def", text)
        self.assertTrue(client.calls)

    async def test_fetch_cookie_text_injects_login_uin(self):
        client = LoginInfoCookieClient(
            {"cookies": "skey=abc; p_skey=def"},
            {"user_id": 123456, "nickname": "bot"},
        )
        text = await fetch_cookie_text(client, domain="user.qzone.qq.com")
        self.assertIn("uin=o123456", text)
        self.assertIn("p_uin=o123456", text)
        self.assertTrue(client.calls)

    def test_extract_cookie_text_merges_credentials_token(self):
        payload = {
            "cookies": "uin=o123456; p_uin=o123456",
            "csrf_token": 123456789,
        }
        text = extract_cookie_text(payload)
        cookies = parse_cookie_text(text)
        self.assertEqual(cookies["g_tk"], "123456789")
        self.assertEqual(cookie_gtk(cookies), 123456789)

    async def test_fetch_cookie_text_accepts_pskey_alias(self):
        client = DirectCookieClient({"cookies": {"uin": "o123456", "p_uin": "o123456", "pskey": "domain-secret"}})
        text = await fetch_cookie_text(client, domain="user.qzone.qq.com")
        cookies = parse_cookie_text(text)
        self.assertEqual(cookies["p_skey"], "domain-secret")
        self.assertGreater(cookie_gtk(cookies), 0)

    async def test_fetch_cookie_text_skips_cookie_without_auth_token(self):
        client = DomainAwareCookieClient()
        text = await fetch_cookie_text(client, domain="user.qzone.qq.com")
        cookies = parse_cookie_text(text)
        self.assertEqual(cookies["p_skey"], "domain-secret")
        self.assertGreater(len(client.calls), 1)


if __name__ == "__main__":
    unittest.main()
