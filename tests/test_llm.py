import asyncio
import types
import unittest
from unittest.mock import AsyncMock

from qzone_bridge.llm import QzoneLLM


class QzoneLLMTests(unittest.TestCase):
    def test_generate_text_awaits_async_provider_lookup(self):
        provider = types.SimpleNamespace(
            text_chat=AsyncMock(return_value=types.SimpleNamespace(completion_text="自然回复"))
        )
        context = types.SimpleNamespace(get_using_provider=AsyncMock(return_value=provider))
        settings = types.SimpleNamespace()
        llm = QzoneLLM(context, settings)

        text = asyncio.run(llm.generate_text(None, "把结果说自然点", system_prompt="不要暴露内部字段"))

        self.assertEqual(text, "自然回复")
        context.get_using_provider.assert_awaited_once()
        provider.text_chat.assert_awaited_once_with(
            prompt="把结果说自然点",
            contexts=[],
            system_prompt="不要暴露内部字段",
        )


if __name__ == "__main__":
    unittest.main()
