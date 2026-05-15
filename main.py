"""AstrBot entry point for the QQ?? bridge."""

from __future__ import annotations

import asyncio
import inspect
import importlib
import json
import re
import sys
import time
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
from qzone_bridge.errors import DaemonUnavailableError, QzoneBridgeError, QzoneCookieAcquireError, QzoneNeedsRebind
from qzone_bridge.media import PostPayload, collect_post_payload
from qzone_bridge.models import FeedEntry
from qzone_bridge.onebot_cookie import fetch_cookie_text
from qzone_bridge.parser import normalize_uin, parse_cookie_text
from qzone_bridge.publish_renderer import RenderProfile, profile_from_event, render_publish_result_image
from qzone_bridge.render import (
    format_action_result,
    format_feed_detail,
    format_feed_list,
    format_like_result,
    format_llm_feed_list,
    format_status,
)
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
        self._publisher_profile_cache: tuple[int, float, Any] | None = None

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
        text = format_action_result("????", payload)
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
        return event.plain_result(f"{text}\n???: {image_path}")

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
                parts.append(f"?? {location}")
            url = exc.detail.get("url")
            if url:
                parts.append(f"?? {url}")
            if parts:
                return f"{exc.message}?{', '.join(parts)}?"
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
        context = getattr(self, "_context", None) or getattr(self, "context", None)

        generator = getattr(context, "llm_generate", None)
        if callable(generator):
            kwargs: dict[str, Any] = {"prompt": prompt, "system_prompt": system_prompt}
            provider_id = await self._current_provider_id(event)
            if provider_id:
                kwargs["chat_provider_id"] = provider_id
            try:
                response = await self._maybe_await(generator(**kwargs))
            except TypeError:
                kwargs.pop("chat_provider_id", None)
                try:
                    response = await self._maybe_await(generator(**kwargs))
                except Exception as exc:
                    logger.debug("qzone llm_generate reply failed: %s", exc)
                else:
                    text = self._text_from_llm_response(response)
                    if self._llm_tool_reply_is_safe(text, payload):
                        return text
                    if text:
                        logger.warning("discarded unsafe qzone llm tool reply: %s", truncate(text, 300))
            except Exception as exc:
                logger.debug("qzone llm_generate reply failed: %s", exc)
            else:
                text = self._text_from_llm_response(response)
                if self._llm_tool_reply_is_safe(text, payload):
                    return text
                if text:
                    logger.warning("discarded unsafe qzone llm tool reply: %s", truncate(text, 300))

        provider_getter = getattr(context, "get_using_provider", None)
        provider = None
        if callable(provider_getter):
            umo = getattr(event, "unified_msg_origin", None)
            attempts: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
            if umo is not None:
                attempts.append(((), {"umo": umo}))
                attempts.append(((umo,), {}))
            attempts.append(((), {}))
            for args, kwargs in attempts:
                try:
                    provider = await self._maybe_await(provider_getter(*args, **kwargs))
                except TypeError:
                    continue
                except Exception as exc:
                    logger.debug("qzone provider lookup failed: %s", exc)
                    provider = None
                    break
                if provider is not None:
                    break

        text_chat = getattr(provider, "text_chat", None)
        if callable(text_chat):
            for kwargs in (
                {"prompt": prompt, "contexts": [], "system_prompt": system_prompt},
                {"prompt": prompt, "context": [], "system_prompt": system_prompt},
                {"prompt": prompt},
            ):
                try:
                    response = await self._maybe_await(text_chat(**kwargs))
                except TypeError:
                    continue
                except Exception as exc:
                    logger.debug("qzone provider text_chat reply failed: %s", exc)
                    break
                text = self._text_from_llm_response(response)
                if self._llm_tool_reply_is_safe(text, payload):
                    return text
                if text:
                    logger.warning("discarded unsafe qzone provider reply: %s", truncate(text, 300))

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
            raise QzoneNeedsRebind("QQ???????????? Cookie")
        if allow_needs_rebind:
            if status.get("daemon_state") != "ready":
                await self.controller.ensure_running()
            return
        if self.settings.auto_start_daemon:
            if status.get("daemon_state") != "ready":
                await self.controller.ensure_running()
        elif status.get("daemon_state") != "ready":
            raise DaemonUnavailableError("daemon ???")

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
            lines = [text, "", "??"]
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
        return "??? AstrBot ???? aiocqhttp(OneBot v11) ???????? /qzone bind ?? Cookie?"

    async def _auto_bind_cookie(
        self,
        event: AstrMessageEvent | None = None,
        *,
        force: bool = False,
        source: str = "aiocqhttp",
    ) -> dict[str, Any]:
        async with self._get_cookie_lock():
            if not self.settings.auto_bind_cookie and not force:
                raise QzoneCookieAcquireError("???? Cookie ??????????")

            bot = self._capture_onebot_client(event)
            if bot is None:
                raise QzoneCookieAcquireError(f"???? OneBot ?????????? Cookie?{self._cookie_binding_hint()}")

            try:
                status = await self.controller.get_status(probe_daemon=False)
            except QzoneBridgeError:
                status = {}

            if not force and status and int(status.get("cookie_count") or 0) > 0 and not bool(status.get("needs_rebind")):
                return status

            cookie_text = await fetch_cookie_text(bot, domain=self.settings.cookie_domain)
            if not cookie_text:
                raise QzoneCookieAcquireError(f"OneBot ????? Cookie?{self._cookie_binding_hint()}")

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
                "QQ????",
                "/qzone status",
                "/qzone bind <cookie>",
                "/qzone autobind",
                "/qzone unbind",
                "/qzone feed [hostuin] [limit] [cursor]",
                "/qzone detail <hostuin> <fid> [appid]",
                "/qzone post <content> [??/????]",
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
            yield self._command_result(event, "??????????")
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
            yield self._command_result(event, "??????? Cookie?")
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
            yield self._command_result(event, "????????? Cookie?")
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
            yield self._command_result(event, "????????")
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
            yield self._command_result(event, "?????????")
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
            yield self._command_result(event, "????????")
            return
        try:
            await self._ensure_cookie_ready(event)
            await self._ensure_daemon()
            payload = await self.controller.comment_post(hostuin=hostuin, fid=fid, content=content)
        except QzoneBridgeError as exc:
            yield self._command_result(event, self._error_text(exc))
            return
        yield self._command_result(event, format_action_result("????", payload))

    @qzone.command("like")
    async def qzone_like(self, event: AstrMessageEvent, hostuin: int, fid: str, appid: int = 311, unlike: bool = False):
        if not self._is_admin(event):
            yield self._command_result(event, "????????")
            return
        try:
            await self._ensure_cookie_ready(event)
            await self._ensure_daemon()
            payload = await self.controller.like_post(hostuin=hostuin, fid=fid, appid=appid, unlike=unlike)
        except QzoneBridgeError as exc:
            yield self._command_result(event, self._error_text(exc))
            return
        yield self._command_result(event, format_like_result(payload))

    @filter.llm_tool(name="qzone_get_status")
    async def tool_get_status(self, event: AstrMessageEvent):
        """?? QQ ?? daemon ???

        Returns:
            ???????
        """
        if not self._is_admin(event):
            yield event.plain_result("???????????")
            return
        try:
            payload = await self._status_with_recovery()
        except QzoneBridgeError as exc:
            yield event.plain_result(self._error_text(exc))
            return
        yield event.plain_result(format_status(payload))

    @filter.llm_tool(name="qzone_list_feed")
    async def tool_list_feed(self, event: AstrMessageEvent, hostuin: int = 0, limit: int = 5, cursor: str = "", scope: str = ""):
        """?? QQ ?????

        Args:
            hostuin (number): ?? QQ ??0 ?????????
            limit (number): ?????
            cursor (string): ?????
            scope (string): self ? profile?
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
        yield event.plain_result(format_llm_feed_list(entries))

    @filter.llm_tool(name="qzone_detail_feed")
    async def tool_detail_feed(self, event: AstrMessageEvent, hostuin: int, fid: str, appid: int = 311):
        """?????????

        Args:
            hostuin (number): ???? QQ ??
            fid (string): ?? fid?
            appid (number): ?? id??? 311?
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
        """???? QQ ?????

        Args:
            content (string): ?????
            confirm (boolean): ???????
            sync_weibo (boolean): ???????
        """
        if not self._is_admin(event):
            yield event.plain_result("??????????")
            return
        post = collect_post_payload(
            event,
            fallback_content=content,
            include_event_text=False,
            command_prefixes=("qzone post",),
            extra_media=media,
        )
        if self.settings.preview_writes and not confirm:
            draft = truncate(post.content or "?????", 120)
            suffix = f"??? {len(post.media)} ?" if post.media else ""
            yield event.plain_result(f"?????: {draft}{suffix}????????")
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
        hostuin: int,
        fid: str,
        content: str,
        confirm: bool = False,
        appid: int = 311,
        private: bool = False,
    ):
        """???????

        Args:
            hostuin (number): ?? QQ ??
            fid (string): ?? fid?
            content (string): ?????
            confirm (boolean): ????????????????
            appid (number): ?? id?
            private (boolean): ???????
        """
        if not self._is_admin(event):
            yield event.plain_result("????????")
            return
        if self.settings.preview_writes and not confirm:
            yield event.plain_result(
                f"?????: hostuin={hostuin}, fid={fid}, content={truncate(content, 120)}????????"
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
        yield event.plain_result(format_action_result("????", payload))

    @filter.llm_tool(name="qzone_like_post")
    async def tool_like_post(
        self,
        event: AstrMessageEvent,
        hostuin: int = 0,
        fid: str = "",
        confirm: bool = False,
        appid: int = 311,
        unlike: bool = False,
        latest: bool = False,
        index: int = 0,
    ):
        """????????????

        Args:
            hostuin (number): ?? QQ ??`0` ?????? QQ?????????????????????
            fid (string): ????? fid???????????? N ????????? `latest` / `index`???? `latest`?`?3?` ???????
            confirm (boolean): ???????????????
            appid (number): ?? appid???? `311`?
            unlike (boolean): ? `true` ?????????????
            latest (boolean): ? `true` ??????? QQ ????????
            index (number): ?????? QQ ?? N ????`1` ???????
        """
        arguments = {
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
                    "message": "????????",
                },
            }
            self._log_tool_call_result({**log_payload, "arguments": arguments})
            llm_payload = {
                "ok": False,
                "public_reason": "????????",
                "reply_guidance": "Use a short natural reply in the active persona. Do not expose error details.",
            }
            text = await self._ask_llm_tool_reply(
                event,
                llm_payload,
                self._llm_error_fallback_text("????????"),
            )
            yield event.plain_result(text)
            return
        try:
            await self._ensure_cookie_ready(event)
            await self._ensure_daemon()
            payload = await self.controller.like_post(
                hostuin=hostuin,
                fid=fid,
                appid=appid,
                unlike=unlike,
                latest=latest,
                index=index,
            )
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
        try:
            await self.controller.close()
        except Exception as exc:
            logger.exception("qzone controller close failed: %s", exc)
