"""AstrBot entry point for the QQ空间 bridge."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

from qzone_bridge.controller import QzoneDaemonController
from qzone_bridge.errors import DaemonUnavailableError, QzoneBridgeError, QzoneNeedsRebind
from qzone_bridge.models import FeedEntry
from qzone_bridge.render import format_action_result, format_feed_detail, format_feed_list, format_status
from qzone_bridge.settings import PluginSettings
from qzone_bridge.utils import truncate


class QzoneStablePlugin(Star):
    def __init__(self, context: Context, config: Any | None = None):
        super().__init__(context)
        raw_config = config if config is not None else getattr(context, "get_config", lambda: {})()
        self.settings = PluginSettings.from_mapping(raw_config)
        self.root = Path(__file__).resolve().parent
        self.data_dir = self.root / "data" / "qzone"
        self.controller = QzoneDaemonController(
            plugin_root=self.root,
            data_dir=self.data_dir,
            default_port=self.settings.daemon_port,
            request_timeout=self.settings.request_timeout,
            start_timeout=self.settings.start_timeout,
            keepalive_interval=self.settings.keepalive_interval,
            user_agent=self.settings.user_agent,
        )

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

    def _error_text(self, exc: QzoneBridgeError) -> str:
        detail = ""
        if exc.detail:
            detail = f"\n{exc.detail}"
        return f"{exc.message}{detail}"

    async def _ensure_daemon(self) -> None:
        if self.settings.auto_start_daemon:
            await self.controller.ensure_running()
        else:
            status = await self.controller.get_status()
            if status.get("daemon_state") == "offline":
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

    @filter.command_group("qzone")
    def qzone(self):
        pass

    @qzone.command("help")
    async def qzone_help(self, event: AstrMessageEvent):
        text = "\n".join(
            [
                "QQ空间插件",
                "/qzone status",
                "/qzone bind <cookie>",
                "/qzone unbind",
                "/qzone feed [hostuin] [limit] [cursor]",
                "/qzone detail <hostuin> <fid> [appid]",
                "/qzone post <content>",
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
        yield event.plain_result(text)

    @qzone.command("status")
    async def qzone_status(self, event: AstrMessageEvent):
        if not self._is_admin(event):
            yield event.plain_result("仅管理员可查看状态。")
            return
        try:
            await self._ensure_daemon()
            payload = await self.controller.get_status()
        except QzoneBridgeError as exc:
            yield event.plain_result(self._error_text(exc))
            return
        yield event.plain_result(format_status(payload))

    @qzone.command("bind")
    async def qzone_bind(self, event: AstrMessageEvent, cookie: str):
        if not self._is_admin(event):
            yield event.plain_result("仅管理员可绑定 Cookie。")
            return
        try:
            await self._ensure_daemon()
            payload = await self.controller.bind_cookie(cookie)
        except QzoneBridgeError as exc:
            logger.warning("qzone bind failed: %s", exc)
            yield event.plain_result(self._error_text(exc))
            return
        yield event.plain_result(format_status(payload))

    @qzone.command("unbind")
    async def qzone_unbind(self, event: AstrMessageEvent):
        if not self._is_admin(event):
            yield event.plain_result("仅管理员可解绑。")
            return
        try:
            await self._ensure_daemon()
            payload = await self.controller.unbind()
        except QzoneBridgeError as exc:
            yield event.plain_result(self._error_text(exc))
            return
        yield event.plain_result(format_status(payload))

    @qzone.command("feed")
    async def qzone_feed(self, event: AstrMessageEvent, hostuin: int = 0, limit: int = 0, cursor: str = ""):
        try:
            await self._ensure_daemon()
            payload = await self.controller.list_feeds(
                hostuin=hostuin,
                limit=self._limit(limit),
                cursor=cursor,
            )
        except QzoneBridgeError as exc:
            yield event.plain_result(self._error_text(exc))
            return
        entries = self._to_feed_entries(payload)
        text = format_feed_list(entries, cursor=str(payload.get("cursor") or ""), has_more=bool(payload.get("has_more")))
        yield event.plain_result(text)

    @qzone.command("detail")
    async def qzone_detail(self, event: AstrMessageEvent, hostuin: int, fid: str, appid: int = 311):
        try:
            await self._ensure_daemon()
            payload = await self.controller.detail_feed(hostuin=hostuin, fid=fid, appid=appid)
        except QzoneBridgeError as exc:
            yield event.plain_result(self._error_text(exc))
            return
        yield event.plain_result(self._render_detail(payload))

    @qzone.command("post")
    async def qzone_post(self, event: AstrMessageEvent, content: str):
        if not self._is_admin(event):
            yield event.plain_result("仅管理员可发说说。")
            return
        try:
            await self._ensure_daemon()
            payload = await self.controller.publish_post(content=content)
        except QzoneBridgeError as exc:
            yield event.plain_result(self._error_text(exc))
            return
        yield event.plain_result(format_action_result("发布成功", payload))

    @qzone.command("comment")
    async def qzone_comment(self, event: AstrMessageEvent, hostuin: int, fid: str, content: str):
        if not self._is_admin(event):
            yield event.plain_result("仅管理员可评论。")
            return
        try:
            await self._ensure_daemon()
            payload = await self.controller.comment_post(hostuin=hostuin, fid=fid, content=content)
        except QzoneBridgeError as exc:
            yield event.plain_result(self._error_text(exc))
            return
        yield event.plain_result(format_action_result("评论成功", payload))

    @qzone.command("like")
    async def qzone_like(self, event: AstrMessageEvent, hostuin: int, fid: str, appid: int = 311, unlike: bool = False):
        if not self._is_admin(event):
            yield event.plain_result("仅管理员可点赞。")
            return
        try:
            await self._ensure_daemon()
            payload = await self.controller.like_post(hostuin=hostuin, fid=fid, appid=appid, unlike=unlike)
        except QzoneBridgeError as exc:
            yield event.plain_result(self._error_text(exc))
            return
        yield event.plain_result(format_action_result("点赞成功", payload))

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
            await self._ensure_daemon()
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
            await self._ensure_daemon()
            payload = await self.controller.detail_feed(hostuin=hostuin, fid=fid, appid=appid)
        except QzoneBridgeError as exc:
            yield event.plain_result(self._error_text(exc))
            return
        yield event.plain_result(self._render_detail(payload))

    @filter.llm_tool(name="qzone_publish_post")
    async def tool_publish_post(self, event: AstrMessageEvent, content: str, confirm: bool = False, sync_weibo: bool = False):
        """发布一条 QQ 空间说说。

        Args:
            content (string): 说说内容。
            confirm (boolean): 是否确认执行。
            sync_weibo (boolean): 是否同步微博。
        """
        if not self._is_admin(event):
            yield event.plain_result("仅管理员可发布说说。")
            return
        if not confirm:
            yield event.plain_result(f"待发布草稿: {truncate(content, 120)}。确认后将执行。")
            return
        try:
            await self._ensure_daemon()
            payload = await self.controller.publish_post(content=content, sync_weibo=sync_weibo)
        except QzoneBridgeError as exc:
            yield event.plain_result(self._error_text(exc))
            return
        yield event.plain_result(format_action_result("发布成功", payload))

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
        if not confirm:
            yield event.plain_result(
                f"待评论草稿: hostuin={hostuin}, fid={fid}, content={truncate(content, 120)}。确认后将执行。"
            )
            return
        try:
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
        if not confirm:
            action = "取消点赞" if unlike else "点赞"
            yield event.plain_result(f"待执行草稿: {action} hostuin={hostuin}, fid={fid}。确认后将执行。")
            return
        try:
            await self._ensure_daemon()
            payload = await self.controller.like_post(hostuin=hostuin, fid=fid, appid=appid, unlike=unlike)
        except QzoneBridgeError as exc:
            yield event.plain_result(self._error_text(exc))
            return
        yield event.plain_result(format_action_result("点赞成功", payload))

    async def terminate(self):
        await self.controller.close()
