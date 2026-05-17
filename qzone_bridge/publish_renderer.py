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
TEXT = (24, 24, 24)
MUTED = (126, 132, 139)
LINE = (226, 226, 226)
ACTION = (24, 24, 24)
CARD_BG = (250, 250, 250)
COMMENT_BG = (247, 247, 247)
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
    """Warm fonts and cached action glyphs before the first publish render."""

    for size, bold in ((28, True), (24, False), (20, False), (18, False), (17, False), (34, True)):
        _font(size, bold=bold)
    _action_strip()


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
            avatar.thumbnail((1600, 1600), QUALITY_RESAMPLE)
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

    width = max(640, min(int(width or 900), 1280))
    margin = 22
    content_width = width - margin * 2
    name_font = _font(28, bold=True)
    time_font = _font(20)
    text_font = _font(24)
    meta_font = _font(18)
    small_font = _font(17)

    scratch = ImageDraw.Draw(Image.new("RGB", (1, 1), WHITE))
    text_lines = _wrap_text(scratch, _render_content_text(post), text_font, content_width)
    line_height = _line_height(scratch, text_font, 1.34)
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
    image_height = _image_block_height(previews, content_width) if previews else 0
    attachment_height = _attachment_block_height(post.attachments, content_width) if post.attachments else 0

    y = 126
    if text_height:
        y += text_height + 18
    if image_height:
        y += image_height + 18
    if attachment_height:
        y += attachment_height + 18
    action_strip = _action_strip()
    actions_y = y + 6
    comment_y = actions_y + action_strip.height + 8
    height = max(240, comment_y + 56 + 22)

    image = Image.new("RGB", (width, height), WHITE)
    draw = ImageDraw.Draw(image)
    _draw_header(draw, image, profile, margin, name_font, time_font, avatar_preview=avatar_preview)

    y = 126
    if text_lines:
        for line in text_lines:
            _safe_text(draw, (margin, y), line, text_font, TEXT)
            y += line_height
        y += 18
    if previews:
        _draw_image_block(draw, image, previews, margin, y, content_width, small_font)
        y += image_height + 18
    if post.attachments:
        _draw_attachment_block(draw, post.attachments, margin, y, content_width, meta_font, small_font)
        y += attachment_height + 18

    actions_y = y + 6
    _draw_actions(image, width, actions_y, strip=action_strip)
    comment_y = actions_y + action_strip.height + 8
    _draw_comment_box(draw, margin, comment_y, content_width, 52, meta_font)

    path = output_dir / f"publish_result_{int(time.time())}_{uuid.uuid4().hex[:10]}.png"
    image.save(path, "PNG", optimize=False, compress_level=1)
    return path


def _render_content_text(post: PostPayload) -> str:
    return str(post.content or "").strip()


def _font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    key = (int(size), bool(bold))
    cached = FONT_CACHE.get(key)
    if cached is not None:
        return cached

    regular = [
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
    data = _read_source_bytes(media.source, max_bytes=12 * 1024 * 1024, remote_timeout=remote_timeout)
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
            preview.thumbnail((1600, 1600), QUALITY_RESAMPLE)
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


def _image_block_height(previews: list[_ImagePreview], width: int) -> int:
    if len(previews) == 1:
        return _single_image_size(previews[0], width)[1]
    cols = _grid_columns(len(previews))
    gap = 8
    tile = (width - gap * (cols - 1)) // cols
    rows = math.ceil(len(previews) / cols)
    return rows * tile + gap * (rows - 1)


def _single_image_size(preview: _ImagePreview, width: int) -> tuple[int, int]:
    max_w = min(width, 540)
    max_h = 690
    if preview.image is None:
        return min(max_w, 420), 280
    source_w, source_h = preview.image.size
    if source_w <= 0 or source_h <= 0:
        return min(max_w, 420), 280
    scale = min(max_w / source_w, max_h / source_h)
    if scale > 1:
        scale = min(scale, 1.35)
    return max(120, int(source_w * scale)), max(120, int(source_h * scale))


def _grid_columns(count: int) -> int:
    if count <= 1:
        return 1
    if count in {2, 4}:
        return 2
    return 3


def _attachment_block_height(attachments: list[PostMedia], width: int) -> int:
    cols = 2 if width >= 620 else 1
    rows = math.ceil(len(attachments) / cols)
    gap = 10
    card_h = 76
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
) -> None:
    avatar_size = 76
    avatar_x = margin
    avatar_y = 26
    _draw_avatar(draw, image, profile, avatar_x, avatar_y, avatar_size, preview=avatar_preview)
    text_x = avatar_x + avatar_size + 18
    _safe_text(draw, (text_x, 32), profile.nickname, name_font, TEXT)
    _safe_text(draw, (text_x, 72), profile.time_text, time_font, MUTED)

    x = image.width - 44
    y = 32
    draw.line([(x, y), (x + 10, y + 10), (x + 20, y)], fill=ACTION, width=3)


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
    large_size = max(size, int(size) * max(2, int(scale)))
    avatar = ImageOps.fit(source.convert("RGB"), (large_size, large_size), method=QUALITY_RESAMPLE).convert("RGBA")
    avatar.putalpha(_circle_mask(large_size))
    return avatar.resize((size, size), QUALITY_RESAMPLE)


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
    key = (int(size), int(scale_key))
    cached = _AVATAR_MASK_CACHE.get(key)
    if cached is not None:
        return cached
    mask = Image.new("L", (size, size), 0)
    mask_draw = ImageDraw.Draw(mask)
    inset = max(1, size // 180)
    mask_draw.ellipse((inset, inset, size - inset - 1, size - inset - 1), fill=255)
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
) -> None:
    if len(previews) == 1:
        target_w, target_h = _single_image_size(previews[0], width)
        _draw_preview_tile(draw, image, previews[0], x, y, target_w, target_h, small_font, crop=False)
        return

    cols = _grid_columns(len(previews))
    gap = 8
    tile = (width - gap * (cols - 1)) // cols
    for index, preview in enumerate(previews):
        col = index % cols
        row = index // cols
        tx = x + col * (tile + gap)
        ty = y + row * (tile + gap)
        _draw_preview_tile(draw, image, preview, tx, ty, tile, tile, small_font, crop=True)


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
) -> None:
    if preview.image is not None:
        if crop:
            rendered = ImageOps.fit(preview.image, (width, height), method=QUALITY_RESAMPLE)
        else:
            rendered = ImageOps.contain(preview.image, (width, height), method=QUALITY_RESAMPLE)
            width, height = rendered.size
        image.paste(rendered, (x, y))
        return

    draw.rectangle((x, y, x + width, y + height), fill=(244, 245, 247), outline=LINE)
    label = source_name(preview.media.source) or preview.media.name or "image"
    label = _truncate_to_width(draw, label, small_font, max(20, width - 24))
    icon_w = min(64, max(42, width // 5))
    icon_x = x + (width - icon_w) // 2
    icon_y = y + max(18, (height - icon_w) // 2 - 12)
    draw.rectangle((icon_x, icon_y, icon_x + icon_w, icon_y + icon_w), outline=ACTION, width=2)
    draw.line((icon_x + 10, icon_y + icon_w - 14, icon_x + 24, icon_y + icon_w - 30), fill=ACTION, width=2)
    draw.line((icon_x + 24, icon_y + icon_w - 30, icon_x + icon_w - 12, icon_y + icon_w - 10), fill=ACTION, width=2)
    _safe_text(draw, (x + 12, y + height - 30), label, small_font, MUTED)


def _draw_attachment_block(
    draw: ImageDraw.ImageDraw,
    attachments: list[PostMedia],
    x: int,
    y: int,
    width: int,
    meta_font: ImageFont.ImageFont,
    small_font: ImageFont.ImageFont,
) -> None:
    cols = 2 if width >= 620 else 1
    gap = 10
    card_h = 76
    card_w = (width - gap * (cols - 1)) // cols
    for index, item in enumerate(attachments):
        col = index % cols
        row = index // cols
        cx = x + col * (card_w + gap)
        cy = y + row * (card_h + gap)
        _draw_file_card(draw, item, cx, cy, card_w, card_h, meta_font, small_font)


def _draw_file_card(
    draw: ImageDraw.ImageDraw,
    item: PostMedia,
    x: int,
    y: int,
    width: int,
    height: int,
    meta_font: ImageFont.ImageFont,
    small_font: ImageFont.ImageFont,
) -> None:
    draw.rounded_rectangle((x, y, x + width, y + height), radius=6, fill=CARD_BG, outline=LINE, width=1)
    name = item.name or source_name(item.source) or item.kind or "file"
    suffix = Path(name).suffix.lower()
    color = FILE_COLORS.get(suffix, (91, 128, 167))
    icon_x = x + 14
    icon_y = y + 14
    icon_w = 48
    draw.rounded_rectangle((icon_x, icon_y, icon_x + icon_w, icon_y + icon_w), radius=5, fill=color)
    ext = suffix[1:5].upper() if suffix else (item.kind or "FILE")[:4].upper()
    ext_font = _font(13, bold=True)
    box = draw.textbbox((0, 0), ext, font=ext_font)
    _safe_text(
        draw,
        (icon_x + (icon_w - (box[2] - box[0])) // 2, icon_y + (icon_w - (box[3] - box[1])) // 2),
        ext,
        ext_font,
        WHITE,
    )
    text_x = icon_x + icon_w + 12
    title = _truncate_to_width(draw, name, meta_font, max(20, width - (text_x - x) - 12))
    _safe_text(draw, (text_x, y + 13), title, meta_font, TEXT)
    meta = item.mime_type or _format_size(item.size) or _kind_label(item.kind)
    if item.size and item.mime_type:
        meta = f"{item.mime_type} | {_format_size(item.size)}"
    meta = _truncate_to_width(draw, meta, small_font, max(20, width - (text_x - x) - 12))
    _safe_text(draw, (text_x, y + 43), meta, small_font, MUTED)


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


def _draw_actions(image: Image.Image, width: int, y: int, *, strip: Image.Image | None = None) -> None:
    strip = strip or _action_strip()
    start_x = max(22, width - strip.width - 22)
    image.paste(strip, (start_x, y), strip)


def _action_strip(target_width: int = 300) -> Image.Image:
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


def _draw_comment_box(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    width: int,
    height: int,
    font: ImageFont.ImageFont,
) -> None:
    radius = max(10, height // 2)
    draw.rounded_rectangle((x, y, x + width, y + height), radius=radius, fill=COMMENT_BG, outline=LINE, width=1)
    _safe_text(draw, (x + 20, y + 14), "\u8bc4\u8bba", font, MUTED)
    camera_x = x + width - 52
    camera_y = y + 13
    draw.rounded_rectangle((camera_x, camera_y + 8, camera_x + 30, camera_y + 26), radius=4, outline=ACTION, width=2)
    draw.rectangle((camera_x + 9, camera_y + 4, camera_x + 21, camera_y + 10), outline=ACTION, width=2)
    draw.ellipse((camera_x + 10, camera_y + 12, camera_x + 20, camera_y + 22), outline=ACTION, width=2)


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
