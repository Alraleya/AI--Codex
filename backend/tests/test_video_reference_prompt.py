import unittest

from backend.workflow.runner import _video_reference_subject_prefix


class VideoReferencePromptTests(unittest.TestCase):
    def test_current_character_ids_resolve_to_model_readable_names(self):
        names = {
            "char_tangxuan": "唐玄",
            "char_sunwukong": "孙悟空",
            "char_zhubajie": "猪八戒",
            "char_shawujing": "沙悟净",
        }
        expected = {
            "char_tangxuan": "角色：唐玄；",
            "char_sunwukong": "角色：孙悟空；",
            "char_zhubajie": "角色：猪八戒；",
            "char_shawujing": "角色：沙悟净；",
        }
        for asset_id, prefix in expected.items():
            with self.subTest(asset_id=asset_id):
                self.assertEqual(
                    _video_reference_subject_prefix(
                        {"role": "character", "asset_id": asset_id}, names
                    ),
                    prefix,
                )

    def test_character_reference_without_semantic_name_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "no semantic subject name"):
            _video_reference_subject_prefix(
                {"role": "character", "asset_id": "char_unknown"}, {}
            )

    def test_scene_and_prop_references_also_get_semantic_names(self):
        names = {"scene_city": "悬空女儿国", "prop_bell": "婚钟"}
        self.assertEqual(
            _video_reference_subject_prefix(
                {"role": "scene", "asset_id": "scene_city"}, names
            ),
            "场景：悬空女儿国；",
        )
        self.assertEqual(
            _video_reference_subject_prefix(
                {"role": "prop", "asset_id": "prop_bell"}, names
            ),
            "道具：婚钟；",
        )


if __name__ == "__main__":
    unittest.main()
