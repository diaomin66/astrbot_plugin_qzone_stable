"""Low-level QQ空间 HTTP client."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from .errors import QzoneAuthError, QzoneNeedsRebind, QzoneParseError, QzoneRequestError
from .models import FeedEntry, SessionState
from .parser import (
    cookie_header,
    cookie_gtk,
    compute_unikey,
    extract_feed_entry,
    extract_feed_page,
    normalize_cookie_fields,
    normalize_uin,
    parse_index_html,
    parse_profile_html,
    unwrap_payload,
)
from .render import cookie_summary
from .utils import extract_callback_json, now_iso

log = logging.getLogger(__name__)


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
        self.session.cookies = normalize_cookie_fields(self.session.cookies)
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

    def _persist_cookie_response(self, response: httpx.Response) -> None:
        for key, value in response.cookies.items():
            if value is not None:
                self.session.cookies[key] = value
        self.session.cookies = normalize_cookie_fields(self.session.cookies)
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
    ) -> httpx.Response:
        if login_required and not self.session.cookies:
            raise QzoneNeedsRebind()
        if login_required and self.gtk == 0:
            raise QzoneNeedsRebind("Cookie 中缺少 p_skey/skey，且没有可用的 g_tk/bkn，无法访问 QQ 空间")

        params = self._merge_params(params, hostuin=hostuin, attach_token=attach_token)
        last_exc: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                response = await self._client.request(
                    method,
                    url,
                    params=params,
                    data=data,
                    json=json_body,
                    headers=self._headers(referer=referer, origin=origin),
                )
                self._persist_cookie_response(response)
                if response.status_code in (302, 401, 403):
                    raise QzoneAuthError(
                        f"QQ空间返回登录失效状态码 {response.status_code}",
                        detail={"status_code": response.status_code, "url": url},
                    )
                if response.status_code == 429:
                    raise QzoneRequestError(
                        "QQ空间请求过于频繁，已被限流",
                        status_code=response.status_code,
                        detail={"url": url, "text": response.text[:500]},
                    )
                if response.status_code >= 500:
                    raise QzoneRequestError(
                        f"QQ空间服务器暂时不可用 ({response.status_code})",
                        status_code=response.status_code,
                        detail={"url": url, "text": response.text[:500]},
                    )
                if response.status_code >= 400:
                    raise QzoneRequestError(
                        f"QQ空间返回 HTTP {response.status_code}",
                        status_code=response.status_code,
                        detail={"url": url, "text": response.text[:500]},
                    )
                return response
            except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPError, QzoneRequestError) as exc:
                last_exc = exc
                if isinstance(exc, QzoneRequestError) and exc.status_code is not None and exc.status_code < 500:
                    raise
                if attempt >= self.max_retries:
                    raise
                await asyncio.sleep(min(2.0 * attempt, 6.0))
        assert last_exc is not None
        raise last_exc

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
        )
        payload: Any
        text = response.text.strip()
        if not text:
            payload = {}
        elif text.startswith("{") or text.startswith("["):
            try:
                payload = response.json()
            except Exception as exc:
                raise QzoneParseError("无法解析 QQ 空间 JSON 响应", detail={"text": text[:500]}) from exc
        else:
            callback_payload = extract_callback_json(text)
            if callback_payload is not None:
                payload = callback_payload
            else:
                try:
                    payload = json.loads(text)
                except Exception:
                    raise QzoneParseError(
                        "QQ 空间接口返回了非 JSON 响应",
                        detail={"text": text[:500], "url": str(response.request.url)},
                    )
        payload = unwrap_payload(payload)
        if isinstance(payload, dict):
            for key in ("ret", "code", "err", "error"):
                if key in payload and payload.get(key) not in (0, "0", None):
                    code = int(payload.get(key) or 0)
                    message = str(payload.get("msg") or payload.get("message") or payload.get("text") or "")
                    if code in (-3000, -10000):
                        raise QzoneNeedsRebind(message or "QQ空间登录态已失效", detail=payload)
                    raise QzoneRequestError(message or f"QQ空间返回错误码 {code}", status_code=response.status_code, detail=payload)
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

    async def publish_mood(self, content: str, *, sync_weibo: bool = False, photos: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        photos = photos or []
        richval = " ".join(photo.get("richval", "") for photo in photos if isinstance(photo, dict))
        payload = await self._request_json(
            "POST",
            "https://mobile.qzone.qq.com/mood/publish_mood",
            data={
                "content": content,
                "richval": richval,
                "issyncweibo": int(bool(sync_weibo)),
                "ugc_right": 1,
                "opr_type": "publish_shuoshuo",
                "format": "json",
                "res_uin": self.login_uin,
            },
            referer=f"https://user.qzone.qq.com/{self.login_uin}",
            origin="https://user.qzone.qq.com",
            hostuin=self.login_uin,
            attach_token=True,
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
            "https://h5.qzone.qq.com/webapp/json/qzoneOperation/addComment",
            json_body=None,
            data={
                "ownuin": hostuin,
                "srcId": fid,
                "isPrivateComment": int(bool(private)),
                "content": content,
                "appid": appid,
                "bypass_param": json.dumps({}),
                "busi_param": json.dumps(busi_param or {}, ensure_ascii=False),
            },
            referer=f"https://user.qzone.qq.com/{hostuin}/mood/{fid}",
            origin="https://user.qzone.qq.com",
            hostuin=hostuin,
            attach_token=True,
        )
        return payload

    async def like_post(
        self,
        hostuin: int,
        fid: str,
        *,
        appid: int = 311,
        curkey: str = "",
        like: bool = True,
    ) -> dict[str, Any]:
        if not curkey:
            cached = self.feed_cache.get((hostuin, fid))
            if cached and cached.curkey:
                curkey = cached.curkey
        if not curkey:
            detail = await self.shuoshuo(fid=fid, uin=hostuin, appid=appid)
            detail_payload = unwrap_payload(detail)
            _, items = extract_feed_page(detail_payload if isinstance(detail_payload, dict) else {}, default_hostuin=hostuin)
            if items:
                curkey = items[0].curkey
        if not curkey:
            raise QzoneParseError("无法解析 curkey，无法点赞", detail={"hostuin": hostuin, "fid": fid})

        unikey = compute_unikey(appid, hostuin, fid)
        path = (
            "https://user.qzone.qq.com/proxy/domain/w.qzone.qq.com/cgi-bin/likes/internal_dolike_app"
            if like
            else "https://user.qzone.qq.com/proxy/domain/w.qzone.qq.com/cgi-bin/likes/internal_unlike_app"
        )
        payload = await self._request_json(
            "POST",
            path,
            data={
                "unikey": unikey,
                "curkey": curkey,
                "appid": appid,
                "opuin": self.login_uin,
                "opr_type": "like" if like else "unlike",
                "format": "purejson",
            },
            referer=f"https://user.qzone.qq.com/{hostuin}/mood/{fid}",
            origin="https://user.qzone.qq.com",
            hostuin=hostuin,
            attach_token=True,
        )
        return payload

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
