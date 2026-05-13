"""AstrBot entry point for the QQ空间 bridge."""

from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

PLUGIN_ROOT = Path(__file__).resolve().parent


def _prepare_local_qzone_bridge_imports() -> None:
    """Force bundled qzone_bridge modules to reload from this plugin directory."""

    def _same_path(value: str) -> bool:
        try:
            return Path(value).resolve() == PLUGIN_ROOT
        except Exception:
            return False

    sys.path[:] = [path for path in sys.path if not _same_path(path)]
    sys.path.insert(0, str(PLUGIN_ROOT))
    importlib.invalidate_caches()
    for name in list(sys.modules):
        if name == "qzone_bridge" or name.startswith("qzone_bridge."):
            sys.modules.pop(name, None)


_prepare_local_qzone_bridge_imports()

from qzone_bridge.controller import QzoneDaemonController
from qzone_bridge.errors import DaemonUnavailableError, QzoneBridgeError, QzoneCookieAcquireError, QzoneNeedsRebind
from qzone_bridge.media import PostPayload, collect_post_payload
from qzone_bridge.models import FeedEntry
from qzone_bridge.onebot_cookie import fetch_cookie_text
from qzone_bridge.parser import normalize_uin, parse_cookie_text
from qzone_bridge.publish_renderer import profile_from_event, render_publish_result_image
from qzone_bridge.render import format_action_result, format_feed_detail, format_feed_list, format_status
from qzone_bridge.settings import PluginSettings
from qzone_bridge.utils import truncate


class QzoneStablePlugin(Star):
    def __init__(self, context: Context, config: Any | None = None):
        super().__init__(context)
        self._context = context
        raw_config = config if config is not None else getattr(context, "get_config", lambda: {})()
        self.settings = PluginSettings.from_mapping(raw_config)
        self.root = Path(__file__).resolve().parent
        self.data_dir = self.root / "data" / "qzone"
        self._onebot_client: Any | None = None
        self._cookie_lock: asyncio.Lock | None = None
        self.controller = QzoneDaemonController(
            plugin_root=self.root,
            data_dir=self.data_dir,
            default_port=self.settings.daemon_port,
            request_timeout=self.settings.request_timeout,
            start_timeout=self.settings.start_timeout,
            keepalive_interval=self.settings.keepalive_interval,
            user_agent=self.settings.user_agent,
            auto_start_daemon=self.settings.auto_start_daemon,
        )
        self._capture_onebot_client_from_context()
        self._daemon_warmup_task: asyncio.Task | None = None

    def _sender_id(self, event: AstrMessageEvent) -> int:
        try:
            if hasattr(event, "get_sender_id"):
                value = event.get_sender_id()
                if value is not None:
                    return int(value)
        except Exception:
            pass
        message_obj = getattr(event, "message_obj", None)
        sender = getattr(message_obj, "sender", None)
        return int(getattr(sender, "user_id", 0) or 0)

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        try:
            if hasattr(event, "is_admin") and event.is_admin():
                return True
        except Exception:
            pass
        return self._sender_id(event) in set(self.settings.admin_uins)

    def _command_result(self, event: AstrMessageEvent, text: str):
        self._stop_event(event)
        return event.plain_result(text)

    async def _publisher_render_profile(self, event: AstrMessageEvent) -> Any:
        profile = profile_from_event(event)
        status: dict[str, Any] = {}
        try:
            status = await self.controller.get_status()
        except QzoneBridgeError:
            status = {}

        login_uin = int(status.get("login_uin") or 0)
        if not login_uin:
            return profile

        nickname = str(
            status.get("login_nickname")
            or status.get("nickname")
            or status.get("publisher_nickname")
            or ""
        ).strip()
        avatar_source = str(status.get("login_avatar") or status.get("avatar") or "").strip()
        if not avatar_source:
            avatar_source = f"https://q1.qlogo.cn/g?b=qq&nk={login_uin}&s=100"

        bot = self._capture_onebot_client(event)
        if bot is not None:
            fetched = await self._fetch_onebot_user_info(bot, login_uin)
            if fetched:
                nickname = nickname or str(fetched.get("nickname") or fetched.get("name") or "").strip()
                avatar_source = str(fetched.get("avatar") or fetched.get("avatar_url") or avatar_source).strip()

        profile.user_id = str(login_uin)
        profile.nickname = nickname or str(login_uin)
        profile.avatar_source = avatar_source
        return profile

    async def _fetch_onebot_user_info(self, bot: Any, uin: int) -> dict[str, Any]:
        for method_name, kwargs in (
            ("get_stranger_info", {"user_id": uin, "no_cache": False}),
            ("get_friend_info", {"user_id": uin}),
            ("get_user_info", {"user_id": uin}),
        ):
            method = getattr(bot, method_name, None)
            if not callable(method):
                continue
            try:
                result = method(**kwargs)
                if asyncio.iscoroutine(result):
                    result = await result
            except TypeError:
                try:
                    result = method(uin)
                    if asyncio.iscoroutine(result):
                        result = await result
                except Exception:
                    continue
            except Exception:
                continue
            if isinstance(result, dict):
                return result
        return {}

    async def _publish_result(self, event: AstrMessageEvent, post: PostPayload, payload: dict[str, Any]):
        text = format_action_result("发布成功", payload)
        if not self.settings.render_publish_result:
            self._stop_event(event)
            return event.plain_result(text)
        profile = await self._publisher_render_profile(event)
        try:
            image_path = await asyncio.to_thread(
                render_publish_result_image,
                post,
                self.data_dir / "rendered_posts",
                profile=profile,
                result=payload,
                width=self.settings.render_result_width,
            )
        except Exception as exc:
            logger.exception("qzone publish result render failed: %s", exc)
            self._stop_event(event)
            return event.plain_result(text)

        image_result = getattr(event, "image_result", None)
        if callable(image_result):
            self._stop_event(event)
            return image_result(str(image_path))
        self._stop_event(event)
        return event.plain_result(f"{text}\n渲染图: {image_path}")

    def _stop_event(self, event: AstrMessageEvent) -> None:
        stopper = getattr(event, "stop_event", None)
        if callable(stopper):
            try:
                stopper()
            except Exception:
                pass

    def _error_text(self, exc: QzoneBridgeError) -> str:
        if not exc.detail:
            return exc.message
        if isinstance(exc.detail, dict):
            parts: list[str] = []
            status_code = exc.detail.get("status_code")
            if status_code is not None:
                parts.append(f"HTTP {status_code}")
            location = exc.detail.get("location")
            if location:
                parts.append(f"跳转 {location}")
            url = exc.detail.get("url")
            if url:
                parts.append(f"来源 {url}")
            if parts:
                return f"{exc.message}（{', '.join(parts)}）"
        return f"{exc.message}\n{exc.detail}"

    async def _ensure_daemon(self, *, allow_needs_rebind: bool = False) -> None:
        status = await self.controller.get_status()
        if status.get("needs_rebind") and not allow_needs_rebind:
            raise QzoneNeedsRebind("QQ空间登录失效，请重新绑定 Cookie")
        if allow_needs_rebind:
            if status.get("daemon_state") != "ready":
                await self.controller.ensure_running()
            return
        if self.settings.auto_start_daemon:
            if status.get("daemon_state") != "ready":
                await self.controller.ensure_running()
        elif status.get("daemon_state") != "ready":
            raise DaemonUnavailableError("daemon 未运行")

    def _limit(self, limit: int | None) -> int:
        if not limit or limit <= 0:
            return self.settings.public_feed_limit
        return min(limit, self.settings.max_feed_limit)

    def _to_feed_entries(self, payload: dict[str, Any]) -> list[FeedEntry]:
        items = payload.get("items") or []
        entries: list[FeedEntry] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            entries.append(FeedEntry(**item))
        return entries

    def _render_detail(self, payload: dict[str, Any]) -> str:
        entry = FeedEntry(**payload["entry"])
        text = format_feed_detail(entry)
        comments = payload.get("comments") or []
        if comments:
            lines = [text, "", "评论"]
            for comment in comments[:5]:
                nickname = comment.get("nickname") or comment.get("uin") or "-"
                lines.append(f"- {nickname}: {truncate(str(comment.get('content') or ''), 80)}")
            return "\n".join(lines)
        return text

    def _get_cookie_lock(self) -> asyncio.Lock:
        if self._cookie_lock is None:
            self._cookie_lock = asyncio.Lock()
        return self._cookie_lock

    def _capture_onebot_client_from_context(self) -> Any | None:
        context = getattr(self, "_context", None) or getattr(self, "context", None)
        platform = None
        if context is not None:
            try:
                platform = context.get_platform("aiocqhttp")
            except Exception:
                platform = None
            if platform is None:
                try:
                    platform_manager = getattr(context, "platform_manager", None)
                    for candidate in getattr(platform_manager, "platform_insts", []):
                        meta = candidate.meta()
                        if getattr(meta, "name", "") == "aiocqhttp":
                            platform = candidate
                            break
                except Exception:
                    platform = None
        if platform is not None:
            bot = getattr(platform, "bot", None)
            if bot is not None:
                self._onebot_client = bot
        return self._onebot_client

    def _capture_onebot_client(self, event: AstrMessageEvent | None = None) -> Any | None:
        bot = getattr(event, "bot", None) if event is not None else None
        if bot is not None:
            self._onebot_client = bot
            return bot
        return self._capture_onebot_client_from_context()

    def _cookie_binding_hint(self) -> str:
        return "请确认 AstrBot 正在使用 aiocqhttp(OneBot v11) 平台，或手动使用 /qzone bind 绑定 Cookie。"

    async def _auto_bind_cookie(
        self,
        event: AstrMessageEvent | None = None,
        *,
        force: bool = False,
        source: str = "aiocqhttp",
    ) -> dict[str, Any]:
        async with self._get_cookie_lock():
            if not self.settings.auto_bind_cookie and not force:
                raise QzoneCookieAcquireError("自动获取 Cookie 已关闭。请手动绑定。")

            bot = self._capture_onebot_client(event)
            if bot is None:
                raise QzoneCookieAcquireError(f"未捕获到 OneBot 客户端，无法自动获取 Cookie。{self._cookie_binding_hint()}")

            try:
                status = await self.controller.get_status(probe_daemon=False)
            except QzoneBridgeError:
                status = {}

            if not force and status and int(status.get("cookie_count") or 0) > 0 and not bool(status.get("needs_rebind")):
                return status

            cookie_text = await fetch_cookie_text(bot, domain=self.settings.cookie_domain)
            if not cookie_text:
                raise QzoneCookieAcquireError(f"OneBot 未返回可用 Cookie。{self._cookie_binding_hint()}")

            try:
                cookie_uin = normalize_uin(parse_cookie_text(cookie_text))
            except Exception:
                cookie_uin = 0
            payload = await self.controller.bind_cookie_local(cookie_text, uin=cookie_uin, source=source)
            return payload

    async def _ensure_cookie_ready(
        self,
        event: AstrMessageEvent | None = None,
        *,
        force: bool = False,
        source: str = "aiocqhttp",
    ) -> dict[str, Any] | None:
        try:
            status = await self.controller.get_status(probe_daemon=False)
        except QzoneBridgeError:
            status = {}
        if not force and status and int(status.get("cookie_count") or 0) > 0 and not bool(status.get("needs_rebind")):
            return status
        return await self._auto_bind_cookie(event, force=force, source=source)

    async def _bootstrap_auto_bind(self, trigger: str) -> None:
        client = self._capture_onebot_client_from_context()
        if client is None or not self.settings.auto_bind_cookie:
            await self._prewarm_daemon_if_cookie_ready(trigger)
            return
        try:
            await self._ensure_cookie_ready(source="aiocqhttp")
        except QzoneBridgeError as exc:
            logger.warning("qzone auto bind on %s failed: %s", trigger, exc)
            return
        await self._prewarm_daemon_if_cookie_ready(trigger)

    async def _prewarm_daemon_if_cookie_ready(self, trigger: str) -> None:
        if not self.settings.auto_start_daemon:
            return
        try:
            status = await self.controller.get_status(probe_daemon=False)
        except QzoneBridgeError as exc:
            logger.debug("qzone daemon prewarm status check on %s failed: %s", trigger, exc)
            return
        if int(status.get("cookie_count") or 0) <= 0 or bool(status.get("needs_rebind")):
            return
        self._schedule_daemon_warmup(trigger)

    def _schedule_daemon_warmup(self, trigger: str) -> None:
        if not self.settings.auto_start_daemon:
            return
        task = self._daemon_warmup_task
        if task is not None and not task.done():
            return

        async def runner() -> None:
            try:
                await self.controller.ensure_running()
            except QzoneBridgeError as exc:
                logger.warning("qzone daemon prewarm on %s failed: %s", trigger, exc)
            except Exception:
                logger.warning("qzone daemon prewarm on %s failed unexpectedly", trigger, exc_info=True)

        self._daemon_warmup_task = asyncio.create_task(runner())

    @filter.command_group("qzone")
    def qzone(self):
        pass

    @filter.on_platform_loaded()
    async def qzone_on_platform_loaded(self):
        await self._bootstrap_auto_bind("platform load")

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    async def qzone_capture_aiocqhttp_client(self, event: AstrMessageEvent):
        self._capture_onebot_client(event)

    @qzone.command("help")
    async def qzone_help(self, event: AstrMessageEvent):
        text = "\n".join(
            [
                "QQ空间插件",
                "/qzone status",
                "/qzone bind <cookie>",
                "/qzone autobind",
                "/qzone unbind",
                "/qzone feed [hostuin] [limit] [cursor]",
                "/qzone detail <hostuin> <fid> [appid]",
                "/qzone post <content> [图片/图片文件]",
                "/qzone comment <hostuin> <fid> <content>",
                "/qzone like <hostuin> <fid> [appid] [unlike]",
                "",
                "LLM tools:",
                "qzone_get_status",
                "qzone_list_feed",
                "qzone_detail_feed",
                "qzone_publish_post",
                "qzone_comment_post",
                "qzone_like_post",
            ]
        )
        yield self._command_result(event, text)

    @qzone.command("status")
    async def qzone_status(self, event: AstrMessageEvent):
        if not self._is_admin(event):
            yield self._command_result(event, "仅管理员可查看状态。")
            return
        try:
            payload = await self.controller.get_status()
        except QzoneBridgeError as exc:
            yield self._command_result(event, self._error_text(exc))
            return
        yield self._command_result(event, format_status(payload))

    @qzone.command("bind")
    async def qzone_bind(self, event: AstrMessageEvent, cookie: str):
        if not self._is_admin(event):
            yield self._command_result(event, "仅管理员可绑定 Cookie。")
            return
        try:
            payload = await self.controller.bind_cookie_local(cookie)
        except QzoneBridgeError as exc:
            logger.warning("qzone bind failed: %s", exc)
            yield self._command_result(event, self._error_text(exc))
            return
        self._schedule_daemon_warmup("manual bind")
        yield self._command_result(event, format_status(payload))

    @qzone.command("autobind")
    async def qzone_autobind(self, event: AstrMessageEvent):
        if not self._is_admin(event):
            yield self._command_result(event, "仅管理员可自动绑定 Cookie。")
            return
        try:
            payload = await self._auto_bind_cookie(event, force=True, source="aiocqhttp")
        except QzoneBridgeError as exc:
            logger.warning("qzone autobind failed: %s", exc)
            yield self._command_result(event, self._error_text(exc))
            return
        self._schedule_daemon_warmup("autobind")
        yield self._command_result(event, format_status(payload))

    @qzone.command("unbind")
    async def qzone_unbind(self, event: AstrMessageEvent):
        if not self._is_admin(event):
            yield self._command_result(event, "仅管理员可解绑。")
            return
        try:
            payload = await self.controller.unbind_local()
        except QzoneBridgeError as exc:
            yield self._command_result(event, self._error_text(exc))
            return
        yield self._command_result(event, format_status(payload))

    @qzone.command("feed")
    async def qzone_feed(self, event: AstrMessageEvent, hostuin: int = 0, limit: int = 0, cursor: str = ""):
        try:
            await self._ensure_cookie_ready(event)
            await self._ensure_daemon()
            payload = await self.controller.list_feeds(
                hostuin=hostuin,
                limit=self._limit(limit),
                cursor=cursor,
            )
        except QzoneBridgeError as exc:
            yield self._command_result(event, self._error_text(exc))
            return
        entries = self._to_feed_entries(payload)
        text = format_feed_list(entries, cursor=str(payload.get("cursor") or ""), has_more=bool(payload.get("has_more")))
        yield self._command_result(event, text)

    @qzone.command("detail")
    async def qzone_detail(self, event: AstrMessageEvent, hostuin: int, fid: str, appid: int = 311):
        try:
            await self._ensure_cookie_ready(event)
            await self._ensure_daemon()
            payload = await self.controller.detail_feed(hostuin=hostuin, fid=fid, appid=appid)
        except QzoneBridgeError as exc:
            yield self._command_result(event, self._error_text(exc))
            return
        yield self._command_result(event, self._render_detail(payload))

    @qzone.command("post")
    async def qzone_post(self, event: AstrMessageEvent, content: str = ""):
        self._stop_event(event)
        if not self._is_admin(event):
            yield self._command_result(event, "仅管理员可发说说。")
            return
        post = collect_post_payload(
            event,
            fallback_content=content,
            include_event_text=True,
            command_prefixes=("qzone post",),
        )
        try:
            await self._ensure_cookie_ready(event)
            payload = await self.controller.publish_post(
                content=post.content,
                media=[item.to_dict() for item in post.media],
                content_sanitized=True,
            )
        except QzoneBridgeError as exc:
            yield self._command_result(event, self._error_text(exc))
            return
        yield await self._publish_result(event, post, payload)

    @qzone.command("comment")
    async def qzone_comment(self, event: AstrMessageEvent, hostuin: int, fid: str, content: str):
        if not self._is_admin(event):
            yield self._command_result(event, "仅管理员可评论。")
            return
        try:
            await self._ensure_cookie_ready(event)
            await self._ensure_daemon()
            payload = await self.controller.comment_post(hostuin=hostuin, fid=fid, content=content)
        except QzoneBridgeError as exc:
            yield self._command_result(event, self._error_text(exc))
            return
        yield self._command_result(event, format_action_result("评论成功", payload))

    @qzone.command("like")
    async def qzone_like(self, event: AstrMessageEvent, hostuin: int, fid: str, appid: int = 311, unlike: bool = False):
        if not self._is_admin(event):
            yield self._command_result(event, "仅管理员可点赞。")
            return
        try:
            await self._ensure_cookie_ready(event)
            await self._ensure_daemon()
            payload = await self.controller.like_post(hostuin=hostuin, fid=fid, appid=appid, unlike=unlike)
        except QzoneBridgeError as exc:
            yield self._command_result(event, self._error_text(exc))
            return
        yield self._command_result(event, format_action_result("点赞成功", payload))

    @filter.llm_tool(name="qzone_get_status")
    async def tool_get_status(self, event: AstrMessageEvent):
        """获取 QQ 空间 daemon 状态。

        Returns:
            文本状态摘要。
        """
        if not self._is_admin(event):
            yield event.plain_result("仅管理员可以查看状态。")
            return
        try:
            payload = await self.controller.get_status()
        except QzoneBridgeError as exc:
            yield event.plain_result(self._error_text(exc))
            return
        yield event.plain_result(format_status(payload))

    @filter.llm_tool(name="qzone_list_feed")
    async def tool_list_feed(self, event: AstrMessageEvent, hostuin: int = 0, limit: int = 5, cursor: str = "", scope: str = ""):
        """列出 QQ 空间说说。

        Args:
            hostuin (number): 目标 QQ 号。0 表示当前登录账号。
            limit (number): 返回条数。
            cursor (string): 翻页游标。
            scope (string): self 或 profile。
        """
        try:
            await self._ensure_cookie_ready(event)
            await self._ensure_daemon()
            payload = await self.controller.list_feeds(
                hostuin=hostuin,
                limit=self._limit(limit),
                cursor=cursor,
                scope=scope,
            )
        except QzoneBridgeError as exc:
            yield event.plain_result(self._error_text(exc))
            return
        entries = self._to_feed_entries(payload)
        yield event.plain_result(format_feed_list(entries, cursor=str(payload.get("cursor") or ""), has_more=bool(payload.get("has_more"))))

    @filter.llm_tool(name="qzone_detail_feed")
    async def tool_detail_feed(self, event: AstrMessageEvent, hostuin: int, fid: str, appid: int = 311):
        """获取单条说说详情。

        Args:
            hostuin (number): 说说所属 QQ 号。
            fid (string): 说说 fid。
            appid (number): 应用 id，默认 311。
        """
        try:
            await self._ensure_cookie_ready(event)
            await self._ensure_daemon()
            payload = await self.controller.detail_feed(hostuin=hostuin, fid=fid, appid=appid)
        except QzoneBridgeError as exc:
            yield event.plain_result(self._error_text(exc))
            return
        yield event.plain_result(self._render_detail(payload))

    @filter.llm_tool(name="qzone_publish_post")
    async def tool_publish_post(
        self,
        event: AstrMessageEvent,
        content: str,
        confirm: bool = False,
        sync_weibo: bool = False,
        media: list[str] | None = None,
    ):
        """发布一条 QQ 空间说说。

        Args:
            content (string): 说说内容。
            confirm (boolean): 是否确认执行。
            sync_weibo (boolean): 是否同步微博。
        """
        if not self._is_admin(event):
            yield event.plain_result("仅管理员可发布说说。")
            return
        post = collect_post_payload(
            event,
            fallback_content=content,
            include_event_text=False,
            command_prefixes=("qzone post",),
            extra_media=media,
        )
        if self.settings.preview_writes and not confirm:
            draft = truncate(post.content or "（仅附件）", 120)
            suffix = f"；附件 {len(post.media)} 个" if post.media else ""
            yield event.plain_result(f"待发布草稿: {draft}{suffix}。确认后将执行。")
            return
        try:
            await self._ensure_cookie_ready(event)
            payload = await self.controller.publish_post(
                content=post.content,
                sync_weibo=sync_weibo,
                media=[item.to_dict() for item in post.media],
                content_sanitized=True,
            )
        except QzoneBridgeError as exc:
            yield event.plain_result(self._error_text(exc))
            return
        yield await self._publish_result(event, post, payload)

    @filter.llm_tool(name="qzone_comment_post")
    async def tool_comment_post(
        self,
        event: AstrMessageEvent,
        hostuin: int,
        fid: str,
        content: str,
        confirm: bool = False,
        appid: int = 311,
        private: bool = False,
    ):
        """评论一条说说。

        Args:
            hostuin (number): 目标 QQ 号。
            fid (string): 说说 fid。
            content (string): 评论内容。
            confirm (boolean): 是否确认执行。
            appid (number): 应用 id。
            private (boolean): 是否私密评论。
        """
        if not self._is_admin(event):
            yield event.plain_result("仅管理员可评论。")
            return
        if self.settings.preview_writes and not confirm:
            yield event.plain_result(
                f"待评论草稿: hostuin={hostuin}, fid={fid}, content={truncate(content, 120)}。确认后将执行。"
            )
            return
        try:
            await self._ensure_cookie_ready(event)
            await self._ensure_daemon()
            payload = await self.controller.comment_post(
                hostuin=hostuin,
                fid=fid,
                content=content,
                appid=appid,
                private=private,
            )
        except QzoneBridgeError as exc:
            yield event.plain_result(self._error_text(exc))
            return
        yield event.plain_result(format_action_result("评论成功", payload))

    @filter.llm_tool(name="qzone_like_post")
    async def tool_like_post(
        self,
        event: AstrMessageEvent,
        hostuin: int,
        fid: str,
        confirm: bool = False,
        appid: int = 311,
        unlike: bool = False,
    ):
        """点赞或取消点赞一条说说。

        Args:
            hostuin (number): 目标 QQ 号。
            fid (string): 说说 fid。
            confirm (boolean): 是否确认执行。
            appid (number): 应用 id。
            unlike (boolean): 是否取消点赞。
        """
        if not self._is_admin(event):
            yield event.plain_result("仅管理员可点赞。")
            return
        if self.settings.preview_writes and not confirm:
            action = "取消点赞" if unlike else "点赞"
            yield event.plain_result(f"待执行草稿: {action} hostuin={hostuin}, fid={fid}。确认后将执行。")
            return
        try:
            await self._ensure_cookie_ready(event)
            await self._ensure_daemon()
            payload = await self.controller.like_post(hostuin=hostuin, fid=fid, appid=appid, unlike=unlike)
        except QzoneBridgeError as exc:
            yield event.plain_result(self._error_text(exc))
            return
        yield event.plain_result(format_action_result("点赞成功", payload))

    async def terminate(self):
        try:
            await self.controller.close()
        except Exception as exc:
            logger.exception("qzone controller close failed: %s", exc)
