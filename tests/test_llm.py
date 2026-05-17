import asyncio
import types
import unittest
from unittest.mock import AsyncMock

from qzone_bridge.llm import QzoneLLM
from qzone_bridge.social import QzoneComment, QzonePost


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

    def test_generate_post_text_extracts_content_from_tool_like_output(self):
        context = types.SimpleNamespace(
            get_current_chat_provider_id=AsyncMock(return_value="persona-provider"),
            llm_generate=AsyncMock(
                return_value=types.SimpleNamespace(
                    completion_text='qzone_publish_post(content="今晚想把风留在窗边", confirm=true)'
                )
            ),
        )
        settings = types.SimpleNamespace(
            post_prompt="写一条像我本人的 QQ 空间说说。",
            post_provider_id="",
        )
        llm = QzoneLLM(context, settings)

        text = asyncio.run(llm.generate_post_text(types.SimpleNamespace(), "夜风"))

        self.assertEqual(text, "今晚想把风留在窗边")
        kwargs = context.llm_generate.await_args.kwargs
        self.assertEqual(kwargs["chat_provider_id"], "persona-provider")
        self.assertIn("沿用当前 AstrBot 人格", kwargs["system_prompt"])
        self.assertIn("只输出最终可发布的说说正文", kwargs["prompt"])
        self.assertNotIn("qzone_publish_post", text)
        self.assertNotIn("confirm", text)

    def test_generate_comment_text_extracts_comment_from_tool_like_output(self):
        context = types.SimpleNamespace(
            get_current_chat_provider_id=AsyncMock(return_value="persona-provider"),
            llm_generate=AsyncMock(
                return_value=types.SimpleNamespace(
                    completion_text='qzone_comment_post(target_uin=1, selector="latest", content="太会拍了")'
                )
            ),
        )
        settings = types.SimpleNamespace(
            comment_prompt="生成一句自然评论。",
            comment_provider_id="",
            comment_max_length=60,
        )
        llm = QzoneLLM(context, settings)
        post = QzonePost(
            hostuin=123456,
            fid="secret-fid",
            summary="夕阳落在操场边",
            images=["https://example.com/a.jpg"],
            comments=[QzoneComment(commentid="c1", nickname="Alice", content="好看")],
        )

        text = asyncio.run(llm.generate_comment_text(types.SimpleNamespace(), post))

        self.assertEqual(text, "太会拍了")
        prompt = context.llm_generate.await_args.kwargs["prompt"]
        self.assertIn("说说内容：夕阳落在操场边", prompt)
        self.assertIn("Alice: 好看", prompt)
        self.assertNotIn("secret-fid", prompt)
        self.assertNotIn("qzone_comment_post", text)
        self.assertNotIn("selector", text)


if __name__ == "__main__":
    unittest.main()
