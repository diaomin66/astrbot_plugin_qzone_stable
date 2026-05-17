"""LLM adapter for Qzone writing, comments, and user-facing replies."""

from __future__ import annotations

import inspect
import json
import re
from typing import Any

from .social import QzoneComment, QzonePost
from .utils import truncate


PERSONA_SYSTEM_PROMPT = (
    "沿用当前 AstrBot 人格和当前聊天语气。"
    "只输出用户真正要拿去使用的最终文本，不要输出工具调用、JSON、参数、解释或执行说明。"
)

POST_OUTPUT_RULES = (
    "生成要求：\n"
    "- 沿用当前 AstrBot 人格和当前聊天语气。\n"
    "- 只输出最终可发布的说说正文。\n"
    "- 不要输出 qzone_publish_post、/qzone、函数调用、JSON、Markdown 代码块、字段名、参数或解释。\n"
    "- 不要写“发布预览”“确认后发布”“已发布”等执行状态。\n"
)

COMMENT_OUTPUT_RULES = (
    "生成要求：\n"
    "- 沿用当前 AstrBot 人格和当前聊天语气。\n"
    "- 只输出最终评论内容，一句就好。\n"
    "- 不要输出 qzone_comment_post、函数调用、JSON、Markdown 代码块、字段名、参数或解释。\n"
)

INSTRUCTION_MARKERS = (
    "qzone_",
    "llm_",
    "/qzone",
    "hostuin",
    "target_uin",
    "selector",
    "appid",
    "auto_generate",
    "private=",
    "sync_weibo",
    "status_code",
    "raw",
    "fid=",
    '"fid"',
    "'fid'",
    "发布预览",
    "发布结果",
    "评论结果",
    "函数调用",
    "工具",
    "指令",
    "命令",
    "参数",
    "json",
    "markdown",
)


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

    @staticmethod
    def _strip_code_fence(text: str) -> str:
        value = str(text or "").strip()
        match = re.fullmatch(r"```(?:\w+)?\s*(.*?)\s*```", value, flags=re.DOTALL)
        if match:
            return match.group(1).strip()
        return value

    @staticmethod
    def _unquote(value: str) -> str:
        text = str(value or "").strip()
        if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"', "“", "”", "‘", "’"}:
            return text[1:-1].strip()
        return text.strip("\"'“”‘’")

    @classmethod
    def _extract_json_field(cls, text: str, fields: tuple[str, ...]) -> str:
        try:
            payload = json.loads(text)
        except Exception:
            return ""
        candidates: list[Any] = []
        if isinstance(payload, dict):
            candidates.append(payload)
        elif isinstance(payload, list):
            candidates.extend(item for item in payload if isinstance(item, dict))
        for item in candidates:
            for field in fields:
                value = item.get(field)
                if isinstance(value, str) and value.strip():
                    return cls._unquote(value)
        return ""

    @classmethod
    def _extract_assignment_field(cls, text: str, fields: tuple[str, ...]) -> str:
        field_pattern = "|".join(re.escape(field) for field in fields)
        quoted = re.search(
            rf"\b(?:{field_pattern})\s*=\s*([\"'])(?P<value>(?:\\.|(?!\1).)*)\1",
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if quoted:
            return quoted.group("value").replace(r"\"", '"').replace(r"\'", "'").strip()
        labelled = re.search(
            rf"^\s*(?:{field_pattern}|说说正文|正文|内容|评论)\s*[:：]\s*(?P<value>.+)$",
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if labelled:
            return cls._unquote(labelled.group("value"))
        return ""

    @classmethod
    def _remove_generation_chatter(cls, text: str) -> str:
        value = cls._unquote(cls._strip_code_fence(text))
        value = re.sub(r"^\s*[-*]\s*", "", value)
        value = re.sub(r"^\s*\d+[.)、]\s*", "", value)
        value = re.sub(r"^\s*(?:说说正文|正文|内容|文案|稿件|评论)\s*[:：]\s*", "", value)
        value = re.sub(
            r"^\s*(?:好的|好呀|可以|行|收到|没问题)[，,。\s]*(?:我来|我给你|给你|帮你)?"
            r"(?:写|发|发布|生成|评论|回一句)?(?:一条|一下)?(?:说说|评论|文案)?\s*[:：]\s*",
            "",
            value,
        )
        value = re.sub(r"\s+", " ", value).strip()
        return cls._unquote(value)

    @staticmethod
    def _looks_instruction_like(text: str) -> bool:
        lowered = str(text or "").lower()
        return any(marker.lower() in lowered for marker in INSTRUCTION_MARKERS)

    @classmethod
    def _clean_generated_text(
        cls,
        text: str,
        *,
        fields: tuple[str, ...] = ("content", "text", "message"),
        fallback: str = "",
    ) -> str:
        raw = cls._strip_code_fence(text)
        extracted = cls._extract_json_field(raw, fields) or cls._extract_assignment_field(raw, fields)
        candidate = extracted or raw
        candidate = cls._remove_generation_chatter(candidate)
        if cls._looks_instruction_like(candidate):
            candidate = ""
        if not candidate and fallback:
            fallback_candidate = cls._remove_generation_chatter(fallback)
            if not cls._looks_instruction_like(fallback_candidate):
                candidate = fallback_candidate
        return candidate.strip()

    async def generate_post_text(self, event: Any, topic: str = "", *, history: str = "") -> str:
        prompt = f"{self.settings.post_prompt}\n\n{POST_OUTPUT_RULES}"
        if str(topic or "").strip():
            prompt = f"{prompt}\n\n主题：{str(topic).strip()}"
        if history:
            prompt = f"{prompt}\n\n聊天记录参考：\n{truncate(history, 8000)}"
        text = await self.generate_text(
            event,
            prompt,
            provider_id=self.settings.post_provider_id,
            system_prompt=PERSONA_SYSTEM_PROMPT,
            prefer_current_provider=True,
        )
        return self._clean_generated_text(text, fallback=str(topic or ""))

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
        prompt = f"{self.settings.comment_prompt}\n\n{COMMENT_OUTPUT_RULES}\n\n{self._comment_context(post)}"
        text = await self.generate_text(
            event,
            prompt,
            provider_id=self.settings.comment_provider_id,
            system_prompt=PERSONA_SYSTEM_PROMPT,
            prefer_current_provider=True,
        )
        cleaned = self._clean_generated_text(text, fields=("content", "comment", "text", "message"))
        return self._clean_short_reply(cleaned)

    async def generate_reply_text(self, event: Any, post: QzonePost, comment: QzoneComment) -> str:
        prompt = (
            f"{self.settings.reply_prompt}\n\n{COMMENT_OUTPUT_RULES}\n\n"
            f"{self._comment_context(post)}\n"
            f"要回复的评论：{comment.nickname or comment.uin}: {comment.content}"
        )
        text = await self.generate_text(
            event,
            prompt,
            provider_id=self.settings.reply_provider_id,
            system_prompt=PERSONA_SYSTEM_PROMPT,
            prefer_current_provider=True,
        )
        cleaned = self._clean_generated_text(text, fields=("content", "comment", "reply", "text", "message"))
        return self._clean_short_reply(cleaned)
