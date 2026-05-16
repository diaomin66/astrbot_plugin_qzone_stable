"""LLM adapter for Qzone writing, comments, and user-facing replies."""

from __future__ import annotations

import inspect
import re
from typing import Any

from .social import QzoneComment, QzonePost
from .utils import truncate


class QzoneLLM:
    def __init__(self, context: Any, settings: Any):
        self.context = context
        self.settings = settings

    async def _maybe_await(self, value: Any) -> Any:
        if inspect.isawaitable(value):
            return await value
        return value

    @staticmethod
    def text_from_response(response: Any) -> str:
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

    async def _provider_by_id(self, event: Any, provider_id: str = "") -> Any | None:
        context = self.context
        if context is None:
            return None
        if provider_id:
            getter = getattr(context, "get_provider_by_id", None)
            if callable(getter):
                try:
                    provider = await self._maybe_await(getter(provider_id))
                except Exception:
                    provider = None
                if provider is not None:
                    return provider
        getter = getattr(context, "get_using_provider", None)
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
                provider = await self._maybe_await(getter(*args, **kwargs))
            except TypeError:
                continue
            except Exception:
                break
            if provider is not None:
                return provider
        return None

    async def current_provider_id(self, event: Any) -> Any | None:
        getter = getattr(self.context, "get_current_chat_provider_id", None)
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
            except Exception:
                return None
            if provider_id:
                return provider_id
        return None

    async def generate_text(
        self,
        event: Any,
        prompt: str,
        *,
        provider_id: str = "",
        system_prompt: str = "",
        prefer_current_provider: bool = False,
    ) -> str:
        provider = await self._provider_by_id(event, provider_id)
        text_chat = getattr(provider, "text_chat", None)
        if callable(text_chat):
            attempts: list[dict[str, Any]] = [{"prompt": prompt}]
            if system_prompt:
                attempts.insert(0, {"prompt": prompt, "contexts": [], "system_prompt": system_prompt})
                attempts.insert(1, {"prompt": prompt, "context": [], "system_prompt": system_prompt})
            for kwargs in attempts:
                try:
                    response = await self._maybe_await(text_chat(**kwargs))
                except TypeError:
                    continue
                return self.text_from_response(response)

        generator = getattr(self.context, "llm_generate", None)
        if callable(generator):
            kwargs: dict[str, Any] = {"prompt": prompt}
            if system_prompt:
                kwargs["system_prompt"] = system_prompt
            if provider_id:
                kwargs["chat_provider_id"] = provider_id
            elif prefer_current_provider:
                current_provider_id = await self.current_provider_id(event)
                if current_provider_id:
                    kwargs["chat_provider_id"] = current_provider_id
            try:
                response = await self._maybe_await(generator(**kwargs))
            except TypeError:
                kwargs.pop("chat_provider_id", None)
                response = await self._maybe_await(generator(**kwargs))
            return self.text_from_response(response)
        return ""

    async def generate_post_text(self, event: Any, topic: str = "", *, history: str = "") -> str:
        prompt = self.settings.post_prompt
        if str(topic or "").strip():
            prompt = f"{prompt}\n\n主题：{str(topic).strip()}"
        if history:
            prompt = f"{prompt}\n\n聊天记录参考：\n{truncate(history, 8000)}"
        return await self.generate_text(event, prompt, provider_id=self.settings.post_provider_id)

    def _comment_context(self, post: QzonePost) -> str:
        lines = [f"说说内容：{post.summary or '(空)'}"]
        if post.images:
            lines.append("图片：" + "，".join(post.images[:6]))
        visible_comments = [comment for comment in post.comments if comment.content][:8]
        if visible_comments:
            lines.append("已有评论：")
            for comment in visible_comments:
                name = comment.nickname or str(comment.uin or "用户")
                lines.append(f"- {name}: {comment.content}")
        return "\n".join(lines)

    def _clean_short_reply(self, text: str) -> str:
        cleaned = re.sub(r"[\s\u3000]+", "", str(text or "")).strip()
        cleaned = cleaned.strip("\"'“”‘’")
        cleaned = cleaned.rstrip("。.")
        max_len = int(getattr(self.settings, "comment_max_length", 60) or 60)
        return truncate(cleaned, max_len)

    async def generate_comment_text(self, event: Any, post: QzonePost) -> str:
        prompt = f"{self.settings.comment_prompt}\n\n{self._comment_context(post)}"
        text = await self.generate_text(event, prompt, provider_id=self.settings.comment_provider_id)
        return self._clean_short_reply(text)

    async def generate_reply_text(self, event: Any, post: QzonePost, comment: QzoneComment) -> str:
        prompt = (
            f"{self.settings.reply_prompt}\n\n"
            f"{self._comment_context(post)}\n"
            f"要回复的评论：{comment.nickname or comment.uin}: {comment.content}"
        )
        text = await self.generate_text(event, prompt, provider_id=self.settings.reply_provider_id)
        return self._clean_short_reply(text)
