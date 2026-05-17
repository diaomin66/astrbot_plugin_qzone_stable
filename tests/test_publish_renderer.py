import io
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageChops, ImageDraw

from qzone_bridge.media import PostMedia, PostPayload
from qzone_bridge.publish_renderer import (
    ACTION_STRIP_ASSET,
    RenderProfile,
    _action_strip,
    _draw_comment_box,
    _font,
    _smooth_circle_image,
    cached_avatar_source,
    preload_publish_render_assets,
    render_publish_result_image,
)


class PublishRendererTests(unittest.TestCase):
    def make_image(self, path: Path, size: tuple[int, int], color: tuple[int, int, int]) -> None:
        image = Image.new("RGB", size, color)
        image.save(path)

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
                self.assertEqual(image.width, 760)
                self.assertGreater(image.height, 400)
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
                self.assertEqual(image.width, 700)

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
            self.assertGreaterEqual(source.width, 1000)
            self.assertGreaterEqual(source.height, 250)

        strip = _action_strip()
        cached = _action_strip()

        self.assertIs(strip, cached)
        self.assertEqual(strip.mode, "RGBA")
        self.assertEqual(strip.width, 300)
        self.assertGreater(strip.height, 40)
        diff = ImageChops.difference(strip.convert("RGB"), Image.new("RGB", strip.size, "white"))
        self.assertIsNotNone(diff.getbbox())

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

    def test_comment_box_has_no_camera_icon(self):
        image = Image.new("RGB", (500, 80), "white")
        draw = ImageDraw.Draw(image)

        _draw_comment_box(draw, 20, 14, 460, 52, _font(18))

        right_inside = image.crop((420, 24, 470, 56))
        dark_pixels = sum(1 for pixel in right_inside.getdata() if max(pixel) < 80)
        self.assertEqual(dark_pixels, 0)

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
