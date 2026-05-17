import io
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageChops

from qzone_bridge.media import PostMedia, PostPayload
from qzone_bridge.publish_renderer import (
    ACTION_STRIP_ASSET,
    BOLD_FONT_ASSET,
    PREVIEW_MAX_EDGE,
    RenderProfile,
    REGULAR_FONT_ASSET,
    RENDER_SCALE,
    _action_strip,
    _font,
    _load_image_preview,
    _smooth_circle_image,
    cached_avatar_source,
    preload_publish_render_assets,
    render_publish_result_image,
)


class PublishRendererTests(unittest.TestCase):
    def make_image(self, path: Path, size: tuple[int, int], color: tuple[int, int, int]) -> None:
        image = Image.new("RGB", size, color)
        image.save(path)

    def test_render_scale_keeps_text_hidpi(self):
        self.assertGreaterEqual(RENDER_SCALE, 3)

    def test_short_text_render_uses_compact_width_and_large_type(self):
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            post = PostPayload(content="short sentence", media=[])

            rendered = render_publish_result_image(
                post,
                temp_path,
                profile=RenderProfile(nickname="Coconut", time_text="06:34"),
                width=900,
            )

            with Image.open(rendered) as image:
                self.assertLess(image.width, 900 * RENDER_SCALE)
                self.assertGreaterEqual(image.width, 520 * RENDER_SCALE)

        font = _font(30 * RENDER_SCALE)
        self.assertIn("Alibaba PuHuiTi", font.getname()[0])

    def test_render_text_images_and_files_to_png(self):
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            photo_a = temp_path / "a.jpg"
            photo_b = temp_path / "b.png"
            self.make_image(photo_a, (320, 640), (120, 180, 210))
            self.make_image(photo_b, (520, 280), (210, 150, 100))

            post = PostPayload(
                content="hello from qzone\nsecond line wraps cleanly",
                media=[
                    PostMedia(kind="image", source=str(photo_a), name="a.jpg"),
                    PostMedia(kind="image", source=str(photo_b), name="b.png"),
                ],
                attachments=[
                    PostMedia(
                        kind="file",
                        source="report.pdf",
                        name="report.pdf",
                        mime_type="application/pdf",
                        size=2048,
                    )
                ],
            )

            rendered = render_publish_result_image(
                post,
                temp_path,
                profile=RenderProfile(nickname="Coconut", time_text="06:34"),
                result={"fid": "fid-1"},
                width=760,
            )

            self.assertTrue(rendered.exists())
            with Image.open(rendered) as image:
                self.assertEqual(image.format, "PNG")
                self.assertEqual(image.width, 760 * RENDER_SCALE)
                self.assertGreater(image.height, 400 * RENDER_SCALE)
                diff = ImageChops.difference(image.convert("RGB"), Image.new("RGB", image.size, "white"))
                self.assertIsNotNone(diff.getbbox())

    def test_render_missing_image_source_uses_placeholder(self):
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            post = PostPayload(
                content="image placeholder",
                media=[PostMedia(kind="image", source=str(temp_path / "missing.jpg"), name="missing.jpg")],
            )

            rendered = render_publish_result_image(
                post,
                temp_path,
                profile=RenderProfile(nickname="Coconut", time_text="06:34"),
                width=700,
            )

            self.assertTrue(rendered.exists())
            with Image.open(rendered) as image:
                self.assertEqual(image.format, "PNG")
                self.assertEqual(image.width, 700 * RENDER_SCALE)

    def test_preload_profile_avatar_writes_local_cache_once(self):
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            profile = RenderProfile(
                nickname="Coconut",
                user_id="123456",
                avatar_source="https://example.test/avatar-a.png",
            )
            calls = 0

            def fake_read(source, *, max_bytes, remote_timeout):
                nonlocal calls
                calls += 1
                image = Image.new("RGB", (512, 512), (120, 180, 210))
                buffer = io.BytesIO()
                image.save(buffer, format="PNG")
                buffer.seek(0)
                return buffer.read()

            with patch("qzone_bridge.publish_renderer._read_source_bytes", side_effect=fake_read):
                resolved = preload_publish_render_assets(profile, temp_path, remote_timeout=0.01)
                second = preload_publish_render_assets(
                    RenderProfile(
                        nickname="Coconut",
                        user_id="123456",
                        avatar_source="https://example.test/avatar-b.png",
                    ),
                    temp_path,
                    remote_timeout=0.01,
                )

            self.assertEqual(calls, 1)
            self.assertTrue(Path(resolved.avatar_source).exists())
            self.assertEqual(second.avatar_source, resolved.avatar_source)
            self.assertEqual(cached_avatar_source(temp_path, profile), resolved.avatar_source)

    def test_result_fid_does_not_add_footer(self):
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            post = PostPayload(content="no footer", media=[])

            without_result = render_publish_result_image(
                post,
                temp_path,
                profile=RenderProfile(nickname="Coconut", time_text="06:34"),
                width=700,
            )
            with_result = render_publish_result_image(
                post,
                temp_path,
                profile=RenderProfile(nickname="Coconut", time_text="06:34"),
                result={"fid": "fid-1"},
                width=700,
            )

            with Image.open(without_result) as base, Image.open(with_result) as rendered:
                self.assertEqual(rendered.size, base.size)

    def test_action_strip_loads_static_png_asset(self):
        self.assertTrue(ACTION_STRIP_ASSET.exists())
        with Image.open(ACTION_STRIP_ASSET) as source:
            alpha = source.convert("RGBA").getchannel("A")
            self.assertGreaterEqual(source.width, 1000)
            self.assertGreaterEqual(source.height, 250)
            self.assertEqual(alpha.getextrema(), (0, 255))

        strip = _action_strip()
        cached = _action_strip()

        self.assertIs(strip, cached)
        self.assertEqual(strip.mode, "RGBA")
        self.assertEqual(strip.width, 260 * RENDER_SCALE)
        self.assertGreater(strip.height, 40)
        diff = ImageChops.difference(strip.convert("RGB"), Image.new("RGB", strip.size, "white"))
        self.assertIsNotNone(diff.getbbox())

    def test_bundled_puhuiti_font_is_used_for_rendering(self):
        self.assertTrue(REGULAR_FONT_ASSET.exists())
        self.assertTrue(BOLD_FONT_ASSET.exists())

        regular = _font(24)
        bold = _font(24, bold=True)

        self.assertIn("Alibaba PuHuiTi", regular.getname()[0])
        self.assertIn("Alibaba PuHuiTi", bold.getname()[0])

    def test_avatar_circle_mask_has_smooth_antialiased_edge(self):
        source = Image.new("RGB", (512, 512), (120, 180, 210))

        avatar = _smooth_circle_image(source, 76)
        alpha = avatar.getchannel("A")
        values = list(alpha.getdata())

        self.assertEqual(avatar.mode, "RGBA")
        self.assertEqual(avatar.size, (76, 76))
        self.assertEqual(alpha.getpixel((0, 0)), 0)
        self.assertEqual(alpha.getpixel((38, 38)), 255)
        self.assertTrue(any(0 < value < 255 for value in values))

    def test_large_local_preview_keeps_high_resolution_detail(self):
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            photo = temp_path / "large.jpg"
            Image.new("RGB", (2300, 1900), (120, 180, 210)).save(photo, "JPEG", quality=96)

            preview = _load_image_preview(
                PostMedia(kind="image", source=str(photo), name="large.jpg"),
                remote_timeout=0.01,
            )

            self.assertIsNotNone(preview.image)
            assert preview.image is not None
            self.assertGreater(max(preview.image.size), 1600)
            self.assertLessEqual(max(preview.image.size), PREVIEW_MAX_EDGE)

    def test_render_omits_bottom_comment_bar(self):
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            post = PostPayload(content="no footer", media=[])

            rendered = render_publish_result_image(
                post,
                temp_path,
                profile=RenderProfile(nickname="Coconut", time_text="06:34"),
                width=700,
            )

            with Image.open(rendered) as image:
                bottom_left = image.crop(
                    (
                        20 * RENDER_SCALE,
                        max(0, image.height - 72 * RENDER_SCALE),
                        240 * RENDER_SCALE,
                        image.height - 12 * RENDER_SCALE,
                    )
                ).convert("RGB")
                non_white = sum(1 for pixel in bottom_left.getdata() if pixel != (255, 255, 255))
                self.assertLess(non_white, 10 * RENDER_SCALE)

    def test_render_height_adapts_to_content_amount(self):
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            short_post = PostPayload(content="short", media=[])
            long_post = PostPayload(content="这是一段比较长的说说内容。" * 28, media=[])

            short_render = render_publish_result_image(
                short_post,
                temp_path,
                profile=RenderProfile(nickname="Coconut", time_text="06:34"),
                width=700,
            )
            long_render = render_publish_result_image(
                long_post,
                temp_path,
                profile=RenderProfile(nickname="Coconut", time_text="06:34"),
                width=700,
            )

            with Image.open(short_render) as short_image, Image.open(long_render) as long_image:
                self.assertGreaterEqual(long_image.width, short_image.width)
                self.assertGreater(long_image.height, short_image.height + 120 * RENDER_SCALE)

    def test_render_loads_multiple_previews_concurrently(self):
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            active = 0
            max_active = 0

            def fake_read(source, *, max_bytes, remote_timeout):
                nonlocal active, max_active
                active += 1
                max_active = max(max_active, active)
                time.sleep(0.03)
                active -= 1
                image = Image.new("RGB", (64, 64), (120, 180, 210))
                buffer = io.BytesIO()
                image.save(buffer, format="PNG")
                buffer.seek(0)
                return buffer.read()

            post = PostPayload(
                content="parallel",
                media=[
                    PostMedia(kind="image", source="a.png", name="a.png"),
                    PostMedia(kind="image", source="b.png", name="b.png"),
                    PostMedia(kind="image", source="c.png", name="c.png"),
                ],
            )

            with patch("qzone_bridge.publish_renderer._read_source_bytes", side_effect=fake_read):
                rendered = render_publish_result_image(
                    post,
                    temp_path,
                    profile=RenderProfile(nickname="Coconut", time_text="06:34"),
                    width=700,
                )

            self.assertTrue(rendered.exists())
            self.assertGreater(max_active, 1)


if __name__ == "__main__":
    unittest.main()
