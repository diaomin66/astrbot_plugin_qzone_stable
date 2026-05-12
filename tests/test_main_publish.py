import asyncio
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock


class Plain:
    def __init__(self, text):
        self.text = text


class Event:
    def __init__(self, components):
        self.message_obj = types.SimpleNamespace(message=components)
        self.stopped = False

    def is_admin(self):
        return True

    def stop_event(self):
        self.stopped = True

    def plain_result(self, text):
        return text


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
        plugin._ensure_cookie_ready = AsyncMock()
        plugin._ensure_daemon = AsyncMock()
        plugin.controller = types.SimpleNamespace(
            publish_post=AsyncMock(return_value={"fid": "fid-1", "message": "ok"})
        )
        return plugin

    def test_qzone_post_stops_event_and_strips_split_command_tokens(self):
        module = self.load_main_module()
        plugin = self.make_plugin(module)
        event = Event([Plain("/qzone "), Plain("post"), Plain("hello")])

        asyncio.run(collect_async_generator(plugin.qzone_post(event, content="/qzone post hello")))

        self.assertTrue(event.stopped)
        plugin.controller.publish_post.assert_awaited_once()
        self.assertEqual(plugin.controller.publish_post.await_args.kwargs["content"], "hello")

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


if __name__ == "__main__":
    unittest.main()
