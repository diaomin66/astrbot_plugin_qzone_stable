"""Low-level QQ 空间 HTTP client."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlparse, urlunparse

import httpx

from .errors import QzoneNeedsRebind, QzoneParseError, QzoneRequestError
from .media import QZONE_MAX_IMAGES, is_supported_image, normalize_media_item, source_name
from .models import FeedEntry, SessionState
from .parser import (
    cookie_header,
    cookie_gtk,
    compute_unikey,
    extract_feed_entry,
    normalize_uin,
    normalize_cookie_fields,
    parse_index_html,
    parse_profile_html,
    unwrap_payload,
)
from .render import cookie_summary
from .utils import extract_callback_json, json_loads, now_iso

log = logging.getLogger(__name__)

AUTH_ERROR_CODES = {-3000}
AUTH_ERROR_KEYWORDS = (
    "\u767b\u5f55",
    "\u5931\u6548",
    "\u8bf7\u5148\u767b\u5f55",
    "skey",
    "g_tk",
    "cookie",
    "expired",
    "login",
)
LOGIN_REDIRECT_HOSTS = ("ptlogin", "ui.ptlogin", "xui.ptlogin", "ssl.ptlogin", "login")
QZONE_IMAGE_UPLOAD_URL = "https://up.qzone.qq.com/cgi-bin/upload/cgi_upload_image"
QZONE_REDIRECT_STATUS_CODES = {301, 302, 303, 307, 308}
QZONE_LIKE_DIRECT_URL = "https://w.qzone.qq.com/cgi-bin/likes/internal_dolike_app"
QZONE_UNLIKE_DIRECT_URL = "https://w.qzone.qq.com/cgi-bin/likes/internal_unlike_app"
QZONE_LIKE_PROXY_URL = "https://user.qzone.qq.com/proxy/domain/w.qzone.qq.com/cgi-bin/likes/internal_dolike_app"
QZONE_UNLIKE_PROXY_URL = "https://user.qzone.qq.com/proxy/domain/w.qzone.qq.com/cgi-bin/likes/internal_unlike_app"
QZONE_LIKE_URL = QZONE_LIKE_DIRECT_URL
QZONE_UNLIKE_URL = QZONE_UNLIKE_DIRECT_URL
QZONE_VISITOR_URL = "https://h5.qzone.qq.com/proxy/domain/g.qzone.qq.com/cgi-bin/friendshow/cgi_get_visitor_more"
QZONE_REPLY_URL = "https://h5.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_re_feeds"
QZONE_DELETE_URL = "https://h5.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_delete_v6"
MAX_UPLOAD_IMAGE_BYTES = 32 * 1024 * 1024
IMAGE_SOURCE_CACHE_TTL_SECONDS = 10 * 60
IMAGE_SOURCE_CACHE_MAX_ITEMS = 16
IMAGE_SOURCE_CACHE_MAX_ITEM_BYTES = 8 * 1024 * 1024
IMAGE_SOURCE_CACHE_MAX_TOTAL_BYTES = 64 * 1024 * 1024


@dataclass(slots=True)
class FeedPageResult:
    scope: str
    hostuin: int
    items: list[FeedEntry]
    has_more: bool
    cursor: str
    raw: dict[str, Any]


class QzoneClient:
    def __init__(
        self,
        session: SessionState,
        *,
        timeout: float = 15.0,
        user_agent: str = "",
        max_retries: int = 3,
    ) -> None:
        self.session = session
        self._normalize_session()
        self.timeout = timeout
        self.max_retries = max(1, int(max_retries))
        self.user_agent = user_agent or (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        )
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout),
            follow_redirects=False,
            trust_env=False,
            headers={"User-Agent": self.user_agent},
        )
        self.feed_cache: dict[tuple[int, str], FeedEntry] = {}
        self._image_source_cache: OrderedDict[str, tuple[float, bytes, str]] = OrderedDict()

    def _normalize_session(self) -> None:
        self.session.cookies = normalize_cookie_fields(dict(self.session.cookies or {}))
        self.session.uin = normalize_uin(self.session.cookies, override=self.session.uin) or self.session.uin

    async def close(self) -> None:
        await self._client.aclose()

    @property
    def login_uin(self) -> int:
        return int(self.session.uin or 0)

    @property
    def cookie_count(self) -> int:
        return len(self.session.cookies)

    @property
    def cookie_text(self) -> str:
        return cookie_header(self.session.cookies)

    @property
    def gtk(self) -> int:
        return cookie_gtk(self.session.cookies)

    def cookie_summary(self) -> str:
        return cookie_summary(self.session.cookies)

    def update_session(self, session: SessionState) -> None:
        self.session = session
        self._normalize_session()

    def _cached_image_source(self, source: str) -> tuple[bytes, str] | None:
        cached = self._image_source_cache.get(source)
        if cached is None:
            return None
        expires_at, data, mime_type = cached
        if expires_at <= time.monotonic():
            self._image_source_cache.pop(source, None)
            return None
        self._image_source_cache.move_to_end(source)
        return data, mime_type

    def _store_image_source_cache(self, source: str, data: bytes, mime_type: str) -> None:
        if not source or not data or len(data) > IMAGE_SOURCE_CACHE_MAX_ITEM_BYTES:
            return
        now = time.monotonic()
        for key, (expires_at, _, _) in list(self._image_source_cache.items()):
            if expires_at <= now:
                self._image_source_cache.pop(key, None)
        self._image_source_cache[source] = (now + IMAGE_SOURCE_CACHE_TTL_SECONDS, data, mime_type)
        self._image_source_cache.move_to_end(source)
        total_bytes = sum(len(item[1]) for item in self._image_source_cache.values())
        while (
            len(self._image_source_cache) > IMAGE_SOURCE_CACHE_MAX_ITEMS
            or total_bytes > IMAGE_SOURCE_CACHE_MAX_TOTAL_BYTES
        ):
            _, (_, evicted_data, _) = self._image_source_cache.popitem(last=False)
            total_bytes -= len(evicted_data)

    @staticmethod
    def _payload_needs_rebind(code: int, message: str) -> bool:
        if code in AUTH_ERROR_CODES:
            return True
        normalized = message.lower()
        return any(keyword in message or keyword in normalized for keyword in AUTH_ERROR_KEYWORDS)

    def _raise_payload_error(self, payload: Any, response: httpx.Response) -> None:
        if not isinstance(payload, dict):
            return
        for key in ("ret", "code", "err", "error"):
            if key in payload and payload.get(key) not in (0, "0", None):
                raw_code = payload.get(key)
                try:
                    code = int(raw_code or 0)
                except (TypeError, ValueError):
                    code = 0
                message = str(payload.get("msg") or payload.get("message") or payload.get("text") or "")
                if self._payload_needs_rebind(code, message):
                    raise QzoneNeedsRebind(message or "QQ 空间登录态已失效", detail=payload)
                code_text = raw_code if raw_code not in (None, "") else code
                raise QzoneRequestError(message or f"QQ 空间接口返回错误 {code_text}", status_code=response.status_code, detail=payload)

    @staticmethod
    def _response_detail(response: httpx.Response) -> dict[str, Any]:
        detail: dict[str, Any] = {
            "status_code": response.status_code,
            "url": str(response.request.url),
        }
        location = response.headers.get("location") or response.headers.get("Location")
        if location:
            detail["location"] = location
        try:
            text = response.text
        except Exception:
            text = ""
        if text:
            detail["text"] = text[:500]
        return detail

    @staticmethod
    def _is_login_redirect(location: str) -> bool:
        if not location:
            return False
        lowered = location.lower()
        return any(host in lowered for host in LOGIN_REDIRECT_HOSTS)

    def _is_qzone_home_redirect(self, response: httpx.Response) -> bool:
        location = response.headers.get("location") or response.headers.get("Location") or ""
        if not location:
            return False
        lowered = location.lower()
        if "user.qzone.qq.com" not in lowered:
            return False
        uin = str(self.login_uin)
        return lowered.rstrip("/").endswith(f"/{uin}") or lowered.rstrip("/").endswith("user.qzone.qq.com")

    @staticmethod
    def _is_allowed_qzone_redirect(current_url: str, location: str) -> bool:
        if not location:
            return False
        target = urljoin(current_url, location)
        parsed = urlparse(target)
        if parsed.scheme.lower() not in {"http", "https"}:
            return False
        host = (parsed.hostname or "").lower()
        return host == "qq.com" or host.endswith(".qq.com")

    @staticmethod
    def _is_like_action_redirect(current_url: str, location: str) -> bool:
        if not location:
            return False
        target = urljoin(current_url, location)
        parsed = urlparse(target)
        return "/cgi-bin/likes/internal_" in parsed.path

    @staticmethod
    def _redirect_url_with_params(target_url: str, params: dict[str, Any] | None) -> str:
        if not params:
            return target_url
        parsed = urlparse(target_url)
        query = parse_qsl(parsed.query, keep_blank_values=True)
        query.extend((str(key), str(value)) for key, value in params.items())
        return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))

    def _persist_cookie_response(self, response: httpx.Response) -> None:
        for key, value in response.cookies.items():
            if value is not None:
                self.session.cookies[key] = value
        self.session.cookies = normalize_cookie_fields(dict(self.session.cookies))
        if self.session.cookies:
            self.session.updated_at = now_iso()

    def _headers(
        self,
        *,
        referer: str | None = None,
        origin: str | None = None,
        extra: dict[str, str] | None = None,
    ) -> dict[str, str]:
        headers = {"User-Agent": self.user_agent}
        if self.session.cookies:
            headers["Cookie"] = self.cookie_text
        if referer:
            headers["Referer"] = referer
        if origin:
            headers["Origin"] = origin
        if extra:
            headers.update(extra)
        return headers

    def _merge_params(self, params: dict[str, Any] | None, *, hostuin: int | None = None, attach_token: bool = False) -> dict[str, Any]:
        merged: dict[str, Any] = dict(params or {})
        if hostuin is None:
            hostuin = self.login_uin
        if attach_token and hostuin:
            token = self.session.qzonetokens.get(str(hostuin))
            if token:
                merged.setdefault("qzonetoken", token)
        if self.gtk:
            merged.setdefault("g_tk", self.gtk)
        return merged

    async def _request_text(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        json_body: Any | None = None,
        referer: str | None = None,
        origin: str | None = None,
        hostuin: int | None = None,
        attach_token: bool = False,
        login_required: bool = True,
        follow_qzone_redirects: bool = False,
        accept_qzone_redirects: bool = False,
        max_attempts: int | None = None,
    ) -> httpx.Response:
        if login_required and not self.session.cookies:
            raise QzoneNeedsRebind()
        if login_required and self.gtk == 0:
            raise QzoneNeedsRebind("Cookie 缺少 p_skey 或 skey，无法计算 g_tk")

        params = self._merge_params(params, hostuin=hostuin, attach_token=attach_token)
        attempts = self.max_retries if max_attempts is None else max(1, int(max_attempts))
        last_exc: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                current_url = url
                current_params = params
                redirects_left = 3
                while True:
                    response = await self._client.request(
                        method,
                        current_url,
                        params=current_params,
                        data=data,
                        json=json_body,
                        headers=self._headers(referer=referer, origin=origin),
                    )
                    self._persist_cookie_response(response)
                    location = response.headers.get("location") or response.headers.get("Location") or ""
                    if response.status_code in QZONE_REDIRECT_STATUS_CODES and self._is_qzone_home_redirect(response):
                        if accept_qzone_redirects and self._is_allowed_qzone_redirect(str(response.request.url), location):
                            return response
                        raise QzoneRequestError(
                            "QQ 空间 H5 页面跳转到主页，接口数据不可用",
                            status_code=response.status_code,
                            detail=self._response_detail(response),
                        )
                    if response.status_code == 401 or (
                        response.status_code in QZONE_REDIRECT_STATUS_CODES and self._is_login_redirect(location)
                    ):
                        raise QzoneNeedsRebind(
                            "QQ 空间登录态已失效，需要重新绑定 Cookie",
                            detail=self._response_detail(response),
                        )
                    if response.status_code in QZONE_REDIRECT_STATUS_CODES:
                        allowed_redirect = self._is_allowed_qzone_redirect(str(response.request.url), location)
                        if (
                            follow_qzone_redirects
                            and redirects_left > 0
                            and allowed_redirect
                            and (
                                not accept_qzone_redirects
                                or self._is_like_action_redirect(str(response.request.url), location)
                            )
                        ):
                            target_url = urljoin(str(response.request.url), location)
                            current_url = self._redirect_url_with_params(target_url, params)
                            current_params = None
                            redirects_left -= 1
                            continue
                        if accept_qzone_redirects and allowed_redirect:
                            return response
                        raise QzoneRequestError(
                            f"QQ 空间接口跳转异常 {response.status_code}",
                            status_code=response.status_code,
                            detail=self._response_detail(response),
                        )
                    break
                if response.status_code == 403:
                    raise QzoneRequestError(
                        "QQ 空间拒绝访问，可能没有权限",
                        status_code=response.status_code,
                        detail=self._response_detail(response),
                    )
                if response.status_code == 429:
                    raise QzoneRequestError(
                        "QQ 空间请求过于频繁，请稍后再试",
                        status_code=response.status_code,
                        detail=self._response_detail(response),
                    )
                if response.status_code >= 500:
                    raise QzoneRequestError(
                        f"QQ 空间服务暂时不可用 ({response.status_code})",
                        status_code=response.status_code,
                        detail=self._response_detail(response),
                    )
                if response.status_code >= 400:
                    raise QzoneRequestError(
                        f"QQ 空间接口 HTTP {response.status_code}",
                        status_code=response.status_code,
                        detail=self._response_detail(response),
                    )
                return response
            except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPError, QzoneRequestError) as exc:
                last_exc = exc
                if isinstance(exc, QzoneRequestError) and exc.status_code is not None and exc.status_code < 500:
                    raise
                if attempt >= attempts:
                    raise
                await asyncio.sleep(min(2.0 * attempt, 6.0))
        assert last_exc is not None
        raise last_exc

    @staticmethod
    def _parse_response_payload(text: str, response: httpx.Response) -> Any:
        if not text:
            return {}

        json_like = text.startswith(("{", "["))
        callback_error: Exception | None = None
        if not json_like:
            try:
                callback_payload = extract_callback_json(text)
            except Exception as exc:
                callback_error = exc
            else:
                if callback_payload is not None:
                    return callback_payload

        try:
            return json.loads(text)
        except Exception as json_exc:
            if not json_like:
                detail = {"text": text[:500], "url": str(response.request.url)}
                raise QzoneParseError(
                    "QQ 空间接口返回的内容不是 JSON",
                    detail=detail,
                ) from (callback_error or json_exc)
            try:
                return json_loads(text)
            except Exception as js_exc:
                detail = {"text": text[:500], "url": str(response.request.url)}
                raise QzoneParseError(
                    "无法解析 QQ 空间 JSON 内容",
                    detail=detail,
                ) from (js_exc or json_exc)

    async def _request_json(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        json_body: Any | None = None,
        referer: str | None = None,
        origin: str | None = None,
        hostuin: int | None = None,
        attach_token: bool = False,
        login_required: bool = True,
        follow_qzone_redirects: bool = False,
        accept_qzone_redirects: bool = False,
        max_attempts: int | None = None,
    ) -> dict[str, Any]:
        response = await self._request_text(
            method,
            url,
            params=params,
            data=data,
            json_body=json_body,
            referer=referer,
            origin=origin,
            hostuin=hostuin,
            attach_token=attach_token,
            login_required=login_required,
            follow_qzone_redirects=follow_qzone_redirects,
            accept_qzone_redirects=accept_qzone_redirects,
            max_attempts=max_attempts,
        )
        if accept_qzone_redirects and response.status_code in QZONE_REDIRECT_STATUS_CODES:
            return {"message": "accepted redirect", "redirect": self._response_detail(response)}
        text = response.text.strip()
        payload = self._parse_response_payload(text, response)
        self._raise_payload_error(payload, response)
        payload = unwrap_payload(payload)
        self._raise_payload_error(payload, response)
        return payload if isinstance(payload, dict) else {"data": payload}

    def _extract_index_or_profile(self, response_text: str, *, profile: bool = False) -> dict[str, Any]:
        try:
            payload = parse_profile_html(response_text) if profile else parse_index_html(response_text)
        except Exception as exc:
            raise QzoneParseError("QQ 空间页面解析失败", detail={"text": response_text[:500]}) from exc
        return payload

    def _store_token(self, hostuin: int, token: str) -> None:
        if hostuin and token:
            self.session.qzonetokens[str(hostuin)] = token

    async def index(self) -> dict[str, Any]:
        response = await self._request_text(
            "GET",
            "https://h5.qzone.qq.com/mqzone/index",
            referer=f"https://user.qzone.qq.com/{self.login_uin}" if self.login_uin else "https://qzone.qq.com/",
            login_required=True,
        )
        payload = self._extract_index_or_profile(response.text, profile=False)
        data = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(data, dict):
            token = str(data.get("qzonetoken") or "")
            if token:
                self._store_token(self.login_uin, token)
        elif isinstance(payload, dict):
            token = str(payload.get("qzonetoken") or "")
            if token:
                self._store_token(self.login_uin, token)
        return payload

    async def profile(self, hostuin: int, start_time: float = 0) -> dict[str, Any]:
        response = await self._request_text(
            "GET",
            "https://h5.qzone.qq.com/mqzone/profile",
            params={"hostuin": hostuin, "starttime": int(start_time * 1000)},
            referer=f"https://user.qzone.qq.com/{hostuin}",
            login_required=True,
        )
        payload = self._extract_index_or_profile(response.text, profile=True)
        token = str(payload.get("qzonetoken") or "")
        if token:
            self._store_token(hostuin, token)
        return payload

    async def get_active_feeds(self, attach_info: str = "") -> dict[str, Any]:
        payload = await self._request_json(
            "GET",
            "https://h5.qzone.qq.com/webapp/json/mqzone_feeds/getActiveFeeds",
            params={"attach_info": attach_info},
            referer=f"https://user.qzone.qq.com/{self.login_uin}",
            hostuin=self.login_uin,
            attach_token=True,
            follow_qzone_redirects=True,
        )
        return payload

    async def get_feeds(self, hostuin: int, attach_info: str = "") -> dict[str, Any]:
        payload = await self._request_json(
            "GET",
            "https://mobile.qzone.qq.com/get_feeds",
            params={
                "hostuin": hostuin,
                "res_attach": attach_info,
                "res_type": 2,
                "refresh_type": 2,
                "format": "json",
            },
            referer=f"https://user.qzone.qq.com/{hostuin}",
            hostuin=hostuin,
            attach_token=True,
            follow_qzone_redirects=True,
        )
        return payload

    async def legacy_feeds(self, hostuin: int, *, page: int = 1, num: int = 10) -> dict[str, Any]:
        payload = await self._request_json(
            "GET",
            "https://user.qzone.qq.com/proxy/domain/taotao.qq.com/cgi-bin/emotion_cgi_msglist_v6",
            params={
                "uin": hostuin,
                "hostUin": hostuin,
                "pos": max(0, (max(1, int(page)) - 1) * max(1, int(num))),
                "num": max(1, int(num)),
                "replynum": 100,
                "callback": "_preloadCallback",
                "code_version": 1,
                "format": "json",
                "need_comment": 1,
                "need_private_comment": 1,
            },
            referer=f"https://user.qzone.qq.com/{hostuin}",
            origin="https://user.qzone.qq.com",
            hostuin=hostuin,
            attach_token=False,
            follow_qzone_redirects=True,
        )
        return payload

    async def legacy_recent_feeds(self, page: int = 1) -> dict[str, Any]:
        payload = await self._request_json(
            "GET",
            "https://user.qzone.qq.com/proxy/domain/ic2.qzone.qq.com/cgi-bin/feeds/feeds3_html_more",
            params={
                "uin": self.login_uin,
                "scope": 0,
                "view": 1,
                "filter": "all",
                "flag": 1,
                "applist": "all",
                "pagenum": max(1, int(page)),
                "aisortEndTime": 0,
                "aisortOffset": 0,
                "aisortBeginTime": 0,
                "begintime": 0,
                "format": "json",
                "useutf8": 1,
                "outputhtmlfeed": 1,
            },
            referer=f"https://user.qzone.qq.com/{self.login_uin}" if self.login_uin else "https://qzone.qq.com/",
            origin="https://user.qzone.qq.com",
            hostuin=self.login_uin,
            attach_token=False,
            follow_qzone_redirects=True,
        )
        return payload

    async def shuoshuo(self, fid: str, uin: int, appid: int = 311, busi_param: str = "") -> dict[str, Any]:
        payload = await self._request_json(
            "GET",
            "https://h5.qzone.qq.com/webapp/json/mqzone_detail/shuoshuo",
            params={
                "cellid": fid,
                "uin": uin,
                "appid": appid,
                "busi_param": busi_param or "",
                "format": "json",
                "count": 20,
                "refresh_type": 31,
                "subid": "",
            },
            referer=f"https://user.qzone.qq.com/{uin}/mood/{fid}",
            hostuin=uin,
            attach_token=True,
            follow_qzone_redirects=True,
        )
        return payload

    async def mfeeds_get_count(self) -> dict[str, Any]:
        payload = await self._request_json(
            "GET",
            "https://mobile.qzone.qq.com/feeds/mfeeds_get_count",
            params={"format": "json"},
            referer=f"https://user.qzone.qq.com/{self.login_uin}" if self.login_uin else "https://qzone.qq.com/",
            hostuin=self.login_uin,
            attach_token=False,
        )
        return payload

    @staticmethod
    def _photo_type(filename: str, mime_type: str = "") -> int:
        lowered = f"{filename} {mime_type}".lower()
        if ".gif" in lowered or "image/gif" in lowered:
            return 2
        if ".png" in lowered or "image/png" in lowered:
            return 3
        if ".bmp" in lowered or "image/bmp" in lowered:
            return 4
        return 1

    @staticmethod
    def _extract_pic_bo(value: str) -> str:
        if not value or "bo=" not in value:
            return ""
        result = value.split("bo=", 1)[1]
        for token in ("!!", "&", "#"):
            result = result.split(token, 1)[0]
        return unquote(result)

    async def _load_image_source(self, media: dict[str, Any]) -> tuple[bytes, str, str]:
        item = normalize_media_item(media, default_kind="image")
        if item is None or not item.source:
            raise QzoneParseError("图片来源为空或无效")

        source = item.source.strip()
        filename = item.name or source_name(source) or "image.jpg"
        mime_type = item.mime_type
        parsed = urlparse(source)
        cached = self._cached_image_source(source) if parsed.scheme.lower() in {"http", "https"} else None
        if cached is not None:
            cached_data, cached_mime = cached
            return cached_data, filename, mime_type or cached_mime
        if source.startswith("base64://"):
            try:
                return base64.b64decode(source[len("base64://") :], validate=False), filename, mime_type
            except Exception as exc:
                raise QzoneParseError("图片 base64 解码失败") from exc
        if source.startswith("data:"):
            try:
                header, encoded = source.split(",", 1)
            except ValueError as exc:
                raise QzoneParseError("图片 data URI 格式错误") from exc
            header_mime = header[5:].split(";", 1)[0]
            if ";base64" not in header:
                raise QzoneParseError("data URI 必须使用 base64 编码")
            try:
                data = base64.b64decode(encoded, validate=False)
            except Exception as exc:
                raise QzoneParseError("图片 data URI 解码失败") from exc
            return data, filename, mime_type or header_mime

        if parsed.scheme.lower() in {"http", "https"}:
            try:
                async with self._client.stream(
                    "GET",
                    source,
                    headers=self._headers(referer=f"https://user.qzone.qq.com/{self.login_uin}"),
                    follow_redirects=True,
                ) as response:
                    self._persist_cookie_response(response)
                    if response.status_code >= 400:
                        text = (await response.aread()).decode("utf-8", errors="ignore")
                        raise QzoneRequestError(
                            f"图片下载失败 HTTP {response.status_code}",
                            status_code=response.status_code,
                            detail={"url": source, "text": text[:500]},
                        )
                    length = response.headers.get("content-length")
                    try:
                        if length and int(length) > MAX_UPLOAD_IMAGE_BYTES:
                            raise QzoneParseError("图片大小超过限制", detail={"url": source})
                    except ValueError:
                        pass
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in response.aiter_bytes():
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > MAX_UPLOAD_IMAGE_BYTES:
                            raise QzoneParseError("图片大小超过限制", detail={"url": source})
                        chunks.append(chunk)
            except httpx.HTTPError as exc:
                raise QzoneRequestError("图片下载失败", detail={"url": source}) from exc
            content_type = response.headers.get("content-type", "").split(";", 1)[0]
            data = b"".join(chunks)
            self._store_image_source_cache(source, data, content_type)
            return data, filename, mime_type or content_type

        path = Path(source)
        def read_local_image() -> bytes:
            if not path.exists() or not path.is_file():
                raise QzoneParseError("图片文件不存在", detail={"path": source})
            stat = path.stat()
            if stat.st_size > MAX_UPLOAD_IMAGE_BYTES:
                raise QzoneParseError("图片大小超过限制", detail={"path": source})
            return path.read_bytes()

        data = await asyncio.to_thread(read_local_image)
        return data, filename or path.name, mime_type

    @staticmethod
    def _photo_payload_from_upload(payload: dict[str, Any], *, filename: str = "", mime_type: str = "") -> dict[str, Any]:
        data = unwrap_payload(payload)
        if not isinstance(data, dict):
            raise QzoneParseError("图片上传返回格式异常", detail=payload)
        albumid = str(data.get("albumid") or data.get("albumId") or "")
        lloc = str(data.get("lloc") or data.get("LLoc") or "")
        sloc = str(data.get("sloc") or data.get("SLoc") or "")
        photo_type = str(data.get("type") or QzoneClient._photo_type(filename, mime_type))
        height = str(data.get("height") or data.get("h") or 0)
        width = str(data.get("width") or data.get("w") or 0)
        if not albumid or not lloc or not sloc:
            raise QzoneParseError("图片上传返回缺少 richval 字段", detail=data)
        url = str(data.get("url") or data.get("origin_url") or data.get("originUrl") or data.get("pre") or "")
        pic_bo = str(data.get("pic_bo") or data.get("picBo") or QzoneClient._extract_pic_bo(url) or "")
        richval = f",{albumid},{lloc},{sloc},{photo_type},{height},{width},,{height},{width}"
        return {
            "albumid": albumid,
            "lloc": lloc,
            "sloc": sloc,
            "type": photo_type,
            "height": height,
            "width": width,
            "url": url,
            "pic_bo": pic_bo,
            "richval": richval,
        }

    async def upload_photo(self, media: dict[str, Any]) -> dict[str, Any]:
        item = normalize_media_item(media, default_kind="image")
        if item is None or not is_supported_image(item):
            raise QzoneParseError("QQ 空间只支持上传图片文件", detail=media)
        data, filename, mime_type = await self._load_image_source(item.to_dict())
        if not data:
            raise QzoneParseError("图片内容为空或无法读取", detail={"name": filename})

        encoded_bytes = await asyncio.to_thread(base64.b64encode, data)
        encoded = encoded_bytes.decode("ascii")
        skey = self.session.cookies.get("skey") or self.session.cookies.get("p_skey") or ""
        p_skey = self.session.cookies.get("p_skey") or self.session.cookies.get("skey") or ""
        upload_payload = await self._request_json(
            "POST",
            QZONE_IMAGE_UPLOAD_URL,
            params={"g_tk": self.gtk},
            data={
                "filename": filename,
                "uin": self.login_uin,
                "skey": skey,
                "zzpaneluin": self.login_uin,
                "p_uin": self.login_uin,
                "p_skey": p_skey,
                "qzonetoken": self.session.qzonetokens.get(str(self.login_uin), ""),
                "uploadtype": "1",
                "albumtype": "7",
                "exttype": "0",
                "refer": "shuoshuo",
                "output_type": "json",
                "charset": "utf-8",
                "output_charset": "utf-8",
                "upload_hd": "1",
                "hd_width": "2048",
                "hd_height": "10000",
                "hd_quality": "96",
                "backUrls": (
                    "http://upbak.photo.qzone.qq.com/cgi-bin/upload/cgi_upload_image,"
                    "http://119.147.64.75/cgi-bin/upload/cgi_upload_image"
                ),
                "url": f"{QZONE_IMAGE_UPLOAD_URL}?g_tk={self.gtk}",
                "base64": "1",
                "picfile": encoded,
                "qzreferrer": f"https://user.qzone.qq.com/{self.login_uin}",
            },
            referer=f"https://user.qzone.qq.com/{self.login_uin}",
            origin="https://user.qzone.qq.com",
            hostuin=self.login_uin,
            attach_token=False,
        )
        return self._photo_payload_from_upload(upload_payload, filename=filename, mime_type=mime_type)

    async def _prepare_publish_photos(self, photos: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
        if photos and len(photos) > QZONE_MAX_IMAGES:
            raise QzoneParseError(f"QQ 空间一次最多只能上传 {QZONE_MAX_IMAGES} 张图片")
        source_photos = list(photos or [])
        prepared: list[dict[str, Any] | None] = [None] * len(source_photos)
        pending: list[tuple[int, dict[str, Any]]] = []
        for index, photo in enumerate(source_photos):
            if isinstance(photo, dict) and photo.get("richval"):
                prepared[index] = photo
                continue
            pending.append((index, photo))

        async def upload_limited(index: int, photo: dict[str, Any], semaphore: asyncio.Semaphore) -> tuple[int, dict[str, Any]]:
            async with semaphore:
                return index, await self.upload_photo(photo)

        if len(pending) == 1:
            index, photo = pending[0]
            prepared[index] = await self.upload_photo(photo)
        elif pending:
            semaphore = asyncio.Semaphore(min(5, len(pending)))
            results = await asyncio.gather(
                *(upload_limited(index, photo, semaphore) for index, photo in pending),
                return_exceptions=True,
            )
            errors = [result for result in results if isinstance(result, Exception)]
            if errors:
                if not all(isinstance(error, QzoneRequestError) for error in errors):
                    raise errors[0]
                log.debug("parallel qzone image upload failed; retrying failed items sequentially", exc_info=errors[0])
                for (index, photo), result in zip(pending, results):
                    if isinstance(result, Exception):
                        prepared[index] = await self.upload_photo(photo)
                    else:
                        _, payload = result
                        prepared[index] = payload
            else:
                for index, payload in results:
                    prepared[index] = payload

        final = [photo for photo in prepared if photo is not None]
        if len(final) > QZONE_MAX_IMAGES:
            raise QzoneParseError(f"QQ 空间一次最多只能上传 {QZONE_MAX_IMAGES} 张图片")
        return final

    async def publish_mood(self, content: str, *, sync_weibo: bool = False, photos: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        photos = await self._prepare_publish_photos(photos)
        richval = "\t".join(photo.get("richval", "") for photo in photos if isinstance(photo, dict))
        pic_bo = ",".join(photo.get("pic_bo", "") for photo in photos if isinstance(photo, dict) and photo.get("pic_bo"))
        data = {
            "syn_tweet_verson": "1",
            "paramstr": "1",
            "who": "1",
            "con": content,
            "feedversion": "1",
            "ver": "1",
            "ugc_right": 1,
            "to_sign": 0,
            "hostuin": self.login_uin,
            "code_version": "1",
            "richval": richval,
            "issyncweibo": int(bool(sync_weibo)),
            "format": "json",
            "qzreferrer": f"https://user.qzone.qq.com/{self.login_uin}",
        }
        if photos:
            data.update(
                {
                    "richtype": "1",
                    "subrichtype": "1",
                    "pic_bo": pic_bo,
                    "pic_template": "",
                }
            )
        payload = await self._request_json(
            "POST",
            "https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_publish_v6",
            data=data,
            referer=f"https://user.qzone.qq.com/{self.login_uin}",
            origin="https://user.qzone.qq.com",
            hostuin=self.login_uin,
            attach_token=False,
        )
        return payload

    async def add_comment(
        self,
        hostuin: int,
        fid: str,
        content: str,
        *,
        appid: int = 311,
        private: bool = False,
        busi_param: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = await self._request_json(
            "POST",
            "https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_re_feeds",
            json_body=None,
            data={
                "topicId": f"{hostuin}_{fid}__1",
                "uin": self.login_uin,
                "hostUin": hostuin,
                "feedsType": 100,
                "inCharset": "utf-8",
                "outCharset": "utf-8",
                "plat": "qzone",
                "source": "ic",
                "platformid": 52,
                "format": "fs",
                "ref": "feeds",
                "content": content,
                "private": int(bool(private)),
                "busi_param": json.dumps(busi_param or {}, ensure_ascii=False),
            },
            referer=f"https://user.qzone.qq.com/{hostuin}/mood/{fid}",
            origin="https://user.qzone.qq.com",
            hostuin=hostuin,
            attach_token=False,
        )
        return payload

    async def get_visitors(self, *, page: int = 1, count: int = 20) -> dict[str, Any]:
        payload = await self._request_json(
            "GET",
            QZONE_VISITOR_URL,
            params={
                "uin": self.login_uin,
                "mask": 7,
                "mod": 2,
                "fupdate": 1,
                "page": max(1, int(page or 1)),
                "count": max(1, min(int(count or 20), 50)),
                "format": "json",
            },
            referer=f"https://user.qzone.qq.com/{self.login_uin}",
            hostuin=self.login_uin,
            attach_token=False,
        )
        return payload

    async def reply_comment(
        self,
        hostuin: int,
        fid: str,
        commentid: str,
        comment_uin: int,
        content: str,
        *,
        appid: int = 311,
    ) -> dict[str, Any]:
        payload = await self._request_json(
            "POST",
            QZONE_REPLY_URL,
            data={
                "topicId": f"{hostuin}_{fid}__1",
                "uin": self.login_uin,
                "hostUin": hostuin,
                "feedsType": 100,
                "inCharset": "utf-8",
                "outCharset": "utf-8",
                "plat": "qzone",
                "source": "ic",
                "platformid": 52,
                "format": "fs",
                "ref": "feeds",
                "content": content,
                "commentId": commentid,
                "commentUin": comment_uin,
                "private": 0,
                "appid": appid,
                "richval": "",
                "richtype": "",
            },
            referer=f"https://user.qzone.qq.com/{hostuin}/mood/{fid}",
            origin="https://user.qzone.qq.com",
            hostuin=hostuin,
            attach_token=False,
        )
        return payload

    async def delete_post(self, fid: str, *, appid: int = 311) -> dict[str, Any]:
        payload = await self._request_json(
            "POST",
            QZONE_DELETE_URL,
            data={
                "uin": self.login_uin,
                "topicId": f"{self.login_uin}_{fid}__1",
                "feedsType": 0,
                "feedsFlag": 0,
                "feedsKey": fid,
                "feedsAppid": appid,
                "feedsTime": int(time.time()),
                "fupdate": 1,
                "ref": "feeds",
                "format": "json",
                "qzreferrer": f"https://user.qzone.qq.com/{self.login_uin}",
            },
            referer=f"https://user.qzone.qq.com/{self.login_uin}/mood/{fid}",
            origin="https://user.qzone.qq.com",
            hostuin=self.login_uin,
            attach_token=False,
        )
        return payload

    @staticmethod
    def _like_endpoint_candidates(like: bool) -> tuple[str, ...]:
        if like:
            return (QZONE_LIKE_PROXY_URL, QZONE_LIKE_DIRECT_URL)
        return (QZONE_UNLIKE_PROXY_URL, QZONE_UNLIKE_DIRECT_URL)

    @staticmethod
    def _like_attempt_detail(endpoint: str, exc: Exception) -> dict[str, Any]:
        detail: dict[str, Any] = {
            "endpoint": endpoint,
            "type": type(exc).__name__,
            "message": getattr(exc, "message", str(exc)),
        }
        status_code = getattr(exc, "status_code", None)
        if status_code is not None:
            detail["status_code"] = status_code
        exc_detail = getattr(exc, "detail", None)
        if exc_detail is not None:
            detail["detail"] = exc_detail
        return detail

    async def like_post(
        self,
        hostuin: int,
        fid: str,
        *,
        appid: int = 311,
        curkey: str = "",
        unikey: str = "",
        like: bool = True,
    ) -> dict[str, Any]:
        cached = self.feed_cache.get((hostuin, fid))
        if not curkey:
            if cached and cached.curkey:
                curkey = cached.curkey
        if not curkey:
            curkey = compute_unikey(appid, hostuin, fid)

        unikey = unikey or (
            cached.unikey
            if cached and cached.unikey
            else compute_unikey(appid, hostuin, fid)
        )
        created_at = cached.created_at if cached else 0
        data = {
            "unikey": unikey,
            "curkey": curkey,
            "appid": appid,
            "opuin": self.login_uin,
            "uin": self.login_uin,
            "hostuin": hostuin,
            "fid": fid,
            "from": 1,
            "typeid": 0,
            "abstime": created_at,
            "active": 0,
            "fupdate": 1,
            "opr_type": "like" if like else "unlike",
            "format": "purejson",
            "qzreferrer": f"https://user.qzone.qq.com/{hostuin}/mood/{fid}",
        }
        attempts: list[dict[str, Any]] = []
        last_exc: Exception | None = None
        for path in self._like_endpoint_candidates(like):
            try:
                return await self._request_json(
                    "POST",
                    path,
                    data=data,
                    referer=f"https://user.qzone.qq.com/{hostuin}/mood/{fid}",
                    origin="https://user.qzone.qq.com",
                    hostuin=hostuin,
                    attach_token=False,
                    follow_qzone_redirects=True,
                    accept_qzone_redirects=True,
                    max_attempts=1,
                )
            except (QzoneRequestError, QzoneParseError) as exc:
                last_exc = exc
                attempts.append(self._like_attempt_detail(path, exc))
                log.warning(
                    "qzone like endpoint failed endpoint=%s status=%s message=%s",
                    path,
                    getattr(exc, "status_code", None),
                    getattr(exc, "message", str(exc)),
                )
                continue

        if isinstance(last_exc, QzoneRequestError):
            status_code = last_exc.status_code
            message = f"{last_exc.message}；所有点赞入口都尝试失败"
        elif last_exc is not None:
            status_code = None
            message = f"{getattr(last_exc, 'message', str(last_exc))}；所有点赞入口都尝试失败"
        else:
            status_code = None
            message = "QQ 空间点赞入口不可用，请稍后再试"
        raise QzoneRequestError(message, status_code=status_code, detail={"attempts": attempts}) from last_exc

    async def detail(self, hostuin: int, fid: str, *, appid: int = 311, busi_param: str = "") -> dict[str, Any]:
        payload = await self.shuoshuo(fid=fid, uin=hostuin, appid=appid, busi_param=busi_param)
        return payload

    def feed_entry_from_payload(self, payload: dict[str, Any], *, default_hostuin: int = 0) -> FeedEntry:
        entry = extract_feed_entry(payload, default_hostuin=default_hostuin)
        self.feed_cache[(entry.hostuin, entry.fid)] = entry
        return entry

    def cache_feed_page(self, hostuin: int, items: list[FeedEntry]) -> None:
        for entry in items:
            self.feed_cache[(hostuin or entry.hostuin, entry.fid)] = entry

    def status_snapshot(self) -> dict[str, Any]:
        return {
            "login_uin": self.login_uin,
            "session_source": self.session.source,
            "cookie_summary": self.cookie_summary(),
            "cookie_count": self.cookie_count,
            "needs_rebind": self.session.needs_rebind or not bool(self.session.cookies),
            "last_ok_at": self.session.last_ok_at or "",
            "last_error": self.session.last_error or "",
            "qzonetoken_hosts": sorted(int(k) for k in self.session.qzonetokens.keys() if str(k).isdigit()),
        }

    def mark_success(self) -> None:
        self.session.last_ok_at = now_iso()
        self.session.needs_rebind = False

    def mark_error(self, error: Exception) -> None:
        self.session.last_error = {"type": type(error).__name__, "message": str(error)}
        if isinstance(error, QzoneNeedsRebind):
            self.session.needs_rebind = True
