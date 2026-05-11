import importlib.util
import inspect
import sys
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


class ImportIsolationTests(unittest.TestCase):
    def test_main_reloads_bundled_qzone_bridge_when_stale_module_is_cached(self):
        root = Path(__file__).resolve().parents[1]
        saved_modules = {
            name: module
            for name, module in sys.modules.items()
            if name == "qzone_bridge"
            or name.startswith("qzone_bridge.")
            or name == "astrbot"
            or name.startswith("astrbot.")
            or name == "_qzone_import_isolation_main"
        }
        original_path = list(sys.path)
        try:
            with TemporaryDirectory() as tmp:
                stale_package = types.ModuleType("qzone_bridge")
                stale_package.__file__ = str(Path(tmp) / "qzone_bridge" / "__init__.py")
                stale_package.__path__ = [str(Path(tmp) / "qzone_bridge")]
                stale_controller = types.ModuleType("qzone_bridge.controller")
                stale_controller.__file__ = str(Path(tmp) / "qzone_bridge" / "controller.py")

                class OldQzoneDaemonController:
                    def __init__(self, *, plugin_root, data_dir):
                        pass

                stale_controller.QzoneDaemonController = OldQzoneDaemonController
                sys.modules["qzone_bridge"] = stale_package
                sys.modules["qzone_bridge.controller"] = stale_controller
                self._install_astrbot_stubs()

                spec = importlib.util.spec_from_file_location("_qzone_import_isolation_main", root / "main.py")
                module = importlib.util.module_from_spec(spec)
                sys.modules[spec.name] = module
                assert spec.loader is not None
                spec.loader.exec_module(module)

                controller_module = sys.modules["qzone_bridge.controller"]
                self.assertIsNot(module.QzoneDaemonController, OldQzoneDaemonController)
                self.assertIn(str(root), str(Path(controller_module.__file__).resolve()))
                self.assertIn("auto_start_daemon", inspect.signature(module.QzoneDaemonController).parameters)
        finally:
            for name in list(sys.modules):
                if (
                    name == "qzone_bridge"
                    or name.startswith("qzone_bridge.")
                    or name == "astrbot"
                    or name.startswith("astrbot.")
                    or name == "_qzone_import_isolation_main"
                ):
                    sys.modules.pop(name, None)
            sys.modules.update(saved_modules)
            sys.path[:] = original_path

    @staticmethod
    def _install_astrbot_stubs() -> None:
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


if __name__ == "__main__":
    unittest.main()
