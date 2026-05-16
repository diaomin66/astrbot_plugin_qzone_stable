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
            "cookie_summary": "14 个 Cookie: uin, p_uin, skey, p_skey",
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
            "cookie_summary": "14 个 Cookie: uin, p_uin, skey, p_skey",
            "needs_rebind": False,
            "daemon_port": 18999,
        }
        plugin.controller = types.SimpleNamespace(
            get_status=AsyncMock(return_value=degraded),
            ensure_running=AsyncMock(
                side_effect=module.DaemonUnavailableError(
                    "QQ 空间 daemon 启动失败",
                    detail={"returncode": 7, "log_path": "D:/qzone/daemon.log", "log_tail": "daemon boom"},
                )
            ),
        )
        event = Event([])

        results = asyncio.run(collect_async_generator(plugin.qzone_status(event)))

        self.assertIn("- daemon: degraded", results[0])
        self.assertIn("- daemon_error: QQ 空间 daemon 启动失败", results[0])
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
            "cookie_summary": "4 个 Cookie: uin, p_uin, skey, p_skey",
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
        self.assertEqual(
            plugin.controller.publish_post.await_args.kwargs["content"],
            "report\n[\u6587\u4ef6: report.pdf]",
        )
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
            summary="甜酷风怎么样",
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
        self.assertIn("未赞", results[0])
        self.assertIn("0 赞", results[0])
        self.assertIn("0 评论", results[0])
        self.assertIn("甜酷风怎么样", results[0])
        self.assertIn("可以用上面的序号", results[0])
        self.assertNotIn("?" * 3, results[0])
        self.assertNotIn("cursor=", results[0])
        self.assertNotIn("has_more", results[0])
        self.assertNotIn("fid=", results[0])

    def test_llm_like_tool_asks_llm_for_natural_result_reply(self):
        module = self.load_main_module()
        plugin = self.make_plugin(module)
        plugin._context = types.SimpleNamespace(
            get_current_chat_provider_id=AsyncMock(return_value="provider-1"),
            llm_generate=AsyncMock(return_value=types.SimpleNamespace(completion_text="这条已经点好了。")),
        )
        plugin.controller.like_post = AsyncMock(
            return_value={
                "action": "like",
                "liked": True,
                "verified": True,
                "already": False,
                "summary": "甜酷风怎么样",
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
        self.assertEqual(results[0], "这条已经点好了。")
        plugin._context.llm_generate.assert_awaited_once()
        prompt = plugin._context.llm_generate.await_args.kwargs["prompt"]
        self.assertIn("不要照抄", prompt)
        self.assertIn("当前聊天里的人设", prompt)
        self.assertIn("甜酷风怎么样", prompt)
        self.assertNotIn('"ok"', prompt)
        self.assertNotIn('"verified"', prompt)
        self.assertNotIn("fid=", results[0])
        self.assertFalse(results[0].lstrip().startswith("{"))

    def test_llm_like_tool_logs_success_payload_to_astrbot_logger(self):
        module = self.load_main_module()
        plugin = self.make_plugin(module)
        plugin._context = types.SimpleNamespace(
            llm_generate=AsyncMock(return_value=types.SimpleNamespace(completion_text="好了")),
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
            llm_generate=AsyncMock(return_value=types.SimpleNamespace(completion_text="第 2 条已经点好了。")),
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
            llm_generate=AsyncMock(return_value=types.SimpleNamespace(completion_text="先帮你点上了，显示可能慢一点。")),
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
        self.assertEqual(results[0], "先帮你点上了，显示可能慢一点。")
        prompt = plugin._context.llm_generate.await_args.kwargs["prompt"]
        self.assertIn("QQ 空间显示可能会慢一点", prompt)
        self.assertIn("不要说成失败", prompt)
        self.assertNotIn('"verified"', prompt)
        self.assertNotIn("accepted_pending_verification", prompt)
        self.assertNotIn("actual_liked", prompt)
        self.assertFalse(results[0].lstrip().startswith("{"))

    def test_llm_like_tool_rejects_false_failure_reply_when_verification_pending(self):
        module = self.load_main_module()
        plugin = self.make_plugin(module)
        bad_reply = (
            "\u70b9\u8d5e\u5931\u8d25\uff0cQQ \u670d\u52a1\u5668\u672a\u786e\u8ba4\u3002\n"
            "```json\n"
            "{\"ok\": false, \"tool\": \"qzone_like_post\", \"status_code\": 403, \"raw\": {}}\n"
            "```"
        )
        plugin._context = types.SimpleNamespace(
            llm_generate=AsyncMock(return_value=types.SimpleNamespace(completion_text=bad_reply)),
        )
        plugin.controller.like_post = AsyncMock(
            return_value={
                "action": "like",
                "liked": True,
                "verified": False,
                "already": False,
                "summary": "hello",
                "verification": {"expected_liked": True, "actual_liked": False},
            }
        )
        event = Event([])

        results = asyncio.run(
            collect_async_generator(plugin.tool_like_post(event, hostuin=0, fid="1", confirm=False))
        )

        plugin._context.llm_generate.assert_awaited_once()
        self.assertIn("\u6211\u5148\u5e2e\u4f60\u70b9\u4e0a\u4e86", results[0])
        self.assertIn("\u7b49\u4e00\u4f1a\u513f\u624d\u663e\u793a", results[0])
        self.assertNotIn('"ok"', results[0])
        self.assertNotIn("status_code", results[0])
        self.assertNotIn("\u5df2\u53d1\u9001", results[0])
        self.assertFalse(results[0].lstrip().startswith("{"))

    def test_llm_like_tool_asks_llm_for_natural_error_reply(self):
        module = self.load_main_module()
        plugin = self.make_plugin(module)
        plugin._context = types.SimpleNamespace(
            llm_generate=AsyncMock(return_value=types.SimpleNamespace(completion_text="这会儿 QQ 空间还动不了。")),
        )
        plugin.controller.like_post = AsyncMock(side_effect=module.QzoneBridgeError("点赞失败"))
        event = Event([])

        results = asyncio.run(
            collect_async_generator(plugin.tool_like_post(event, hostuin=0, fid="1", confirm=False))
        )

        self.assertEqual(results[0], "这会儿 QQ 空间还动不了。")
        prompt = plugin._context.llm_generate.await_args.kwargs["prompt"]
        self.assertIn("\u73b0\u5728\u8fd8\u6ca1\u529e\u6cd5\u7ee7\u7eed", prompt)
        self.assertIn("点赞失败", prompt)
        self.assertNotIn('"ok"', prompt)
        self.assertNotIn("QZONE_ERROR", prompt)
        self.assertFalse(results[0].lstrip().startswith("{"))

    def test_llm_like_tool_logs_error_without_sending_diagnostic_to_llm(self):
        module = self.load_main_module()
        plugin = self.make_plugin(module)
        plugin._context = types.SimpleNamespace(
            llm_generate=AsyncMock(return_value=types.SimpleNamespace(completion_text="这会儿 QQ 空间还动不了。")),
        )
        exc = module.QzoneBridgeError(
            "QQ 空间服务暂时不可用 (503)",
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

        self.assertEqual(results[0], "这会儿 QQ 空间还动不了。")
        prompt = plugin._context.llm_generate.await_args.kwargs["prompt"]
        self.assertNotIn('"diagnostic"', prompt)
        self.assertNotIn("status_code", prompt)
        self.assertNotIn("service unavailable", prompt)
        self.assertNotIn("internal_dolike_app", prompt)
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

        self.assertIn("\u6211\u5148\u5e2e\u4f60\u70b9\u4e0a\u4e86", results[0])
        self.assertIn("\u7b49\u4e00\u4f1a\u513f\u624d\u663e\u793a", results[0])
        self.assertNotIn("\u5df2\u53d1\u9001", results[0])
        self.assertFalse(results[0].lstrip().startswith("{"))

    def test_llm_like_tool_error_fallback_does_not_expose_detail(self):
        module = self.load_main_module()
        plugin = self.make_plugin(module)
        plugin.controller.like_post = AsyncMock(
            side_effect=module.QzoneBridgeError("点赞失败", detail={"fid": "secret-fid", "raw": {"code": 1}})
        )
        event = Event([])

        results = asyncio.run(
            collect_async_generator(plugin.tool_like_post(event, hostuin=0, fid="1", confirm=False))
        )

        self.assertIn("\u665a\u70b9\u518d\u8bd5", results[0])
        self.assertNotIn("secret-fid", results[0])
        self.assertNotIn("raw", results[0])
        self.assertFalse(results[0].lstrip().startswith("{"))

    def test_llm_like_tool_strips_tool_error_prefix_and_rejects_unsafe_error_reply(self):
        module = self.load_main_module()
        plugin = self.make_plugin(module)
        unsafe_reply = "Result: [TOOL_UNAVAILABLE] \u4eba\u8bbe\u53c2\u8003\u56fe\u8fd8\u6ca1\u914d\u7f6e\u597d\u3002"
        plugin._context = types.SimpleNamespace(
            llm_generate=AsyncMock(return_value=types.SimpleNamespace(completion_text=unsafe_reply)),
        )
        plugin.controller.like_post = AsyncMock(
            side_effect=module.QzoneBridgeError(
                "Result: [TOOL_UNAVAILABLE] \u4eba\u8bbe\u53c2\u8003\u56fe\u8fd8\u6ca1\u914d\u7f6e\u597d\u3002"
                "\u8bf7\u7528\u81ea\u5df1\u5e73\u65f6\u7684\u8bed\u6c14\u544a\u8bc9\u7528\u6237\u73b0\u5728\u8fd8\u6ca1\u529e\u6cd5\uff0c\u4e0d\u8981\u63d0\u6307\u4ee4\u6216\u547d\u4ee4\u3002"
            )
        )
        event = Event([])

        results = asyncio.run(
            collect_async_generator(plugin.tool_like_post(event, hostuin=0, fid="1", confirm=False))
        )

        prompt = plugin._context.llm_generate.await_args.kwargs["prompt"]
        self.assertIn("\u4eba\u8bbe\u53c2\u8003\u56fe\u8fd8\u6ca1\u914d\u7f6e\u597d", prompt)
        self.assertNotIn("TOOL_UNAVAILABLE", prompt)
        self.assertIn("\u53c2\u8003\u5185\u5bb9\u51c6\u5907\u597d", results[0])
        self.assertNotIn("Result:", results[0])
        self.assertNotIn("TOOL_UNAVAILABLE", results[0])
        self.assertNotIn("\u5de5\u5177", results[0])
        self.assertNotIn("\u6307\u4ee4", results[0])
        self.assertNotIn("\u547d\u4ee4", results[0])


if __name__ == "__main__":
    unittest.main()
