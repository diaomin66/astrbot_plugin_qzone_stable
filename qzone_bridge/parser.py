"""Parsing helpers for QQ空间 payloads."""

from __future__ import annotations

import html as html_lib
import json
import re
from typing import Any

from .models import FeedEntry
from .utils import entire_closing, extract_scripts, firstn, gtk, json_loads, truncate


COOKIE_SECRET_KEYS = ("p_skey", "skey", "pskey", "skey2")
COOKIE_GTK_KEYS = ("g_tk", "gtk", "bkn", "csrf_token")
COOKIE_KEY_ALIASES = {
    "p_skey": "p_skey",
    "pskey": "p_skey",
    "gtk": "g_tk",
    "bkn": "g_tk",
    "csrf_token": "g_tk",
}
FEED_CONTAINER_KEYS = ("feedpage", "main")
FEED_LIST_KEYS = ("vFeeds", "vfeeds", "msglist", "data", "feeds", "feedlist", "feedList")
FEED_CURSOR_KEYS = ("attachinfo", "attach_info", "attachInfo", "attach", "externparam", "res_attach")
FEED_HAS_MORE_KEYS = ("hasmore", "hasMore", "hasMoreFeeds", "has_more")
HTML_ATTR_RE_TEMPLATE = r"""\b{name}\s*=\s*(["'])(.*?)\1"""
HTML_BREAK_RE = re.compile(r"<\s*br\s*/?\s*>", re.I)
HTML_BLOCK_RE = re.compile(r"</\s*(?:p|div|li|tr)\s*>", re.I)
HTML_TAG_RE = re.compile(r"<[^>]+>")


def _dig(value: Any, *keys: str) -> Any:
    current = value
    for key in keys:
        if not isinstance(current, dict):
            return None
        if key in current:
            current = current[key]
            continue
        return None
    return current


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        return "".join(_text(item) for item in value)
    if isinstance(value, dict):
        for key in ("summary", "content", "text", "msg", "title"):
            if key in value:
                result = _text(value.get(key))
                if result:
                    return result
        return ""
    return str(value)


def _html_to_text(value: Any) -> str:
    text = _text(value)
    if not text:
        return ""
    text = HTML_BREAK_RE.sub("\n", text)
    text = HTML_BLOCK_RE.sub("\n", text)
    text = HTML_TAG_RE.sub("", text)
    text = html_lib.unescape(text)
    return re.sub(r"[ \t\r\f\v]+", " ", text).strip()


def _html_attr(markup: Any, name: str) -> str:
    text = _text(markup)
    if not text:
        return ""
    pattern = HTML_ATTR_RE_TEMPLATE.format(name=re.escape(name))
    match = re.search(pattern, text, re.S | re.I)
    if not match:
        return ""
    return html_lib.unescape(match.group(2)).strip()


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value or 0)
    except Exception:
        return default


def parse_cookie_text(cookie_text: str) -> dict[str, str]:
    cookie_text = cookie_text.strip()
    if not cookie_text:
        return {}
    if cookie_text.startswith("{") or cookie_text.startswith("["):
        payload = json.loads(cookie_text)
        if isinstance(payload, dict):
            return normalize_cookie_fields(payload)
        raise ValueError("cookie JSON must be an object")

    cookie_text = cookie_text.replace("\n", ";")
    cookie_text = cookie_text.replace("\r", ";")
    if cookie_text.lower().startswith("cookie:"):
        cookie_text = cookie_text.split(":", 1)[1].strip()

    cookies: dict[str, str] = {}
    for part in cookie_text.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, value = part.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"')
        if key:
            cookies[key] = value
    return normalize_cookie_fields(cookies)


def normalize_cookie_fields(cookies: dict[str, Any]) -> dict[str, str]:
    """Normalize OneBot/browser cookie aliases into Qzone-compatible keys."""

    normalized: dict[str, str] = {}
    for key, value in cookies.items():
        if value in (None, ""):
            continue
        original = str(key).strip()
        if not original:
            continue
        cookie_value = str(value).strip().strip('"')
        if not cookie_value:
            continue

        alias_key = original.lower().replace("-", "_")
        canonical = COOKIE_KEY_ALIASES.get(alias_key, original)
        normalized.setdefault(original, cookie_value)
        normalized.setdefault(canonical, cookie_value)

    if "uin" in normalized and "p_uin" not in normalized:
        normalized["p_uin"] = normalized["uin"]
    if "p_uin" in normalized and "uin" not in normalized:
        normalized["uin"] = normalized["p_uin"]
    return normalized


def cookie_gtk(cookies: dict[str, str]) -> int:
    """Return a usable g_tk from skey-like cookies or direct OneBot tokens."""

    normalized = normalize_cookie_fields(cookies)
    for key in COOKIE_SECRET_KEYS:
        value = normalized.get(key)
        if value:
            return gtk(value)
    for key in COOKIE_GTK_KEYS:
        value = normalized.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text.isdigit():
            return int(text)
    return 0


def normalize_uin(cookies: dict[str, str], override: int | None = None) -> int:
    if override:
        return int(override)
    candidates = [
        cookies.get("uin"),
        cookies.get("p_uin"),
        cookies.get("ptui_loginuin"),
        cookies.get("luin"),
    ]
    for candidate in candidates:
        if not candidate:
            continue
        cleaned = str(candidate).strip().lstrip("oO")
        if cleaned.isdigit():
            return int(cleaned)
    return 0


def cookie_header(cookies: dict[str, str]) -> str:
    return "; ".join(f"{key}={value}" for key, value in cookies.items())


def compute_unikey(appid: int, hostuin: int, fid: str) -> str:
    if appid == 311:
        return f"https://user.qzone.qq.com/{hostuin}/mood/{fid}"
    return f"https://user.qzone.qq.com/{hostuin}/app/{appid}/{fid}"


def topic_id(appid: int, hostuin: int, fid: str, created_at: int = 0) -> str:
    if appid == 311:
        return f"{hostuin}_{fid}__1"
    return f"{hostuin}_{created_at}"


def parse_index_html(html_text: str) -> dict[str, Any]:
    scripts = extract_scripts(html_text)
    script = firstn(scripts, lambda item: "shine0callback" in item)
    if not script:
        raise ValueError("index page script not found")

    match = re.search(r'window\.shine0callback.*return "([0-9a-f]+?)";', script)
    if not match:
        raise ValueError("qzonetoken not found")
    qzonetoken = match.group(1)

    match = re.search(r"var FrontPage =.*?data\s*:\s*\{", script, re.S)
    if not match:
        raise ValueError("index page data not found")
    data = script[match.end() - 1 : match.end() + entire_closing(script[match.end() - 1 :])]
    payload = json_loads(data)
    if not isinstance(payload, dict):
        raise ValueError("unexpected index payload")
    if isinstance(payload.get("data"), dict):
        payload["data"]["qzonetoken"] = qzonetoken
    else:
        payload["qzonetoken"] = qzonetoken
    return payload


def parse_profile_html(html_text: str) -> dict[str, Any]:
    scripts = extract_scripts(html_text)
    script = firstn(scripts, lambda item: "shine0callback" in item)
    if not script:
        raise ValueError("profile page script not found")

    match = re.search(r'window\.shine0callback.*return "([0-9a-f]+?)";', script)
    if not match:
        raise ValueError("profile qzonetoken not found")
    qzonetoken = match.group(1)

    match = re.search(r"var FrontPage =.*?data\s*:\s*\[", script, re.S)
    if not match:
        raise ValueError("profile page data not found")
    data = script[match.end() - 1 : match.end() + entire_closing(script[match.end() - 1 :], "[")]
    data = re.sub(r",,\]$", "]", data)
    payload = json_loads(data)
    if not isinstance(payload, list):
        raise ValueError("unexpected profile payload")
    if len(payload) < 2:
        raise ValueError("profile payload incomplete")
    info = unwrap_payload(payload[0]) if isinstance(payload[0], dict) else payload[0]
    feedpage = unwrap_payload(payload[1]) if isinstance(payload[1], dict) else payload[1]
    return {"info": info, "feedpage": feedpage, "qzonetoken": qzonetoken}


def unwrap_payload(payload: Any) -> Any:
    if isinstance(payload, dict) and "data" in payload and payload["data"] is not None:
        return payload["data"]
    return payload


def extract_hostuin(feed_item: dict[str, Any], default: int = 0) -> int:
    html_markup = feed_item.get("html")
    candidates = [
        feed_item.get("uin"),
        feed_item.get("hostuin"),
        feed_item.get("hostUin"),
        _html_attr(html_markup, "data-uin"),
        _html_attr(html_markup, "uin"),
        _dig(feed_item, "userinfo", "uin"),
        _dig(feed_item, "user", "uin"),
        _dig(feed_item, "userinfo", "user", "uin"),
        default,
    ]
    for candidate in candidates:
        if candidate is None:
            continue
        try:
            value = int(candidate or 0)
        except Exception:
            continue
        if value:
            return value
    return default


def extract_fid(feed_item: dict[str, Any]) -> str:
    html_markup = feed_item.get("html")
    candidates = [
        feed_item.get("fid"),
        feed_item.get("tid"),
        feed_item.get("cellid"),
        feed_item.get("key"),
        feed_item.get("ugckey"),
        feed_item.get("ugcrightkey"),
        _html_attr(html_markup, "data-fid"),
        _html_attr(html_markup, "fid"),
        _html_attr(html_markup, "data-tid"),
        _html_attr(html_markup, "tid"),
        _html_attr(html_markup, "data-cellid"),
        _dig(feed_item, "id", "cellid"),
        _dig(feed_item, "common", "ugcrightkey"),
        _dig(feed_item, "common", "ugckey"),
    ]
    for candidate in candidates:
        if candidate:
            return str(candidate)
    return ""


def extract_summary_text(feed_item: dict[str, Any]) -> str:
    candidates = [
        _text(feed_item.get("content")),
        _text(feed_item.get("con")),
        _dig(feed_item, "summary", "summary"),
        _text(feed_item.get("summary")),
        _dig(feed_item, "original", "summary", "summary"),
        _text(feed_item.get("text")),
        _html_to_text(feed_item.get("html")),
    ]
    for candidate in candidates:
        if candidate:
            return truncate(str(candidate).strip(), 500)
    return ""


def extract_feed_entry(feed_item: dict[str, Any], *, default_hostuin: int = 0) -> FeedEntry:
    common = feed_item.get("common") or feed_item.get("cell_comm") or {}
    if not isinstance(common, dict):
        common = {}
    userinfo = feed_item.get("userinfo") or feed_item.get("user") or {}
    if not isinstance(userinfo, dict):
        userinfo = {}
    like = feed_item.get("like") or {}
    if not isinstance(like, dict):
        like = {}
    comment = feed_item.get("comment") or {}
    if not isinstance(comment, dict):
        comment = {}
    operation = feed_item.get("operation") or {}
    if not isinstance(operation, dict):
        operation = {}
    original = feed_item.get("original") or {}
    if not isinstance(original, dict):
        original = {}
    html_markup = feed_item.get("html")

    hostuin = extract_hostuin(feed_item, default_hostuin)
    appid = _int(
        common.get("appid")
        or feed_item.get("appid")
        or _html_attr(html_markup, "data-appid")
        or 311,
        311,
    )
    fid = extract_fid(feed_item)
    created_at = _int(common.get("time") or feed_item.get("abstime") or feed_item.get("created_time") or 0)
    summary = extract_summary_text(feed_item)
    if not summary:
        summary = extract_summary_text(original)

    curkey = str(
        feed_item.get("curkey")
        or feed_item.get("curlikekey")
        or common.get("curkey")
        or common.get("curlikekey")
        or _html_attr(html_markup, "data-curkey")
        or _html_attr(html_markup, "curkey")
        or compute_unikey(appid, hostuin, fid)
        or ""
    )
    unikey = (
        _html_attr(html_markup, "data-unikey")
        or _html_attr(html_markup, "unikey")
        or compute_unikey(appid, hostuin, fid)
    )
    topic = topic_id(appid, hostuin, fid, created_at)
    nickname = str(
        feed_item.get("name")
        or feed_item.get("nickname")
        or userinfo.get("nickname")
        or userinfo.get("name")
        or ""
    )
    like_count = _int(
        like.get("num")
        or like.get("likeNum")
        or like.get("count")
        or feed_item.get("likeNum")
        or feed_item.get("likenum")
        or feed_item.get("like_num")
        or 0
    )
    raw_comments = feed_item.get("commentlist")
    comment_count = _int(
        comment.get("num") or comment.get("commentcount") or feed_item.get("cmtnum") or feed_item.get("commentnum") or 0
    )
    if not comment_count and isinstance(raw_comments, list):
        comment_count = len(raw_comments)
    liked = bool(like.get("isliked") or like.get("ismylike") or feed_item.get("isliked") or False)
    busi_param = operation.get("busi_param") or {}
    if not isinstance(busi_param, dict):
        busi_param = {}

    return FeedEntry(
        hostuin=hostuin,
        fid=fid,
        appid=appid,
        summary=summary,
        nickname=nickname,
        created_at=created_at,
        like_count=like_count,
        comment_count=comment_count,
        liked=liked,
        curkey=curkey,
        unikey=unikey,
        busi_param=busi_param,
        topic_id=topic,
        raw=feed_item,
    )


def _looks_like_feed_page(value: dict[str, Any]) -> bool:
    return any(key in value for key in (*FEED_LIST_KEYS, *FEED_CURSOR_KEYS, *FEED_HAS_MORE_KEYS))


def normalize_feed_page(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    for key in FEED_CONTAINER_KEYS:
        value = payload.get(key)
        if isinstance(value, dict):
            return value

    data = payload.get("data")
    if isinstance(data, dict):
        for key in FEED_CONTAINER_KEYS:
            value = data.get(key)
            if isinstance(value, dict):
                return value
        if _looks_like_feed_page(data):
            return data

    return payload


def extract_raw_feeds(feedpage: dict[str, Any]) -> list[Any]:
    if not isinstance(feedpage, dict):
        return []
    raw_feeds: Any = []
    for key in FEED_LIST_KEYS:
        value = feedpage.get(key)
        if value:
            raw_feeds = value
            break
    if isinstance(raw_feeds, dict):
        raw_feeds = extract_raw_feeds(raw_feeds)
    if not isinstance(raw_feeds, list):
        return []
    return raw_feeds


def feed_page_has_more(feedpage: dict[str, Any]) -> bool:
    for key in FEED_HAS_MORE_KEYS:
        if key not in feedpage:
            continue
        value = feedpage.get(key)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes"}
        return bool(value)
    return False


def feed_page_cursor(feedpage: dict[str, Any]) -> str:
    for key in FEED_CURSOR_KEYS:
        value = feedpage.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


def extract_feed_page(payload: dict[str, Any], *, default_hostuin: int = 0) -> tuple[dict[str, Any], list[FeedEntry]]:
    feedpage = normalize_feed_page(payload)
    if not isinstance(feedpage, dict):
        return {}, []
    raw_feeds = extract_raw_feeds(feedpage)
    items = [
        extract_feed_entry(item, default_hostuin=default_hostuin)
        for item in raw_feeds
        if isinstance(item, dict)
    ]
    return feedpage, items
