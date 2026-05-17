import asyncio
import importlib.util
import inspect
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
    def marker_decorator(marker_name, *args, **kwargs):
        def decorator(func):
            setattr(func, f"_{marker_name}_args", args)
            setattr(func, f"_{marker_name}_kwargs", kwargs)
            return func

        return decorator

    def identity_decorator(*args, **kwargs):
        def decorator(func):
            return func

        return decorator

    def command_decorator(*args, **kwargs):
        return marker_decorator("filter_command", *args, **kwargs)

    def group_command_decorator(*args, **kwargs):
        return marker_decorator("qzone_command", *args, **kwargs)

    def command_group(*args, **kwargs):
        def decorator(func):
            setattr(func, "_command_group_args", args)
            setattr(func, "_command_group_kwargs", kwargs)
            func.command = group_command_decorator
            return func

        return decorator

    filter_stub = types.SimpleNamespace(
        command=command_decorator,
        command_group=command_group,
        on_platform_loaded=identity_decorator,
        platform_adapter_type=identity_decorator,
        llm_tool=identity_decorator,
        permission_type=identity_decorator,
        PermissionType=types.SimpleNamespace(ADMIN="admin"),
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
        plugin._schedule_publish_render_asset_preload = lambda *args, **kwargs: None
        return plugin

    def test_legacy_qzone_group_subcommands_stay_registered(self):
        module = self.load_main_module()

        expected = {
            "qzone_help": "help",
            "qzone_status": "status",
            "qzone_bind": "bind",
            "qzone_autobind": "autobind",
            "qzone_unbind": "unbind",
            "qzone_feed": "feed",
            "qzone_detail": "detail",
            "qzone_post": "post",
            "qzone_comment": "comment",
            "qzone_like": "like",
        }
        for method_name, command_name in expected.items():
            with self.subTest(method=method_name):
                method = getattr(module.QzoneStablePlugin, method_name)
                self.assertEqual(getattr(method, "_qzone_command_args", (None,))[0], command_name)

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

    def test_qzone_post_uses_preloaded_qzone_publisher_profile(self):
        module = self.load_main_module()
        plugin = self.make_plugin(module)
        plugin.controller.get_status = AsyncMock(return_value={"login_uin": 123456})
        cached_avatar = plugin.data_dir / "render_assets" / "avatar_123456.png"
        cached_avatar.parent.mkdir(parents=True, exist_ok=True)
        cached_avatar.write_bytes(b"png")
        plugin._publisher_profile_cache = (
            123456,
            module.RenderProfile(nickname="QzoneOwner", user_id="123456", avatar_source=str(cached_avatar)),
        )
        fake_image = plugin.data_dir / "rendered.png"
        fake_image.write_bytes(b"png")
        captured = {}

        event = Event([Plain("/qzone post hello")])
        event.bot = types.SimpleNamespace(get_stranger_info=AsyncMock(return_value={"nickname": "NetworkName"}))

        def fake_render(post, output_dir, **kwargs):
            captured.update(kwargs)
            return fake_image

        with patch.object(module, "render_publish_result_image", side_effect=fake_render):
            results = asyncio.run(collect_async_generator(plugin.qzone_post(event, content="/qzone post hello")))

        self.assertEqual(results, [{"image": str(fake_image)}])
        self.assertEqual(captured["profile"].nickname, "QzoneOwner")
        self.assertEqual(captured["profile"].user_id, "123456")
        self.assertEqual(captured["profile"].avatar_source, str(cached_avatar))
        event.bot.get_stranger_info.assert_not_awaited()

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

    def test_llm_view_post_uses_persona_reply_and_keeps_visible_numbering(self):
        module = self.load_main_module()
        plugin = self.make_plugin(module)
        plugin._context = types.SimpleNamespace(
            get_current_chat_provider_id=AsyncMock(return_value="provider-1"),
            llm_generate=AsyncMock(return_value=types.SimpleNamespace(completion_text="第 2 条是：two，Alice 也回了 nice。")),
        )
        entries = [
            module.FeedEntry(hostuin=123456, fid="fid-1", appid=311, summary="one"),
            module.FeedEntry(hostuin=123456, fid="fid-2", appid=311, summary="two"),
        ]
        plugin.controller.list_feeds = AsyncMock(return_value={"items": [asdict(item) for item in entries]})
        plugin.controller.detail_feed = AsyncMock(
            return_value={
                "entry": asdict(entries[1]),
                "comments": [{"commentid": "c1", "uin": 9988, "nickname": "Alice", "content": "nice"}],
                "raw": {},
            }
        )
        event = Event([])

        results = asyncio.run(
            collect_async_generator(plugin.tool_view_post(event, target_uin=123456, selector="2", detail=True))
        )

        self.assertEqual(results[0], "第 2 条是：two，Alice 也回了 nice。")
        prompt = plugin._context.llm_generate.await_args.kwargs["prompt"]
        self.assertIn("第 2 条", prompt)
        self.assertIn("two", prompt)
        self.assertIn("Alice: nice", prompt)
        self.assertNotIn("fid-2", prompt)
        self.assertNotIn("fid-2", results[0])

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

    def test_llm_like_tool_accepts_semantic_selector(self):
        module = self.load_main_module()
        plugin = self.make_plugin(module)
        plugin._context = types.SimpleNamespace(
            llm_generate=AsyncMock(return_value=types.SimpleNamespace(completion_text="第 2 条已经点好了。")),
        )
        entries = [
            module.FeedEntry(hostuin=123456, fid="fid-1", appid=311, summary="one"),
            module.FeedEntry(hostuin=123456, fid="fid-2", appid=311, summary="two", busi_param={"ugc": 1}),
        ]
        plugin.controller.list_feeds = AsyncMock(return_value={"items": [asdict(item) for item in entries]})
        plugin.controller.like_post = AsyncMock(
            return_value={
                "action": "like",
                "liked": True,
                "verified": True,
                "already": False,
                "summary": "two",
            }
        )
        event = Event([])

        results = asyncio.run(
            collect_async_generator(
                plugin.tool_like_post(event, target_uin=123456, selector="2")
            )
        )

        plugin.controller.list_feeds.assert_awaited_once()
        plugin.controller.like_post.assert_awaited_once_with(
            hostuin=123456,
            fid="fid-2",
            appid=311,
            unlike=False,
        )
        self.assertEqual(results[0], "第 2 条已经点好了。")

    def test_llm_like_tool_uses_target_uin_for_legacy_fid(self):
        module = self.load_main_module()
        plugin = self.make_plugin(module)
        plugin._context = types.SimpleNamespace(
            llm_generate=AsyncMock(return_value=types.SimpleNamespace(completion_text="这条已经点好了。")),
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

        results = asyncio.run(
            collect_async_generator(
                plugin.tool_like_post(event, target_uin=123456, fid="fid-1")
            )
        )

        plugin.controller.like_post.assert_awaited_once_with(
            hostuin=123456,
            fid="fid-1",
            appid=311,
            unlike=False,
            latest=False,
            index=0,
        )
        self.assertEqual(results[0], "这条已经点好了。")

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

    def test_target_range_parser_supports_at_and_one_based_range(self):
        module = self.load_main_module()
        plugin = self.make_plugin(module)
        event = Event([], message_str="看说说 @123456 2~4")

        target, start, end = plugin._parse_target_range(event, ("看说说", "查看说说"))

        self.assertEqual(target, 123456)
        self.assertEqual((start, end), (2, 4))

    def test_view_feed_uses_target_command_detail_shape(self):
        module = self.load_main_module()
        plugin = self.make_plugin(module)
        entries = [
            module.FeedEntry(hostuin=123456, fid="fid-0", appid=311, summary="zero"),
            module.FeedEntry(hostuin=123456, fid="fid-1", appid=311, summary="one"),
        ]
        plugin.controller.list_feeds = AsyncMock(return_value={"items": [asdict(item) for item in entries]})
        plugin.controller.detail_feed = AsyncMock(
            return_value={
                "entry": asdict(entries[1]),
                "comments": [{"commentid": "c1", "uin": 9988, "nickname": "Alice", "content": "nice"}],
                "raw": {},
            }
        )
        event = Event([], message_str="看说说 2")

        results = asyncio.run(collect_async_generator(plugin.view_feed(event)))

        plugin.controller.list_feeds.assert_awaited_once()
        plugin.controller.detail_feed.assert_awaited_once_with(hostuin=123456, fid="fid-1", appid=311)
        self.assertIn("one", results[0])
        self.assertIn("Alice", results[0])

    def test_comment_feed_passes_busi_param_from_detail_entry(self):
        module = self.load_main_module()
        plugin = self.make_plugin(module)
        entry = module.FeedEntry(
            hostuin=123456,
            fid="fid-1",
            appid=311,
            summary="hello",
            busi_param={"ugc": 1},
        )
        plugin.controller.list_feeds = AsyncMock(return_value={"items": [asdict(entry)]})
        plugin.controller.detail_feed = AsyncMock(return_value={"entry": asdict(entry), "comments": [], "raw": {}})
        plugin.controller.comment_post = AsyncMock(return_value={"commentid": "c1"})
        plugin._generate_comment_text = AsyncMock(return_value="hello")
        event = Event([], message_str="评说说")

        asyncio.run(collect_async_generator(plugin.comment_feed(event)))

        plugin.controller.comment_post.assert_awaited_once()
        self.assertEqual(plugin.controller.comment_post.await_args.kwargs["busi_param"], {"ugc": 1})

    def test_llm_comment_tool_generates_comment_from_semantic_selector(self):
        module = self.load_main_module()
        plugin = self.make_plugin(module)
        plugin.settings.preview_writes = True
        plugin._context = types.SimpleNamespace(
            llm_generate=AsyncMock(
                side_effect=[
                    types.SimpleNamespace(completion_text="真不错"),
                    types.SimpleNamespace(completion_text="评论好了。"),
                ]
            ),
        )
        entries = [
            module.FeedEntry(hostuin=123456, fid="fid-1", appid=311, summary="one"),
            module.FeedEntry(hostuin=123456, fid="fid-2", appid=311, summary="two"),
        ]
        plugin.controller.list_feeds = AsyncMock(return_value={"items": [asdict(item) for item in entries]})
        plugin.controller.detail_feed = AsyncMock(
            return_value={
                "entry": asdict(entries[1]),
                "comments": [{"commentid": "c1", "uin": 9988, "nickname": "Alice", "content": "nice"}],
                "raw": {},
            }
        )
        plugin.controller.comment_post = AsyncMock(return_value={"commentid": "c2", "message": "ok"})
        event = Event([])

        results = asyncio.run(
            collect_async_generator(
                plugin.tool_comment_post(event, target_uin=123456, selector="2", content="")
            )
        )

        plugin.controller.comment_post.assert_awaited_once_with(
            hostuin=123456,
            fid="fid-2",
            content="真不错",
            appid=311,
            private=False,
            busi_param={},
        )
        first_prompt = plugin._context.llm_generate.await_args_list[0].kwargs["prompt"]
        self.assertIn("说说内容：two", first_prompt)
        self.assertIn("Alice: nice", first_prompt)
        self.assertNotIn("fid-2", first_prompt)
        self.assertEqual(results[0], "评论好了。")

    def test_llm_comment_tool_docstring_keeps_compat_args_separately_typed(self):
        module = self.load_main_module()

        doc = inspect.getdoc(module.QzoneStablePlugin.tool_comment_post) or ""

        self.assertNotIn("hostuin/fid/confirm/appid/latest/index", doc)
        for name in ("hostuin", "fid", "confirm", "appid", "latest", "index"):
            self.assertRegex(doc, rf"(?m)^\s*{name}\s+\([^)]+\):")

    def test_comment_feed_uses_explicit_comment_text_without_llm(self):
        module = self.load_main_module()
        plugin = self.make_plugin(module)
        entry = module.FeedEntry(hostuin=123456, fid="fid-1", appid=311, summary="hello")
        plugin.controller.list_feeds = AsyncMock(return_value={"items": [asdict(entry)]})
        plugin.controller.detail_feed = AsyncMock(return_value={"entry": asdict(entry), "comments": [], "raw": {}})
        plugin.controller.comment_post = AsyncMock(return_value={"commentid": "c1"})
        plugin._generate_comment_text = AsyncMock(return_value="should-not-use")
        event = Event([], message_str="评说说 1 写得真好")

        asyncio.run(collect_async_generator(plugin.comment_feed(event)))

        plugin._generate_comment_text.assert_not_awaited()
        self.assertEqual(plugin.controller.comment_post.await_args.kwargs["content"], "写得真好")

    def test_contribution_approve_publishes_draft(self):
        module = self.load_main_module()
        plugin = self.make_plugin(module)
        plugin.settings.render_publish_result = False
        plugin.drafts = module.DraftStore(plugin.data_dir / "drafts.json")
        event = Event([Plain("投稿 hello")], message_str="投稿 hello")

        contribute_results = asyncio.run(collect_async_generator(plugin.contribute_post(event)))
        draft = plugin.drafts.get(1)
        self.assertIsNotNone(draft)
        self.assertIn("#1", contribute_results[0])

        approve_event = Event([], message_str="过稿 1")
        approve_results = asyncio.run(collect_async_generator(plugin.approve_post(approve_event)))

        plugin.controller.publish_post.assert_awaited()
        self.assertEqual(plugin.drafts.get(1).status, "published")
        self.assertIn("发布结果", approve_results[0])

    def test_approve_post_adds_submitter_name_when_configured(self):
        module = self.load_main_module()
        plugin = self.make_plugin(module)
        plugin.settings.render_publish_result = False
        plugin.settings.show_name = True
        plugin.drafts = module.DraftStore(plugin.data_dir / "drafts.json")
        event = Event([Plain("投稿 hello")], message_str="投稿 hello")
        plugin._sender_id = lambda event: 123456
        plugin._sender_name = lambda event: "Alice"

        asyncio.run(collect_async_generator(plugin.contribute_post(event)))
        approve_event = Event([], message_str="过稿 1")
        asyncio.run(collect_async_generator(plugin.approve_post(approve_event)))

        content = plugin.controller.publish_post.await_args.kwargs["content"]
        self.assertTrue(content.startswith("【来自 Alice 的投稿】"))
        self.assertIn("hello", content)

    def test_reply_comment_accepts_cached_viewed_post_id(self):
        module = self.load_main_module()
        plugin = self.make_plugin(module)
        entry = module.FeedEntry(hostuin=123456, fid="fid-1", appid=311, summary="hello")
        post = module.post_from_entry(entry, local_id=0)
        plugin._post_store().upsert(post)
        plugin.controller.detail_feed = AsyncMock(
            return_value={
                "entry": asdict(entry),
                "comments": [{"commentid": "c1", "uin": 9988, "nickname": "Bob", "content": "nice"}],
                "raw": {},
            }
        )
        plugin.controller.reply_comment = AsyncMock(return_value={"commentid": "r1", "message": "ok"})
        plugin.controller.get_status = AsyncMock(return_value={"login_uin": 123456})
        plugin._generate_reply_text = AsyncMock(return_value="收到")
        event = Event([], message_str="回评 1")

        results = asyncio.run(collect_async_generator(plugin.reply_comment(event)))

        plugin.controller.reply_comment.assert_awaited_once_with(
            hostuin=123456,
            fid="fid-1",
            commentid="c1",
            comment_uin=9988,
            content="收到",
            appid=311,
        )
        self.assertIn("回复结果", results[0])

    def test_auto_comment_persists_dedupe_between_runs(self):
        module = self.load_main_module()
        plugin = self.make_plugin(module)
        entries = [
            module.FeedEntry(hostuin=111, fid="fid-1", appid=311, summary="one"),
            module.FeedEntry(hostuin=222, fid="fid-2", appid=311, summary="two"),
        ]
        plugin.controller.list_feeds = AsyncMock(return_value={"items": [asdict(item) for item in entries]})
        plugin.controller.detail_feed = AsyncMock(
            side_effect=[
                {"entry": asdict(entries[0]), "comments": [], "raw": {}},
                {"entry": asdict(entries[0]), "comments": [], "raw": {}},
                {"entry": asdict(entries[1]), "comments": [], "raw": {}},
            ]
        )
        plugin.controller.comment_post = AsyncMock(return_value={"commentid": "c1"})
        plugin._generate_comment_text = AsyncMock(return_value="好")

        asyncio.run(plugin._auto_comment_once())
        asyncio.run(plugin._auto_comment_once())

        self.assertEqual(plugin.controller.comment_post.await_count, 2)
        first = plugin.controller.comment_post.await_args_list[0].kwargs
        second = plugin.controller.comment_post.await_args_list[1].kwargs
        self.assertEqual(first["fid"], "fid-1")
        self.assertEqual(second["fid"], "fid-2")

    def test_markdown_result_uses_pillowmd_when_configured(self):
        module = self.load_main_module()
        plugin = self.make_plugin(module)
        plugin.settings.pillowmd_style_dir = "style-dir"
        saved_pillowmd = sys.modules.get("pillowmd")

        class FakeImage:
            def Save(self, output_dir):
                path = Path(output_dir) / "render.png"
                path.write_bytes(b"png")
                return path

        class FakeStyle:
            async def AioRender(self, **kwargs):
                return FakeImage()

        fake_pillowmd = types.SimpleNamespace(LoadMarkdownStyles=lambda style_dir: FakeStyle())
        sys.modules["pillowmd"] = fake_pillowmd
        if saved_pillowmd is None:
            self.addCleanup(lambda: sys.modules.pop("pillowmd", None))
        else:
            self.addCleanup(lambda: sys.modules.__setitem__("pillowmd", saved_pillowmd))
        event = Event([])

        result = asyncio.run(plugin._markdown_result(event, "hello", subdir="test"))

        self.assertIn("image", result)
        self.assertTrue(Path(result["image"]).exists())

    def test_generate_post_text_appends_filtered_group_history(self):
        module = self.load_main_module()
        plugin = self.make_plugin(module)
        plugin.settings.post_max_msg = 10
        plugin.settings.ignore_users = ["999"]
        plugin._context = types.SimpleNamespace(
            llm_generate=AsyncMock(return_value=types.SimpleNamespace(completion_text="draft"))
        )
        event = Event([], message_str="写说说")
        event.message_obj.group_id = 100
        event.bot = types.SimpleNamespace(
            api=types.SimpleNamespace(
                call_action=AsyncMock(
                    return_value={
                        "messages": [
                            {
                                "message_id": 1,
                                "sender": {"user_id": 123, "nickname": "Alice"},
                                "message": [{"type": "text", "data": {"text": "今天真热"}}],
                            },
                            {
                                "message_id": 2,
                                "sender": {"user_id": 999, "nickname": "Ignored"},
                                "message": [{"type": "text", "data": {"text": "跳过我"}}],
                            },
                        ]
                    }
                )
            )
        )

        result = asyncio.run(plugin._generate_post_text(event, "天气"))

        self.assertEqual(result, "draft")
        prompt = plugin._context.llm_generate.await_args.kwargs["prompt"]
        self.assertIn("聊天记录参考", prompt)
        self.assertIn("Alice: 今天真热", prompt)
        self.assertNotIn("跳过我", prompt)

    def test_write_feed_saves_clean_draft_when_model_returns_tool_call(self):
        module = self.load_main_module()
        plugin = self.make_plugin(module)
        plugin.drafts = module.DraftStore(plugin.data_dir / "drafts.json")
        plugin._context = types.SimpleNamespace(
            get_current_chat_provider_id=AsyncMock(return_value="provider-1"),
            llm_generate=AsyncMock(
                return_value=types.SimpleNamespace(
                    completion_text='qzone_publish_post(content="把晚风装进口袋", confirm=true)'
                )
            ),
        )
        event = Event([Plain("写说说 晚风")], message_str="写说说 晚风")

        asyncio.run(collect_async_generator(plugin.write_feed(event)))

        drafts = plugin.drafts.list(status="pending")
        self.assertEqual(len(drafts), 1)
        self.assertEqual(drafts[0].content, "把晚风装进口袋")
        prompt = plugin._context.llm_generate.await_args.kwargs["prompt"]
        self.assertIn("只输出最终可发布的说说正文", prompt)
        self.assertNotIn("qzone_publish_post", drafts[0].content)

    def test_legacy_llm_publish_feed_uses_persona_reply_without_action_dump(self):
        module = self.load_main_module()
        plugin = self.make_plugin(module)
        plugin._context = types.SimpleNamespace(
            get_current_chat_provider_id=AsyncMock(return_value="provider-1"),
            llm_generate=AsyncMock(return_value=types.SimpleNamespace(completion_text="发好啦，刚刚那句挺顺的。")),
        )
        event = Event([])

        result = asyncio.run(plugin.llm_publish_feed(event, text="hello", get_image=False))

        plugin.controller.publish_post.assert_awaited_once()
        self.assertEqual(result, "发好啦，刚刚那句挺顺的。")
        self.assertNotIn("发布结果", result)
        self.assertNotIn("fid", result)
        self.assertFalse(result.lstrip().startswith("{"))

    def test_semantic_publish_tool_uses_persona_reply_without_render_dump(self):
        module = self.load_main_module()
        plugin = self.make_plugin(module)
        plugin._context = types.SimpleNamespace(
            get_current_chat_provider_id=AsyncMock(return_value="provider-1"),
            llm_generate=AsyncMock(return_value=types.SimpleNamespace(completion_text="发好了，这条很像你。")),
        )
        event = Event([])

        results = asyncio.run(
            collect_async_generator(plugin.tool_publish_post(event, content="hello", confirm=True))
        )

        plugin.controller.publish_post.assert_awaited_once()
        self.assertEqual(results[0], "发好了，这条很像你。")
        self.assertNotIn("发布结果", results[0])
        self.assertNotIn("fid", results[0])
        self.assertFalse(str(results[0]).lstrip().startswith("{"))

    def test_legacy_llm_view_feed_comments_when_user_intent_is_comment(self):
        module = self.load_main_module()
        plugin = self.make_plugin(module)
        plugin._context = types.SimpleNamespace(
            get_current_chat_provider_id=AsyncMock(return_value="provider-1"),
            llm_generate=AsyncMock(return_value=types.SimpleNamespace(completion_text="评论好了，这句接得挺自然。")),
        )
        entries = [
            module.FeedEntry(hostuin=123456, fid="fid-1", appid=311, summary="one"),
            module.FeedEntry(hostuin=123456, fid="fid-2", appid=311, summary="two"),
        ]
        plugin.controller.list_feeds = AsyncMock(return_value={"items": [asdict(item) for item in entries]})
        plugin.controller.detail_feed = AsyncMock(return_value={"entry": asdict(entries[1]), "comments": [], "raw": {}})
        plugin.controller.comment_post = AsyncMock(return_value={"commentid": "c2", "message": "ok"})
        plugin._generate_comment_text = AsyncMock(return_value="真不错")
        event = Event([], message_str="帮我评论 123456 的第二条说说")

        result = asyncio.run(plugin.llm_view_feed(event, user_id="123456", pos=1, reply=False))

        plugin.controller.comment_post.assert_awaited_once_with(
            hostuin=123456,
            fid="fid-2",
            content="真不错",
            appid=311,
            private=False,
            busi_param={},
        )
        self.assertEqual(result, "评论好了，这句接得挺自然。")
        self.assertNotIn("fid-2", result)
        self.assertNotIn("qzone_comment_post", result)

    def test_semantic_view_tool_comments_when_user_intent_is_comment(self):
        module = self.load_main_module()
        plugin = self.make_plugin(module)
        plugin._context = types.SimpleNamespace(
            get_current_chat_provider_id=AsyncMock(return_value="provider-1"),
            llm_generate=AsyncMock(return_value=types.SimpleNamespace(completion_text="评论接上了。")),
        )
        entries = [
            module.FeedEntry(hostuin=123456, fid="fid-1", appid=311, summary="one"),
            module.FeedEntry(hostuin=123456, fid="fid-2", appid=311, summary="two"),
        ]
        plugin.controller.list_feeds = AsyncMock(return_value={"items": [asdict(item) for item in entries]})
        plugin.controller.detail_feed = AsyncMock(return_value={"entry": asdict(entries[1]), "comments": [], "raw": {}})
        plugin.controller.comment_post = AsyncMock(return_value={"commentid": "c2", "message": "ok"})
        plugin._generate_comment_text = AsyncMock(return_value="真不错")
        event = Event([], message_str="帮我评论第二条说说")

        results = asyncio.run(
            collect_async_generator(plugin.tool_view_post(event, target_uin=123456, selector="2", detail=True))
        )

        plugin.controller.comment_post.assert_awaited_once_with(
            hostuin=123456,
            fid="fid-2",
            content="真不错",
            appid=311,
            private=False,
            busi_param={},
        )
        self.assertEqual(results[0], "评论接上了。")
        self.assertNotIn("fid-2", results[0])
        self.assertNotIn("qzone_comment_post", results[0])

    def test_semantic_view_tool_does_not_comment_when_user_only_wants_comments_shown(self):
        module = self.load_main_module()
        plugin = self.make_plugin(module)
        plugin._context = types.SimpleNamespace(
            get_current_chat_provider_id=AsyncMock(return_value="provider-1"),
            llm_generate=AsyncMock(return_value=types.SimpleNamespace(completion_text="第 2 条下面 Alice 说 nice。")),
        )
        entries = [
            module.FeedEntry(hostuin=123456, fid="fid-1", appid=311, summary="one"),
            module.FeedEntry(hostuin=123456, fid="fid-2", appid=311, summary="two"),
        ]
        plugin.controller.list_feeds = AsyncMock(return_value={"items": [asdict(item) for item in entries]})
        plugin.controller.detail_feed = AsyncMock(
            return_value={
                "entry": asdict(entries[1]),
                "comments": [{"commentid": "c1", "uin": 9988, "nickname": "Alice", "content": "nice"}],
                "raw": {},
            }
        )
        plugin.controller.comment_post = AsyncMock(return_value={"commentid": "c2", "message": "ok"})
        event = Event([], message_str="帮我看看第二条说说的评论区")

        results = asyncio.run(
            collect_async_generator(plugin.tool_view_post(event, target_uin=123456, selector="2", detail=True))
        )

        plugin.controller.comment_post.assert_not_awaited()
        self.assertEqual(results[0], "第 2 条下面 Alice 说 nice。")

    def test_semantic_view_tool_does_not_like_when_user_only_wants_like_count(self):
        module = self.load_main_module()
        plugin = self.make_plugin(module)
        plugin._context = types.SimpleNamespace(
            get_current_chat_provider_id=AsyncMock(return_value="provider-1"),
            llm_generate=AsyncMock(return_value=types.SimpleNamespace(completion_text="第 1 条现在有 3 个赞。")),
        )
        entry = module.FeedEntry(hostuin=123456, fid="fid-1", appid=311, summary="one", like_count=3)
        plugin.controller.list_feeds = AsyncMock(return_value={"items": [asdict(entry)]})
        plugin.controller.detail_feed = AsyncMock(return_value={"entry": asdict(entry), "comments": [], "raw": {}})
        plugin.controller.like_post = AsyncMock(return_value={"action": "like", "liked": True})
        event = Event([], message_str="帮我看看第一条说说的点赞数")

        results = asyncio.run(
            collect_async_generator(plugin.tool_view_post(event, target_uin=123456, selector="1", detail=True))
        )

        plugin.controller.like_post.assert_not_awaited()
        self.assertEqual(results[0], "第 1 条现在有 3 个赞。")

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
