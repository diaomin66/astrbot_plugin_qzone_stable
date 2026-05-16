import unittest

from qzone_bridge.selection import parse_post_selection, selection_from_tool_args


class PostSelectionTests(unittest.TestCase):
    def test_latest_aliases_select_first_post(self):
        selection = parse_post_selection("看说说 最新", ("看说说",))

        self.assertEqual(selection.target_uin, 0)
        self.assertEqual((selection.start, selection.end), (1, 1))
        self.assertEqual(selection.selector, "latest")

    def test_chinese_ordinal_and_plain_number_are_one_based(self):
        ordinal = parse_post_selection("看说说 第2条", ("看说说",))
        plain = parse_post_selection("赞说说 2", ("赞说说",))

        self.assertEqual((ordinal.start, ordinal.end), (2, 2))
        self.assertEqual((plain.start, plain.end), (2, 2))

    def test_range_at_target_and_comment_text_are_preserved(self):
        selection = parse_post_selection("评说说 @123456 1~3 写得真好", ("评说说",))

        self.assertEqual(selection.target_uin, 123456)
        self.assertEqual((selection.start, selection.end), (1, 3))
        self.assertEqual(selection.comment_text, "写得真好")

    def test_zero_keeps_legacy_latest_behavior(self):
        selection = parse_post_selection("看说说 0", ("看说说",))

        self.assertEqual((selection.start, selection.end), (1, 1))
        self.assertEqual(selection.selector, "latest")

    def test_hostuin_fid_compatibility(self):
        selection = parse_post_selection(
            "看说说 3112333596 1c7182b96589046ad3380900",
            ("看说说",),
        )

        self.assertEqual(selection.target_uin, 3112333596)
        self.assertEqual(selection.fid, "1c7182b96589046ad3380900")
        self.assertEqual(selection.selector, "fid")

    def test_tool_selector_keeps_legacy_fid_and_new_selector(self):
        legacy = selection_from_tool_args(hostuin=123456, fid="fid-1", appid=311)
        semantic = selection_from_tool_args(target_uin=123456, selector="第2条")

        self.assertEqual((legacy.target_uin, legacy.fid, legacy.selector), (123456, "fid-1", "fid"))
        self.assertEqual((semantic.target_uin, semantic.start, semantic.end), (123456, 2, 2))


if __name__ == "__main__":
    unittest.main()
