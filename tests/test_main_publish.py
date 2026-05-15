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

    def test_qzone_status_auto_recovers_degraded_daemon(self):
        module = self.load_main_module()
        plugin = self.make_plugin(module)
        degraded = {
            "daemon_state": "degraded",
            "login_uin": 3112333596,
            "session_source": "aiocqhttp",
            "cookie_count": 14,
            "cookie_summary": "14???: uin, p_uin, skey, p_skey",
            "needs_rebind": False,
            "daemon_port": 18999,
        }
        ready = dict(degraded, daemon_state="ready", daemon_pid=1234)
        plugin.controller = types.SimpleNamespace(
            get_status=AsyncMock(return_value=degraded),
            ensure_running=AsyncMock(return_value=ready),
        )
        event = Event([])

        results = asyncio.run(collect_async_generator(plugin.qzone_status(event)))

        plugin.controller.ensure_running.assert_awaited_once()
        self.assertIn("- daemon: ready", results[0])
        self.assertIn("- source: aiocqhttp", results[0])
        self.assertIn("- pid: 1234", results[0])

    def test_qzone_status_reports_daemon_start_failure_detail(self):
        module = self.load_main_module()
        plugin = self.make_plugin(module)
        degraded = {
            "daemon_state": "degraded",
            "login_uin": 3112333596,
            "session_source": "aiocqhttp",
            "cookie_count": 14,
            "cookie_summary": "14???: uin, p_uin, skey, p_skey",
            "needs_rebind": False,
            "daemon_port": 18999,
        }
        plugin.controller = types.SimpleNamespace(
            get_status=AsyncMock(return_value=degraded),
            ensure_running=AsyncMock(
                side_effect=module.DaemonUnavailableError(
                    "QQ?? daemon ????",
                    detail={"returncode": 7, "log_path": "D:/qzone/daemon.log", "log_tail": "daemon boom"},
                )
            ),
        )
        event = Event([])

        results = asyncio.run(collect_async_generator(plugin.qzone_status(event)))

        self.assertIn("- daemon: degraded", results[0])
        self.assertIn("- daemon_error: QQ?? daemon ????", results[0])
        self.assertIn("- daemon_returncode: 7", results[0])
        self.assertIn("- daemon_log: D:/qzone/daemon.log", results[0])
        self.assertIn("daemon boom", results[0])

    def test_qzone_bind_returns_recovered_ready_status(self):
        module = self.load_main_module()
        plugin = self.make_plugin(module)
        degraded = {
            "daemon_state": "degraded",
            "login_uin": 3112333596,
            "session_source": "manual",
            "cookie_count": 4,
            "cookie_summary": "4???: uin, p_uin, skey, p_skey",
            "needs_rebind": False,
            "daemon_port": 18999,
        }
        ready = dict(degraded, daemon_state="ready", daemon_pid=5678)
        plugin.controller = types.SimpleNamespace(
            bind_cookie_local=AsyncMock(return_value=degraded),
            get_status=AsyncMock(return_value=degraded),
            ensure_running=AsyncMock(return_value=ready),
        )
        plugin._schedule_daemon_warmup = lambda trigger: None
        event = Event([])

        results = asyncio.run(collect_async_generator(plugin.qzone_bind(event, "uin=o3112333596; p_skey=abc")))

        plugin.controller.bind_cookie_local.assert_awaited_once()
        plugin.controller.ensure_running.assert_awaited_once()
        self.assertIn("- daemon: ready", results[0])
        self.assertIn("- pid: 5678", results[0])

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
        self.assertEqual(plugin.controller.publish_post.await_args.kwargs["content"], "report\n[??: report.pdf]")
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
            summary="??????",
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
        self.assertIn("??", results[0])
        self.assertIn("???", results[0])
        self.assertNotIn("cursor=", results[0])
        self.assertNotIn("has_more", results[0])
        self.assertNotIn("fid=", results[0])

    def test_llm_like_tool_asks_llm_for_natural_result_reply(self):
        module = self.load_main_module()
        plugin = self.make_plugin(module)
        plugin._context = types.SimpleNamespace(
            get_current_chat_provider_id=AsyncMock(return_value="provider-1"),
            llm_generate=AsyncMock(return_value=types.SimpleNamespace(completion_text="?????????")),
        )
        plugin.controller.like_post = AsyncMock(
            return_value={
                "action": "like",
                "liked": True,
                "verified": True,
                "already": False,
                "summary": "??????",
            }
        )
        event = Event([])

        results = asyncio.run(
            collect_async_generator(plugin.tool_like_post(event, hostuin=0, fid="1"))
        )

        plugin.controller.like_post.assert_awaited_once_with(
            hostuin=0,
            fid="1",
            appid=311,
            unlike=False,
            latest=False,
            index=0,
        )
        self.assertEqual(results[0], "?????????")
        plugin._context.llm_generate.assert_awaited_once()
        prompt = plugin._context.llm_generate.await_args.kwargs["prompt"]
        self.assertIn('"ok":true', prompt)
        self.assertIn('"verified":true', prompt)
        self.assertIn("??????", prompt)
        self.assertNotIn("fid=", results[0])
        self.assertFalse(results[0].lstrip().startswith("{"))

    def test_llm_like_tool_logs_success_payload_to_astrbot_logger(self):
        module = self.load_main_module()
        plugin = self.make_plugin(module)
        plugin._context = types.SimpleNamespace(
            llm_generate=AsyncMock(return_value=types.SimpleNamespace(completion_text="????")),
        )
        plugin.controller.like_post = AsyncMock(
            return_value={
                "action": "like",
                "liked": True,
                "verified": True,
                "already": False,
                "summary": "hello",
                "raw": {"message": "ok"},
            }
        )
        event = Event([])

        with patch.object(module.logger, "info") as log_info:
            asyncio.run(collect_async_generator(plugin.tool_like_post(event, hostuin=0, fid="1")))

        log_line = next(
            call.args[1]
            for call in log_info.call_args_list
            if call.args and call.args[0] == "qzone llm tool result: %s"
        )
        logged = json.loads(log_line)
        self.assertTrue(logged["ok"])
        self.assertEqual(logged["tool"], "qzone_like_post")
        self.assertEqual(logged["arguments"]["fid"], "1")
        self.assertEqual(logged["result"]["raw"]["message"], "ok")

    def test_llm_like_tool_forwards_latest_and_index_reference(self):
        module = self.load_main_module()
        plugin = self.make_plugin(module)
        plugin._context = types.SimpleNamespace(
            llm_generate=AsyncMock(return_value=types.SimpleNamespace(completion_text="?????? 2 ??????")),
        )
        plugin.controller.like_post = AsyncMock(
            return_value={
                "action": "like",
                "liked": True,
                "verified": True,
                "already": False,
                "summary": "hello",
            }
        )
        event = Event([])

        asyncio.run(
            collect_async_generator(
                plugin.tool_like_post(event, hostuin=3112333596, latest=True, index=2)
            )
        )

        plugin.controller.like_post.assert_awaited_once_with(
            hostuin=3112333596,
            fid="",
            appid=311,
            unlike=False,
            latest=True,
            index=2,
        )

    def test_llm_like_tool_ignores_preview_confirmation(self):
        module = self.load_main_module()
        plugin = self.make_plugin(module)
        plugin._context = types.SimpleNamespace(
            llm_generate=AsyncMock(return_value=types.SimpleNamespace(completion_text="???????????????????")),
        )
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
        self.assertEqual(results[0], "???????????????????")
        prompt = plugin._context.llm_generate.await_args.kwargs["prompt"]
        self.assertIn('"verified":false', prompt)
        self.assertFalse(results[0].lstrip().startswith("{"))

    def test_llm_like_tool_asks_llm_for_natural_error_reply(self):
        module = self.load_main_module()
        plugin = self.make_plugin(module)
        plugin._context = types.SimpleNamespace(
            llm_generate=AsyncMock(return_value=types.SimpleNamespace(completion_text="?????QQ ??????????")),
        )
        plugin.controller.like_post = AsyncMock(side_effect=module.QzoneBridgeError("????"))
        event = Event([])

        results = asyncio.run(
            collect_async_generator(plugin.tool_like_post(event, hostuin=0, fid="1", confirm=False))
        )

        self.assertEqual(results[0], "?????QQ ??????????")
        prompt = plugin._context.llm_generate.await_args.kwargs["prompt"]
        self.assertIn('"ok":false', prompt)
        self.assertIn("????", prompt)
        self.assertFalse(results[0].lstrip().startswith("{"))

    def test_llm_like_tool_logs_error_and_sends_diagnostic_to_llm(self):
        module = self.load_main_module()
        plugin = self.make_plugin(module)
        plugin._context = types.SimpleNamespace(
            llm_generate=AsyncMock(return_value=types.SimpleNamespace(completion_text="?????QQ ??????????")),
        )
        exc = module.QzoneBridgeError(
            "QQ?????????? (503)",
            detail={
                "status_code": 503,
                "url": "https://w.qzone.qq.com/cgi-bin/likes/internal_dolike_app?g_tk=123",
                "text": "service unavailable",
            },
        )
        exc.status_code = 503
        plugin.controller.like_post = AsyncMock(side_effect=exc)
        event = Event([])

        with patch.object(module.logger, "warning") as log_warning:
            results = asyncio.run(
                collect_async_generator(plugin.tool_like_post(event, hostuin=0, fid="1", confirm=False))
            )

        self.assertEqual(results[0], "?????QQ ??????????")
        prompt = plugin._context.llm_generate.await_args.kwargs["prompt"]
        self.assertIn('"diagnostic"', prompt)
        self.assertIn('"status_code":503', prompt)
        self.assertIn("service unavailable", prompt)
        log_line = next(
            call.args[1]
            for call in log_warning.call_args_list
            if call.args and call.args[0] == "qzone llm tool result: %s"
        )
        logged = json.loads(log_line)
        self.assertFalse(logged["ok"])
        self.assertEqual(logged["error"]["status_code"], 503)
        self.assertEqual(logged["detail"]["text"], "service unavailable")
        self.assertIn("g_tk=%2A%2A%2A", logged["detail"]["url"])

    def test_llm_like_tool_falls_back_to_natural_text_without_llm_provider(self):
        module = self.load_main_module()
        plugin = self.make_plugin(module)
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

        self.assertIn("???????", results[0])
        self.assertFalse(results[0].lstrip().startswith("{"))

    def test_llm_like_tool_error_fallback_does_not_expose_detail(self):
        module = self.load_main_module()
        plugin = self.make_plugin(module)
        plugin.controller.like_post = AsyncMock(
            side_effect=module.QzoneBridgeError("????", detail={"fid": "secret-fid", "raw": {"code": 1}})
        )
        event = Event([])

        results = asyncio.run(
            collect_async_generator(plugin.tool_like_post(event, hostuin=0, fid="1", confirm=False))
        )

        self.assertEqual(results[0], "????")
        self.assertNotIn("secret-fid", results[0])
        self.assertFalse(results[0].lstrip().startswith("{"))


if __name__ == "__main__":
    unittest.main()
