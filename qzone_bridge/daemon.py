"""Standalone Qzone daemon."""

from __future__ import annotations

import asyncio
import contextlib
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any
from datetime import datetime, timezone

from aiohttp import web

from .astrbot_logging import configure_standalone_logging, get_logger
from .client import QzoneClient
from .errors import QzoneAuthError, QzoneBridgeError, QzoneNeedsRebind, QzoneParseError, QzoneRequestError
from .media import (
    QZONE_MAX_IMAGES,
    media_reference_text,
    normalize_media_list,
    sanitize_publish_content,
    split_publishable_images,
)
from .models import FeedEntry, SessionState
from .parser import (
    compute_unikey,
    extract_feed_page,
    feed_page_cursor,
    feed_page_has_more,
    normalize_uin,
    parse_cookie_text,
    unwrap_payload,
)
from .protocol import SECRET_HEADER, fail, ok
from .selection import NUMERIC_FID_MIN_LENGTH
from .social import extract_comments
from .storage import StateStore, ensure_state_secret
from .utils import now_iso, from_iso

log = get_logger(__name__)
LIKE_VERIFY_RETRY_DELAYS_SECONDS = (0.35, 0.85, 1.6)
TRUE_TEXT_VALUES = {"1", "true", "yes", "y", "on"}
FALSE_TEXT_VALUES = {"0", "false", "no", "n", "off", ""}
LATEST_FEED_REFERENCES = {
    "latest",
    "newest",
    "recent",
    "last",
    "\u6700\u65b0",
    "\u6700\u65b0\u4e00\u6761",
    "\u6700\u8fd1\u4e00\u6761",
    "\u6700\u540e\u4e00\u6761",
}
FEED_REFERENCE_PREFIXES = ("\u7b2c",)
FEED_REFERENCE_SUFFIXES = ("\u6761",)
LOSSY_LATEST_FEED_REFERENCES = {"最新", "最近"}


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in TRUE_TEXT_VALUES:
            return True
        if normalized in FALSE_TEXT_VALUES:
            return False
    return bool(value)


def _coerce_int(value: Any, default: int = 0, *, field: str = "value") -> int:
    if value in (None, ""):
        return default
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise QzoneParseError(f"{field} 必须是整数") from exc


def _query_int(request: web.Request, key: str, default: int = 0) -> int:
    return _coerce_int(request.query.get(key), default, field=key)


def _body_int(body: dict[str, Any], key: str, default: int = 0) -> int:
    return _coerce_int(body.get(key), default, field=key)


def _body_bool(body: dict[str, Any], key: str, default: bool = False) -> bool:
    return _coerce_bool(body.get(key), default)


async def _bridge_response(service: "QzoneDaemonService", action) -> web.Response:
    try:
        payload = await action()
    except QzoneBridgeError as exc:
        service._set_error(exc)
        return fail(exc.code, exc.message, detail=_error_detail(exc))
    return ok(payload)


class QzoneDaemonService:
    def __init__(
        self,
        store: StateStore,
        *,
        secret: str,
        port: int,
        keepalive_interval: int = 120,
        request_timeout: float = 15.0,
        user_agent: str = "",
        version: str = "0.1.0",
    ) -> None:
        self.store = store
        self.state = ensure_state_secret(store.read())
        self.state.runtime.secret = secret
        self.state.runtime.daemon_port = int(port)
        self.state.runtime.daemon_pid = os.getpid()
        self.state.runtime.version = version
        self.state.runtime.started_at = now_iso()
        self.state.runtime.last_seen_at = now_iso()
        self.client = QzoneClient(self.state.session, timeout=request_timeout, user_agent=user_agent)
        self.keepalive_interval = max(30, int(keepalive_interval))
        self.health_state = "idle"
        self._keepalive_task: asyncio.Task | None = None
        self._warmup_task: asyncio.Task | None = None
        self._save_task: asyncio.Task | None = None
        self.recent_feed_entries: list[FeedEntry] = []
        self._closing = False

    def save(self) -> None:
        self.store.write(self.state)
        self.state = ensure_state_secret(self.store.read())
        self.client.update_session(self.state.session)

    def touch(self) -> None:
        self.state.runtime.last_seen_at = now_iso()

    def _ensure_session_ready(self) -> None:
        if self.state.session.needs_rebind or not self.state.session.cookies or not self.state.session.uin:
            raise QzoneNeedsRebind()

    def _schedule_save(self) -> None:
        if self._closing:
            return
        task = self._save_task
        if task is not None and not task.done():
            return

        async def runner() -> None:
            await asyncio.sleep(0.05)
            try:
                self.save()
            except Exception:
                log.warning("qzone daemon deferred state save failed", exc_info=True)

        self._save_task = asyncio.create_task(runner())

    def _set_success(self, *, defer_save: bool = False) -> None:
        self.health_state = "ready" if self.state.session.cookies else "needs_rebind"
        self.state.session.last_ok_at = now_iso()
        self.state.session.last_error = None
        self.state.session.needs_rebind = not bool(self.state.session.cookies)
        self.touch()
        if defer_save:
            self._schedule_save()
        else:
            self.save()

    def _set_error(self, exc: Exception) -> None:
        if isinstance(exc, (QzoneNeedsRebind, QzoneAuthError)):
            self.health_state = "needs_rebind"
            self.state.session.needs_rebind = True
            self.state.session.qzonetokens.clear()
            self.client.feed_cache.clear()
            self.recent_feed_entries.clear()
        elif isinstance(exc, QzoneRequestError) and exc.status_code is not None and 400 <= exc.status_code < 500:
            if self.state.session.cookies and not self.state.session.needs_rebind:
                self.health_state = "ready"
            else:
                self.health_state = "needs_rebind"
        else:
            self.health_state = "degraded"
        self.state.session.last_error = {
            "type": type(exc).__name__,
            "message": str(exc),
        }
        self.touch()
        self.save()

    def snapshot(self) -> dict[str, Any]:
        runtime = self.state.runtime
        session = self.state.session
        started_at = from_iso(runtime.started_at)
        uptime = 0
        if started_at:
            uptime = int((datetime.now(timezone.utc) - started_at).total_seconds())
        return {
            "daemon_state": self.health_state,
            "daemon_pid": runtime.daemon_pid,
            "daemon_port": runtime.daemon_port,
            "daemon_version": runtime.version,
            "started_at": runtime.started_at,
            "last_seen_at": runtime.last_seen_at,
            "uptime_seconds": uptime,
            "login_uin": session.uin,
            "session_source": session.source,
            "cookie_summary": self.client.cookie_summary(),
            "cookie_count": self.client.cookie_count,
            "needs_rebind": session.needs_rebind or not bool(session.cookies),
            "last_ok_at": session.last_ok_at,
            "last_error": session.last_error,
            "qzonetoken_hosts": sorted(int(k) for k in session.qzonetokens.keys() if str(k).isdigit()),
            "feed_cache_size": len(self.client.feed_cache),
            "session_revision": session.revision,
        }

    async def bootstrap(self) -> None:
        self.save()
        if self.state.session.cookies and self.state.session.uin and not self.state.session.needs_rebind:
            self.health_state = "ready"
            self._warmup_task = asyncio.create_task(self._background_warmup())
        else:
            self.health_state = "needs_rebind"
            self.save()
        self._keepalive_task = asyncio.create_task(self._keepalive_loop())

    async def close(self) -> None:
        self._closing = True
        if self._warmup_task:
            self._warmup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._warmup_task
        if self._keepalive_task:
            self._keepalive_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._keepalive_task
        if self._save_task:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._save_task
        self.health_state = "offline"
        self.state.runtime.daemon_pid = 0
        self.state.runtime.started_at = ""
        self.touch()
        self.save()
        self.client.feed_cache.clear()
        await self.client.close()

    async def warmup(self) -> None:
        self._ensure_session_ready()
        await self.client.mfeeds_get_count()
        self._set_success(defer_save=True)

    async def _background_warmup(self) -> None:
        try:
            await self.warmup()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._set_error(exc)

    async def ensure_token(self, hostuin: int | None = None) -> None:
        self._ensure_session_ready()
        hostuin = int(hostuin or self.state.session.uin or 0)
        if not hostuin:
            raise QzoneNeedsRebind()
        if hostuin == self.state.session.uin:
            if not self.state.session.qzonetokens.get(str(hostuin)):
                await self.client.index()
        else:
            if not self.state.session.qzonetokens.get(str(hostuin)):
                await self.client.profile(hostuin)
        self.save()

    async def bind_cookie(self, cookie_text: str, *, uin: int = 0, source: str = "manual") -> dict[str, Any]:
        cookies = parse_cookie_text(cookie_text)
        if not cookies:
            raise QzoneParseError("Cookie 内容为空或无法解析")
        resolved_uin = normalize_uin(cookies, override=uin)
        if not resolved_uin:
            raise QzoneParseError("Cookie 缺少 uin / p_uin，无法识别登录 QQ")
        self.state.session = SessionState(
            uin=resolved_uin,
            cookies=cookies,
            qzonetokens={},
            source=source,
            updated_at=now_iso(),
            last_ok_at="",
            last_error=None,
            revision=self.state.session.revision + 1,
            needs_rebind=False,
        )
        self.client.update_session(self.state.session)
        self.client.feed_cache.clear()
        self.recent_feed_entries.clear()
        self.save()
        try:
            await self.warmup()
        except Exception as exc:
            self._set_error(exc)
            raise
        return self.snapshot()

    async def unbind(self) -> dict[str, Any]:
        self.state.session = SessionState(
            uin=0,
            cookies={},
            qzonetokens={},
            source="manual",
            updated_at=now_iso(),
            last_ok_at="",
            last_error=None,
            revision=self.state.session.revision + 1,
            needs_rebind=True,
        )
        self.client.update_session(self.state.session)
        self.client.feed_cache.clear()
        self.recent_feed_entries.clear()
        self.save()
        self.health_state = "needs_rebind"
        return self.snapshot()

    @staticmethod
    def _should_fallback_feed_fetch(exc: Exception) -> bool:
        if isinstance(exc, QzoneParseError):
            return True
        if not isinstance(exc, QzoneRequestError):
            return False
        if exc.status_code in {301, 302, 303, 307, 308, 403, 429}:
            return True
        return exc.status_code is not None and exc.status_code >= 500

    async def list_feeds(self, *, hostuin: int = 0, limit: int = 5, cursor: str = "", scope: str = "") -> dict[str, Any]:
        self._ensure_session_ready()
        if limit <= 0:
            limit = 5
        hostuin = int(hostuin or self.state.session.uin or 0)
        if not hostuin:
            raise QzoneNeedsRebind()
        scope = scope or ("self" if hostuin == self.state.session.uin else "profile")
        items: list[FeedEntry] = []
        next_cursor = cursor or ""
        has_more = False
        page_round = 0
        while len(items) < limit and page_round < 6:
            if scope == "self":
                if page_round == 0 and not next_cursor:
                    try:
                        payload = unwrap_payload(await self.client.index())
                    except (QzoneRequestError, QzoneParseError) as exc:
                        if not self._should_fallback_feed_fetch(exc):
                            raise
                        log.warning("qzone self feed primary fetch failed, using legacy fallback: %s", exc)
                        payload = await self.client.legacy_recent_feeds()
                else:
                    payload = unwrap_payload(await self.client.get_active_feeds(next_cursor))
                feedpage = payload
            else:
                if page_round == 0 and not next_cursor:
                    try:
                        payload = await self.client.profile(hostuin)
                    except (QzoneRequestError, QzoneParseError) as exc:
                        if not self._should_fallback_feed_fetch(exc):
                            raise
                        log.warning("qzone profile feed primary fetch failed, using legacy fallback: %s", exc)
                        payload = await self.client.legacy_feeds(hostuin, page=1, num=max(limit, 20))
                else:
                    payload = unwrap_payload(await self.client.get_feeds(hostuin, next_cursor))
                feedpage = payload

            feedpage, page_items = extract_feed_page(feedpage, default_hostuin=hostuin)
            if not isinstance(feedpage, dict):
                break
            self.client.cache_feed_page(hostuin, page_items)
            items.extend(page_items)
            has_more = feed_page_has_more(feedpage)
            next_cursor = feed_page_cursor(feedpage)
            if not has_more or not next_cursor:
                break
            page_round += 1

        visible_items = items[:limit]
        self.recent_feed_entries = visible_items
        return {
            "scope": scope,
            "hostuin": hostuin,
            "items": [asdict(item) for item in visible_items],
            "has_more": has_more,
            "cursor": next_cursor,
            "count": min(len(items), limit),
        }

    def _detail_payload_from_entry(self, entry: FeedEntry) -> dict[str, Any]:
        self.client.cache_feed_page(entry.hostuin, [entry])
        raw = entry.raw if isinstance(entry.raw, dict) else {}
        comments = [item.to_dict() for item in extract_comments(raw)]
        return {"entry": asdict(entry), "comments": comments, "raw": raw}

    async def _detail_from_cached_or_legacy_feed(self, *, hostuin: int, fid: str) -> dict[str, Any] | None:
        cached = self.client.feed_cache.get((hostuin, fid))
        if cached is not None:
            return self._detail_payload_from_entry(cached)

        fetchers: list[Any] = []
        if hostuin == self.state.session.uin:
            fetchers.append(self.client.legacy_recent_feeds)
        fetchers.append(lambda: self.client.legacy_feeds(hostuin, page=1, num=20))

        for fetch in fetchers:
            try:
                payload = unwrap_payload(await fetch())
            except Exception as exc:
                log.debug("qzone detail feed fallback failed: %s", exc)
                continue
            feedpage, entries = extract_feed_page(payload, default_hostuin=hostuin)
            if not feedpage:
                continue
            self.client.cache_feed_page(hostuin, entries)
            for entry in entries:
                if entry.fid == fid:
                    return self._detail_payload_from_entry(entry)
        return None

    async def detail_feed(self, *, hostuin: int, fid: str, appid: int = 311, busi_param: str = "") -> dict[str, Any]:
        hostuin = int(hostuin or self.state.session.uin or 0)
        if not hostuin:
            raise QzoneNeedsRebind()
        token_error: Exception | None = None
        try:
            await self.ensure_token(hostuin)
        except (QzoneRequestError, QzoneParseError) as exc:
            if not self._should_fallback_feed_fetch(exc):
                raise
            token_error = exc
            log.warning("qzone detail token probe failed, trying detail without fresh token: %s", exc)

        try:
            payload = unwrap_payload(await self.client.detail(hostuin, fid, appid=appid, busi_param=busi_param))
            if not isinstance(payload, dict):
                raise QzoneParseError("说说详情返回格式异常")
            entry = self.client.feed_entry_from_payload(payload, default_hostuin=hostuin)
            return self._detail_payload_from_entry(entry)
        except (QzoneRequestError, QzoneParseError) as exc:
            if token_error is None and not self._should_fallback_feed_fetch(exc):
                raise
            log.warning("qzone detail primary fetch failed, using feed fallback: %s", exc)
            fallback = await self._detail_from_cached_or_legacy_feed(hostuin=hostuin, fid=fid)
            if fallback is not None:
                return fallback
            raise

    async def view_visitors(self, *, page: int = 1, count: int = 20) -> dict[str, Any]:
        self._ensure_session_ready()
        payload = unwrap_payload(await self.client.get_visitors(page=page, count=count))
        if not isinstance(payload, dict):
            raise QzoneParseError("访客列表返回格式异常")
        raw_items = payload.get("items") or payload.get("visitors") or payload.get("data") or payload.get("list") or []
        if isinstance(raw_items, dict):
            raw_items = raw_items.get("items") or raw_items.get("list") or raw_items.get("visitors") or []
        visitors: list[dict[str, Any]] = []
        if isinstance(raw_items, list):
            for item in raw_items:
                if not isinstance(item, dict):
                    continue
                user = item.get("user") if isinstance(item.get("user"), dict) else {}
                visitors.append(
                    {
                        "uin": int(item.get("uin") or user.get("uin") or item.get("user_id") or 0),
                        "nickname": item.get("nickname") or user.get("nickname") or item.get("name") or "",
                        "time": item.get("time") or item.get("visitTime") or item.get("timestamp") or 0,
                        "raw": item,
                    }
                )
        self._set_success(defer_save=True)
        return {"items": visitors, "count": len(visitors), "raw": payload}

    async def publish_post(
        self,
        *,
        content: str,
        sync_weibo: bool = False,
        media: list[dict[str, Any]] | None = None,
        content_sanitized: bool = False,
    ) -> dict[str, Any]:
        content = sanitize_publish_content(content, content_sanitized=content_sanitized)
        normalized_media = normalize_media_list(media)
        photos, fallback_media = split_publishable_images(normalized_media)
        if len(photos) > QZONE_MAX_IMAGES:
            raise QzoneParseError(f"QQ 空间一次最多只能上传 {QZONE_MAX_IMAGES} 张图片")
        if fallback_media:
            refs = "\n".join(media_reference_text(item) for item in fallback_media)
            content = "\n".join(part for part in (content.strip(), refs) if part)
        if not content.strip() and not photos:
            raise QzoneParseError("说说内容或图片不能为空")
        self._ensure_session_ready()
        payload = unwrap_payload(
            await self.client.publish_mood(
                content,
                sync_weibo=sync_weibo,
                photos=[item.to_dict() for item in photos],
            )
        )
        if not isinstance(payload, dict):
            raise QzoneParseError("说说发布返回格式异常")
        self._set_success(defer_save=True)
        return {
            "fid": payload.get("fid") or payload.get("tid") or "",
            "message": payload.get("msg") or payload.get("message") or "",
            "media_count": len(normalized_media),
            "photo_count": len(photos),
            "raw": payload,
        }

    async def comment_post(
        self,
        *,
        hostuin: int,
        fid: str,
        content: str,
        appid: int = 311,
        private: bool = False,
        busi_param: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not content.strip():
            raise QzoneParseError("评论内容不能为空")
        self._ensure_session_ready()
        payload = unwrap_payload(
            await self.client.add_comment(
                hostuin,
                fid,
                content,
                appid=appid,
                private=private,
                busi_param=busi_param or {},
            )
        )
        if not isinstance(payload, dict):
            raise QzoneParseError("评论发布返回格式异常")
        self._set_success(defer_save=True)
        return {
            "commentid": payload.get("commentid") or payload.get("commentId") or 0,
            "commentLikekey": payload.get("commentLikekey") or "",
            "message": payload.get("msg") or payload.get("message") or "",
            "raw": payload,
        }

    async def reply_comment(
        self,
        *,
        hostuin: int,
        fid: str,
        commentid: str,
        comment_uin: int,
        content: str,
        appid: int = 311,
    ) -> dict[str, Any]:
        if not content.strip():
            raise QzoneParseError("回复内容不能为空")
        self._ensure_session_ready()
        payload = unwrap_payload(
            await self.client.reply_comment(
                hostuin,
                fid,
                commentid,
                comment_uin,
                content,
                appid=appid,
            )
        )
        if not isinstance(payload, dict):
            raise QzoneParseError("回复评论返回格式异常")
        self._set_success(defer_save=True)
        return {
            "commentid": payload.get("commentid") or payload.get("commentId") or 0,
            "message": payload.get("msg") or payload.get("message") or "",
            "raw": payload,
        }

    async def delete_post(self, *, fid: str, appid: int = 311) -> dict[str, Any]:
        if not str(fid or "").strip():
            raise QzoneParseError("说说 fid 不能为空")
        self._ensure_session_ready()
        payload = unwrap_payload(await self.client.delete_post(str(fid), appid=appid))
        if not isinstance(payload, dict):
            raise QzoneParseError("删除说说返回格式异常")
        self._set_success(defer_save=True)
        return {
            "fid": fid,
            "message": payload.get("msg") or payload.get("message") or "",
            "raw": payload,
        }

    @staticmethod
    def _feed_reference_index(fid: str, *, hostuin: int, latest: bool = False, index: int = 0) -> int:
        if latest:
            return 1
        if index > 0:
            return int(index)
        fid_text = str(fid or "").strip()
        if not fid_text:
            return 0
        if not hostuin and fid_text.isdigit() and len(fid_text) < NUMERIC_FID_MIN_LENGTH:
            return int(fid_text)
        if fid_text.lower() in LATEST_FEED_REFERENCES or fid_text in LATEST_FEED_REFERENCES:
            return 1
        if fid_text in LOSSY_LATEST_FEED_REFERENCES:
            return 1
        return QzoneDaemonService._localized_feed_reference_index(fid_text)

    @staticmethod
    def _localized_feed_reference_index(fid_text: str) -> int:
        text = str(fid_text or "").strip()
        if not text:
            return 0

        matched_marker = False
        for prefix in FEED_REFERENCE_PREFIXES:
            if text.startswith(prefix):
                text = text[len(prefix) :].strip()
                matched_marker = True
                break
        for suffix in FEED_REFERENCE_SUFFIXES:
            if text.endswith(suffix):
                text = text[: -len(suffix)].strip()
                matched_marker = True
                break

        if matched_marker and text.isdigit():
            return int(text)
        lossy_text = str(fid_text or "").strip()
        if lossy_text.startswith("?") and lossy_text.endswith("?"):
            lossy_inner = lossy_text.strip("?").strip()
            if lossy_inner.isdigit():
                return int(lossy_inner)
        return 0

    def _recent_feed_reference(self, reference_index: int, *, hostuin: int) -> FeedEntry | None:
        if reference_index <= 0 or reference_index > len(self.recent_feed_entries):
            return None
        entry = self.recent_feed_entries[reference_index - 1]
        if hostuin and entry.hostuin != hostuin:
            return None
        return entry

    async def _resolve_recent_feed_reference(
        self,
        hostuin: int,
        fid: str,
        appid: int,
        curkey: str = "",
        *,
        latest: bool = False,
        index: int = 0,
    ) -> tuple[int, str, int, str]:
        fid_text = str(fid or "").strip()
        target_hostuin = int(hostuin or self.state.session.uin or 0)
        reference_index = self._feed_reference_index(
            fid_text,
            hostuin=int(hostuin or 0),
            latest=latest,
            index=index,
        )
        if reference_index > 0:
            cached_entry = self._recent_feed_reference(reference_index, hostuin=target_hostuin if hostuin else 0)
            if cached_entry is not None:
                return (
                    cached_entry.hostuin,
                    cached_entry.fid,
                    cached_entry.appid or appid,
                    curkey or cached_entry.curkey,
                )
            if not target_hostuin:
                raise QzoneNeedsRebind()
            feed_payload = await self.list_feeds(hostuin=target_hostuin, limit=reference_index, scope="profile")
            items = feed_payload.get("items") or []
            if reference_index > len(items):
                raise QzoneParseError(f"第 {reference_index} 条说说不存在")
            entry = FeedEntry(**items[reference_index - 1])
            return entry.hostuin, entry.fid, entry.appid or appid, curkey or entry.curkey
        return target_hostuin, fid_text, int(appid or 311), curkey

    @staticmethod
    def _http_like_key(appid: int, hostuin: int, fid: str) -> str:
        return compute_unikey(appid, hostuin, fid).replace("https://", "http://", 1)

    async def _refresh_like_entry(self, hostuin: int, fid: str, appid: int) -> FeedEntry | None:
        try:
            payload = unwrap_payload(await self.client.detail(hostuin, fid, appid=appid))
        except Exception as exc:
            log.debug("qzone like verification refresh failed: %s", exc)
        else:
            if isinstance(payload, dict):
                entry = self.client.feed_entry_from_payload(payload, default_hostuin=hostuin)
                self.client.cache_feed_page(hostuin, [entry])
                return entry

        for fetch in (
            self.client.legacy_recent_feeds if hostuin == self.state.session.uin else None,
            lambda: self.client.legacy_feeds(hostuin, page=1, num=20),
        ):
            if fetch is None:
                continue
            try:
                payload = unwrap_payload(await fetch())
            except Exception as exc:
                log.debug("qzone like verification feed fallback failed: %s", exc)
                continue
            feedpage, entries = extract_feed_page(payload, default_hostuin=hostuin)
            if not feedpage:
                continue
            self.client.cache_feed_page(hostuin, entries)
            for entry in entries:
                if entry.fid == fid:
                    return entry
        return None

    @staticmethod
    def _normalize_action_payload(payload: Any) -> dict[str, Any]:
        payload = unwrap_payload(payload)
        if isinstance(payload, dict):
            return payload
        return {"value": payload}

    async def _retry_like_entry_until_fresh(
        self,
        hostuin: int,
        fid: str,
        appid: int,
        target_liked: bool,
        current_entry: FeedEntry | None,
    ) -> FeedEntry | None:
        entry = current_entry
        for delay in LIKE_VERIFY_RETRY_DELAYS_SECONDS:
            await asyncio.sleep(delay)
            refreshed = await self._refresh_like_entry(hostuin, fid, appid)
            if refreshed is not None:
                entry = refreshed
            if entry is None or entry.liked == target_liked:
                return entry
        return entry

    async def like_post(
        self,
        *,
        hostuin: int,
        fid: str,
        appid: int = 311,
        curkey: str = "",
        unlike: bool = False,
        latest: bool = False,
        index: int = 0,
    ) -> dict[str, Any]:
        self._ensure_session_ready()
        hostuin, fid, appid, curkey = await self._resolve_recent_feed_reference(
            hostuin,
            fid,
            appid,
            curkey,
            latest=latest,
            index=index,
        )
        if not hostuin or not fid:
            raise QzoneParseError("没有指定要点赞的说说")

        target_liked = not unlike
        before_entry = await self._refresh_like_entry(hostuin, fid, appid)
        if before_entry is not None and before_entry.liked == target_liked:
            self._set_success(defer_save=True)
            return {
                "action": "unlike" if unlike else "like",
                "liked": before_entry.liked,
                "verified": True,
                "already": True,
                "summary": before_entry.summary,
                "raw": {},
            }

        payload = self._normalize_action_payload(
            await self.client.like_post(hostuin, fid, appid=appid, curkey=curkey, like=not unlike)
        )
        verified_entry = await self._refresh_like_entry(hostuin, fid, appid)
        if verified_entry is not None and verified_entry.liked != target_liked:
            fallback_key = self._http_like_key(appid, hostuin, fid)
            if fallback_key not in {curkey, compute_unikey(appid, hostuin, fid)}:
                payload = self._normalize_action_payload(
                    await self.client.like_post(
                        hostuin,
                        fid,
                        appid=appid,
                        curkey=fallback_key,
                        unikey=fallback_key,
                        like=not unlike,
                    )
                )
                verified_entry = await self._refresh_like_entry(hostuin, fid, appid)

        if verified_entry is not None and verified_entry.liked != target_liked:
            verified_entry = await self._retry_like_entry_until_fresh(
                hostuin,
                fid,
                appid,
                target_liked,
                verified_entry,
            )

        verification: dict[str, Any] | None = None
        if verified_entry is not None and verified_entry.liked != target_liked:
            verification = {
                "expected_liked": target_liked,
                "actual_liked": verified_entry.liked,
            }
            log.debug(
                "qzone like request accepted but verification stayed stale: hostuin=%s fid=%s expected=%s actual=%s",
                hostuin,
                fid,
                target_liked,
                verified_entry.liked,
            )
        self._set_success(defer_save=True)
        result = {
            "action": "unlike" if unlike else "like",
            "liked": verified_entry.liked
            if verified_entry is not None and verified_entry.liked == target_liked
            else target_liked,
            "verified": verified_entry is not None and verified_entry.liked == target_liked,
            "already": False,
            "summary": verified_entry.summary if verified_entry is not None else "",
            "message": payload.get("msg") or payload.get("message") or "",
            "raw": payload,
        }
        if verification is not None:
            result["verification"] = verification
        return result

    async def health(self) -> dict[str, Any]:
        if self.state.session.needs_rebind or not self.state.session.cookies or not self.state.session.uin:
            if self.health_state != "needs_rebind":
                self.health_state = "needs_rebind"
                self.save()
            return self.snapshot()
        try:
            await self.client.mfeeds_get_count()
        except Exception as exc:
            self._set_error(exc)
            raise
        self._set_success()
        return self.snapshot()

    async def _keepalive_loop(self) -> None:
        while not self._closing:
            await asyncio.sleep(self.keepalive_interval)
            if self._closing:
                break
            if self.state.session.needs_rebind:
                if self.health_state != "needs_rebind":
                    self.health_state = "needs_rebind"
                    self.save()
                continue
            if not self.state.session.cookies or not self.state.session.uin:
                if self.health_state != "needs_rebind":
                    self.health_state = "needs_rebind"
                    self.save()
                continue
            try:
                await self.health()
            except Exception as exc:
                log.debug("qzone keepalive failed: %s", exc)


async def _json_body(request: web.Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        return {}
    return payload


def _error_detail(exc: QzoneBridgeError):
    detail = exc.detail
    status_code = getattr(exc, "status_code", None)
    if status_code is None:
        return detail
    if isinstance(detail, dict):
        merged = dict(detail)
        merged.setdefault("status_code", status_code)
        return merged
    if detail is None:
        return {"status_code": status_code}
    return {"status_code": status_code, "detail": detail}


SERVICE_APP_KEY = web.AppKey("qzone_service", QzoneDaemonService)
SHUTDOWN_EVENT_APP_KEY = web.AppKey("qzone_shutdown_event", asyncio.Event)


def create_app(service: QzoneDaemonService, shutdown_event: asyncio.Event | None = None) -> web.Application:
    app = web.Application(client_max_size=32 * 1024 * 1024)
    app[SERVICE_APP_KEY] = service
    if shutdown_event is not None:
        app[SHUTDOWN_EVENT_APP_KEY] = shutdown_event

    @web.middleware
    async def auth_middleware(request: web.Request, handler):
        secret = request.headers.get(SECRET_HEADER) or ""
        if secret != service.state.runtime.secret:
            return fail("UNAUTHORIZED", "secret 不正确", status=401)
        return await handler(request)

    app.middlewares.append(auth_middleware)

    async def health(request: web.Request) -> web.Response:
        service.touch()
        return ok(service.snapshot())

    async def status(request: web.Request) -> web.Response:
        service.touch()
        service.save()
        return ok(service.snapshot())

    async def bind(request: web.Request) -> web.Response:
        body = await _json_body(request)
        cookie_text = str(body.get("cookie_text") or body.get("cookie") or "")
        source = str(body.get("source") or "manual")
        return await _bridge_response(
            service,
            lambda: service.bind_cookie(cookie_text, uin=_body_int(body, "uin", 0), source=source),
        )

    async def unbind(request: web.Request) -> web.Response:
        return await _bridge_response(service, service.unbind)

    async def feeds(request: web.Request) -> web.Response:
        cursor = request.query.get("cursor") or ""
        scope = request.query.get("scope") or ""
        return await _bridge_response(
            service,
            lambda: service.list_feeds(
                hostuin=_query_int(request, "hostuin", 0),
                limit=_query_int(request, "limit", 5),
                cursor=cursor,
                scope=scope,
            ),
        )

    async def detail(request: web.Request) -> web.Response:
        fid = request.query.get("fid") or ""
        busi_param = request.query.get("busi_param") or ""
        return await _bridge_response(
            service,
            lambda: service.detail_feed(
                hostuin=_query_int(request, "hostuin", 0),
                fid=fid,
                appid=_query_int(request, "appid", 311),
                busi_param=busi_param,
            ),
        )

    async def visitors(request: web.Request) -> web.Response:
        return await _bridge_response(
            service,
            lambda: service.view_visitors(
                page=_query_int(request, "page", 1),
                count=_query_int(request, "count", 20),
            ),
        )

    async def post(request: web.Request) -> web.Response:
        body = await _json_body(request)
        content = str(body.get("content") or "")
        media = body.get("media") or body.get("attachments") or body.get("photos") or []
        return await _bridge_response(
            service,
            lambda: service.publish_post(
                content=content,
                sync_weibo=_body_bool(body, "sync_weibo", False),
                media=media,
                content_sanitized=_body_bool(body, "content_sanitized", False),
            ),
        )

    async def comment(request: web.Request) -> web.Response:
        body = await _json_body(request)
        busi_param = body.get("busi_param")
        if not isinstance(busi_param, dict):
            busi_param = {}
        return await _bridge_response(
            service,
            lambda: service.comment_post(
                hostuin=_body_int(body, "hostuin", 0),
                fid=str(body.get("fid") or ""),
                content=str(body.get("content") or ""),
                appid=_body_int(body, "appid", 311),
                private=_body_bool(body, "private", False),
                busi_param=busi_param,
            ),
        )

    async def reply(request: web.Request) -> web.Response:
        body = await _json_body(request)
        return await _bridge_response(
            service,
            lambda: service.reply_comment(
                hostuin=_body_int(body, "hostuin", 0),
                fid=str(body.get("fid") or ""),
                commentid=str(body.get("commentid") or body.get("commentId") or ""),
                comment_uin=_coerce_int(
                    body.get("comment_uin") or body.get("commentUin"),
                    0,
                    field="comment_uin",
                ),
                content=str(body.get("content") or ""),
                appid=_body_int(body, "appid", 311),
            ),
        )

    async def delete(request: web.Request) -> web.Response:
        body = await _json_body(request)
        return await _bridge_response(
            service,
            lambda: service.delete_post(
                fid=str(body.get("fid") or ""),
                appid=_body_int(body, "appid", 311),
            ),
        )

    async def like(request: web.Request) -> web.Response:
        body = await _json_body(request)
        return await _bridge_response(
            service,
            lambda: service.like_post(
                hostuin=_body_int(body, "hostuin", 0),
                fid=str(body.get("fid") or ""),
                appid=_body_int(body, "appid", 311),
                curkey=str(body.get("curkey") or ""),
                unlike=_body_bool(body, "unlike", False),
                latest=_body_bool(body, "latest", False),
                index=_body_int(body, "index", 0),
            ),
        )

    async def shutdown(request: web.Request) -> web.Response:
        service.touch()
        service.save()
        event = request.app.get(SHUTDOWN_EVENT_APP_KEY)
        if isinstance(event, asyncio.Event):
            asyncio.get_running_loop().call_later(0.1, event.set)
        return ok({"stopping": True})

    app.router.add_get("/health", health)
    app.router.add_get("/status", status)
    app.router.add_post("/bind", bind)
    app.router.add_post("/unbind", unbind)
    app.router.add_get("/feeds", feeds)
    app.router.add_get("/detail", detail)
    app.router.add_get("/visitors", visitors)
    app.router.add_post("/post", post)
    app.router.add_post("/comment", comment)
    app.router.add_post("/reply", reply)
    app.router.add_post("/delete", delete)
    app.router.add_post("/like", like)
    app.router.add_post("/shutdown", shutdown)
    return app


async def run_daemon(
    *,
    data_dir: Path,
    port: int,
    secret: str,
    keepalive_interval: int,
    request_timeout: float,
    user_agent: str,
    version: str,
) -> None:
    store = StateStore(data_dir)
    service = QzoneDaemonService(
        store,
        secret=secret,
        port=port,
        keepalive_interval=keepalive_interval,
        request_timeout=request_timeout,
        user_agent=user_agent,
        version=version,
    )
    await service.bootstrap()

    shutdown_event = asyncio.Event()
    app = create_app(service, shutdown_event=shutdown_event)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host="127.0.0.1", port=port)
    await site.start()
    log.info("Qzone daemon started on 127.0.0.1:%s", port)
    try:
        await shutdown_event.wait()
    except asyncio.CancelledError:
        pass
    finally:
        await service.close()
        await runner.cleanup()


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Qzone daemon")
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--secret", required=True)
    parser.add_argument("--keepalive-interval", type=int, default=120)
    parser.add_argument("--request-timeout", type=float, default=15.0)
    parser.add_argument("--user-agent", default="")
    parser.add_argument("--version", default="0.1.0")
    args = parser.parse_args()

    configure_standalone_logging()
    asyncio.run(
        run_daemon(
            data_dir=Path(args.data_dir),
            port=args.port,
            secret=args.secret,
            keepalive_interval=args.keepalive_interval,
            request_timeout=args.request_timeout,
            user_agent=args.user_agent,
            version=args.version,
        )
    )


if __name__ == "__main__":
    main()
