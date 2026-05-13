import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw

from qzone_bridge.media import PostMedia, PostPayload
from qzone_bridge.publish_renderer import ACTION, RenderProfile, _draw_share_icon, render_publish_result_image


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

    def test_share_icon_stays_within_action_bounds(self):
        image = Image.new("RGB", (70, 60), "white")
        draw = ImageDraw.Draw(image)

        _draw_share_icon(draw, 10, 10)

        mask = ImageChops.difference(image, Image.new("RGB", image.size, "white"))
        bbox = mask.getbbox()
        self.assertIsNotNone(bbox)
        self.assertGreater(bbox[2] - bbox[0], 28)
        self.assertLessEqual(bbox[2], 56)
        self.assertLessEqual(bbox[3], 46)
        self.assertIn(ACTION, image.getdata())


if __name__ == "__main__":
    unittest.main()
