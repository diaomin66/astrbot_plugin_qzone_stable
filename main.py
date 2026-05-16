"""AstrBot entry point for the QQ 空间 bridge."""

from __future__ import annotations

import asyncio
import inspect
import importlib
import json
import random
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

PLUGIN_ROOT = Path(__file__).resolve().parent

SENSITIVE_LOG_KEYS = {
    "cookie",
    "cookies",
    "p_skey",
    "skey",
    "pt4_token",
    "pt_key",
    "qzonetoken",
    "secret",
    "token",
}
SENSITIVE_URL_QUERY_KEYS = {"g_tk", "gtk", "p_skey", "skey", "pt4_token", "pt_key", "qzonetoken", "token", "secret"}
LLM_INTERNAL_KEYS = SENSITIVE_LOG_KEYS | {"raw", "cursor", "fid", "curkey", "unikey", "busi_param"}
LLM_REPLY_FORBIDDEN_TERMS = (
    "Result:",
    "result:",
    "[TOOL_",
    "TOOL_",
    "qzone_like_post",
    "status_code",
    "diagnostic",
    "API",
    "api",
    "工具",
    "系统",
    "后台",
    "参数",
    "指令",
    "命令",
    "内部",
    "错误代码",
    "状态码",
    "生成",
    "绘制",
    "绘图",
    "渲染",
    "处理完成",
    "任务完成",
    "已发送",
)


def _redact_url(value: str) -> str:
    try:
        parsed = urlparse(value)
    except Exception:
        return value
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return value
    query = []
    changed = False
    for key, item_value in parse_qsl(parsed.query, keep_blank_values=True):
        lowered = key.lower()
        if lowered in SENSITIVE_URL_QUERY_KEYS or "token" in lowered or "skey" in lowered:
            query.append((key, "***"))
            changed = True
        else:
            query.append((key, item_value))
    if not changed:
        return value
    return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))


def _redact_for_log(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            lowered = key_text.lower()
            if lowered in SENSITIVE_LOG_KEYS or "cookie" in lowered or "skey" in lowered or "secret" in lowered:
                redacted[key_text] = "***"
            else:
                redacted[key_text] = _redact_for_log(item)
        return redacted
    if isinstance(value, list):
        return [_redact_for_log(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_for_log(item) for item in value]
    if isinstance(value, str):
        return _redact_url(value)
    return value


def _safe_for_llm(value: Any) -> Any:
    if isinstance(value, dict):
        visible: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            lowered = key_text.lower()
            if (
                lowered in LLM_INTERNAL_KEYS
                or "cookie" in lowered
                or "skey" in lowered
                or "secret" in lowered
                or "token" in lowered
            ):
                continue
            visible[key_text] = _safe_for_llm(item)
        return visible
    if isinstance(value, list):
        return [_safe_for_llm(item) for item in value]
    if isinstance(value, tuple):
        return [_safe_for_llm(item) for item in value]
    if isinstance(value, str):
        return truncate(_redact_url(value), 500)
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    return truncate(str(value), 500)


def _public_error_reason(message: Any) -> str:
    text = str(message or "").strip()
    text = re.sub(r"^\s*(?:Result|结果)\s*[:：]\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^\s*\[[A-Z0-9_:-]+\]\s*", "", text)
    text = re.split(r"(?:\n|【对话要求】|请用|严格禁止|不要提)", text, maxsplit=1)[0].strip()
    text = text.strip(" \t\r\n:：-—")
    if not text:
        return "现在还没办法继续"
    return truncate(text, 80)


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
from qzone_bridge.drafts import DraftPost, DraftStore
from qzone_bridge.errors import DaemonUnavailableError, QzoneBridgeError, QzoneCookieAcquireError, QzoneNeedsRebind
from qzone_bridge.llm import QzoneLLM
from qzone_bridge.media import PostPayload, collect_post_payload, normalize_media_list
from qzone_bridge.models import FeedEntry
from qzone_bridge.onebot_cookie import fetch_cookie_text
from qzone_bridge.parser import normalize_uin, parse_cookie_text
from qzone_bridge.post_service import QzonePostService
from qzone_bridge.posts import PostStore
from qzone_bridge.publish_renderer import RenderProfile, profile_from_event, render_publish_result_image
from qzone_bridge.render import (
    format_action_result,
    format_feed_detail,
    format_feed_list,
    format_like_result,
    format_llm_feed_list,
    format_status,
)
from qzone_bridge.selection import PostSelection, parse_post_selection, selection_from_tool_args
from qzone_bridge.settings import PluginSettings
from qzone_bridge.social import QzoneComment, QzonePost, post_from_entry
from qzone_bridge.utils import truncate


def _identity_filter_decorator(*args: Any, **kwargs: Any):
    def decorator(func):
        return func

    return decorator


if not hasattr(filter, "command"):
    setattr(filter, "command", _identity_filter_decorator)
if not hasattr(filter, "permission_type"):
    setattr(filter, "permission_type", _identity_filter_decorator)
if not hasattr(filter, "PermissionType"):
    setattr(filter, "PermissionType", type("PermissionType", (), {"ADMIN": "admin"}))


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
        self._scheduled_tasks: list[asyncio.Task] = []
        self._publisher_profile_cache: tuple[int, float, Any] | None = None
        self.drafts = DraftStore(self.data_dir / "drafts.json")
        self.posts = PostStore(self.data_dir / "posts.json")
        self.llm = QzoneLLM(self._context, self.settings)
        self._pillowmd_style: Any | None = None
        self._pillowmd_style_dir = ""

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

    def _post_store(self) -> PostStore:
        expected = self.data_dir / "posts.json"
        if getattr(self.posts, "path", None) != expected:
            self.posts = PostStore(expected)
        return self.posts

    def _post_service(self) -> QzonePostService:
        return QzonePostService(
            self.controller,
            self._post_store(),
            max_feed_limit=self.settings.max_feed_limit,
        )

    def _llm_adapter(self) -> QzoneLLM:
        self.llm.context = getattr(self, "_context", None) or getattr(self, "context", None)
        self.llm.settings = self.settings
        return self.llm

    @staticmethod
    def _onebot_file_uri(path: Path) -> str:
        return "file:///" + path.resolve().as_posix().lstrip("/")

    async def _render_markdown_image(self, text: str, subdir: str = "markdown") -> Path | None:
        style_dir = str(self.settings.pillowmd_style_dir or "").strip()
        if not style_dir:
            return None
        try:
            import pillowmd  # type: ignore

            if self._pillowmd_style is None or self._pillowmd_style_dir != style_dir:
                self._pillowmd_style = pillowmd.LoadMarkdownStyles(style_dir)
                self._pillowmd_style_dir = style_dir
            output_dir = self.data_dir / "pillowmd" / subdir
            output_dir.mkdir(parents=True, exist_ok=True)
            rendered = await self._pillowmd_style.AioRender(text=text, useImageUrl=True)
            return Path(rendered.Save(output_dir))
        except Exception as exc:
            logger.debug("qzone pillowmd render failed: %s", exc)
            return None

    async def _markdown_result(self, event: AstrMessageEvent, text: str, subdir: str = "markdown"):
        image_path = await self._render_markdown_image(text, subdir=subdir)
        image_result = getattr(event, "image_result", None)
        if image_path is not None and callable(image_result):
            self._stop_event(event)
            return image_result(str(image_path))
        return self._command_result(event, text)

    async def _publisher_render_profile(self, event: AstrMessageEvent) -> Any:
        profile = profile_from_event(event)
        status: dict[str, Any] = {}
        try:
            status = await self.controller.get_status(probe_daemon=False)
        except QzoneBridgeError:
            status = {}

        login_uin = int(status.get("login_uin") or 0)
        if not login_uin:
            return profile

        now = time.monotonic()
        cached = self._publisher_profile_cache
        if cached is not None:
            cached_uin, expires_at, cached_profile = cached
            if cached_uin == login_uin and expires_at > now:
                return RenderProfile(
                    nickname=cached_profile.nickname,
                    user_id=cached_profile.user_id,
                    avatar_source=cached_profile.avatar_source,
                    time_text=profile.time_text,
                )

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
            try:
                fetched = await asyncio.wait_for(self._fetch_onebot_user_info(bot, login_uin), timeout=0.35)
            except Exception:
                fetched = {}
            if fetched:
                nickname = nickname or str(fetched.get("nickname") or fetched.get("name") or "").strip()
                avatar_source = str(fetched.get("avatar") or fetched.get("avatar_url") or avatar_source).strip()

        profile.user_id = str(login_uin)
        profile.nickname = nickname or str(login_uin)
        profile.avatar_source = avatar_source
        self._publisher_profile_cache = (
            login_uin,
            now + 10 * 60,
            RenderProfile(
                nickname=profile.nickname,
                user_id=profile.user_id,
                avatar_source=profile.avatar_source,
                time_text="",
            ),
        )
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

    def _schedule_publisher_profile(self, event: AstrMessageEvent) -> asyncio.Task | None:
        if not self.settings.render_publish_result:
            return None
        return asyncio.create_task(self._publisher_render_profile(event))

    async def _publish_result(
        self,
        event: AstrMessageEvent,
        post: PostPayload,
        payload: dict[str, Any],
        *,
        profile_task: asyncio.Task | None = None,
    ):
        text = format_action_result("发布结果", payload)
        if not self.settings.render_publish_result:
            self._stop_event(event)
            return event.plain_result(text)
        try:
            profile = await profile_task if profile_task is not None else await self._publisher_render_profile(event)
        except Exception:
            profile = profile_from_event(event)
        try:
            image_path = await asyncio.to_thread(
                render_publish_result_image,
                post,
                self.data_dir / "rendered_posts",
                profile=profile,
                result=payload,
                width=self.settings.render_result_width,
                remote_timeout=self.settings.render_remote_timeout,
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
        return event.plain_result(f"{text}\n图片路径: {image_path}")

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
                parts.append(f"地址 {url}")
            if parts:
                return f"{exc.message}（{', '.join(parts)}）"
        return f"{exc.message}\n{exc.detail}"

    def _log_tool_call_result(self, payload: dict[str, Any]) -> None:
        try:
            data = json.dumps(
                _redact_for_log(payload),
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            )
        except Exception:
            data = str(_redact_for_log(payload))
        if payload.get("ok"):
            logger.info("qzone llm tool result: %s", data)
        else:
            logger.warning("qzone llm tool result: %s", data)

    @staticmethod
    def _bridge_error_log_payload(tool: str, exc: QzoneBridgeError, arguments: dict[str, Any]) -> dict[str, Any]:
        error: dict[str, Any] = {
            "type": type(exc).__name__,
            "code": exc.code,
            "message": exc.message,
        }
        status_code = getattr(exc, "status_code", None)
        if status_code is not None:
            error["status_code"] = status_code
        return {
            "ok": False,
            "tool": tool,
            "arguments": arguments,
            "error": error,
            "detail": exc.detail,
        }

    @staticmethod
    def _status_error_payload(exc: QzoneBridgeError) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "type": type(exc).__name__,
            "code": exc.code,
            "message": exc.message,
        }
        detail = _safe_for_llm(exc.detail)
        if detail not in (None, {}, []):
            payload["detail"] = detail
        return payload

    async def _status_with_recovery(self) -> dict[str, Any]:
        status = await self.controller.get_status()
        should_start = (
            self.settings.auto_start_daemon
            and status.get("daemon_state") != "ready"
            and int(status.get("cookie_count") or 0) > 0
            and not bool(status.get("needs_rebind"))
        )
        if not should_start:
            return status
        try:
            return await self.controller.ensure_running()
        except QzoneBridgeError as exc:
            try:
                detail_text = json.dumps(_redact_for_log(exc.detail), ensure_ascii=False, default=str)
            except Exception:
                detail_text = str(_redact_for_log(exc.detail))
            logger.warning("qzone daemon status recovery failed: %s detail=%s", exc.message, detail_text)
            fallback = await self.controller.get_status(probe_daemon=False)
            fallback["daemon_start_error"] = self._status_error_payload(exc)
            return fallback


    async def _maybe_await(self, value: Any) -> Any:
        if inspect.isawaitable(value):
            return await value
        return value

    @staticmethod
    def _text_from_llm_response(response: Any) -> str:
        if response is None:
            return ""
        if isinstance(response, str):
            return response.strip()
        for attr in ("completion_text", "text", "content", "message"):
            value = getattr(response, attr, None)
            if isinstance(value, str) and value.strip():
                return value.strip()
        if isinstance(response, dict):
            for key in ("completion_text", "text", "content", "message"):
                value = response.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return ""

    @staticmethod
    def _llm_reply_looks_structured(text: str) -> bool:
        stripped = str(text or "").strip()
        if not stripped:
            return False
        lowered = stripped.lower()
        if stripped.startswith(("{", "[", "```")) or "```json" in lowered:
            return True
        if re.match(r"^\s*(?:result|结果)\s*[:：]", stripped, flags=re.IGNORECASE):
            return True
        structured_markers = (
            '"ok"',
            '"tool"',
            '"raw"',
            '"detail"',
            '"diagnostic"',
            '"status_code"',
            "'ok'",
            "'tool'",
            "'raw'",
            "'detail'",
            "'diagnostic'",
            "'status_code'",
        )
        return sum(1 for marker in structured_markers if marker in lowered) >= 2

    @staticmethod
    def _llm_reply_mentions_forbidden_terms(text: str) -> bool:
        lowered = text.lower()
        return any(term.lower() in lowered for term in LLM_REPLY_FORBIDDEN_TERMS)

    @staticmethod
    def _llm_reply_contradicts_payload(text: str, payload: dict[str, Any]) -> bool:
        if not payload.get("ok") or payload.get("tool") != "qzone_like_post":
            return False
        result = payload.get("result")
        if not isinstance(result, dict) or result.get("verified") is not False:
            return False
        lowered = str(text or "").lower()
        bad_markers = (
            "ok:false",
            "ok: false",
            '"ok":false',
            '"ok": false',
            "'ok':false",
            "'ok': false",
            "status_code",
            "403",
            "failed",
            "failure",
            "unsuccessful",
            "not successful",
            "intercepted",
            "\u5931\u8d25",
            "\u672a\u6210\u529f",
            "\u4e0d\u6210\u529f",
            "\u62e6\u622a",
            "\u672a\u751f\u6548",
        )
        return any(marker in lowered for marker in bad_markers)

    @classmethod
    def _llm_tool_reply_is_safe(cls, text: str, payload: dict[str, Any]) -> bool:
        if not text.strip():
            return False
        if cls._llm_reply_looks_structured(text):
            return False
        if cls._llm_reply_mentions_forbidden_terms(text):
            return False
        return not cls._llm_reply_contradicts_payload(text, payload)

    @staticmethod
    def _llm_tool_reply_summary(payload: dict[str, Any]) -> str:
        if payload.get("ok"):
            result = payload.get("result")
            if payload.get("tool") == "qzone_like_post" and isinstance(result, dict):
                unlike = result.get("action") == "unlike"
                action = "取消点赞" if unlike else "点赞"
                summary = truncate(str(result.get("summary") or "").strip(), 60)
                target = f"「{summary}」" if summary else "这条说说"
                if result.get("already"):
                    return f"{target}之前已经是{action}状态。"
                if result.get("verified") is False:
                    pending = "取消了" if unlike else "点上了"
                    return f"{target}这次已经{pending}，QQ 空间显示可能会慢一点。"
                done = "取消掉了" if unlike else "点好了"
                return f"{target}这次已经{done}。"
            visible = _safe_for_llm(result)
            if isinstance(visible, dict):
                message = visible.get("message") or visible.get("summary") or visible.get("text")
                if message:
                    return str(message)
            return "这件事已经好了。"

        reason = (
            payload.get("public_reason")
            or payload.get("public_message")
            or payload.get("message")
            or ""
        )
        error = payload.get("error")
        if not reason and isinstance(error, dict):
            reason = error.get("message") or ""
        reason_text = _public_error_reason(reason)
        return f"现在还没办法继续。可参考的简短原因：{reason_text}"

    @staticmethod
    def _llm_error_fallback_text(message: Any) -> str:
        reason = _public_error_reason(message)
        lowered = reason.lower()
        if "参考图" in reason or "人设" in reason:
            return "这会儿还没法弄，等参考内容准备好再来吧。"
        if "cookie" in lowered or "登录" in reason or "登入" in reason:
            return "这会儿还没法动空间，登录状态得先补一下。"
        if "权限" in reason or "管理员" in reason:
            return "这个我现在不能直接动，得让管理员来。"
        return "这会儿还没法弄，晚点再试一下吧。"

    async def _current_provider_id(self, event: AstrMessageEvent) -> Any | None:
        context = getattr(self, "_context", None) or getattr(self, "context", None)
        getter = getattr(context, "get_current_chat_provider_id", None)
        if not callable(getter):
            return None
        umo = getattr(event, "unified_msg_origin", None)
        attempts: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        if umo is not None:
            attempts.append(((), {"umo": umo}))
            attempts.append(((umo,), {}))
        attempts.append(((), {}))
        for args, kwargs in attempts:
            try:
                provider_id = await self._maybe_await(getter(*args, **kwargs))
            except TypeError:
                continue
            except Exception as exc:
                logger.debug("qzone llm provider id lookup failed: %s", exc)
                return None
            if provider_id:
                return provider_id
        return None

    async def _ask_llm_tool_reply(self, event: AstrMessageEvent, payload: dict[str, Any], fallback: str) -> str:
        summary = self._llm_tool_reply_summary(payload)
        prompt = (
            "下面这句只是给你理解刚才发生了什么，不要照抄，也不要复述成固定格式：\n"
            f"{summary}\n"
            "请沿用当前聊天里的人设和说话习惯，给用户回一句自然中文，像真人顺口聊天。\n"
            "要求：\n"
            "- 一句话为主，最多两句；可以很短。\n"
            "- 不要输出 JSON、Markdown 代码块、字段解释、前缀或标签。\n"
            "- 不要提工具、系统、后台、API、参数、指令、命令、错误代码、状态码或内部流程。\n"
            "- 不要说“生成”“绘制”“绘图”“渲染”“处理完成”“任务完成”“已发送”。\n"
            "- 失败或暂时不可用时，只生活化地说现在还不行或晚点再来，不要展开技术原因。\n"
            "- 成功时随口收尾一句就好；如果只是显示同步慢，不要说成失败。\n"
        )
        system_prompt = (
            "沿用当前聊天角色和语气。你只负责把结果变成自然口语回复，不能暴露任何内部实现信息。"
        )
        try:
            text = await self._llm_adapter().generate_text(
                event,
                prompt,
                system_prompt=system_prompt,
                prefer_current_provider=True,
            )
        except Exception as exc:
            logger.debug("qzone llm tool reply failed: %s", exc)
        else:
            if self._llm_tool_reply_is_safe(text, payload):
                return text
            if text:
                logger.warning("discarded unsafe qzone llm tool reply: %s", truncate(text, 300))

        return fallback

    def _llm_like_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        visible_payload = _safe_for_llm(payload)
        if visible_payload.get("verified") is False:
            visible_payload.pop("verification", None)
            visible_payload["accepted"] = True
            visible_payload["operation_status"] = "accepted_pending_verification"
            visible_payload["verification_meaning"] = "QQ readback is stale; do not treat this as failure."
        elif visible_payload.get("verified") is True:
            visible_payload["accepted"] = True
            visible_payload["operation_status"] = (
                "already_in_target_state" if visible_payload.get("already") else "verified_success"
            )
        return {
            "ok": True,
            "tool": "qzone_like_post",
            "result": visible_payload,
            "reply_guidance": [
                "Reply in Chinese natural language only.",
                "Do not output JSON or expose internal fields.",
                "If verified is false but ok is true, say the request was accepted and QQ readback may sync shortly; do not call it a failure.",
                "Only describe failure when ok is false.",
            ],
        }

    @staticmethod
    def _like_fallback_text(payload: dict[str, Any]) -> str:
        unlike = payload.get("action") == "unlike"
        action = "\u53d6\u6d88\u70b9\u8d5e" if unlike else "\u70b9\u8d5e"
        summary = truncate(str(payload.get("summary") or "").strip(), 60)
        target = f"\u300c{summary}\u300d" if summary else "\u8fd9\u6761\u8bf4\u8bf4"
        if payload.get("verified"):
            if payload.get("already"):
                return f"{target}\u4e4b\u524d\u5c31\u5df2\u7ecf{action}\u4e86\u3002"
            done = "\u53d6\u6d88\u6389" if unlike else "\u70b9\u597d"
            return f"{target}\u6211\u5e2e\u4f60{done}\u4e86\u3002"
        pending = "\u53d6\u6d88\u4e86" if unlike else "\u70b9\u4e0a\u4e86"
        return f"{target}\u6211\u5148\u5e2e\u4f60{pending}\uff0cQQ \u7a7a\u95f4\u90a3\u8fb9\u53ef\u80fd\u8981\u7b49\u4e00\u4f1a\u513f\u624d\u663e\u793a\u3002"

    def _llm_error_payload(self, tool: str, exc: QzoneBridgeError) -> dict[str, Any]:
        return {
            "ok": False,
            "public_reason": _public_error_reason(exc.message),
            "reply_guidance": "Use a short natural reply in the active persona. Do not expose error details.",
        }

    async def _ensure_daemon(self, *, allow_needs_rebind: bool = False) -> None:
        status = await self.controller.get_status()
        if status.get("needs_rebind") and not allow_needs_rebind:
            raise QzoneNeedsRebind("QQ 空间登录态已失效，需要重新绑定 Cookie")
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

    @staticmethod
    def _event_text(event: AstrMessageEvent) -> str:
        value = getattr(event, "message_str", None)
        if isinstance(value, str) and value.strip():
            return value.strip()
        message_obj = getattr(event, "message_obj", None)
        parts: list[str] = []
        for item in getattr(message_obj, "message", []) or []:
            if isinstance(item, str):
                parts.append(item)
                continue
            text = getattr(item, "text", None)
            if isinstance(text, str):
                parts.append(text)
                continue
            data = getattr(item, "data", None)
            if isinstance(data, dict) and isinstance(data.get("text"), str):
                parts.append(data["text"])
        return "".join(parts).strip()

    @staticmethod
    def _message_after_command(text: str, names: tuple[str, ...]) -> str:
        text = str(text or "").strip()
        text = re.sub(r"^(?:[!/／]\s*)", "", text).strip()
        for name in sorted(names, key=len, reverse=True):
            if text == name:
                return ""
            if text.startswith(name):
                rest = text[len(name):]
                if not rest or rest[0].isspace() or rest[0] in {":", "："}:
                    return rest.lstrip(" \t:：")
        return text

    def _sender_name(self, event: AstrMessageEvent) -> str:
        for getter in ("get_sender_name", "get_sender_nickname"):
            method = getattr(event, getter, None)
            if callable(method):
                try:
                    value = method()
                except Exception:
                    value = None
                if value:
                    return str(value)
        message_obj = getattr(event, "message_obj", None)
        sender = getattr(message_obj, "sender", None)
        for attr in ("nickname", "card", "name"):
            value = getattr(sender, attr, None)
            if value:
                return str(value)
        return str(self._sender_id(event) or "")

    def _group_id(self, event: AstrMessageEvent) -> int:
        getter = getattr(event, "get_group_id", None)
        if callable(getter):
            try:
                value = getter()
                if value:
                    return int(value)
            except Exception:
                pass
        message_obj = getattr(event, "message_obj", None)
        try:
            return int(getattr(message_obj, "group_id", 0) or 0)
        except Exception:
            return 0

    def _self_id(self, event: AstrMessageEvent) -> int:
        getter = getattr(event, "get_self_id", None)
        if callable(getter):
            try:
                value = getter()
                if value:
                    return int(value)
            except Exception:
                pass
        try:
            status = getattr(self.controller, "store", None).read()  # type: ignore[union-attr]
            return int(status.session.uin or 0)
        except Exception:
            return 0

    @staticmethod
    def _at_uins(event: AstrMessageEvent, text: str = "") -> list[int]:
        uins: list[int] = []
        message_obj = getattr(event, "message_obj", None)
        for item in getattr(message_obj, "message", []) or []:
            data = getattr(item, "data", None)
            if isinstance(data, dict):
                value = data.get("qq") or data.get("uin") or data.get("user_id")
                if value and str(value).isdigit():
                    uins.append(int(value))
            for attr in ("qq", "uin", "user_id"):
                value = getattr(item, attr, None)
                if value and str(value).isdigit():
                    uins.append(int(value))
        for match in re.finditer(r"\[CQ:at,qq=(\d+)[^\]]*\]|@(\d{5,})", text):
            value = match.group(1) or match.group(2)
            if value:
                uins.append(int(value))
        deduped: list[int] = []
        for uin in uins:
            if uin not in deduped:
                deduped.append(uin)
        return deduped

    def _parse_target_range(self, event: AstrMessageEvent, names: tuple[str, ...]) -> tuple[int, int, int]:
        selection = parse_post_selection(self._event_text(event), names)
        return selection.target_uin, selection.start, selection.end

    def _selection_for_event(self, event: AstrMessageEvent, names: tuple[str, ...]) -> PostSelection:
        return parse_post_selection(self._event_text(event), names)

    async def _posts_for_selection(
        self,
        selection: PostSelection,
        *,
        target_id: int | None = None,
        with_detail: bool = False,
        no_commented: bool = False,
        no_self: bool = False,
        login_uin: int | None = None,
    ) -> list[QzonePost]:
        if target_id is not None:
            selection.target_uin = int(target_id)
        return await self._post_service().resolve_posts(
            selection,
            with_detail=with_detail,
            no_commented=no_commented,
            no_self=no_self,
            login_uin=self._self_id_placeholder(login_uin),
        )

    @staticmethod
    def _self_id_placeholder(login_uin: int | None) -> int:
        return int(login_uin or 0)

    async def _posts_for_event(
        self,
        event: AstrMessageEvent,
        names: tuple[str, ...],
        *,
        target_id: int | None = None,
        with_detail: bool = False,
        no_commented: bool = False,
        no_self: bool = False,
    ) -> list[QzonePost]:
        return await self._posts_for_selection(
            self._selection_for_event(event, names),
            target_id=target_id,
            with_detail=with_detail,
            no_commented=no_commented,
            no_self=no_self,
            login_uin=self._self_id(event),
        )

    @staticmethod
    def _format_posts(posts: list[QzonePost], *, detail: bool = False) -> str:
        if not posts:
            return "没有找到可见说说。"
        if detail:
            return "\n\n".join(post.detail_text(post.local_id) for post in posts)
        return "\n\n".join(post.brief(post.local_id) for post in posts)

    @staticmethod
    def _format_visitors(payload: dict[str, Any]) -> str:
        items = payload.get("items") or []
        if not items:
            return "暂时没有访客记录。"
        lines = ["最近访客"]
        for index, item in enumerate(items[:20], 1):
            if not isinstance(item, dict):
                continue
            name = item.get("nickname") or item.get("uin") or "-"
            uin = item.get("uin") or ""
            lines.append(f"{index}. {name} {uin}".strip())
        return "\n".join(lines)

    def _draft_id_from_event(self, event: AstrMessageEvent, names: tuple[str, ...]) -> tuple[int, str]:
        raw = self._message_after_command(self._event_text(event), names)
        parts = raw.split(maxsplit=1)
        if not parts:
            return -1, ""
        try:
            return int(parts[0]), parts[1] if len(parts) > 1 else ""
        except ValueError:
            return -1, raw

    def _collect_target_post_payload(self, event: AstrMessageEvent, content: str, prefixes: tuple[str, ...]) -> PostPayload:
        return collect_post_payload(
            event,
            fallback_content=content,
            include_event_text=True,
            command_prefixes=prefixes,
        )

    def _create_draft(self, event: AstrMessageEvent, post: PostPayload, *, anonymous: bool = False) -> DraftPost:
        return self.drafts.add(
            author_uin=self._sender_id(event),
            author_name=self._sender_name(event),
            group_id=self._group_id(event),
            content=post.content,
            media=[item.to_dict() for item in post.media],
            anonymous=anonymous,
        )

    async def _notify_review_target(self, event: AstrMessageEvent, draft: DraftPost, message: str) -> None:
        bot = self._capture_onebot_client(event)
        if bot is None:
            return
        text = f"{message}\n{draft.preview(include_private=True)}"
        rendered = await self._render_markdown_image(text, subdir="drafts")
        outgoing: Any = text
        if rendered is not None:
            outgoing = [
                {"type": "text", "data": {"text": f"{message}\n"}},
                {"type": "image", "data": {"file": self._onebot_file_uri(rendered)}},
            ]
        try:
            if self.settings.manage_group and hasattr(bot, "send_group_msg"):
                result = bot.send_group_msg(group_id=self.settings.manage_group, message=outgoing)
                await self._maybe_await(result)
                return
            if hasattr(bot, "send_private_msg"):
                for admin in self.settings.admin_uins:
                    result = bot.send_private_msg(user_id=admin, message=outgoing)
                    await self._maybe_await(result)
        except Exception as exc:
            logger.debug("qzone draft review notification failed: %s", exc)

    async def _notify_draft_author(self, event: AstrMessageEvent, draft: DraftPost, message: str) -> None:
        bot = self._capture_onebot_client(event)
        if bot is None or not draft.author_uin:
            return
        try:
            if draft.group_id and hasattr(bot, "send_group_msg"):
                result = bot.send_group_msg(group_id=draft.group_id, message=message)
            elif hasattr(bot, "send_private_msg"):
                result = bot.send_private_msg(user_id=draft.author_uin, message=message)
            else:
                return
            await self._maybe_await(result)
        except Exception as exc:
            logger.debug("qzone draft author notification failed: %s", exc)

    def _draft_publish_content(self, draft: DraftPost) -> str:
        content = draft.content.strip()
        if not self.settings.show_name:
            return content
        name = "匿名者" if draft.anonymous else (draft.author_name or str(draft.author_uin or "未知用户"))
        header = f"【来自 {name} 的投稿】"
        return "\n\n".join(part for part in (header, content) if part)

    def _auto_comment_state_path(self) -> Path:
        return self.data_dir / "auto_comment_state.json"

    def _load_auto_comment_keys(self) -> set[str]:
        path = self._auto_comment_state_path()
        if not path.exists():
            return set()
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return set()
        items = payload.get("commented") if isinstance(payload, dict) else payload
        if not isinstance(items, list):
            return set()
        return {str(item) for item in items if item}

    def _save_auto_comment_keys(self, keys: set[str]) -> None:
        path = self._auto_comment_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"commented": sorted(keys)[-500:]}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def _auto_comment_key(self, post: QzonePost | FeedEntry) -> str:
        return f"{int(getattr(post, 'hostuin', 0) or 0)}:{getattr(post, 'fid', '')}"

    async def _chat_history_context(self, event: AstrMessageEvent | None = None) -> str:
        bot = self._capture_onebot_client(event)
        if bot is None:
            return ""
        group_id = self._group_id(event) if event is not None else 0
        if group_id and str(group_id) in self.settings.ignore_groups:
            return ""
        if not group_id:
            group_id = await self._pick_history_group_id(bot)
        if not group_id or str(group_id) in self.settings.ignore_groups:
            return ""
        lines = await self._fetch_group_history_lines(bot, group_id)
        return "\n".join(lines[-self.settings.post_max_msg :])

    async def _pick_history_group_id(self, bot: Any) -> int:
        getter = getattr(bot, "get_group_list", None)
        if not callable(getter):
            return 0
        try:
            groups = await self._maybe_await(getter())
        except Exception:
            return 0
        candidates: list[int] = []
        for item in groups or []:
            if not isinstance(item, dict):
                continue
            group_id = str(item.get("group_id") or "")
            if group_id.isdigit() and group_id not in self.settings.ignore_groups:
                candidates.append(int(group_id))
        return random.choice(candidates) if candidates else 0

    async def _fetch_group_history_lines(self, bot: Any, group_id: int) -> list[str]:
        lines: list[str] = []
        message_seq = 0
        max_messages = max(1, int(self.settings.post_max_msg or 500))
        while len(lines) < max_messages:
            try:
                result = await self._get_group_history_page(bot, group_id, message_seq)
            except Exception as exc:
                logger.debug("qzone group history fetch failed: %s", exc)
                break
            messages = result.get("messages") if isinstance(result, dict) else []
            if not isinstance(messages, list) or not messages:
                break
            for message in messages:
                line = self._history_message_to_line(message)
                if line:
                    lines.append(line)
                    if len(lines) >= max_messages:
                        break
            first = messages[0] if isinstance(messages[0], dict) else {}
            next_seq = int(first.get("message_id") or first.get("message_seq") or 0)
            if not next_seq or next_seq == message_seq:
                break
            message_seq = next_seq
        return lines

    async def _get_group_history_page(self, bot: Any, group_id: int, message_seq: int) -> dict[str, Any]:
        api = getattr(bot, "api", None)
        call_action = getattr(api, "call_action", None)
        payload = {
            "group_id": int(group_id),
            "message_seq": int(message_seq or 0),
            "count": min(200, max(1, int(self.settings.post_max_msg or 500))),
            "reverseOrder": True,
        }
        if callable(call_action):
            return await self._maybe_await(call_action("get_group_msg_history", **payload))
        getter = getattr(bot, "get_group_msg_history", None)
        if callable(getter):
            return await self._maybe_await(getter(**payload))
        return {}

    def _history_message_to_line(self, message: Any) -> str:
        if not isinstance(message, dict):
            return ""
        sender = message.get("sender") if isinstance(message.get("sender"), dict) else {}
        sender_id = str(sender.get("user_id") or message.get("user_id") or "")
        if sender_id and sender_id in self.settings.ignore_users:
            return ""
        nickname = str(sender.get("card") or sender.get("nickname") or sender_id or "用户")
        parts: list[str] = []
        for segment in message.get("message") or []:
            if not isinstance(segment, dict) or segment.get("type") != "text":
                continue
            data = segment.get("data") if isinstance(segment.get("data"), dict) else {}
            text = str(data.get("text") or "").strip()
            if text:
                parts.append(text)
        content = "".join(parts).strip()
        if not content:
            return ""
        return f"{nickname}: {content}"

    async def _generate_text(
        self,
        event: AstrMessageEvent,
        prompt: str,
        *,
        provider_id: str = "",
        system_prompt: str = "",
    ) -> str:
        return await self._llm_adapter().generate_text(
            event,
            prompt,
            provider_id=provider_id,
            system_prompt=system_prompt,
        )

    async def _generate_post_text(self, event: AstrMessageEvent, topic: str = "") -> str:
        history = await self._chat_history_context(event)
        return await self._llm_adapter().generate_post_text(event, topic, history=history)

    async def _generate_comment_text(self, event: AstrMessageEvent, post: QzonePost) -> str:
        return await self._llm_adapter().generate_comment_text(event, post)

    async def _generate_reply_text(self, event: AstrMessageEvent, post: QzonePost, comment: QzoneComment) -> str:
        return await self._llm_adapter().generate_reply_text(event, post, comment)

    @staticmethod
    def _cron_delay_seconds(cron: str, offset_seconds: int) -> float:
        parts = str(cron or "").split()
        if len(parts) < 2:
            return 0.0
        try:
            minute = int(parts[0])
            hour = int(parts[1])
        except ValueError:
            return 0.0
        now = datetime.now()
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        offset = int(offset_seconds or 0)
        if offset > 0:
            target += timedelta(seconds=random.randint(-offset, offset))
            if target <= now:
                target = now + timedelta(seconds=1)
        return max(1.0, (target - now).total_seconds())

    def _start_scheduled_tasks(self) -> None:
        if self._scheduled_tasks:
            return
        if self.settings.publish_cron:
            self._scheduled_tasks.append(
                asyncio.create_task(
                    self._scheduled_loop("publish", self.settings.publish_cron, self.settings.publish_offset, self._auto_publish_once)
                )
            )
        if self.settings.comment_cron:
            self._scheduled_tasks.append(
                asyncio.create_task(
                    self._scheduled_loop("comment", self.settings.comment_cron, self.settings.comment_offset, self._auto_comment_once)
                )
            )

    async def _scheduled_loop(self, name: str, cron: str, offset: int, action: Any) -> None:
        while True:
            delay = self._cron_delay_seconds(cron, offset)
            if delay <= 0:
                return
            await asyncio.sleep(delay)
            try:
                await action()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("qzone scheduled %s failed: %s", name, exc)

    async def _auto_publish_once(self) -> None:
        fake_event = None
        text = await self._generate_post_text(fake_event, "")  # type: ignore[arg-type]
        if not text.strip():
            return
        await self._ensure_cookie_ready()
        await self._ensure_daemon()
        await self.controller.publish_post(content=text.strip(), content_sanitized=True)

    async def _auto_comment_once(self) -> None:
        await self._ensure_cookie_ready()
        await self._ensure_daemon()
        payload = await self.controller.list_feeds(hostuin=0, limit=5)
        entries = self._to_feed_entries(payload)
        commented_keys = self._load_auto_comment_keys()
        login_uin = 0
        try:
            status = await self.controller.get_status(probe_daemon=False)
            login_uin = int(status.get("login_uin") or 0)
        except Exception:
            pass
        for entry in entries:
            if login_uin and entry.hostuin == login_uin:
                continue
            key = self._auto_comment_key(entry)
            if key in commented_keys:
                continue
            detail_payload: dict[str, Any] | None = None
            try:
                detail_payload = await self.controller.detail_feed(hostuin=entry.hostuin, fid=entry.fid, appid=entry.appid)
                entry_data = detail_payload.get("entry")
                if isinstance(entry_data, dict):
                    detail_entry = FeedEntry(**entry_data)
                    if detail_entry.fid == entry.fid and detail_entry.hostuin == entry.hostuin:
                        entry = detail_entry
            except Exception as exc:
                logger.debug("qzone scheduled comment detail check failed: %s", exc)
            post = post_from_entry(entry, detail=(detail_payload or {}).get("raw"), local_id=0)
            if detail_payload and detail_payload.get("comments"):
                post.comments = [
                    QzoneComment(
                        commentid=str(item.get("commentid") or ""),
                        uin=int(item.get("uin") or 0),
                        nickname=str(item.get("nickname") or ""),
                        content=str(item.get("content") or ""),
                    )
                    for item in detail_payload.get("comments") or []
                    if isinstance(item, dict)
                ]
            if login_uin and any(comment.uin == login_uin for comment in post.comments):
                commented_keys.add(key)
                self._save_auto_comment_keys(commented_keys)
                continue
            self._post_store().upsert(post)
            text = await self._generate_comment_text(None, post)  # type: ignore[arg-type]
            if not text.strip():
                continue
            await self._post_service().comment_post(post, text.strip())
            if self.settings.like_when_comment:
                await self._post_service().like_post(post)
            commented_keys.add(key)
            self._save_auto_comment_keys(commented_keys)
            break

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
        return "没有从 AstrBot 拿到 aiocqhttp(OneBot v11) 客户端，请先用 /qzone bind 手动绑定 Cookie。"

    async def _auto_bind_cookie(
        self,
        event: AstrMessageEvent | None = None,
        *,
        force: bool = False,
        source: str = "aiocqhttp",
    ) -> dict[str, Any]:
        async with self._get_cookie_lock():
            if not self.settings.auto_bind_cookie and not force:
                raise QzoneCookieAcquireError("自动绑定 Cookie 未开启")

            bot = self._capture_onebot_client(event)
            if bot is None:
                raise QzoneCookieAcquireError(f"无法从 OneBot 获取 Cookie。{self._cookie_binding_hint()}")

            try:
                status = await self.controller.get_status(probe_daemon=False)
            except QzoneBridgeError:
                status = {}

            if not force and status and int(status.get("cookie_count") or 0) > 0 and not bool(status.get("needs_rebind")):
                return status

            cookie_text = await fetch_cookie_text(bot, domain=self.settings.cookie_domain)
            if not cookie_text:
                raise QzoneCookieAcquireError(f"OneBot 没有返回可用 Cookie。{self._cookie_binding_hint()}")

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

    async def initialize(self):
        if self.settings.cookies_str:
            try:
                await self.controller.bind_cookie_local(self.settings.cookies_str, source="config")
            except QzoneBridgeError as exc:
                logger.warning("qzone config cookie bind failed: %s", exc)
        self._start_scheduled_tasks()

    @filter.command_group("qzone")
    def qzone(self):
        pass

    @filter.on_platform_loaded()
    async def qzone_on_platform_loaded(self):
        self._start_scheduled_tasks()
        await self._bootstrap_auto_bind("platform load")

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    async def qzone_capture_aiocqhttp_client(self, event: AstrMessageEvent):
        self._capture_onebot_client(event)
        if self.settings.read_prob <= 0 or random.random() >= self.settings.read_prob:
            return
        group_id = str(self._group_id(event) or "")
        sender_id = str(self._sender_id(event) or "")
        if group_id and group_id in self.settings.ignore_groups:
            return
        if sender_id and sender_id in self.settings.ignore_users:
            return
        try:
            await self._ensure_cookie_ready(event)
            await self._ensure_daemon()
            posts = await self._posts_for_event(
                event,
                ("看说说", "查看说说"),
                target_id=int(sender_id or 0),
                no_commented=True,
                no_self=True,
            )
            if not posts:
                return
            post = posts[0]
            content = await self._generate_comment_text(event, post)
            if not content.strip():
                return
            await self._post_service().comment_post(post, content.strip())
            if self.settings.like_when_comment:
                await self._post_service().like_post(post)
        except Exception as exc:
            logger.debug("qzone probabilistic read/comment failed: %s", exc)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("查看访客")
    async def view_visitor(self, event: AstrMessageEvent):
        try:
            await self._ensure_cookie_ready(event)
            await self._ensure_daemon()
            payload = await self.controller.view_visitors()
        except QzoneBridgeError as exc:
            yield self._command_result(event, self._error_text(exc))
            return
        yield await self._markdown_result(event, self._format_visitors(payload), subdir="visitors")

    @filter.command("看说说", alias={"查看说说"})
    async def view_feed(self, event: AstrMessageEvent):
        try:
            await self._ensure_cookie_ready(event)
            await self._ensure_daemon()
            posts = await self._posts_for_event(event, ("看说说", "查看说说"), with_detail=True)
        except QzoneBridgeError as exc:
            yield self._command_result(event, self._error_text(exc))
            return
        yield await self._markdown_result(event, self._format_posts(posts, detail=True), subdir="posts")

    @filter.command("评说说", alias={"评论说说", "读说说"})
    async def comment_feed(self, event: AstrMessageEvent):
        try:
            await self._ensure_cookie_ready(event)
            await self._ensure_daemon()
            selection = self._selection_for_event(event, ("评说说", "评论说说", "读说说"))
            posts = await self._posts_for_selection(
                selection,
                with_detail=True,
                no_commented=True,
                no_self=True,
                login_uin=self._self_id(event),
            )
            if not posts:
                yield self._command_result(event, "没有找到适合评论的说说。")
                return
            lines: list[str] = []
            for post in posts:
                content = selection.comment_text or await self._generate_comment_text(event, post)
                if not content.strip():
                    content = "挺有意思的。"
                await self._post_service().comment_post(post, content)
                if self.settings.like_when_comment:
                    await self._post_service().like_post(post)
                lines.append(f"已评论第 {post.local_id} 条：{truncate(content, 60)}")
        except QzoneBridgeError as exc:
            yield self._command_result(event, self._error_text(exc))
            return
        yield await self._markdown_result(event, "\n".join(lines), subdir="posts")

    @filter.command("赞说说")
    async def like_feed(self, event: AstrMessageEvent):
        try:
            await self._ensure_cookie_ready(event)
            await self._ensure_daemon()
            posts = await self._posts_for_event(event, ("赞说说",))
            if not posts:
                yield self._command_result(event, "没有找到可点赞的说说。")
                return
            lines: list[str] = []
            for post in posts:
                payload = await self._post_service().like_post(post)
                lines.append(format_like_result(payload))
        except QzoneBridgeError as exc:
            yield self._command_result(event, self._error_text(exc))
            return
        yield await self._markdown_result(event, "\n".join(lines), subdir="posts")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("发说说")
    async def publish_feed(self, event: AstrMessageEvent, content: str = ""):
        self._stop_event(event)
        post = self._collect_target_post_payload(event, content, ("发说说",))
        profile_task: asyncio.Task | None = None
        try:
            await self._ensure_cookie_ready(event)
            profile_task = self._schedule_publisher_profile(event)
            payload = await self.controller.publish_post(
                content=post.content,
                media=[item.to_dict() for item in post.media],
                content_sanitized=True,
            )
            if payload.get("fid"):
                self._post_store().upsert(
                    QzonePost(
                        hostuin=self._self_id(event),
                        fid=str(payload.get("fid") or ""),
                        appid=311,
                        summary=post.content,
                        images=[str(item.source) for item in post.media],
                    )
                )
        except QzoneBridgeError as exc:
            if profile_task is not None:
                profile_task.cancel()
            yield self._command_result(event, self._error_text(exc))
            return
        yield await self._publish_result(event, post, payload, profile_task=profile_task)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("写说说", alias={"写稿"})
    async def write_feed(self, event: AstrMessageEvent):
        topic = self._message_after_command(self._event_text(event), ("写说说", "写稿"))
        post = self._collect_target_post_payload(event, topic, ("写说说", "写稿"))
        try:
            text = await self._generate_post_text(event, post.content)
        except Exception as exc:
            logger.warning("qzone write feed generation failed: %s", exc)
            text = ""
        post.content = text.strip() or post.content
        if not post.content.strip() and not post.media:
            yield self._command_result(event, "说说生成失败。")
            return
        draft = self._create_draft(event, post, anonymous=False)
        await self._notify_review_target(event, draft, "有一条 AI 写稿等待审核")
        yield await self._markdown_result(event, f"已生成稿件 #{draft.id}，可用 过稿 {draft.id} 发布。", subdir="drafts")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("删说说")
    async def delete_feed(self, event: AstrMessageEvent):
        try:
            await self._ensure_cookie_ready(event)
            await self._ensure_daemon()
            posts = await self._posts_for_event(
                event,
                ("删说说",),
                target_id=self._self_id(event),
            )
            if not posts:
                yield self._command_result(event, "没有找到可删除的说说。")
                return
            lines: list[str] = []
            for post in posts:
                payload = await self.controller.delete_post(fid=post.fid, appid=post.appid)
                lines.append(format_action_result("删除结果", payload))
        except QzoneBridgeError as exc:
            yield self._command_result(event, self._error_text(exc))
            return
        yield await self._markdown_result(event, "\n".join(lines), subdir="posts")

    @filter.command("回评", alias={"回复评论"})
    async def reply_comment(self, event: AstrMessageEvent):
        raw = self._message_after_command(self._event_text(event), ("回评", "回复评论"))
        parts = raw.split()
        post_id = int(parts[0]) if parts and re.fullmatch(r"-?\d+", parts[0]) else -1
        comment_index = int(parts[1]) if len(parts) > 1 and re.fullmatch(r"-?\d+", parts[1]) else -1
        saved = self._post_store().get(post_id)
        draft = None if saved else self.drafts.get(post_id)
        if saved is None and (draft is None or not draft.published_fid):
            yield self._command_result(event, f"稿件 #{post_id} 不存在或还没有发布。")
            return
        try:
            await self._ensure_cookie_ready(event)
            await self._ensure_daemon()
            status = await self.controller.get_status(probe_daemon=False)
            if saved is not None:
                hostuin = saved.hostuin
                fid = saved.fid
                appid = saved.appid
            else:
                hostuin = int(status.get("login_uin") or self._self_id(event) or 0)
                fid = draft.published_fid  # type: ignore[union-attr]
                appid = 311
            detail = await self.controller.detail_feed(hostuin=hostuin, fid=fid, appid=appid)
            entry = FeedEntry(**detail["entry"])
            post = post_from_entry(entry, detail=detail.get("raw"), local_id=post_id)
            if detail.get("comments"):
                post.comments = [
                    QzoneComment(
                        commentid=str(item.get("commentid") or ""),
                        uin=int(item.get("uin") or 0),
                        nickname=str(item.get("nickname") or ""),
                        content=str(item.get("content") or ""),
                    )
                    for item in detail.get("comments") or []
                    if isinstance(item, dict)
                ]
            self._post_store().upsert(post)
            login_uin = int(status.get("login_uin") or 0)
            comments = [item for item in post.comments if not login_uin or item.uin != login_uin]
            if not comments:
                yield self._command_result(event, "这条说说暂时没有可回复的评论。")
                return
            comment = comments[comment_index] if 0 <= comment_index < len(comments) else comments[-1]
            content = await self._generate_reply_text(event, post, comment)
            if not content.strip():
                content = "收到啦。"
            payload = await self.controller.reply_comment(
                hostuin=hostuin,
                fid=fid,
                commentid=comment.commentid,
                comment_uin=comment.uin,
                content=content.strip(),
                appid=post.appid,
            )
        except QzoneBridgeError as exc:
            yield self._command_result(event, self._error_text(exc))
            return
        yield await self._markdown_result(event, format_action_result("回复结果", payload), subdir="posts")

    @filter.command("投稿")
    async def contribute_post(self, event: AstrMessageEvent, content: str = ""):
        post = self._collect_target_post_payload(event, content, ("投稿",))
        if not post.content.strip() and not post.media:
            yield self._command_result(event, "投稿内容或图片不能为空。")
            return
        draft = self._create_draft(event, post, anonymous=False)
        await self._notify_review_target(event, draft, "收到一条投稿")
        yield await self._markdown_result(event, f"投稿已收到，稿件编号 #{draft.id}。", subdir="drafts")

    @filter.command("匿名投稿")
    async def anon_contribute_post(self, event: AstrMessageEvent, content: str = ""):
        post = self._collect_target_post_payload(event, content, ("匿名投稿",))
        if not post.content.strip() and not post.media:
            yield self._command_result(event, "投稿内容或图片不能为空。")
            return
        draft = self._create_draft(event, post, anonymous=True)
        await self._notify_review_target(event, draft, "收到一条匿名投稿")
        yield await self._markdown_result(event, f"匿名投稿已收到，稿件编号 #{draft.id}。", subdir="drafts")

    @filter.command("撤稿")
    async def recall_post(self, event: AstrMessageEvent):
        draft_id, _ = self._draft_id_from_event(event, ("撤稿",))
        draft = self.drafts.get(draft_id)
        if draft is None:
            yield self._command_result(event, f"稿件 #{draft_id} 不存在。")
            return
        if draft.author_uin != self._sender_id(event) and not self._is_admin(event):
            yield self._command_result(event, "只能撤回自己的投稿。")
            return
        if draft.status != "pending":
            yield self._command_result(event, f"稿件 #{draft.id} 当前是 {draft.status}，不能撤回。")
            return
        draft.status = "recalled"
        self.drafts.save(draft)
        yield await self._markdown_result(event, f"稿件 #{draft.id} 已撤回。", subdir="drafts")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("看稿", alias={"查看稿件"})
    async def view_post(self, event: AstrMessageEvent):
        draft_id, _ = self._draft_id_from_event(event, ("看稿", "查看稿件"))
        if draft_id > 0:
            draft = self.drafts.get(draft_id)
            text = draft.preview(include_private=True) if draft else f"稿件 #{draft_id} 不存在。"
        else:
            drafts = self.drafts.list(status="pending")[-10:]
            text = "\n\n".join(draft.preview(include_private=True) for draft in drafts) or "暂无待审核稿件。"
        yield await self._markdown_result(event, text, subdir="drafts")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("过稿", alias={"通过稿件", "通过投稿"})
    async def approve_post(self, event: AstrMessageEvent):
        draft_id, _ = self._draft_id_from_event(event, ("过稿", "通过稿件", "通过投稿"))
        draft = self.drafts.get(draft_id)
        if draft is None:
            yield self._command_result(event, f"稿件 #{draft_id} 不存在。")
            return
        if draft.status != "pending":
            yield self._command_result(event, f"稿件 #{draft.id} 当前是 {draft.status}，不能过稿。")
            return
        post = PostPayload(content=self._draft_publish_content(draft), media=normalize_media_list(draft.media))
        profile_task: asyncio.Task | None = None
        try:
            await self._ensure_cookie_ready(event)
            profile_task = self._schedule_publisher_profile(event)
            payload = await self.controller.publish_post(
                content=post.content,
                media=[item.to_dict() for item in post.media],
                content_sanitized=True,
            )
            draft.status = "published"
            draft.published_fid = str(payload.get("fid") or "")
            self.drafts.save(draft)
            if draft.published_fid:
                login_uin = 0
                try:
                    login_uin = int((await self.controller.get_status(probe_daemon=False)).get("login_uin") or 0)
                except Exception:
                    login_uin = self._self_id(event)
                saved_post = QzonePost(
                    hostuin=login_uin,
                    fid=draft.published_fid,
                    appid=311,
                    summary=post.content,
                    nickname="",
                    images=[str(item.source) for item in post.media],
                )
                self._post_store().upsert(saved_post)
            await self._notify_draft_author(event, draft, f"你的投稿 #{draft.id} 已通过并发布。")
        except QzoneBridgeError as exc:
            if profile_task is not None:
                profile_task.cancel()
            yield self._command_result(event, self._error_text(exc))
            return
        yield await self._publish_result(event, post, payload, profile_task=profile_task)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("拒稿", alias={"拒绝稿件", "拒绝投稿"})
    async def reject_post(self, event: AstrMessageEvent):
        draft_id, reason = self._draft_id_from_event(event, ("拒稿", "拒绝稿件", "拒绝投稿"))
        draft = self.drafts.get(draft_id)
        if draft is None:
            yield self._command_result(event, f"稿件 #{draft_id} 不存在。")
            return
        if draft.status != "pending":
            yield self._command_result(event, f"稿件 #{draft.id} 当前是 {draft.status}，不能拒稿。")
            return
        draft.status = "rejected"
        draft.reject_reason = reason or "未填写原因"
        self.drafts.save(draft)
        await self._notify_draft_author(event, draft, f"你的投稿 #{draft.id} 未通过：{draft.reject_reason}")
        yield await self._markdown_result(event, f"稿件 #{draft.id} 已拒绝。", subdir="drafts")

    @qzone.command("help")
    async def qzone_help(self, event: AstrMessageEvent):
        text = "\n".join(
            [
                "QQ 空间命令",
                "序号从 1 开始；最新/0 表示最新一条，支持 1~3、@用户 2。",
                "查看访客",
                "看说说/查看说说 [@用户] [序号/范围]",
                "评说说/评论说说/读说说 [@用户] [序号/范围] [评论内容]",
                "赞说说 [@用户] [序号/范围]",
                "发说说 <内容> [图片]",
                "写说说/写稿 <主题> [图片]",
                "删说说 <序号>",
                "回评/回复评论 <稿件ID> [评论序号]",
                "投稿 <内容> [图片]",
                "匿名投稿 <内容> [图片]",
                "撤稿 <稿件ID>",
                "看稿/查看稿件 [稿件ID]",
                "过稿/通过稿件/通过投稿 <稿件ID>",
                "拒稿/拒绝稿件/拒绝投稿 <稿件ID> [原因]",
                "",
                "保留的管理命令:",
                "/qzone status",
                "/qzone bind <cookie>",
                "/qzone autobind",
                "/qzone unbind",
                "",
                "LLM tools:",
                "llm_view_feed",
                "llm_publish_feed",
                "qzone_get_status",
                "qzone_list_feed",
                "qzone_view_post",
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
            yield self._command_result(event, "只有管理员可以查看状态。")
            return
        try:
            payload = await self._status_with_recovery()
        except QzoneBridgeError as exc:
            yield self._command_result(event, self._error_text(exc))
            return
        yield self._command_result(event, format_status(payload))

    @qzone.command("bind")
    async def qzone_bind(self, event: AstrMessageEvent, cookie: str):
        if not self._is_admin(event):
            yield self._command_result(event, "只有管理员可以绑定 Cookie。")
            return
        try:
            payload = await self.controller.bind_cookie_local(cookie)
        except QzoneBridgeError as exc:
            logger.warning("qzone bind failed: %s", exc)
            yield self._command_result(event, self._error_text(exc))
            return
        self._schedule_daemon_warmup("manual bind")
        try:
            payload = await self._status_with_recovery()
        except QzoneBridgeError:
            pass
        yield self._command_result(event, format_status(payload))

    @qzone.command("autobind")
    async def qzone_autobind(self, event: AstrMessageEvent):
        if not self._is_admin(event):
            yield self._command_result(event, "只有管理员可以自动绑定 Cookie。")
            return
        try:
            payload = await self._auto_bind_cookie(event, force=True, source="aiocqhttp")
        except QzoneBridgeError as exc:
            logger.warning("qzone autobind failed: %s", exc)
            yield self._command_result(event, self._error_text(exc))
            return
        self._schedule_daemon_warmup("autobind")
        try:
            payload = await self._status_with_recovery()
        except QzoneBridgeError:
            pass
        yield self._command_result(event, format_status(payload))

    @qzone.command("unbind")
    async def qzone_unbind(self, event: AstrMessageEvent):
        if not self._is_admin(event):
            yield self._command_result(event, "只有管理员可以解绑。")
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
            yield self._command_result(event, "只有管理员可以发说说。")
            return
        post = collect_post_payload(
            event,
            fallback_content=content,
            include_event_text=True,
            command_prefixes=("qzone post",),
        )
        profile_task: asyncio.Task | None = None
        try:
            await self._ensure_cookie_ready(event)
            profile_task = self._schedule_publisher_profile(event)
            payload = await self.controller.publish_post(
                content=post.content,
                media=[item.to_dict() for item in post.media],
                content_sanitized=True,
            )
        except QzoneBridgeError as exc:
            if profile_task is not None:
                profile_task.cancel()
            yield self._command_result(event, self._error_text(exc))
            return
        yield await self._publish_result(event, post, payload, profile_task=profile_task)

    @qzone.command("comment")
    async def qzone_comment(self, event: AstrMessageEvent, hostuin: int, fid: str, content: str):
        if not self._is_admin(event):
            yield self._command_result(event, "只有管理员可以评论。")
            return
        try:
            await self._ensure_cookie_ready(event)
            await self._ensure_daemon()
            payload = await self.controller.comment_post(hostuin=hostuin, fid=fid, content=content)
        except QzoneBridgeError as exc:
            yield self._command_result(event, self._error_text(exc))
            return
        yield self._command_result(event, format_action_result("评论结果", payload))

    @qzone.command("like")
    async def qzone_like(self, event: AstrMessageEvent, hostuin: int, fid: str, appid: int = 311, unlike: bool = False):
        if not self._is_admin(event):
            yield self._command_result(event, "只有管理员可以点赞。")
            return
        try:
            await self._ensure_cookie_ready(event)
            await self._ensure_daemon()
            payload = await self.controller.like_post(hostuin=hostuin, fid=fid, appid=appid, unlike=unlike)
        except QzoneBridgeError as exc:
            yield self._command_result(event, self._error_text(exc))
            return
        yield self._command_result(event, format_like_result(payload))

    @filter.llm_tool()
    async def llm_view_feed(
        self,
        event: AstrMessageEvent,
        user_id: str | None = None,
        pos: int = 0,
        like: bool = False,
        reply: bool = False,
    ):
        """查看、点赞、评论某位用户 QQ 空间的一条说说。"""
        try:
            await self._ensure_cookie_ready(event)
            await self._ensure_daemon()
            hostuin = int(user_id or self._sender_id(event) or 0)
            selection = PostSelection(
                target_uin=hostuin,
                start=(pos + 1 if pos >= 0 else -1),
                end=(pos + 1 if pos >= 0 else -1),
                selector="index" if pos >= 0 else "last",
            )
            posts = await self._posts_for_selection(selection, with_detail=True, login_uin=self._self_id(event))
            if not posts:
                return "查询结果为空。"
            post = posts[0]
            actions: list[str] = []
            if reply:
                content = await self._generate_comment_text(event, post)
                if not content.strip():
                    content = "挺有意思的。"
                await self._post_service().comment_post(post, content.strip())
                actions.append("已评论")
            if like:
                await self._post_service().like_post(post)
                actions.append("已点赞")
            prefix = "，".join(actions)
            return "\n".join(part for part in (prefix, post.detail_text(post.local_id)) if part)
        except Exception as exc:
            logger.warning("llm_view_feed failed: %s", exc)
            return str(getattr(exc, "message", str(exc)))

    @filter.llm_tool()
    async def llm_publish_feed(
        self,
        event: AstrMessageEvent,
        text: str = "",
        get_image: bool = True,
    ):
        """发布一条 QQ 空间说说。"""
        if not self._is_admin(event):
            return "只有管理员可以发说说。"
        post = collect_post_payload(
            event,
            fallback_content=text,
            include_event_text=bool(get_image),
            command_prefixes=("发说说", "qzone post"),
        )
        if not get_image:
            post.media = []
        try:
            await self._ensure_cookie_ready(event)
            await self._ensure_daemon()
            payload = await self.controller.publish_post(
                content=post.content,
                media=[item.to_dict() for item in post.media],
                content_sanitized=True,
            )
        except QzoneBridgeError as exc:
            return self._error_text(exc)
        return f"已发布说说到 QQ 空间：\n{post.content}\n{format_action_result('发布结果', payload)}"

    @filter.llm_tool(name="qzone_get_status")
    async def tool_get_status(self, event: AstrMessageEvent):
        """获取 QQ 空间 daemon 状态。

        Returns:
            当前状态摘要。
        """
        if not self._is_admin(event):
            yield event.plain_result("只有管理员可以查看 QQ 空间状态。")
            return
        try:
            payload = await self._status_with_recovery()
        except QzoneBridgeError as exc:
            yield event.plain_result(self._error_text(exc))
            return
        yield event.plain_result(format_status(payload))

    @filter.llm_tool(name="qzone_list_feed")
    async def tool_list_feed(
        self,
        event: AstrMessageEvent,
        target_uin: int = 0,
        limit: int = 5,
        cursor: str = "",
        scope: str = "auto",
        hostuin: int = 0,
    ):
        """获取 QQ 空间说说列表。

        Args:
            target_uin (number): 目标 QQ 号，0 表示当前登录账号或好友动态流。
            limit (number): 返回数量。
            cursor (string): 翻页游标。
            scope (string): auto/self/profile。
            hostuin (number): 兼容旧参数，优先级低于 target_uin。
        """
        try:
            await self._ensure_cookie_ready(event)
            await self._ensure_daemon()
            effective_hostuin = int(target_uin or hostuin or 0)
            effective_scope = "" if scope == "auto" else scope
            payload = await self.controller.list_feeds(
                hostuin=effective_hostuin,
                limit=self._limit(limit),
                cursor=cursor,
                scope=effective_scope,
            )
        except QzoneBridgeError as exc:
            yield event.plain_result(self._error_text(exc))
            return
        entries = self._to_feed_entries(payload)
        yield event.plain_result(format_llm_feed_list(entries))

    @filter.llm_tool(name="qzone_detail_feed")
    async def tool_detail_feed(self, event: AstrMessageEvent, hostuin: int, fid: str, appid: int = 311):
        """获取说说详情。

        Args:
            hostuin (number): 目标 QQ 号。
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

    @filter.llm_tool(name="qzone_view_post")
    async def tool_view_post(
        self,
        event: AstrMessageEvent,
        target_uin: int = 0,
        selector: str = "latest",
        detail: bool = True,
        hostuin: int = 0,
        fid: str = "",
        appid: int = 311,
    ):
        """按自然选择器查看一条或多条 QQ 空间说说。

        Args:
            target_uin (number): 目标 QQ 号，0 表示当前登录账号或好友动态流。
            selector (string): latest、最新、第2条、2、1~3 或 fid。
            detail (boolean): 是否获取评论等详情。
            hostuin (number): 兼容旧参数。
            fid (string): 兼容旧参数。
            appid (number): 兼容旧参数。
        """
        try:
            await self._ensure_cookie_ready(event)
            await self._ensure_daemon()
            selection = selection_from_tool_args(
                target_uin=target_uin,
                selector=selector,
                hostuin=hostuin,
                fid=fid,
                appid=appid,
            )
            posts = await self._posts_for_selection(selection, with_detail=detail, login_uin=self._self_id(event))
        except QzoneBridgeError as exc:
            yield event.plain_result(self._error_text(exc))
            return
        yield event.plain_result(self._format_posts(posts, detail=detail))

    @filter.llm_tool(name="qzone_publish_post")
    async def tool_publish_post(
        self,
        event: AstrMessageEvent,
        content: str,
        confirm: bool = False,
        sync_weibo: bool = False,
        media: list[str] | None = None,
    ):
        """发布 QQ 空间说说。

        Args:
            content (string): 说说内容。
            confirm (boolean): 是否确认发布。
            sync_weibo (boolean): 是否同步到微博。
        """
        if not self._is_admin(event):
            yield event.plain_result("只有管理员可以发说说。")
            return
        post = collect_post_payload(
            event,
            fallback_content=content,
            include_event_text=False,
            command_prefixes=("qzone post",),
            extra_media=media,
        )
        if self.settings.preview_writes and not confirm:
            draft = truncate(post.content or "空内容", 120)
            suffix = f"，附带 {len(post.media)} 个媒体" if post.media else ""
            yield event.plain_result(f"发布预览: {draft}{suffix}。确认后再发布。")
            return
        profile_task: asyncio.Task | None = None
        try:
            await self._ensure_cookie_ready(event)
            profile_task = self._schedule_publisher_profile(event)
            payload = await self.controller.publish_post(
                content=post.content,
                sync_weibo=sync_weibo,
                media=[item.to_dict() for item in post.media],
                content_sanitized=True,
            )
        except QzoneBridgeError as exc:
            if profile_task is not None:
                profile_task.cancel()
            yield event.plain_result(self._error_text(exc))
            return
        yield await self._publish_result(event, post, payload, profile_task=profile_task)

    @filter.llm_tool(name="qzone_comment_post")
    async def tool_comment_post(
        self,
        event: AstrMessageEvent,
        target_uin: int = 0,
        selector: str = "latest",
        content: str = "",
        auto_generate: bool = True,
        private: bool = False,
        like_after_comment: bool = False,
        hostuin: int = 0,
        fid: str = "",
        confirm: bool = False,
        appid: int = 311,
        latest: bool = False,
        index: int = 0,
    ):
        """按自然选择器评论一条说说。

        Args:
            target_uin (number): 目标 QQ 号，0 表示当前登录账号或好友动态流。
            selector (string): latest、最新、第2条、2、1~3 或 fid。
            content (string): 评论内容，留空时可由 LLM 生成。
            auto_generate (boolean): content 为空时是否自动生成评论。
            private (boolean): 是否私密评论。
            like_after_comment (boolean): 评论后是否顺手点赞。
            hostuin (number): 兼容旧参数，目标 QQ 号。
            fid (string): 兼容旧参数，说说 fid。
            confirm (boolean): 兼容旧参数，目前不会拦截执行。
            appid (number): 兼容旧参数，说说 appid，默认 311。
            latest (boolean): 兼容旧参数，为 true 时操作最新一条说说。
            index (number): 兼容旧参数，操作最近列表第 N 条。
        """
        arguments = {
            "target_uin": target_uin,
            "selector": selector,
            "content": content,
            "auto_generate": auto_generate,
            "private": private,
            "like_after_comment": like_after_comment,
            "hostuin": hostuin,
            "fid": fid,
            "confirm": confirm,
            "appid": appid,
            "latest": latest,
            "index": index,
        }
        if not self._is_admin(event):
            payload = {"ok": False, "tool": "qzone_comment_post", "public_reason": "没有权限"}
            self._log_tool_call_result({**payload, "arguments": arguments})
            text = await self._ask_llm_tool_reply(
                event,
                payload,
                self._llm_error_fallback_text("没有权限"),
            )
            yield event.plain_result(text)
            return
        try:
            await self._ensure_cookie_ready(event)
            await self._ensure_daemon()
            selection = selection_from_tool_args(
                target_uin=target_uin,
                selector=selector,
                hostuin=hostuin,
                fid=fid,
                appid=appid,
                latest=latest,
                index=index,
            )
            posts = await self._posts_for_selection(
                selection,
                with_detail=auto_generate and not content.strip(),
                login_uin=self._self_id(event),
            )
            if not posts:
                raise QzoneBridgeError("没有找到可评论的说说")
            results: list[dict[str, Any]] = []
            for post in posts:
                comment_text = content.strip()
                if not comment_text and auto_generate:
                    comment_text = await self._generate_comment_text(event, post)
                if not comment_text:
                    raise QzoneBridgeError("评论内容为空")
                payload = await self._post_service().comment_post(post, comment_text, private=private)
                results.append({"post": QzonePostService.post_payload(post), "comment": comment_text, "result": payload})
                if like_after_comment:
                    await self._post_service().like_post(post)
        except QzoneBridgeError as exc:
            log_payload = self._bridge_error_log_payload("qzone_comment_post", exc, arguments)
            self._log_tool_call_result(log_payload)
            text = await self._ask_llm_tool_reply(
                event,
                self._llm_error_payload("qzone_comment_post", exc),
                self._llm_error_fallback_text(exc.message),
            )
            yield event.plain_result(text)
            return
        log_payload = {"ok": True, "tool": "qzone_comment_post", "arguments": arguments, "result": results}
        self._log_tool_call_result(log_payload)
        text = await self._ask_llm_tool_reply(
            event,
            {"ok": True, "tool": "qzone_comment_post", "result": {"message": "评论发好了。", "count": len(results)}},
            "评论发好了。",
        )
        yield event.plain_result(text)

    @filter.llm_tool(name="qzone_like_post")
    async def tool_like_post(
        self,
        event: AstrMessageEvent,
        target_uin: int = 0,
        selector: str = "latest",
        hostuin: int = 0,
        fid: str = "",
        confirm: bool = False,
        appid: int = 311,
        unlike: bool = False,
        latest: bool = False,
        index: int = 0,
    ):
        """点赞或取消点赞一条说说。

        Args:
            target_uin (number): 目标 QQ 号，`0` 表示当前登录账号或好友动态流。
            selector (string): latest、最新、第2条、2、1~3 或 fid。
            hostuin (number): 兼容旧参数，目标 QQ 号。
            fid (string): 兼容旧参数，说说 fid，也可以用最近列表的序号。
            confirm (boolean): 兼容旧参数，目前不会拦截执行。
            appid (number): 说说 appid，默认 `311`。
            unlike (boolean): 为 `true` 时取消点赞。
            latest (boolean): 为 `true` 时操作最新一条说说。
            index (number): 操作最近列表第 N 条，`1` 表示第一条。
        """
        arguments = {
            "target_uin": target_uin,
            "selector": selector,
            "hostuin": hostuin,
            "fid": fid,
            "confirm": confirm,
            "appid": appid,
            "unlike": unlike,
            "latest": latest,
            "index": index,
        }
        if not self._is_admin(event):
            log_payload = {
                "ok": False,
                "tool": "qzone_like_post",
                "error": {
                    "type": "PermissionError",
                    "code": "QZONE_PERMISSION",
                    "message": "没有权限",
                },
            }
            self._log_tool_call_result({**log_payload, "arguments": arguments})
            llm_payload = {
                "ok": False,
                "public_reason": "没有权限",
                "reply_guidance": "Use a short natural reply in the active persona. Do not expose error details.",
            }
            text = await self._ask_llm_tool_reply(
                event,
                llm_payload,
                self._llm_error_fallback_text("没有权限"),
            )
            yield event.plain_result(text)
            return
        try:
            await self._ensure_cookie_ready(event)
            await self._ensure_daemon()
            legacy_direct = bool(fid or latest or index)
            if legacy_direct:
                effective_hostuin = int(hostuin or target_uin or 0)
                payload = await self.controller.like_post(
                    hostuin=effective_hostuin,
                    fid=fid,
                    appid=appid,
                    unlike=unlike,
                    latest=latest,
                    index=index,
                )
            else:
                selection = selection_from_tool_args(
                    target_uin=target_uin,
                    selector=selector,
                    hostuin=hostuin,
                    appid=appid,
                )
                posts = await self._posts_for_selection(selection, login_uin=self._self_id(event))
                if not posts:
                    raise QzoneBridgeError("没有找到可点赞的说说")
                payloads = [await self._post_service().like_post(post, unlike=unlike) for post in posts]
                payload = payloads[0] if len(payloads) == 1 else {
                    "action": "unlike" if unlike else "like",
                    "liked": not unlike,
                    "verified": all(item.get("verified", True) is not False for item in payloads),
                    "summary": f"{len(payloads)} 条说说",
                    "items": payloads,
                }
        except QzoneBridgeError as exc:
            log_payload = self._bridge_error_log_payload("qzone_like_post", exc, arguments)
            self._log_tool_call_result(log_payload)
            llm_payload = self._llm_error_payload("qzone_like_post", exc)
            text = await self._ask_llm_tool_reply(
                event,
                llm_payload,
                self._llm_error_fallback_text(exc.message),
            )
            yield event.plain_result(text)
            return
        self._log_tool_call_result(
            {
                "ok": True,
                "tool": "qzone_like_post",
                "arguments": arguments,
                "result": payload,
            }
        )
        text = await self._ask_llm_tool_reply(
            event,
            self._llm_like_payload(payload),
            self._like_fallback_text(payload),
        )
        yield event.plain_result(text)

    async def terminate(self):
        for task in self._scheduled_tasks:
            task.cancel()
        if self._scheduled_tasks:
            await asyncio.gather(*self._scheduled_tasks, return_exceptions=True)
            self._scheduled_tasks.clear()
        try:
            await self.controller.close()
        except Exception as exc:
            logger.exception("qzone controller close failed: %s", exc)
