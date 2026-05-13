"""Standalone QQ空间 daemon."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any
from datetime import datetime, timezone

from aiohttp import web

from .client import FeedPageResult, QzoneClient
from .errors import QzoneAuthError, QzoneBridgeError, QzoneNeedsRebind, QzoneParseError, QzoneRequestError
from .media import QZONE_MAX_IMAGES, media_reference_text, normalize_media_list, split_publishable_images, strip_command_prefix
from .models import BridgeState, FeedEntry, SessionState
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
from .storage import StateStore, ensure_state_secret
from .utils import now_iso, from_iso

log = logging.getLogger(__name__)


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
            raise QzoneParseError("Cookie 为空，无法绑定")
        resolved_uin = normalize_uin(cookies, override=uin)
        if not resolved_uin:
            raise QzoneParseError("Cookie 中未找到 uin / p_uin，请补齐后重试")
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
                    except QzoneRequestError as exc:
                        if exc.status_code not in {301, 302, 303, 307, 308}:
                            raise
                        payload = await self.client.legacy_recent_feeds()
                else:
                    payload = unwrap_payload(await self.client.get_active_feeds(next_cursor))
                feedpage = payload
            else:
                if page_round == 0 and not next_cursor:
                    payload = await self.client.profile(hostuin)
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

    async def detail_feed(self, *, hostuin: int, fid: str, appid: int = 311, busi_param: str = "") -> dict[str, Any]:
        hostuin = int(hostuin or self.state.session.uin or 0)
        if not hostuin:
            raise QzoneNeedsRebind()
        await self.ensure_token(hostuin)
        payload = unwrap_payload(await self.client.detail(hostuin, fid, appid=appid, busi_param=busi_param))
        if not isinstance(payload, dict):
            raise QzoneParseError("说说详情返回结构异常")
        entry = self.client.feed_entry_from_payload(payload, default_hostuin=hostuin)
        self.client.cache_feed_page(hostuin, [entry])
        comments = []
        comment_block = payload.get("comment")
        if isinstance(comment_block, dict):
            raw_comments = comment_block.get("comments") or []
            if isinstance(raw_comments, list):
                for item in raw_comments:
                    if not isinstance(item, dict):
                        continue
                    comments.append(
                        {
                            "commentid": item.get("commentid"),
                            "content": item.get("content") or "",
                            "date": item.get("date") or 0,
                            "nickname": (item.get("user") or {}).get("nickname") if isinstance(item.get("user"), dict) else "",
                            "uin": (item.get("user") or {}).get("uin") if isinstance(item.get("user"), dict) else 0,
                            "is_private": bool(item.get("isPrivate") or False),
                        }
                    )
        return {"entry": asdict(entry), "comments": comments, "raw": payload}

    async def publish_post(
        self,
        *,
        content: str,
        sync_weibo: bool = False,
        media: list[dict[str, Any]] | None = None,
        content_sanitized: bool = False,
    ) -> dict[str, Any]:
        content = str(content or "")
        if not content_sanitized:
            content = strip_command_prefix(content, ("qzone post",)).strip()
        normalized_media = normalize_media_list(media)
        photos, fallback_media = split_publishable_images(normalized_media)
        if len(photos) > QZONE_MAX_IMAGES:
            raise QzoneParseError(f"QQ空间说说最多支持 {QZONE_MAX_IMAGES} 张图片")
        if fallback_media:
            refs = "\n".join(media_reference_text(item) for item in fallback_media)
            content = "\n".join(part for part in (content.strip(), refs) if part)
        if not content.strip() and not photos:
            raise QzoneParseError("发布内容不能为空")
        self._ensure_session_ready()
        payload = unwrap_payload(
            await self.client.publish_mood(
                content,
                sync_weibo=sync_weibo,
                photos=[item.to_dict() for item in photos],
            )
        )
        if not isinstance(payload, dict):
            raise QzoneParseError("发布说说返回结构异常")
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
    ) -> dict[str, Any]:
        if not content.strip():
            raise QzoneParseError("评论内容不能为空")
        self._ensure_session_ready()
        payload = unwrap_payload(await self.client.add_comment(hostuin, fid, content, appid=appid, private=private))
        if not isinstance(payload, dict):
            raise QzoneParseError("评论返回结构异常")
        self._set_success(defer_save=True)
        return {
            "commentid": payload.get("commentid") or payload.get("commentId") or 0,
            "commentLikekey": payload.get("commentLikekey") or "",
            "message": payload.get("msg") or payload.get("message") or "",
            "raw": payload,
        }

    def _resolve_recent_feed_reference(self, hostuin: int, fid: str, appid: int, curkey: str = "") -> tuple[int, str, int, str]:
        fid_text = str(fid or "").strip()
        if not hostuin and fid_text.isdigit():
            index = int(fid_text) - 1
            if 0 <= index < len(self.recent_feed_entries):
                entry = self.recent_feed_entries[index]
                return entry.hostuin, entry.fid, entry.appid or appid, curkey or entry.curkey
        return int(hostuin or self.state.session.uin or 0), fid_text, int(appid or 311), curkey

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

    async def like_post(
        self,
        *,
        hostuin: int,
        fid: str,
        appid: int = 311,
        curkey: str = "",
        unlike: bool = False,
    ) -> dict[str, Any]:
        self._ensure_session_ready()
        hostuin, fid, appid, curkey = self._resolve_recent_feed_reference(hostuin, fid, appid, curkey)
        if not hostuin or not fid:
            raise QzoneParseError("点赞目标不完整，请先选择一条说说")

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
            action_text = "取消点赞" if unlike else "点赞"
            raise QzoneRequestError(
                f"{action_text}请求已发送，但 QQ 空间未确认状态变更",
                detail={
                    "hostuin": hostuin,
                    "fid": fid,
                    "expected_liked": target_liked,
                    "actual_liked": verified_entry.liked,
                    "raw": payload,
                },
            )
        self._set_success(defer_save=True)
        return {
            "action": "unlike" if unlike else "like",
            "liked": verified_entry.liked if verified_entry is not None else target_liked,
            "verified": verified_entry is not None,
            "already": False,
            "summary": verified_entry.summary if verified_entry is not None else "",
            "message": payload.get("msg") or payload.get("message") or "",
            "raw": payload,
        }

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


def create_app(service: QzoneDaemonService, shutdown_event: asyncio.Event | None = None) -> web.Application:
    app = web.Application(client_max_size=32 * 1024 * 1024)
    app["service"] = service
    if shutdown_event is not None:
        app["shutdown_event"] = shutdown_event

    @web.middleware
    async def auth_middleware(request: web.Request, handler):
        secret = request.headers.get(SECRET_HEADER) or request.query.get("secret") or ""
        if secret != service.state.runtime.secret:
            return fail("UNAUTHORIZED", "secret 不匹配", status=401)
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
        uin = int(body.get("uin") or 0)
        source = str(body.get("source") or "manual")
        try:
            payload = await service.bind_cookie(cookie_text, uin=uin, source=source)
        except QzoneBridgeError as exc:
            service._set_error(exc)
            return fail(exc.code, exc.message, detail=exc.detail)
        return ok(payload)

    async def unbind(request: web.Request) -> web.Response:
        payload = await service.unbind()
        return ok(payload)

    async def feeds(request: web.Request) -> web.Response:
        hostuin = int(request.query.get("hostuin") or 0)
        limit = int(request.query.get("limit") or 5)
        cursor = request.query.get("cursor") or ""
        scope = request.query.get("scope") or ""
        try:
            payload = await service.list_feeds(hostuin=hostuin, limit=limit, cursor=cursor, scope=scope)
        except QzoneBridgeError as exc:
            service._set_error(exc)
            return fail(exc.code, exc.message, detail=exc.detail)
        return ok(payload)

    async def detail(request: web.Request) -> web.Response:
        hostuin = int(request.query.get("hostuin") or 0)
        fid = request.query.get("fid") or ""
        appid = int(request.query.get("appid") or 311)
        busi_param = request.query.get("busi_param") or ""
        try:
            payload = await service.detail_feed(hostuin=hostuin, fid=fid, appid=appid, busi_param=busi_param)
        except QzoneBridgeError as exc:
            service._set_error(exc)
            return fail(exc.code, exc.message, detail=exc.detail)
        return ok(payload)

    async def post(request: web.Request) -> web.Response:
        body = await _json_body(request)
        content = str(body.get("content") or "")
        sync_weibo = bool(body.get("sync_weibo") or False)
        media = body.get("media") or body.get("attachments") or body.get("photos") or []
        content_sanitized = bool(body.get("content_sanitized") or False)
        try:
            payload = await service.publish_post(
                content=content,
                sync_weibo=sync_weibo,
                media=media,
                content_sanitized=content_sanitized,
            )
        except QzoneBridgeError as exc:
            service._set_error(exc)
            return fail(exc.code, exc.message, detail=exc.detail)
        return ok(payload)

    async def comment(request: web.Request) -> web.Response:
        body = await _json_body(request)
        try:
            payload = await service.comment_post(
                hostuin=int(body.get("hostuin") or 0),
                fid=str(body.get("fid") or ""),
                content=str(body.get("content") or ""),
                appid=int(body.get("appid") or 311),
                private=bool(body.get("private") or False),
            )
        except QzoneBridgeError as exc:
            service._set_error(exc)
            return fail(exc.code, exc.message, detail=exc.detail)
        return ok(payload)

    async def like(request: web.Request) -> web.Response:
        body = await _json_body(request)
        try:
            payload = await service.like_post(
                hostuin=int(body.get("hostuin") or 0),
                fid=str(body.get("fid") or ""),
                appid=int(body.get("appid") or 311),
                curkey=str(body.get("curkey") or ""),
                unlike=bool(body.get("unlike") or False),
            )
        except QzoneBridgeError as exc:
            service._set_error(exc)
            return fail(exc.code, exc.message, detail=exc.detail)
        return ok(payload)

    async def shutdown(request: web.Request) -> web.Response:
        service.touch()
        service.save()
        event = request.app.get("shutdown_event")
        if isinstance(event, asyncio.Event):
            asyncio.get_running_loop().call_later(0.1, event.set)
        return ok({"stopping": True})

    app.router.add_get("/health", health)
    app.router.add_get("/status", status)
    app.router.add_post("/bind", bind)
    app.router.add_post("/unbind", unbind)
    app.router.add_get("/feeds", feeds)
    app.router.add_get("/detail", detail)
    app.router.add_post("/post", post)
    app.router.add_post("/comment", comment)
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

    logging.basicConfig(level=os.getenv("QZONE_DAEMON_LOG_LEVEL", "INFO"))
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
