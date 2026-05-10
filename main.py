"""AstrBot entry point for the QQ??? bridge."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

PLUGIN_ROOT = Path(__file__).resolve().parent
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

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
                raise DaemonUnavailableError("daemon ?????)

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
            lines = [text, "", "???"]
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
                "QQ??????",
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
            yield event.plain_result("????????????????)
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
            yield event.plain_result("???????????Cookie??)
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
            yield event.plain_result("?????????????)
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
            yield event.plain_result("??????????????)
            return
        try:
            await self._ensure_daemon()
            payload = await self.controller.publish_post(content=content)
        except QzoneBridgeError as exc:
            yield event.plain_result(self._error_text(exc))
            return
        yield event.plain_result(format_action_result("??????", payload))

    @qzone.command("comment")
    async def qzone_comment(self, event: AstrMessageEvent, hostuin: int, fid: str, content: str):
        if not self._is_admin(event):
            yield event.plain_result("?????????????)
            return
        try:
            await self._ensure_daemon()
            payload = await self.controller.comment_post(hostuin=hostuin, fid=fid, content=content)
        except QzoneBridgeError as exc:
            yield event.plain_result(self._error_text(exc))
            return
        yield event.plain_result(format_action_result("??????", payload))

    @qzone.command("like")
    async def qzone_like(self, event: AstrMessageEvent, hostuin: int, fid: str, appid: int = 311, unlike: bool = False):
        if not self._is_admin(event):
            yield event.plain_result("?????????????)
            return
        try:
            await self._ensure_daemon()
            payload = await self.controller.like_post(hostuin=hostuin, fid=fid, appid=appid, unlike=unlike)
        except QzoneBridgeError as exc:
            yield event.plain_result(self._error_text(exc))
            return
        yield event.plain_result(format_action_result("??????", payload))

    @filter.llm_tool(name="qzone_get_status")
    async def tool_get_status(self, event: AstrMessageEvent):
        """??? QQ ??? daemon ??????
        Returns:
            ????????????        """
        if not self._is_admin(event):
            yield event.plain_result("??????????????????)
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
        """??? QQ ????????
        Args:
            hostuin (number): ??? QQ ???? ??????????????            limit (number): ????????            cursor (string): ????????            scope (string): self ??profile??        """
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
        """??????????????
        Args:
            hostuin (number): ???????QQ ????            fid (string): ??? fid??            appid (number): ??? id?????311??        """
        try:
            await self._ensure_daemon()
            payload = await self.controller.detail_feed(hostuin=hostuin, fid=fid, appid=appid)
        except QzoneBridgeError as exc:
            yield event.plain_result(self._error_text(exc))
            return
        yield event.plain_result(self._render_detail(payload))

    @filter.llm_tool(name="qzone_publish_post")
    async def tool_publish_post(self, event: AstrMessageEvent, content: str, confirm: bool = False, sync_weibo: bool = False):
        """???????QQ ????????
        Args:
            content (string): ????????            confirm (boolean): ???????????            sync_weibo (boolean): ???????????        """
        if not self._is_admin(event):
            yield event.plain_result("????????????????)
            return
        if not confirm:
            yield event.plain_result(f"???????? {truncate(content, 120)}?????????????)
            return
        try:
            await self._ensure_daemon()
            payload = await self.controller.publish_post(content=content, sync_weibo=sync_weibo)
        except QzoneBridgeError as exc:
            yield event.plain_result(self._error_text(exc))
            return
        yield event.plain_result(format_action_result("??????", payload))

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
        """????????????
        Args:
            hostuin (number): ??? QQ ????            fid (string): ??? fid??            content (string): ????????            confirm (boolean): ???????????            appid (number): ??? id??            private (boolean): ???????????        """
        if not self._is_admin(event):
            yield event.plain_result("?????????????)
            return
        if not confirm:
            yield event.plain_result(
                f"???????? hostuin={hostuin}, fid={fid}, content={truncate(content, 120)}?????????????
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
        yield event.plain_result(format_action_result("??????", payload))

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
        """???????????????????
        Args:
            hostuin (number): ??? QQ ????            fid (string): ??? fid??            confirm (boolean): ???????????            appid (number): ??? id??            unlike (boolean): ???????????        """
        if not self._is_admin(event):
            yield event.plain_result("?????????????)
            return
        if not confirm:
            action = "??????" if unlike else "???"
            yield event.plain_result(f"???????? {action} hostuin={hostuin}, fid={fid}?????????????)
            return
        try:
            await self._ensure_daemon()
            payload = await self.controller.like_post(hostuin=hostuin, fid=fid, appid=appid, unlike=unlike)
        except QzoneBridgeError as exc:
            yield event.plain_result(self._error_text(exc))
            return
        yield event.plain_result(format_action_result("??????", payload))

    async def terminate(self):
        await self.controller.close()
