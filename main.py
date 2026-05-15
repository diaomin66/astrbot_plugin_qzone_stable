"""AstrBot entry point for the QQ?? bridge."""

from __future__ import annotations

import asyncio
import inspect
import importlib
import json
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
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        prompt = (
            "??? QQ ???????? JSON?????????????????????"
            f"\n{data}\n"
            "????????????????? JSON?????????tool?code?fid?cursor?raw?detail?diagnostic ??????"
            "?? ok ? true????????????verified ? false ??? QQ ??????????????????"
            "?? ok ? false????? diagnostic/status_code/text ???????????????????????????"
        )
        system_prompt = "?? AstrBot ? QQ ??????????????????????????????????"
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
                    if text:
                        return text
            except Exception as exc:
                logger.debug("qzone llm_generate reply failed: %s", exc)
            else:
                text = self._text_from_llm_response(response)
                if text:
                    return text

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
                if text:
                    return text

        return fallback

    def _llm_like_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        visible_payload = _safe_for_llm(payload)
        return {
            "ok": True,
            "tool": "qzone_like_post",
            "result": visible_payload,
            "reply_guidance": (
                "??? result ??????????"
                "?? verified ? true??? QQ ????????"
                "?? verified ? false?????????????????"
                "???? fid?cursor?raw ??????"
            ),
        }

    def _llm_error_payload(self, tool: str, exc: QzoneBridgeError) -> dict[str, Any]:
        error: dict[str, Any] = {
            "type": type(exc).__name__,
            "code": exc.code,
            "message": exc.message,
        }
        status_code = getattr(exc, "status_code", None)
        if status_code is not None:
            error["status_code"] = status_code
        result: dict[str, Any] = {
            "ok": False,
            "tool": tool,
            "error": error,
            "reply_guidance": "??? error ? diagnostic ?????????????????????????",
        }
        diagnostic = _safe_for_llm(exc.detail)
        if diagnostic not in (None, {}, []):
            result["diagnostic"] = diagnostic
        return result

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
            payload = {
                "ok": False,
                "tool": "qzone_like_post",
                "error": {
                    "type": "PermissionError",
                    "code": "QZONE_PERMISSION",
                    "message": "????????",
                },
                "reply_guidance": "???????????????????",
            }
            self._log_tool_call_result({**payload, "arguments": arguments})
            text = await self._ask_llm_tool_reply(event, payload, "????????")
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
                exc.message,
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
            format_like_result(payload),
        )
        yield event.plain_result(text)

    async def terminate(self):
        try:
            await self.controller.close()
        except Exception as exc:
            logger.exception("qzone controller close failed: %s", exc)
