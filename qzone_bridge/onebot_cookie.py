"""Helpers for acquiring QQ空间 cookies from OneBot clients."""

from __future__ import annotations

import inspect
import json
import re
from typing import Any

from .parser import normalize_uin, parse_cookie_text

COOKIE_ACTIONS = ("get_cookies", "get_credentials")
LOGIN_INFO_ACTIONS = ("get_login_info",)
COOKIE_DOMAIN_FALLBACKS = ("user.qzone.qq.com", "qzone.qq.com")
COOKIE_VALUE_KEYS = (
    "cookies",
    "cookie",
    "cookie_text",
    "cookie_str",
    "cookies_str",
    "data",
    "result",
    "retdata",
    "ret_data",
    "payload",
    "response",
)
COOKIE_NAME_ALLOWLIST = {
    "uin",
    "p_uin",
    "skey",
    "p_skey",
    "pt4_token",
    "pt_key",
    "pt_login_sig",
    "clientkey",
    "superkey",
    "qzonetoken",
    "qm_keyst",
    "qm_sid",
    "o_cookie",
    "uin_cookie",
    "skey2",
    "rv2",
    "ptcz",
    "lskey",
    "ldw",
    "g_tk",
    "bkn",
    "pskey",
    "qqmusic_key",
}
COOKIE_META_KEYS = {
    "domain",
    "path",
    "expires",
    "max_age",
    "secure",
    "httponly",
    "ret",
    "code",
    "status",
    "message",
    "msg",
    "error",
    "errno",
    "errcode",
    "success",
}

_COOKIE_PAIR_RE = re.compile(
    r"\b(?:uin|p_uin|skey|p_skey|pt4_token|pt_key|pt_login_sig|clientkey|superkey|qzonetoken|qm_keyst|qm_sid|o_cookie|uin_cookie|skey2|rv2|ptcz|lskey|ldw|g_tk|bkn|pskey|qqmusic_key)\s*=",
    re.I,
)


def _is_cookie_string(value: str) -> bool:
    text = value.strip()
    return bool(text) and "=" in text and bool(_COOKIE_PAIR_RE.search(text))


def _cookie_mapping_to_text(mapping: dict[str, Any]) -> str:
    items: list[str] = []
    for key, value in mapping.items():
        normalized = str(key).strip()
        if not normalized:
            continue
        lower = normalized.lower()
        if lower in COOKIE_META_KEYS:
            continue
        if lower not in COOKIE_NAME_ALLOWLIST:
            continue
        if value in (None, ""):
            continue
        if isinstance(value, (str, int, float, bool)):
            items.append(f"{normalized}={value}")
    return "; ".join(items)


def _cookie_pair_object_to_text(value: dict[str, Any]) -> str:
    name = value.get("name") or value.get("key")
    cookie_value = value.get("value")
    if name and isinstance(cookie_value, (str, int, float, bool)):
        normalized = str(name).strip()
        if normalized and normalized.lower() not in COOKIE_META_KEYS:
            return f"{normalized}={cookie_value}"
    return ""


def _extract_uin_from_payload(payload: Any, *, _depth: int = 0, _seen: set[int] | None = None) -> int:
    if _seen is None:
        _seen = set()
    if payload is None or _depth > 8:
        return 0
    if isinstance(payload, bool):
        return 0
    if isinstance(payload, (int, float)):
        value = int(payload)
        return value if value > 0 else 0
    if isinstance(payload, bytes):
        try:
            payload = payload.decode("utf-8")
        except Exception:
            return 0
    if isinstance(payload, str):
        text = payload.strip()
        if not text:
            return 0
        if text.startswith("{") or text.startswith("["):
            try:
                return _extract_uin_from_payload(json.loads(text), _depth=_depth + 1, _seen=_seen)
            except Exception:
                return 0
        cleaned = text.lstrip("oO")
        return int(cleaned) if cleaned.isdigit() else 0
    if isinstance(payload, (list, tuple)):
        for item in payload:
            uin = _extract_uin_from_payload(item, _depth=_depth + 1, _seen=_seen)
            if uin:
                return uin
        return 0
    if not isinstance(payload, dict):
        return 0

    obj_id = id(payload)
    if obj_id in _seen:
        return 0
    _seen.add(obj_id)

    for key in ("uin", "user_id", "uid", "qq", "account"):
        if key in payload:
            uin = _extract_uin_from_payload(payload.get(key), _depth=_depth + 1, _seen=_seen)
            if uin:
                return uin

    for key in ("data", "result", "retdata", "ret_data", "payload", "response"):
        if key in payload:
            uin = _extract_uin_from_payload(payload.get(key), _depth=_depth + 1, _seen=_seen)
            if uin:
                return uin

    for value in payload.values():
        if isinstance(value, (dict, list, tuple, str, bytes, int, float)):
            uin = _extract_uin_from_payload(value, _depth=_depth + 1, _seen=_seen)
            if uin:
                return uin
    return 0


async def fetch_login_uin(bot: Any) -> int:
    for action in LOGIN_INFO_ACTIONS:
        try:
            payload = await call_onebot_action(bot, action)
        except Exception:
            continue
        uin = _extract_uin_from_payload(payload)
        if uin:
            return uin
    return 0


def _inject_login_uin(cookie_text: str, uin: int) -> str:
    if not uin:
        return cookie_text
    try:
        if normalize_uin(parse_cookie_text(cookie_text)):
            return cookie_text
    except Exception:
        pass
    prefix = f"uin=o{uin}; p_uin=o{uin}"
    cookie_text = cookie_text.strip()
    if not cookie_text:
        return prefix
    return f"{prefix}; {cookie_text}"


def extract_cookie_text(payload: Any, *, _depth: int = 0, _seen: set[int] | None = None) -> str:
    """Extract a Cookie header string from OneBot action payloads."""

    if _seen is None:
        _seen = set()
    if payload is None or _depth > 8:
        return ""
    if isinstance(payload, bytes):
        try:
            payload = payload.decode("utf-8")
        except Exception:
            return ""
    if isinstance(payload, str):
        text = payload.strip()
        if not text:
            return ""
        if text.startswith("{") or text.startswith("["):
            try:
                return extract_cookie_text(json.loads(text), _depth=_depth + 1, _seen=_seen)
            except Exception:
                pass
        return text if _is_cookie_string(text) else ""
    if isinstance(payload, (list, tuple)):
        parts: list[str] = []
        for item in payload:
            text = extract_cookie_text(item, _depth=_depth + 1, _seen=_seen)
            if text:
                parts.append(text)
        if parts:
            return "; ".join(parts)
        return ""
    if not isinstance(payload, dict):
        return ""

    obj_id = id(payload)
    if obj_id in _seen:
        return ""
    _seen.add(obj_id)

    pair_text = _cookie_pair_object_to_text(payload)
    if pair_text:
        return pair_text

    mapped_text = _cookie_mapping_to_text(payload)
    if mapped_text:
        return mapped_text

    for key in COOKIE_VALUE_KEYS:
        if key in payload:
            text = extract_cookie_text(payload.get(key), _depth=_depth + 1, _seen=_seen)
            if text:
                return text

    for value in payload.values():
        if isinstance(value, (dict, list, tuple, str, bytes)):
            text = extract_cookie_text(value, _depth=_depth + 1, _seen=_seen)
            if text:
                return text
    return ""


def _unique(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        item = str(item).strip()
        if not item or item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def iter_cookie_domains(configured_domain: str) -> list[str]:
    """Yield domain candidates for OneBot cookie requests."""

    domain = (configured_domain or "").strip()
    candidates: list[str] = []
    if domain:
        candidates.append(domain)
        if "://" in domain:
            try:
                from urllib.parse import urlsplit

                parsed = urlsplit(domain)
                host = parsed.netloc or parsed.path
                if host:
                    candidates.append(host)
                    candidates.append(f"https://{host}")
                    candidates.append(f"https://{host}/")
            except Exception:
                pass
    for fallback in COOKIE_DOMAIN_FALLBACKS:
        candidates.append(fallback)
        candidates.append(f"https://{fallback}")
        candidates.append(f"https://{fallback}/")
    return _unique(candidates)


async def call_onebot_action(bot: Any, action: str, **params: Any) -> Any:
    """Call a OneBot action via direct method or call_action fallback."""

    method = getattr(bot, action, None)
    if callable(method):
        result = method(**params)
        if inspect.isawaitable(result):
            return await result
        return result

    call_action = getattr(bot, "call_action", None)
    if not callable(call_action):
        raise AttributeError("OneBot client does not expose call_action")

    result = call_action(action, **params)
    if inspect.isawaitable(result):
        return await result
    return result


async def fetch_cookie_text(bot: Any, *, domain: str) -> str:
    """Try several OneBot actions and domains until a Cookie header is found."""

    login_uin: int | None = None
    for action in COOKIE_ACTIONS:
        for candidate_domain in iter_cookie_domains(domain):
            for call_kwargs in (
                {"domain": candidate_domain},
                {},
            ):
                try:
                    payload = await call_onebot_action(bot, action, **call_kwargs)
                except Exception:
                    continue
                cookie_text = extract_cookie_text(payload)
                if cookie_text:
                    try:
                        cookies = parse_cookie_text(cookie_text)
                    except Exception:
                        cookies = {}
                    if not normalize_uin(cookies):
                        if login_uin is None:
                            login_uin = await fetch_login_uin(bot)
                        if login_uin:
                            cookie_text = _inject_login_uin(cookie_text, login_uin)
                            try:
                                cookies = parse_cookie_text(cookie_text)
                            except Exception:
                                cookies = {}
                    if normalize_uin(cookies):
                        return cookie_text
    return ""
