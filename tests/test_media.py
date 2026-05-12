import types
import unittest

from qzone_bridge.media import collect_post_payload


class Plain:
    def __init__(self, text):
        self.text = text


class Image:
    def __init__(self, file="", url=""):
        self.file = file
        self.url = url


class File:
    def __init__(self, file, name=""):
        self.file = file
        self.name = name


class MediaPayloadTests(unittest.TestCase):
    def event(self, components):
        return types.SimpleNamespace(message_obj=types.SimpleNamespace(message=components))

    def test_collects_command_text_and_image_components(self):
        payload = collect_post_payload(
            self.event([Plain("/qzone post hello "), Image(file="photo.jpg"), Plain("tail")]),
            fallback_content="Image(file='photo.jpg')",
            include_event_text=True,
            command_prefixes=("qzone post",),
        )

        self.assertEqual(payload.content, "hello tail")
        self.assertEqual(len(payload.media), 1)
        self.assertEqual(payload.media[0].kind, "image")
        self.assertEqual(payload.media[0].source, "photo.jpg")

    def test_collects_text_after_qzone_post_without_slash(self):
        payload = collect_post_payload(
            self.event([Plain("qzone post 6:25")]),
            include_event_text=True,
            command_prefixes=("qzone post",),
        )

        self.assertEqual(payload.content, "6:25")
        self.assertEqual(payload.media, [])

    def test_collects_text_from_raw_string_component(self):
        payload = collect_post_payload(
            self.event(["/qzone post raw text"]),
            include_event_text=True,
            command_prefixes=("qzone post",),
        )

        self.assertEqual(payload.content, "raw text")
        self.assertEqual(payload.media, [])

    def test_strips_prefix_split_across_text_components(self):
        payload = collect_post_payload(
            self.event([Plain("/qzone "), Plain("post split text")]),
            include_event_text=True,
            command_prefixes=("qzone post",),
        )

        self.assertEqual(payload.content, "split text")
        self.assertEqual(payload.media, [])

    def test_strips_prefix_split_across_token_components(self):
        payload = collect_post_payload(
            self.event([Plain("/qzone "), Plain("post"), Plain("token text")]),
            include_event_text=True,
            command_prefixes=("qzone post",),
        )

        self.assertEqual(payload.content, "token text")
        self.assertEqual(payload.media, [])

    def test_strips_prefix_split_without_boundary_spaces(self):
        payload = collect_post_payload(
            self.event([Plain("/qzone"), Plain("post"), Plain("compact text")]),
            include_event_text=True,
            command_prefixes=("qzone post",),
        )

        self.assertEqual(payload.content, "compact text")
        self.assertEqual(payload.media, [])

    def test_uses_event_message_str_when_components_are_missing(self):
        event = types.SimpleNamespace(message_obj=types.SimpleNamespace(message=[]), message_str="/qzone post full text")
        payload = collect_post_payload(
            event,
            fallback_content="full",
            include_event_text=True,
            command_prefixes=("qzone post",),
        )

        self.assertEqual(payload.content, "full text")
        self.assertEqual(payload.media, [])

    def test_strips_fullwidth_slash_command_prefix(self):
        payload = collect_post_payload(
            self.event([Plain("\uff0fqzone post fullwidth")]),
            include_event_text=True,
            command_prefixes=("qzone post",),
        )

        self.assertEqual(payload.content, "fullwidth")
        self.assertEqual(payload.media, [])

    def test_event_prefix_is_stripped_only_once(self):
        payload = collect_post_payload(
            self.event([Plain("/qzone post qzone post literal")]),
            include_event_text=True,
            command_prefixes=("qzone post",),
        )

        self.assertEqual(payload.content, "qzone post literal")
        self.assertEqual(payload.media, [])

    def test_strips_command_prefix_from_fallback_content(self):
        payload = collect_post_payload(
            self.event([]),
            fallback_content="qzone post 6:25",
            include_event_text=True,
            command_prefixes=("qzone post",),
        )

        self.assertEqual(payload.content, "6:25")
        self.assertEqual(payload.media, [])

    def test_strips_slash_command_prefix_from_fallback_content(self):
        payload = collect_post_payload(
            self.event([]),
            fallback_content="/qzone   post   6:25",
            include_event_text=True,
            command_prefixes=("qzone post",),
        )

        self.assertEqual(payload.content, "6:25")
        self.assertEqual(payload.media, [])

    def test_onebot_image_prefers_download_url(self):
        payload = collect_post_payload(
            self.event(
                [
                    {"type": "text", "data": {"text": "/qzone post"}},
                    {"type": "image", "data": {"file": "abc.image", "url": "https://example.com/a.png"}},
                ]
            ),
            fallback_content="[CQ:image,file=abc.image]",
            include_event_text=True,
            command_prefixes=("qzone post",),
        )

        self.assertEqual(payload.content, "")
        self.assertEqual(payload.media[0].source, "https://example.com/a.png")

    def test_non_image_file_becomes_readable_reference(self):
        payload = collect_post_payload(
            self.event([Plain("/qzone post report "), File(file="notes.txt", name="notes.txt")]),
            include_event_text=True,
            command_prefixes=("qzone post",),
        )

        self.assertEqual(payload.media, [])
        self.assertEqual(payload.content, "report\n[文件: notes.txt]")

    def test_llm_mode_keeps_explicit_content_and_file_reference(self):
        payload = collect_post_payload(
            self.event([Plain("please post this"), File(file="report.pdf", name="report.pdf")]),
            fallback_content="weekly report",
            include_event_text=False,
        )

        self.assertEqual(payload.content, "weekly report\n[文件: report.pdf]")
        self.assertEqual(payload.media, [])

    def test_stringified_image_fallback_is_ignored_when_image_component_exists(self):
        payload = collect_post_payload(
            self.event([Image(file="photo.jpg")]),
            fallback_content="Image(file='photo.jpg')",
            include_event_text=False,
        )

        self.assertEqual(payload.content, "")
        self.assertEqual(len(payload.media), 1)


if __name__ == "__main__":
    unittest.main()
