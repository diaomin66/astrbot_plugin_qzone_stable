"""Media helpers for building QQ Space posts from AstrBot messages."""

from __future__ import annotations

import contextlib
import mimetypes
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote, urlparse


QZONE_MAX_IMAGES = 9
QZONE_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"}
TEXT_KINDS = {"plain", "text"}
MEDIA_KINDS = {"image", "file", "video", "record", "audio", "voice"}
COMPONENT_STRING_RE = re.compile(r"\b(?:Image|Video|File|Record|Plain)\s*\(|\[CQ:(?:image|video|file|record)\b", re.I)
COMMAND_SEPARATOR_CHARS = ":\uFF1A,\uFF0C;\uFF1B"
COMMAND_PREFIX_CHARS = "/\uFF0F!\uFF01#\uFF03.\uFF0E\u3002~\uFF5E?\uFF1F"
LEADING_SPACE_CHARS = " \t\r\n\f\v\u3000\ufeff\u200b\u200c\u200d"


@dataclass(slots=True)
class PostMedia:
    kind: str
    source: str
    name: str = ""
    mime_type: str = ""
    size: int = 0
    raw_type: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class PostPayload:
    content: str
    media: list[PostMedia]
    attachments: list[PostMedia] = field(default_factory=list)

    def to_request_body(self) -> dict[str, Any]:
        return {
            "content": self.content,
            "media": [item.to_dict() for item in self.media],
        }


def _is_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme.lower() in {"http", "https"}


def _is_base64_source(value: str) -> bool:
    return value.startswith("base64://") or value.startswith("data:")


def _looks_like_path(value: str) -> bool:
    if not value:
        return False
    if value.startswith("file://"):
        return True
    if Path(value).exists():
        return True
    return bool(re.match(r"^[a-zA-Z]:[\\/]", value) or value.startswith(("/", "\\")))


def normalize_source(value: Any) -> str:
    if value is None:
        return ""
    source = str(value).strip()
    if source.startswith("file://"):
        parsed = urlparse(source)
        if parsed.netloc and parsed.path:
            return unquote(f"//{parsed.netloc}{parsed.path}")
        return unquote(parsed.path)
    return source


def source_name(source: str) -> str:
    if not source:
        return ""
    if _is_url(source) or source.startswith("file://"):
        parsed = urlparse(source)
        name = Path(unquote(parsed.path)).name
    elif _is_base64_source(source):
        name = ""
    else:
        name = Path(source).name
    return name or ""


def guess_mime_type(name_or_source: str) -> str:
    if not name_or_source or _is_base64_source(name_or_source):
        return ""
    guessed, _ = mimetypes.guess_type(name_or_source)
    return guessed or ""


def is_supported_image(media: PostMedia | dict[str, Any]) -> bool:
    if isinstance(media, dict):
        kind = str(media.get("kind") or media.get("type") or "").lower()
        source = str(media.get("source") or media.get("file") or media.get("url") or media.get("path") or "")
        name = str(media.get("name") or source_name(source) or "")
        mime_type = str(media.get("mime_type") or media.get("mime") or guess_mime_type(name or source) or "")
    else:
        kind = media.kind
        source = media.source
        name = media.name
        mime_type = media.mime_type or guess_mime_type(name or source)

    if kind == "image":
        return True
    if mime_type.lower().startswith("image/"):
        return True
    suffix = Path(name or source).suffix.lower()
    return suffix in QZONE_IMAGE_SUFFIXES


def normalize_media_item(item: Any, *, default_kind: str = "file") -> PostMedia | None:
    if item is None:
        return None
    if isinstance(item, PostMedia):
        return item
    if isinstance(item, str):
        source = normalize_source(item)
        if not source:
            return None
        name = source_name(source)
        media = PostMedia(
            kind=default_kind,
            source=source,
            name=name,
            mime_type=guess_mime_type(name or source),
        )
        if is_supported_image(media):
            media.kind = "image"
        return media
    if isinstance(item, dict):
        source = normalize_source(item.get("source") or item.get("file") or item.get("url") or item.get("path") or "")
        if not source:
            return None
        kind = str(item.get("kind") or item.get("type") or default_kind).lower()
        if kind == "voice":
            kind = "audio"
        name = str(item.get("name") or item.get("filename") or source_name(source) or "")
        mime_type = str(item.get("mime_type") or item.get("mime") or guess_mime_type(name or source) or "")
        size_value = item.get("size") or 0
        try:
            size = int(size_value or 0)
        except (TypeError, ValueError):
            size = 0
        media = PostMedia(kind=kind, source=source, name=name, mime_type=mime_type, size=size, raw_type=kind)
        if is_supported_image(media):
            media.kind = "image"
        return media
    return None


def normalize_media_list(items: Iterable[Any] | None) -> list[PostMedia]:
    if isinstance(items, (str, dict, PostMedia)):
        items = [items]
    media: list[PostMedia] = []
    for item in items or []:
        normalized = normalize_media_item(item)
        if normalized:
            media.append(normalized)
    return media


def split_publishable_images(media: Iterable[PostMedia]) -> tuple[list[PostMedia], list[PostMedia]]:
    images: list[PostMedia] = []
    fallback: list[PostMedia] = []
    for item in media:
        if is_supported_image(item):
            normalized = PostMedia(
                kind="image",
                source=item.source,
                name=item.name or source_name(item.source),
                mime_type=item.mime_type or guess_mime_type(item.name or item.source),
                size=item.size,
                raw_type=item.raw_type or item.kind,
            )
            images.append(normalized)
        else:
            fallback.append(item)
    return images, fallback


def media_reference_text(media: PostMedia) -> str:
    labels = {
        "file": "文件",
        "video": "视频",
        "audio": "音频",
        "record": "语音",
        "voice": "语音",
        "image": "图片",
    }
    label = labels.get(media.kind, "附件")
    name = media.name or source_name(media.source) or label
    if media.source and media.source != name:
        return f"[{label}: {name}] {media.source}"
    return f"[{label}: {name}]"


def _component_kind(component: Any) -> str:
    if isinstance(component, str):
        return "plain"
    if isinstance(component, dict):
        raw = component.get("type") or component.get("kind") or component.get("message_type") or ""
    else:
        raw = getattr(component, "type", None) or getattr(component, "kind", None) or component.__class__.__name__
    kind = str(raw or "").split(".")[-1].lower()
    aliases = {
        "plain": "plain",
        "text": "plain",
        "image": "image",
        "picture": "image",
        "file": "file",
        "video": "video",
        "record": "record",
        "voice": "audio",
        "audio": "audio",
    }
    return aliases.get(kind, kind)


def _component_mapping(component: Any) -> dict[str, Any]:
    if isinstance(component, dict):
        data = component.get("data")
        merged = dict(component)
        if isinstance(data, dict):
            merged.update(data)
        return merged
    data: dict[str, Any] = {}
    component_data = getattr(component, "data", None)
    if isinstance(component_data, dict):
        data.update(component_data)
    for attr in (
        "text",
        "content",
        "message",
        "file",
        "url",
        "path",
        "name",
        "filename",
        "mime",
        "mime_type",
        "size",
    ):
        if hasattr(component, attr):
            data[attr] = getattr(component, attr)
    return data


def _component_text(component: Any) -> str:
    data = _component_mapping(component)
    for key in ("text", "content", "message"):
        value = data.get(key)
        if value not in (None, ""):
            return str(value)
    if isinstance(component, str):
        return component
    return ""


def _choose_media_source(data: dict[str, Any]) -> str:
    candidates = [
        data.get("url"),
        data.get("path"),
        data.get("file"),
        data.get("source"),
        data.get("attachment_id"),
    ]
    normalized = [normalize_source(value) for value in candidates if value not in (None, "")]
    for value in normalized:
        if _is_base64_source(value) or (not _is_url(value) and _looks_like_path(value)):
            return value
    for value in normalized:
        if _is_url(value) or _is_base64_source(value) or _looks_like_path(value):
            return value
    return normalized[0] if normalized else ""


def _component_media(component: Any, kind: str) -> PostMedia | None:
    data = _component_mapping(component)
    source = _choose_media_source(data)
    if not source:
        return None
    name = str(data.get("name") or data.get("filename") or source_name(source) or "")
    mime_type = str(data.get("mime_type") or data.get("mime") or guess_mime_type(name or source) or "")
    try:
        size = int(data.get("size") or 0)
    except (TypeError, ValueError):
        size = 0
    media = PostMedia(kind=kind, source=source, name=name, mime_type=mime_type, size=size, raw_type=kind)
    if is_supported_image(media):
        media.kind = "image"
    return media


def _event_message_text(event: Any) -> str:
    message_obj = getattr(event, "message_obj", None)
    for owner in (event, message_obj):
        value = getattr(owner, "message_str", None)
        if isinstance(value, str) and value:
            return value
        getter = getattr(owner, "get_message_str", None)
        if callable(getter):
            with contextlib.suppress(Exception):
                value = getter()
            if isinstance(value, str) and value:
                return value
    return ""


def iter_event_components(event: Any) -> list[Any]:
    message_obj = getattr(event, "message_obj", None)
    candidates = [
        getattr(message_obj, "message", None),
        getattr(message_obj, "messages", None),
        getattr(message_obj, "chain", None),
        getattr(message_obj, "message_chain", None),
        getattr(event, "message", None),
        getattr(event, "messages", None),
        getattr(event, "chain", None),
        getattr(event, "message_chain", None),
    ]
    for candidate in candidates:
        if candidate is None:
            continue
        if isinstance(candidate, (list, tuple)) and candidate:
            return list(candidate)
        inner = getattr(candidate, "chain", None) or getattr(candidate, "messages", None)
        if isinstance(inner, (list, tuple)) and inner:
            return list(inner)
    raw = getattr(message_obj, "raw_message", None) or getattr(event, "raw_message", None)
    if isinstance(raw, list) and raw:
        return list(raw)
    if isinstance(raw, dict) and isinstance(raw.get("message"), list) and raw.get("message"):
        return list(raw["message"])
    event_text = _event_message_text(event)
    if event_text:
        return [event_text]
    return []


def _strip_leading_command_noise(text: str) -> tuple[str, bool]:
    stripped_noise = False
    value = text.lstrip(LEADING_SPACE_CHARS)
    stripped_noise = stripped_noise or value != text
    while value:
        match = re.match(r"\[CQ:at,[^\]]+\]\s*", value, re.I)
        if match:
            value = value[match.end() :].lstrip(LEADING_SPACE_CHARS)
            stripped_noise = True
            continue
        mention_boundary = re.escape(LEADING_SPACE_CHARS + COMMAND_SEPARATOR_CHARS + COMMAND_PREFIX_CHARS)
        match = re.match(r"@\S+?(?:[" + mention_boundary + r"]+|$)", value)
        if match:
            value = value[match.end() :].lstrip(LEADING_SPACE_CHARS)
            stripped_noise = True
            continue
        break
    return value, stripped_noise


def _strip_command_separator(text: str) -> str:
    value = text.lstrip()
    if value[:1] in COMMAND_SEPARATOR_CHARS:
        value = value[1:].lstrip()
    return value


def strip_command_prefix(text: str, prefixes: Iterable[str]) -> str:
    stripped, stripped_noise = _strip_leading_command_noise(text)
    for prefix in prefixes:
        prefix = prefix.strip().lstrip("/\uff0f").strip()
        if not prefix:
            continue
        command_marker = r"(?:[" + re.escape(COMMAND_PREFIX_CHARS) + r"]+\s*)?"
        pattern = r"^" + command_marker + r"\s*" + r"\s+".join(re.escape(part) for part in prefix.split())
        match = re.match(pattern, stripped, re.I)
        if match:
            return _strip_command_separator(stripped[match.end() :])
    if stripped_noise:
        return text
    return text


def looks_like_component_string(text: str) -> bool:
    return bool(text and COMPONENT_STRING_RE.search(text))


def join_text_parts_for_command_scan(parts: Iterable[str]) -> str:
    text = ""
    for part in parts:
        if not part:
            continue
        if text and not text[-1].isspace() and not part[0].isspace():
            text += " "
        text += part
    return text


def strip_command_prefix_from_parts(text: str, parts: Iterable[str], prefixes: Iterable[str]) -> str:
    stripped = strip_command_prefix(text, prefixes).strip()
    if stripped != text:
        return stripped
    spaced = join_text_parts_for_command_scan(parts).strip()
    if spaced and spaced != text:
        stripped_spaced = strip_command_prefix(spaced, prefixes).strip()
        if stripped_spaced != spaced:
            return stripped_spaced
    return stripped


def collect_post_payload(
    event: Any,
    *,
    fallback_content: str = "",
    include_event_text: bool = True,
    command_prefixes: Iterable[str] = (),
    extra_media: Iterable[Any] | None = None,
) -> PostPayload:
    content_parts: list[str] = []
    reference_parts: list[str] = []
    media: list[PostMedia] = []
    attachments: list[PostMedia] = []
    first_text = True
    event_prefix_stripped = False
    components = iter_event_components(event)
    event_text = _event_message_text(event)
    event_text_consumed = False

    for component in components:
        kind = _component_kind(component)
        if kind in TEXT_KINDS:
            text = _component_text(component)
            if first_text and command_prefixes:
                original_text = text
                text = strip_command_prefix(text, command_prefixes)
                event_prefix_stripped = text != original_text
            first_text = False
            if include_event_text and text:
                content_parts.append(text)
            continue
        if kind in MEDIA_KINDS:
            item = _component_media(component, kind)
            if not item:
                continue
            if item.kind == "image":
                media.append(item)
            else:
                attachments.append(item)
                reference_parts.append(media_reference_text(item))

    for item in normalize_media_list(extra_media):
        if item.kind == "image":
            media.append(item)
        else:
            attachments.append(item)
            reference_parts.append(media_reference_text(item))
    if include_event_text and command_prefixes and event_text:
        event_content = strip_command_prefix(event_text, command_prefixes).strip()
        if event_content != event_text.strip():
            event_text_consumed = True
            if media and looks_like_component_string(event_content):
                content_parts = []
            else:
                content_parts = [event_content] if event_content else []
            event_prefix_stripped = True
    if include_event_text and not content_parts and components and not event_text_consumed:
        if event_text and not (media and looks_like_component_string(event_text)):
            content_parts.append(event_text)
    content = "".join(content_parts).strip() if include_event_text else ""
    if content and command_prefixes and not event_prefix_stripped:
        content = strip_command_prefix_from_parts(content, content_parts, command_prefixes)
    fallback = str(fallback_content or "").strip()
    if command_prefixes:
        fallback = strip_command_prefix(fallback, command_prefixes).strip()
    use_fallback = bool(fallback and not (media and looks_like_component_string(fallback)))
    if not content and use_fallback:
        content = fallback
    if not include_event_text and use_fallback:
        content = fallback
    if reference_parts:
        refs = "\n".join(reference_parts)
        content = "\n".join(part for part in (content, refs) if part)
    return PostPayload(content=content, media=media, attachments=attachments)
