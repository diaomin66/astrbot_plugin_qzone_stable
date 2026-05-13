import asyncio
import importlib.util
import json
import sys
import tempfile
import types
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import AsyncMock, patch


class Plain:
    def __init__(self, text):
        self.text = text


class Event:
    def __init__(self, components, message_str=""):
        self.message_obj = types.SimpleNamespace(message=components)
        self.message_str = message_str
        self.stopped = False
        self.images = []
        self.bot = None

    def is_admin(self):
        return True

    def stop_event(self):
        self.stopped = True

    def plain_result(self, text):
        return text

    def image_result(self, path):
        self.images.append(path)
        return {"image": path}


class File:
    def __init__(self, file, name=""):
        self.file = file
        self.name = name


def install_astrbot_stubs():
    def identity_decorator(*args, **kwargs):
        def decorator(func):
            return func

        return decorator

    def command_group(*args, **kwargs):
        def decorator(func):
            func.command = identity_decorator
            return func

        return decorator

    filter_stub = types.SimpleNamespace(
        command_group=command_group,
        on_platform_loaded=identity_decorator,
        platform_adapter_type=identity_decorator,
        llm_tool=identity_decorator,
        PlatformAdapterType=types.SimpleNamespace(AIOCQHTTP="aiocqhttp"),
    )
    logger_stub = types.SimpleNamespace(
        debug=lambda *args, **kwargs: None,
        info=lambda *args, **kwargs: None,
        warning=lambda *args, **kwargs: None,
        exception=lambda *args, **kwargs: None,
    )

    class AstrMessageEvent:
        pass

    class Context:
        pass

    class Star:
        def __init__(self, context):
            self.context = context

    astrbot_module = types.ModuleType("astrbot")
    api_module = types.ModuleType("astrbot.api")
    event_module = types.ModuleType("astrbot.api.event")
    star_module = types.ModuleType("astrbot.api.star")
    api_module.logger = logger_stub
    event_module.AstrMessageEvent = AstrMessageEvent
    event_module.filter = filter_stub
    star_module.Context = Context
    star_module.Star = Star
    sys.modules["astrbot"] = astrbot_module
    sys.modules["astrbot.api"] = api_module
    sys.modules["astrbot.api.event"] = event_module
    sys.modules["astrbot.api.star"] = star_module


async def collect_async_generator(generator):
    results = []
    async for item in generator:
        results.append(item)
    return results


class MainPublishTests(unittest.TestCase):
    def load_main_module(self):
        root = Path(__file__).resolve().parents[1]
        saved_modules = {
            name: module
            for name, module in sys.modules.items()
            if name == "qzone_bridge"
            or name.startswith("qzone_bridge.")
            or name == "astrbot"
            or name.startswith("astrbot.")
            or name == "_qzone_main_publish_test"
        }
        install_astrbot_stubs()
        spec = importlib.util.spec_from_file_location("_qzone_main_publish_test", root / "main.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        assert spec.loader is not None
        spec.loader.exec_module(module)
        self.addCleanup(self.restore_modules, saved_modules)
        return module

    @staticmethod
    def restore_modules(saved_modules):
        for name in list(sys.modules):
            if (
                name == "qzone_bridge"
                or name.startswith("qzone_bridge.")
                or name == "astrbot"
                or name.startswith("astrbot.")
                or name == "_qzone_main_publish_test"
            ):
                sys.modules.pop(name, None)
        sys.modules.update(saved_modules)

    def make_plugin(self, module):
        plugin = module.QzoneStablePlugin(types.SimpleNamespace(), config={"admin_uins": []})
        data_dir = tempfile.TemporaryDirectory()
        self.addCleanup(data_dir.cleanup)
        plugin.data_dir = Path(data_dir.name)
        plugin._ensure_cookie_ready = AsyncMock()
        plugin._ensure_daemon = AsyncMock()
        plugin.controller = types.SimpleNamespace(
            get_status=AsyncMock(return_value={"login_uin": 0}),
            publish_post=AsyncMock(return_value={"fid": "fid-1", "message": "ok"}),
        )
        return plugin

    def test_qzone_post_stops_event_and_strips_split_command_tokens(self):
        module = self.load_main_module()
        plugin = self.make_plugin(module)
        event = Event([Plain("/qzone "), Plain("post"), Plain("hello")])

        asyncio.run(collect_async_generator(plugin.qzone_post(event, content="/qzone post hello")))

        self.assertTrue(event.stopped)
        plugin.controller.publish_post.assert_awaited_once()
        plugin._ensure_daemon.assert_not_awaited()
        self.assertEqual(plugin.controller.publish_post.await_args.kwargs["content"], "hello")
        self.assertTrue(plugin.controller.publish_post.await_args.kwargs["content_sanitized"])

    def test_qzone_post_prefers_pipeline_text_over_raw_custom_wake_prefix(self):
        module = self.load_main_module()
        plugin = self.make_plugin(module)
        event = Event([Plain("bot qzone post hello")], message_str="qzone post hello")

        asyncio.run(collect_async_generator(plugin.qzone_post(event, content="hello")))

        self.assertTrue(event.stopped)
        plugin.controller.publish_post.assert_awaited_once()
        plugin._ensure_daemon.assert_not_awaited()
        self.assertEqual(plugin.controller.publish_post.await_args.kwargs["content"], "hello")
        self.assertTrue(plugin.controller.publish_post.await_args.kwargs["content_sanitized"])

    def test_qzone_post_keeps_literal_command_text_after_real_command(self):
        module = self.load_main_module()
        plugin = self.make_plugin(module)
        event = Event([Plain("/qzone post qzone post literal")])

        asyncio.run(collect_async_generator(plugin.qzone_post(event, content="/qzone post qzone post literal")))

        plugin.controller.publish_post.assert_awaited_once()
        self.assertEqual(plugin.controller.publish_post.await_args.kwargs["content"], "qzone post literal")
        self.assertTrue(plugin.controller.publish_post.await_args.kwargs["content_sanitized"])

    def test_llm_publish_tool_strips_command_prefix_from_tool_content(self):
        module = self.load_main_module()
        plugin = self.make_plugin(module)
        event = Event([])

        asyncio.run(
            collect_async_generator(
                plugin.tool_publish_post(event, content="/qzone post hello", confirm=True)
            )
        )

        plugin.controller.publish_post.assert_awaited_once()
        self.assertEqual(plugin.controller.publish_post.await_args.kwargs["content"], "hello")
        self.assertTrue(plugin.controller.publish_post.await_args.kwargs["content_sanitized"])

    def test_llm_publish_tool_strips_no_space_typo_from_tool_content(self):
        module = self.load_main_module()
        plugin = self.make_plugin(module)
        event = Event([])

        asyncio.run(
            collect_async_generator(
                plugin.tool_publish_post(event, content="/qzone post1", confirm=True)
            )
        )

        plugin.controller.publish_post.assert_awaited_once()
        self.assertEqual(plugin.controller.publish_post.await_args.kwargs["content"], "1")
        self.assertTrue(plugin.controller.publish_post.await_args.kwargs["content_sanitized"])

    def test_qzone_post_returns_rendered_image_result(self):
        module = self.load_main_module()
        plugin = self.make_plugin(module)
        event = Event([Plain("/qzone post hello")])

        results = asyncio.run(collect_async_generator(plugin.qzone_post(event, content="/qzone post hello")))

        self.assertEqual(len(results), 1)
        self.assertIn("image", results[0])
        self.assertTrue(Path(results[0]["image"]).exists())

    def test_qzone_post_keeps_files_out_of_publish_media_but_renders_success(self):
        module = self.load_main_module()
        plugin = self.make_plugin(module)
        event = Event([Plain("/qzone post report "), File(file="report.pdf", name="report.pdf")])

        results = asyncio.run(collect_async_generator(plugin.qzone_post(event, content="/qzone post report")))

        plugin.controller.publish_post.assert_awaited_once()
        self.assertEqual(plugin.controller.publish_post.await_args.kwargs["media"], [])
        self.assertEqual(plugin.controller.publish_post.await_args.kwargs["content"], "report\n[文件: report.pdf]")
        self.assertTrue(Path(results[0]["image"]).exists())

    def test_qzone_post_renders_logged_in_qzone_publisher(self):
        module = self.load_main_module()
        plugin = self.make_plugin(module)
        plugin.controller.get_status = AsyncMock(return_value={"login_uin": 123456})
        fake_image = plugin.data_dir / "rendered.png"
        fake_image.write_bytes(b"png")
        captured = {}

        async def get_stranger_info(**kwargs):
            self.assertEqual(kwargs["user_id"], 123456)
            return {"nickname": "QzoneOwner"}

        event = Event([Plain("/qzone post hello")])
        event.bot = types.SimpleNamespace(get_stranger_info=get_stranger_info)

        def fake_render(post, output_dir, **kwargs):
            captured.update(kwargs)
            return fake_image

        with patch.object(module, "render_publish_result_image", side_effect=fake_render):
            results = asyncio.run(collect_async_generator(plugin.qzone_post(event, content="/qzone post hello")))

        self.assertEqual(results, [{"image": str(fake_image)}])
        self.assertEqual(captured["profile"].nickname, "QzoneOwner")
        self.assertEqual(captured["profile"].user_id, "123456")
        self.assertIn("123456", captured["profile"].avatar_source)

    def test_llm_list_feed_hides_cursor_and_internal_ids(self):
        module = self.load_main_module()
        plugin = self.make_plugin(module)
        entry = module.FeedEntry(
            hostuin=3112333596,
            fid="1c7182b96589046ad3380900",
            appid=311,
            summary="瘦了…………",
            liked=False,
        )
        plugin.controller.list_feeds = AsyncMock(
            return_value={
                "items": [asdict(entry)],
                "cursor": "back_server_info=secret",
                "has_more": True,
            }
        )
        event = Event([])

        results = asyncio.run(collect_async_generator(plugin.tool_list_feed(event, limit=1)))

        self.assertEqual(len(results), 1)
        self.assertIn("瘦了", results[0])
        self.assertIn("未点赞", results[0])
        self.assertNotIn("cursor=", results[0])
        self.assertNotIn("has_more", results[0])
        self.assertNotIn("fid=", results[0])

    def test_llm_like_tool_returns_structured_result_for_llm_reply(self):
        module = self.load_main_module()
        plugin = self.make_plugin(module)
        plugin.controller.like_post = AsyncMock(
            return_value={
                "action": "like",
                "liked": True,
                "verified": True,
                "already": False,
                "summary": "瘦了…………",
            }
        )
        event = Event([])

        results = asyncio.run(
            collect_async_generator(plugin.tool_like_post(event, hostuin=0, fid="1"))
        )

        plugin.controller.like_post.assert_awaited_once_with(hostuin=0, fid="1", appid=311, unlike=False)
        payload = json.loads(results[0])
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["tool"], "qzone_like_post")
        self.assertTrue(payload["result"]["verified"])
        self.assertTrue(payload["result"]["liked"])
        self.assertEqual(payload["result"]["summary"], "瘦了…………")
        self.assertIn("reply_guidance", payload)
        self.assertNotIn("raw", payload["result"])
        self.assertNotIn("fid=", results[0])

    def test_llm_like_tool_ignores_preview_confirmation(self):
        module = self.load_main_module()
        plugin = self.make_plugin(module)
        plugin.settings.preview_writes = True
        plugin.controller.like_post = AsyncMock(
            return_value={
                "action": "like",
                "liked": True,
                "verified": False,
                "already": False,
                "summary": "hello",
            }
        )
        event = Event([])

        results = asyncio.run(
            collect_async_generator(plugin.tool_like_post(event, hostuin=0, fid="1", confirm=False))
        )

        plugin.controller.like_post.assert_awaited_once()
        payload = json.loads(results[0])
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["result"]["verified"])

    def test_llm_like_tool_returns_structured_error_for_llm_reply(self):
        module = self.load_main_module()
        plugin = self.make_plugin(module)
        plugin.controller.like_post = AsyncMock(side_effect=module.QzoneBridgeError("点赞失败"))
        event = Event([])

        results = asyncio.run(
            collect_async_generator(plugin.tool_like_post(event, hostuin=0, fid="1", confirm=False))
        )

        payload = json.loads(results[0])
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["tool"], "qzone_like_post")
        self.assertEqual(payload["error"]["message"], "点赞失败")
        self.assertIn("reply_guidance", payload)


if __name__ == "__main__":
    unittest.main()
