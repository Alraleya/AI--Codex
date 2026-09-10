import os
import tempfile
import unittest
from pathlib import Path

from backend.core.assets import register_asset, resolve_asset_path, validate_image
from backend.core.status import make_creative_brief, new_status


class AssetTests(unittest.TestCase):
    def _status(self):
        return new_status(
            "paperplane",
            "ep01",
            make_creative_brief("雨夜的纸飞机", "逆风返回"),
        )

    def test_path_traversal_and_absolute_paths_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for unsafe in ("../secret.png", "/tmp/secret.png", "a\\b.png"):
                with self.assertRaises(ValueError):
                    resolve_asset_path(root, unsafe)

    def test_symlink_escape_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            root = Path(directory)
            os.symlink(outside, root / "outside")
            with self.assertRaises(ValueError):
                resolve_asset_path(root, "outside/asset.png")

    def test_fake_image_extension_does_not_pass_magic_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fake.png"
            path.write_bytes(b"not an image" * 100)
            with self.assertRaisesRegex(ValueError, "supported PNG"):
                validate_image(path)

    def test_real_png_bytes_can_be_registered_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "char_girl_sheet.png"
            path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 2048)
            status = self._status()
            record = register_asset(
                status,
                root,
                "char.girl.image",
                "character_design",
                "image",
                "character_sheet",
                "女孩",
                path.name,
            )
            self.assertEqual(record["mime"], "image/png")
            self.assertGreater(record["size"], 1024)
            with self.assertRaisesRegex(ValueError, "already registered"):
                register_asset(
                    status,
                    root,
                    "char.girl.image",
                    "character_design",
                    "image",
                    "character_sheet",
                    "女孩",
                    path.name,
                )
            with self.assertRaisesRegex(ValueError, "path already registered"):
                register_asset(
                    status,
                    root,
                    "char.girl.image.second",
                    "character_design",
                    "image",
                    "character_sheet",
                    "女孩",
                    path.name,
                )


if __name__ == "__main__":
    unittest.main()
