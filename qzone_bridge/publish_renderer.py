"""QQ Space-style image renderer for published post results."""

from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import math
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote_to_bytes, urlparse

import httpx
from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError

from .media import PostMedia, PostPayload, source_name


WHITE = (255, 255, 255)
TEXT = (18, 18, 18)
MUTED = (96, 104, 112)
LINE = (226, 226, 226)
ACTION = (24, 24, 24)
CARD_BG = (250, 250, 250)
FILE_COLORS = {
    ".pdf": (216, 74, 64),
    ".doc": (64, 112, 205),
    ".docx": (64, 112, 205),
    ".xls": (56, 145, 91),
    ".xlsx": (56, 145, 91),
    ".ppt": (218, 109, 57),
    ".pptx": (218, 109, 57),
    ".zip": (132, 102, 193),
    ".rar": (132, 102, 193),
    ".7z": (132, 102, 193),
    ".mp4": (77, 145, 210),
    ".mov": (77, 145, 210),
    ".mp3": (205, 107, 184),
    ".wav": (205, 107, 184),
    ".txt": (112, 121, 130),
    ".md": (112, 121, 130),
}
FONT_CACHE: dict[tuple[int, bool], ImageFont.ImageFont] = {}
QUALITY_RESAMPLE = Image.Resampling.LANCZOS
RENDER_SCALE = 3
PREVIEW_MAX_EDGE = 3200
SOURCE_IMAGE_MAX_BYTES = 32 * 1024 * 1024
ACTION_STRIP_DEFAULT_WIDTH = 260 * RENDER_SCALE
PREVIEW_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="qzone-render")
_THREAD_LOCAL = threading.local()
_BYTES_CACHE: dict[str, tuple[float, bytes]] = {}
_BYTES_CACHE_LOCK = threading.Lock()
_BYTES_CACHE_TTL = 10 * 60
_BYTES_CACHE_MAX_ITEMS = 64
_BYTES_CACHE_MAX_ITEM_SIZE = 4 * 1024 * 1024
_LAST_PRUNE_AT = 0.0
_PRUNE_INTERVAL_SECONDS = 60.0
ASSET_DIR = Path(__file__).with_name("assets")
ACTION_STRIP_ASSET = ASSET_DIR / "publish_actions.png"
FONT_ASSET_DIR = ASSET_DIR / "fonts"
REGULAR_FONT_ASSET = FONT_ASSET_DIR / "AlibabaPuHuiTi-3-55-Regular.ttf"
BOLD_FONT_ASSET = FONT_ASSET_DIR / "AlibabaPuHuiTi-3-75-SemiBold.ttf"
_ACTION_STRIP_CACHE: dict[tuple[str, int], Image.Image] = {}
_AVATAR_MASK_CACHE: dict[tuple[int, int], Image.Image] = {}


@dataclass(slots=True)
class RenderProfile:
    nickname: str = ""
    user_id: str = ""
    avatar_source: str = ""
    time_text: str = ""


@dataclass(slots=True)
class _ImagePreview:
    media: PostMedia
    image: Image.Image | None
    failed: bool = False


def preload_static_render_assets() -> None:
    """Warm fonts and the static action-strip image before the first publish render."""

    for size, bold in ((27, True), (25, True), (24, False), (22, False), (20, False), (18, False), (17, False), (34, True)):
        _font(_scale_px(size), bold=bold)
    for target_width in (220, 230, 240, 250, 260):
        _action_strip(_scale_px(target_width))


def preload_publish_render_assets(
    profile: RenderProfile,
    cache_dir: Path,
    *,
    avatar_sources: list[str] | tuple[str, ...] = (),
    remote_timeout: float = 2.5,
) -> RenderProfile:
    """Resolve render assets to local cached files so publish rendering does no profile I/O."""

    preload_static_render_assets()
    resolved = RenderProfile(
        nickname=profile.nickname,
        user_id=profile.user_id,
        avatar_source=profile.avatar_source,
        time_text=profile.time_text,
    )
    cache_path = _avatar_cache_path(cache_dir, profile)
    if cache_path.is_file():
        resolved.avatar_source = str(cache_path)
        return resolved

    seen: set[str] = set()
    sources = [profile.avatar_source, *avatar_sources]
    for source in sources:
        source = str(source or "").strip()
        if not source or source in seen:
            continue
        seen.add(source)
        preview = _load_image_preview(
            PostMedia(kind="image", source=source, name="avatar"),
            remote_timeout=remote_timeout,
        )
        if preview.image is None:
            continue
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            avatar = preview.image.copy()
            avatar.thumbnail((PREVIEW_MAX_EDGE, PREVIEW_MAX_EDGE), QUALITY_RESAMPLE)
            avatar.save(cache_path, "PNG", optimize=False, compress_level=1)
        except OSError:
            continue
        resolved.avatar_source = str(cache_path)
        return resolved

    resolved.avatar_source = ""
    return resolved


def cached_avatar_source(cache_dir: Path, profile: RenderProfile) -> str:
    cache_path = _avatar_cache_path(cache_dir, profile)
    return str(cache_path) if cache_path.is_file() else ""


def _avatar_cache_path(cache_dir: Path, profile: RenderProfile) -> Path:
    user_id = re.sub(r"\D+", "", str(profile.user_id or ""))
    if user_id:
        stem = f"avatar_{user_id}"
    else:
        seed = f"{profile.nickname}|{profile.avatar_source}".encode("utf-8", "ignore")
        stem = "avatar_" + hashlib.sha1(seed).hexdigest()[:16]
    return cache_dir / f"{stem}.png"


def profile_from_event(event: Any) -> RenderProfile:
    """Best-effort sender profile extraction from AstrBot-like events."""

    message_obj = getattr(event, "message_obj", None)
    sender = getattr(message_obj, "sender", None) or getattr(event, "sender", None)
    owners = [event, message_obj, sender]

    nickname = ""
    for getter_name in ("get_sender_name", "get_sender_nickname"):
        getter = getattr(event, getter_name, None)
        if callable(getter):
            try:
                value = getter()
            except Exception:
                value = ""
            if value:
                nickname = str(value)
                break
    if not nickname:
        for owner in owners:
            for attr in ("card", "nickname", "nick", "name", "username", "display_name"):
                value = getattr(owner, attr, None)
                if value:
                    nickname = str(value)
                    break
            if nickname:
                break

    user_id = ""
    for getter_name in ("get_sender_id", "get_user_id"):
        getter = getattr(event, getter_name, None)
        if callable(getter):
            try:
                value = getter()
            except Exception:
                value = ""
            if value:
                user_id = str(value)
                break
    if not user_id:
        for owner in owners:
            value = getattr(owner, "user_id", None) or getattr(owner, "uin", None) or getattr(owner, "qq", None)
            if value:
                user_id = str(value)
                break

    avatar_source = ""
    for owner in owners:
        for attr in ("avatar", "avatar_url", "avatar_path", "face", "face_url"):
            value = getattr(owner, attr, None)
            if value:
                avatar_source = str(value)
                break
        if avatar_source:
            break

    return RenderProfile(
        nickname=nickname or user_id or "QQ Space",
        user_id=user_id,
        avatar_source=avatar_source,
        time_text=datetime.now().strftime("%H:%M"),
    )


def render_publish_result_image(
    post: PostPayload,
    output_dir: Path,
    *,
    profile: RenderProfile | None = None,
    result: dict[str, Any] | None = None,
    width: int = 900,
    remote_timeout: float = 1.5,
) -> Path:
    """Render a published post into a PNG and return the file path."""

    output_dir.mkdir(parents=True, exist_ok=True)
    _prune_output_dir(output_dir)
    profile = profile or RenderProfile(nickname="QQ Space", time_text=datetime.now().strftime("%H:%M"))
    if not profile.time_text:
        profile.time_text = datetime.now().strftime("%H:%M")
    if not profile.nickname:
        profile.nickname = profile.user_id or "QQ Space"

    render_scale = RENDER_SCALE
    scratch = ImageDraw.Draw(Image.new("RGB", (1, 1), WHITE))
    content_text = _render_content_text(post)
    logical_width = _adaptive_logical_width(
        post,
        int(width or 900),
        scratch,
        content_text,
        scale=render_scale,
    )
    width = _scale_px(logical_width, render_scale)
    margin = _scale_px(24, render_scale)
    content_width = width - margin * 2
    meta_font = _font(_scale_px(18, render_scale))
    small_font = _font(_scale_px(17, render_scale))

    text_font, text_lines, line_spacing = _content_text_layout(
        scratch,
        content_text,
        content_width,
        scale=render_scale,
    )
    dense_layout = len(text_lines) > 8 or len(post.media) + len(post.attachments) > 2
    compact_text_len = len(re.sub(r"\s+", "", content_text))
    short_text_only = not post.media and not post.attachments and compact_text_len <= 45
    avatar_size = _scale_px(70 if len(text_lines) > 12 else 76 if dense_layout else 80, render_scale)
    header_y = _scale_px(22 if short_text_only else 24 if dense_layout else 26, render_scale)
    header_gap = _scale_px(16 if short_text_only else 18 if dense_layout else 22, render_scale)
    content_y = header_y + avatar_size + header_gap
    name_font = _font(_scale_px(28 if len(text_lines) > 12 else 30 if dense_layout else 31, render_scale), bold=True)
    time_font = _font(_scale_px(19 if dense_layout else 20, render_scale))
    block_gap = _scale_px(12 if short_text_only else 14 if dense_layout else 18, render_scale)
    action_gap = _scale_px(4 if short_text_only else 6 if dense_layout else 8, render_scale)
    bottom_padding = _scale_px(16 if short_text_only else 20 if dense_layout else 24, render_scale)
    line_height = _line_height(scratch, text_font, line_spacing)
    text_height = len(text_lines) * line_height if text_lines else 0

    preview_targets: list[PostMedia] = []
    avatar_offset = 0
    if profile.avatar_source:
        preview_targets.append(PostMedia(kind="image", source=profile.avatar_source, name="avatar"))
        avatar_offset = 1
    preview_targets.extend(post.media[:9])
    loaded_previews = _load_image_previews(preview_targets, remote_timeout=remote_timeout)
    avatar_preview = loaded_previews[0] if avatar_offset else None
    previews = loaded_previews[avatar_offset:]
    image_height = _image_block_height(previews, content_width, scale=render_scale) if previews else 0
    attachment_height = (
        _attachment_block_height(post.attachments, content_width, scale=render_scale) if post.attachments else 0
    )

    y = content_y
    if text_height:
        y += text_height + block_gap
    if image_height:
        y += image_height + block_gap
    if attachment_height:
        y += attachment_height + block_gap
    action_strip = _action_strip(
        _action_strip_render_width(logical_width, dense_layout=dense_layout, scale=render_scale)
    )
    actions_y = y + action_gap
    height = max(_scale_px(240, render_scale), actions_y + action_strip.height + bottom_padding)

    image = Image.new("RGB", (width, height), WHITE)
    draw = ImageDraw.Draw(image)
    _draw_header(
        draw,
        image,
        profile,
        margin,
        name_font,
        time_font,
        avatar_preview=avatar_preview,
        avatar_size=avatar_size,
        avatar_y=header_y,
        scale=render_scale,
    )

    y = content_y
    if text_lines:
        for line in text_lines:
            _safe_text(draw, (margin, y), line, text_font, TEXT)
            y += line_height
        y += block_gap
    if previews:
        _draw_image_block(draw, image, previews, margin, y, content_width, small_font, scale=render_scale)
        y += image_height + block_gap
    if post.attachments:
        _draw_attachment_block(
            draw,
            post.attachments,
            margin,
            y,
            content_width,
            meta_font,
            small_font,
            scale=render_scale,
        )
        y += attachment_height + block_gap

    actions_y = y + action_gap
    _draw_actions(image, width, actions_y, strip=action_strip, scale=render_scale)

    path = output_dir / f"publish_result_{int(time.time())}_{uuid.uuid4().hex[:10]}.png"
    image.save(path, "PNG", optimize=False, compress_level=1)
    return path


def _render_content_text(post: PostPayload) -> str:
    return str(post.content or "").strip()


def _scale_px(value: int | float, scale: int = RENDER_SCALE) -> int:
    return max(1, int(round(float(value) * scale)))


def _adaptive_logical_width(
    post: PostPayload,
    requested_width: int,
    draw: ImageDraw.ImageDraw,
    content_text: str,
    *,
    scale: int,
) -> int:
    requested = max(520, min(int(requested_width or 900), 1280))
    if post.media or post.attachments:
        return max(640, requested)

    compact_len = len(re.sub(r"\s+", "", str(content_text or "")))
    if compact_len <= 0:
        return min(requested, 560)

    text_size = _preferred_content_font_size(compact_len)
    font = _font(_scale_px(text_size, scale))
    paragraphs = [line.strip() for line in str(content_text).replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    longest_px = max((_measure(draw, line, font) for line in paragraphs if line), default=0)
    natural = math.ceil(longest_px / max(1, scale)) + 76

    if compact_len <= 18:
        min_width, max_width = 520, 620
    elif compact_len <= 45:
        min_width, max_width = 560, 720
    elif compact_len <= 90:
        min_width, max_width = 640, 820
    else:
        min_width, max_width = 700, requested
    return max(min_width, min(requested, max_width, natural))


def _preferred_content_font_size(compact_len: int) -> int:
    if compact_len <= 45:
        return 30
    if compact_len <= 120:
        return 29
    if compact_len <= 260:
        return 27
    return 25


def _content_text_layout(
    draw: ImageDraw.ImageDraw,
    text: str,
    max_width: int,
    *,
    scale: int = 1,
) -> tuple[ImageFont.ImageFont, list[str], float]:
    compact_text = re.sub(r"\s+", "", str(text or ""))
    if not compact_text:
        return _font(_scale_px(27, scale)), [], 1.26
    if len(compact_text) <= 45:
        candidates = ((30, 4, 1.2), (29, 6, 1.2), (28, 999, 1.2))
    elif len(compact_text) <= 120:
        candidates = ((29, 8, 1.22), (28, 11, 1.22), (27, 999, 1.22))
    elif len(compact_text) <= 260:
        candidates = ((27, 12, 1.24), (26, 17, 1.24), (25, 999, 1.24))
    else:
        candidates = ((25, 18, 1.25), (24, 26, 1.25), (23, 999, 1.25))
    for size, line_limit, spacing in candidates:
        font = _font(_scale_px(size, scale))
        lines = _wrap_text(draw, text, font, max_width)
        if len(lines) <= line_limit:
            return font, lines, spacing
    size, _line_limit, spacing = candidates[-1]
    font = _font(_scale_px(size, scale))
    return font, _wrap_text(draw, text, font, max_width), spacing


def _font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    key = (int(size), bool(bold))
    cached = FONT_CACHE.get(key)
    if cached is not None:
        return cached

    regular = [
        REGULAR_FONT_ASSET,
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\simhei.ttf",
        r"C:\Windows\Fonts\simsun.ttc",
        "/System/Library/Fonts/PingFang.ttc",
        "/Library/Fonts/Arial Unicode.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    bold_fonts = [
        BOLD_FONT_ASSET,
        r"C:\Windows\Fonts\msyhbd.ttc",
        r"C:\Windows\Fonts\simhei.ttf",
        "/System/Library/Fonts/PingFang.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ]
    for candidate in bold_fonts if bold else regular:
        try:
            if Path(candidate).exists():
                font = ImageFont.truetype(candidate, size=size)
                FONT_CACHE[key] = font
                return font
        except Exception:
            continue
    try:
        font = ImageFont.truetype("arial.ttf", size=size)
    except Exception:
        font = ImageFont.load_default()
    FONT_CACHE[key] = font
    return font


def _line_height(draw: ImageDraw.ImageDraw, font: ImageFont.ImageFont, factor: float = 1.25) -> int:
    box = draw.textbbox((0, 0), "Ag", font=font)
    return max(12, int((box[3] - box[1]) * factor))


def _measure(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> int:
    if not text:
        return 0
    try:
        box = draw.textbbox((0, 0), text, font=font)
    except UnicodeEncodeError:
        box = draw.textbbox((0, 0), _ascii_fallback(text), font=font)
    return max(0, box[2] - box[0])


def _safe_text(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    font: ImageFont.ImageFont,
    fill: tuple[int, int, int],
) -> None:
    try:
        draw.text(xy, text, font=font, fill=fill)
    except UnicodeEncodeError:
        draw.text(xy, _ascii_fallback(text), font=font, fill=fill)


def _ascii_fallback(text: str) -> str:
    return text.encode("ascii", "replace").decode("ascii")


def _wrap_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: int) -> list[str]:
    text = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        return []
    lines: list[str] = []
    for paragraph in text.split("\n"):
        if not paragraph:
            lines.append("")
            continue
        current = ""
        for char in paragraph.replace("\t", " "):
            candidate = current + char
            if _measure(draw, candidate, font) <= max_width:
                current = candidate
                continue
            if current:
                lines.append(current.rstrip())
                current = char.lstrip()
            else:
                lines.append(char)
                current = ""
        if current:
            lines.append(current.rstrip())
    return lines


def _truncate_to_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: int) -> str:
    if _measure(draw, text, font) <= max_width:
        return text
    suffix = "..."
    current = ""
    for char in text:
        candidate = current + char + suffix
        if _measure(draw, candidate, font) > max_width:
            break
        current += char
    return (current.rstrip() + suffix) if current else suffix


def _load_image_preview(media: PostMedia, *, remote_timeout: float) -> _ImagePreview:
    data = _read_source_bytes(media.source, max_bytes=SOURCE_IMAGE_MAX_BYTES, remote_timeout=remote_timeout)
    if not data:
        return _ImagePreview(media=media, image=None, failed=True)
    try:
        with Image.open(io.BytesIO(data)) as opened:
            opened.seek(0)
            preview = ImageOps.exif_transpose(opened)
            if preview.mode not in {"RGB", "RGBA"}:
                preview = preview.convert("RGB")
            elif preview.mode == "RGBA":
                base = Image.new("RGB", preview.size, WHITE)
                base.paste(preview, mask=preview.getchannel("A"))
                preview = base
            else:
                preview = preview.copy()
            preview.thumbnail((PREVIEW_MAX_EDGE, PREVIEW_MAX_EDGE), QUALITY_RESAMPLE)
            return _ImagePreview(media=media, image=preview, failed=False)
    except (OSError, UnidentifiedImageError):
        return _ImagePreview(media=media, image=None, failed=True)


def _load_image_previews(items: list[PostMedia], *, remote_timeout: float) -> list[_ImagePreview]:
    if not items:
        return []
    if len(items) == 1:
        return [_load_image_preview(items[0], remote_timeout=remote_timeout)]
    futures = [
        PREVIEW_EXECUTOR.submit(_load_image_preview, item, remote_timeout=remote_timeout)
        for item in items
    ]
    previews: list[_ImagePreview] = []
    for item, future in zip(items, futures):
        try:
            previews.append(future.result())
        except Exception:
            previews.append(_ImagePreview(media=item, image=None, failed=True))
    return previews


def _read_source_bytes(source: str, *, max_bytes: int, remote_timeout: float) -> bytes:
    source = str(source or "").strip()
    if not source:
        return b""
    cache_key = _bytes_cache_key(source)
    cached = _get_cached_bytes(cache_key)
    if cached:
        return cached[:max_bytes]
    if source.startswith("base64://"):
        try:
            return base64.b64decode(source[len("base64://") :], validate=False)[:max_bytes]
        except Exception:
            return b""
    if source.startswith("data:"):
        try:
            header, encoded = source.split(",", 1)
        except ValueError:
            return b""
        if ";base64" in header:
            try:
                return base64.b64decode(encoded, validate=False)[:max_bytes]
            except Exception:
                return b""
        return unquote_to_bytes(encoded)[:max_bytes]

    parsed = urlparse(source)
    if parsed.scheme.lower() in {"http", "https"}:
        try:
            client = _thread_http_client()
            with client.stream(
                "GET",
                source,
                timeout=httpx.Timeout(remote_timeout),
                follow_redirects=True,
            ) as response:
                if response.status_code >= 400:
                    return b""
                length = response.headers.get("content-length")
                if length and int(length) > max_bytes:
                    return b""
                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_bytes():
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > max_bytes:
                        return b""
                    chunks.append(chunk)
            data = b"".join(chunks)
            _store_cached_bytes(cache_key, data)
            return data
        except Exception:
            return b""

    if source.startswith("file://"):
        parsed = urlparse(source)
        source = parsed.path or ""
    path = Path(source)
    try:
        stat = path.stat()
        if not path.is_file() or stat.st_size > max_bytes:
            return b""
        local_key = f"file:{path.resolve()}:{stat.st_mtime_ns}:{stat.st_size}"
        cached = _get_cached_bytes(local_key)
        if cached:
            return cached[:max_bytes]
        data = path.read_bytes()
        _store_cached_bytes(local_key, data)
        return data
    except OSError:
        return b""


def _thread_http_client() -> httpx.Client:
    client = getattr(_THREAD_LOCAL, "http_client", None)
    if client is None:
        client = httpx.Client(trust_env=False)
        _THREAD_LOCAL.http_client = client
    return client


def _bytes_cache_key(source: str) -> str:
    parsed = urlparse(source)
    if parsed.scheme.lower() in {"http", "https"}:
        return f"url:{source}"
    return ""


def _get_cached_bytes(key: str) -> bytes:
    if not key:
        return b""
    now = time.monotonic()
    with _BYTES_CACHE_LOCK:
        cached = _BYTES_CACHE.get(key)
        if not cached:
            return b""
        expires_at, data = cached
        if expires_at <= now:
            _BYTES_CACHE.pop(key, None)
            return b""
        return data


def _store_cached_bytes(key: str, data: bytes) -> None:
    if not key or not data or len(data) > _BYTES_CACHE_MAX_ITEM_SIZE:
        return
    now = time.monotonic()
    with _BYTES_CACHE_LOCK:
        if len(_BYTES_CACHE) >= _BYTES_CACHE_MAX_ITEMS:
            oldest_key = min(_BYTES_CACHE, key=lambda item: _BYTES_CACHE[item][0])
            _BYTES_CACHE.pop(oldest_key, None)
        _BYTES_CACHE[key] = (now + _BYTES_CACHE_TTL, data)


def _image_block_height(previews: list[_ImagePreview], width: int, *, scale: int = 1) -> int:
    if len(previews) == 1:
        return _single_image_size(previews[0], width, scale=scale)[1]
    cols = _grid_columns(len(previews))
    gap = _scale_px(8, scale)
    tile = (width - gap * (cols - 1)) // cols
    rows = math.ceil(len(previews) / cols)
    return rows * tile + gap * (rows - 1)


def _single_image_size(preview: _ImagePreview, width: int, *, scale: int = 1) -> tuple[int, int]:
    max_w = min(width, _scale_px(540, scale))
    max_h = _scale_px(690, scale)
    if preview.image is None:
        return min(max_w, _scale_px(420, scale)), _scale_px(280, scale)
    source_w, source_h = preview.image.size
    if source_w <= 0 or source_h <= 0:
        return min(max_w, _scale_px(420, scale)), _scale_px(280, scale)
    fit_scale = min(max_w / source_w, max_h / source_h)
    if fit_scale > 1:
        fit_scale = min(fit_scale, 1.35)
    return max(_scale_px(120, scale), int(source_w * fit_scale)), max(
        _scale_px(120, scale),
        int(source_h * fit_scale),
    )


def _grid_columns(count: int) -> int:
    if count <= 1:
        return 1
    if count in {2, 4}:
        return 2
    return 3


def _attachment_block_height(attachments: list[PostMedia], width: int, *, scale: int = 1) -> int:
    cols = 2 if width >= _scale_px(620, scale) else 1
    rows = math.ceil(len(attachments) / cols)
    gap = _scale_px(10, scale)
    card_h = _scale_px(76, scale)
    return rows * card_h + gap * (rows - 1)


def _draw_header(
    draw: ImageDraw.ImageDraw,
    image: Image.Image,
    profile: RenderProfile,
    margin: int,
    name_font: ImageFont.ImageFont,
    time_font: ImageFont.ImageFont,
    *,
    avatar_preview: _ImagePreview | None = None,
    avatar_size: int = 76,
    avatar_y: int = 26,
    scale: int = 1,
) -> None:
    avatar_x = margin
    _draw_avatar(draw, image, profile, avatar_x, avatar_y, avatar_size, preview=avatar_preview)
    text_x = avatar_x + avatar_size + _scale_px(18, scale)
    name_height = _line_height(draw, name_font, 1.0)
    time_height = _line_height(draw, time_font, 1.0)
    text_block_height = name_height + _scale_px(8, scale) + time_height
    text_y = avatar_y + max(0, (avatar_size - text_block_height) // 2) - _scale_px(1, scale)
    name = _truncate_to_width(draw, profile.nickname, name_font, max(_scale_px(20, scale), image.width - text_x - _scale_px(80, scale)))
    _safe_text(draw, (text_x, text_y), name, name_font, TEXT)
    _safe_text(draw, (text_x, text_y + name_height + _scale_px(8, scale)), profile.time_text, time_font, MUTED)

    x = image.width - _scale_px(44, scale)
    y = avatar_y + _scale_px(8, scale)
    draw.line(
        [(x, y), (x + _scale_px(10, scale), y + _scale_px(10, scale)), (x + _scale_px(20, scale), y)],
        fill=ACTION,
        width=_scale_px(3, scale),
    )


def _draw_avatar(
    draw: ImageDraw.ImageDraw,
    image: Image.Image,
    profile: RenderProfile,
    x: int,
    y: int,
    size: int,
    *,
    preview: _ImagePreview | None = None,
) -> None:
    if preview and preview.image:
        avatar = _smooth_circle_image(preview.image, size)
        image.paste(avatar.convert("RGB"), (x, y), avatar.getchannel("A"))
        return

    avatar = _fallback_avatar_image(profile, size)
    image.paste(avatar.convert("RGB"), (x, y), avatar.getchannel("A"))


def _smooth_circle_image(source: Image.Image, size: int, *, scale: int = 4) -> Image.Image:
    avatar = ImageOps.fit(source.convert("RGB"), (size, size), method=QUALITY_RESAMPLE).convert("RGBA")
    avatar.putalpha(_circle_mask(size, scale_key=scale))
    return avatar


def _fallback_avatar_image(profile: RenderProfile, size: int, *, scale: int = 4) -> Image.Image:
    large_size = max(size, int(size) * max(2, int(scale)))
    avatar = Image.new("RGBA", (large_size, large_size), (0, 0, 0, 0))
    mask = _circle_mask(large_size)
    color_layer = Image.new("RGBA", (large_size, large_size), (*_profile_color(profile.nickname or profile.user_id), 255))
    avatar.paste(color_layer, (0, 0), mask)
    draw = ImageDraw.Draw(avatar)
    initial = (profile.nickname or profile.user_id or "Q")[:1].upper()
    font = _font(max(12, 34 * max(2, int(scale))), bold=True)
    box = draw.textbbox((0, 0), initial, font=font)
    _safe_text(
        draw,
        ((large_size - (box[2] - box[0])) // 2, (large_size - (box[3] - box[1])) // 2 - 2 * scale),
        initial,
        font,
        WHITE,
    )
    return avatar.resize((size, size), QUALITY_RESAMPLE)


def _circle_mask(size: int, *, scale_key: int = 1) -> Image.Image:
    key = (int(size), max(1, int(scale_key)))
    cached = _AVATAR_MASK_CACHE.get(key)
    if cached is not None:
        return cached
    scale = key[1]
    large_size = int(size) * scale
    mask = Image.new("L", (large_size, large_size), 0)
    mask_draw = ImageDraw.Draw(mask)
    inset = max(1, large_size // 180)
    mask_draw.ellipse((inset, inset, large_size - inset - 1, large_size - inset - 1), fill=255)
    if scale > 1:
        mask = mask.resize((int(size), int(size)), QUALITY_RESAMPLE)
    _AVATAR_MASK_CACHE[key] = mask
    return mask


def _profile_color(seed: str) -> tuple[int, int, int]:
    palette = [
        (73, 128, 200),
        (74, 154, 126),
        (196, 102, 86),
        (143, 117, 190),
        (201, 136, 73),
    ]
    return palette[sum(seed.encode("utf-8", "ignore")) % len(palette)]


def _draw_image_block(
    draw: ImageDraw.ImageDraw,
    image: Image.Image,
    previews: list[_ImagePreview],
    x: int,
    y: int,
    width: int,
    small_font: ImageFont.ImageFont,
    *,
    scale: int = 1,
) -> None:
    if len(previews) == 1:
        target_w, target_h = _single_image_size(previews[0], width, scale=scale)
        _draw_preview_tile(draw, image, previews[0], x, y, target_w, target_h, small_font, crop=False, scale=scale)
        return

    cols = _grid_columns(len(previews))
    gap = _scale_px(8, scale)
    tile = (width - gap * (cols - 1)) // cols
    for index, preview in enumerate(previews):
        col = index % cols
        row = index // cols
        tx = x + col * (tile + gap)
        ty = y + row * (tile + gap)
        _draw_preview_tile(draw, image, preview, tx, ty, tile, tile, small_font, crop=True, scale=scale)


def _draw_preview_tile(
    draw: ImageDraw.ImageDraw,
    image: Image.Image,
    preview: _ImagePreview,
    x: int,
    y: int,
    width: int,
    height: int,
    small_font: ImageFont.ImageFont,
    *,
    crop: bool,
    scale: int = 1,
) -> None:
    if preview.image is not None:
        if crop:
            rendered = ImageOps.fit(preview.image, (width, height), method=QUALITY_RESAMPLE)
        else:
            rendered = ImageOps.contain(preview.image, (width, height), method=QUALITY_RESAMPLE)
            width, height = rendered.size
        image.paste(rendered, (x, y))
        return

    draw.rectangle((x, y, x + width, y + height), fill=(244, 245, 247), outline=LINE, width=_scale_px(1, scale))
    label = source_name(preview.media.source) or preview.media.name or "image"
    label = _truncate_to_width(draw, label, small_font, max(_scale_px(20, scale), width - _scale_px(24, scale)))
    icon_w = min(_scale_px(64, scale), max(_scale_px(42, scale), width // 5))
    icon_x = x + (width - icon_w) // 2
    icon_y = y + max(_scale_px(18, scale), (height - icon_w) // 2 - _scale_px(12, scale))
    draw.rectangle((icon_x, icon_y, icon_x + icon_w, icon_y + icon_w), outline=ACTION, width=_scale_px(2, scale))
    draw.line(
        (
            icon_x + _scale_px(10, scale),
            icon_y + icon_w - _scale_px(14, scale),
            icon_x + _scale_px(24, scale),
            icon_y + icon_w - _scale_px(30, scale),
        ),
        fill=ACTION,
        width=_scale_px(2, scale),
    )
    draw.line(
        (
            icon_x + _scale_px(24, scale),
            icon_y + icon_w - _scale_px(30, scale),
            icon_x + icon_w - _scale_px(12, scale),
            icon_y + icon_w - _scale_px(10, scale),
        ),
        fill=ACTION,
        width=_scale_px(2, scale),
    )
    _safe_text(draw, (x + _scale_px(12, scale), y + height - _scale_px(30, scale)), label, small_font, MUTED)


def _draw_attachment_block(
    draw: ImageDraw.ImageDraw,
    attachments: list[PostMedia],
    x: int,
    y: int,
    width: int,
    meta_font: ImageFont.ImageFont,
    small_font: ImageFont.ImageFont,
    *,
    scale: int = 1,
) -> None:
    cols = 2 if width >= _scale_px(620, scale) else 1
    gap = _scale_px(10, scale)
    card_h = _scale_px(76, scale)
    card_w = (width - gap * (cols - 1)) // cols
    for index, item in enumerate(attachments):
        col = index % cols
        row = index // cols
        cx = x + col * (card_w + gap)
        cy = y + row * (card_h + gap)
        _draw_file_card(draw, item, cx, cy, card_w, card_h, meta_font, small_font, scale=scale)


def _draw_file_card(
    draw: ImageDraw.ImageDraw,
    item: PostMedia,
    x: int,
    y: int,
    width: int,
    height: int,
    meta_font: ImageFont.ImageFont,
    small_font: ImageFont.ImageFont,
    *,
    scale: int = 1,
) -> None:
    draw.rounded_rectangle(
        (x, y, x + width, y + height),
        radius=_scale_px(6, scale),
        fill=CARD_BG,
        outline=LINE,
        width=_scale_px(1, scale),
    )
    name = item.name or source_name(item.source) or item.kind or "file"
    suffix = Path(name).suffix.lower()
    color = FILE_COLORS.get(suffix, (91, 128, 167))
    icon_x = x + _scale_px(14, scale)
    icon_y = y + _scale_px(14, scale)
    icon_w = _scale_px(48, scale)
    draw.rounded_rectangle((icon_x, icon_y, icon_x + icon_w, icon_y + icon_w), radius=_scale_px(5, scale), fill=color)
    ext = suffix[1:5].upper() if suffix else (item.kind or "FILE")[:4].upper()
    ext_font = _font(_scale_px(13, scale), bold=True)
    box = draw.textbbox((0, 0), ext, font=ext_font)
    _safe_text(
        draw,
        (icon_x + (icon_w - (box[2] - box[0])) // 2, icon_y + (icon_w - (box[3] - box[1])) // 2),
        ext,
        ext_font,
        WHITE,
    )
    text_x = icon_x + icon_w + _scale_px(12, scale)
    title = _truncate_to_width(draw, name, meta_font, max(_scale_px(20, scale), width - (text_x - x) - _scale_px(12, scale)))
    _safe_text(draw, (text_x, y + _scale_px(13, scale)), title, meta_font, TEXT)
    meta = item.mime_type or _format_size(item.size) or _kind_label(item.kind)
    if item.size and item.mime_type:
        meta = f"{item.mime_type} | {_format_size(item.size)}"
    meta = _truncate_to_width(draw, meta, small_font, max(_scale_px(20, scale), width - (text_x - x) - _scale_px(12, scale)))
    _safe_text(draw, (text_x, y + _scale_px(43, scale)), meta, small_font, MUTED)


def _kind_label(kind: str) -> str:
    return {
        "file": "file",
        "video": "video",
        "audio": "audio",
        "record": "audio",
        "voice": "audio",
    }.get(kind, "attachment")


def _format_size(size: int) -> str:
    if not size:
        return ""
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return ""


def _draw_actions(image: Image.Image, width: int, y: int, *, strip: Image.Image | None = None, scale: int = 1) -> None:
    strip = strip or _action_strip()
    start_x = max(_scale_px(22, scale), width - strip.width - _scale_px(22, scale))
    image.paste(strip, (start_x, y), strip)


def _action_strip_render_width(card_width: int, *, dense_layout: bool, scale: int = 1) -> int:
    ratio = 0.34 if dense_layout else 0.36
    target_width = int(round((max(1, card_width) * ratio) / 10.0) * 10)
    return _scale_px(max(220, min(260, target_width)), scale)


def _action_strip(target_width: int = ACTION_STRIP_DEFAULT_WIDTH) -> Image.Image:
    stat = ACTION_STRIP_ASSET.stat()
    key = (f"{ACTION_STRIP_ASSET.resolve()}:{stat.st_mtime_ns}:{stat.st_size}", int(target_width))
    cached = _ACTION_STRIP_CACHE.get(key)
    if cached is not None:
        return cached
    with Image.open(ACTION_STRIP_ASSET) as opened:
        strip = opened.convert("RGBA")
    if target_width > 0 and strip.width != target_width:
        target_height = max(1, round(strip.height * (target_width / strip.width)))
        strip = strip.resize((target_width, target_height), QUALITY_RESAMPLE)
    _ACTION_STRIP_CACHE[key] = strip
    return strip


def _prune_output_dir(output_dir: Path, *, keep: int = 128, max_age_seconds: int = 3 * 24 * 3600) -> None:
    global _LAST_PRUNE_AT
    now = time.monotonic()
    if now - _LAST_PRUNE_AT < _PRUNE_INTERVAL_SECONDS:
        return
    _LAST_PRUNE_AT = now
    try:
        files = sorted(
            [path for path in output_dir.glob("publish_result_*.png") if path.is_file()],
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return
    cutoff = time.time() - max_age_seconds
    for index, path in enumerate(files):
        try:
            if index >= keep or path.stat().st_mtime < cutoff:
                os.remove(path)
        except OSError:
            continue
