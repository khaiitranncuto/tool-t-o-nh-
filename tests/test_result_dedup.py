import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app


def valid_png(seed: bytes = b"x") -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\rIHDR"
        + (1024).to_bytes(4, "big")
        + (1024).to_bytes(4, "big")
        + seed * 10_000
    )


class ResultDedupTests(unittest.TestCase):
    def setUp(self):
        self.old_project_state = dict(app.project_state)
        app.reset_result_dedup_states()

    def tearDown(self):
        app.project_state.clear()
        app.project_state.update(self.old_project_state)
        app.reset_result_dedup_states()

    def test_existing_output_hash_is_rejected_as_prompt_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image_dir = root / app.PROJECT_IMAGE_DIR
            image_dir.mkdir()
            image_data = valid_png()
            (image_dir / "1.png").write_bytes(image_data)
            app.project_state.update(loaded=True, root=str(root))

            with patch.object(app, "download_result_image", return_value=image_data):
                with self.assertRaises(app.FlowTaskError) as raised:
                    app.persist_result_image(
                        {"id": 7},
                        {"key": "new-url", "url": "new-url", "tag": "IMG"},
                        2,
                    )

            self.assertEqual(raised.exception.kind, "PROMPT")
            self.assertTrue(raised.exception.submitted)
            self.assertIn("duplicate_flow_image", str(raised.exception))
            self.assertFalse((image_dir / "2.png").exists())

    def test_new_hash_is_saved_then_rejected_if_flow_returns_it_again(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app.project_state.update(loaded=True, root=str(root))
            image_data = valid_png(b"z")

            with patch.object(app, "download_result_image", return_value=image_data):
                first = app.persist_result_image(
                    {"id": 1}, {"key": "url-1", "url": "url-1", "tag": "IMG"}, 1
                )
                with self.assertRaises(app.FlowTaskError) as raised:
                    app.persist_result_image(
                        {"id": 2}, {"key": "url-2", "url": "url-2", "tag": "IMG"}, 2
                    )

            self.assertTrue(Path(first).is_file())
            self.assertEqual(raised.exception.kind, "PROMPT")
            self.assertFalse((root / app.PROJECT_IMAGE_DIR / "2.png").exists())

    def test_multiple_outputs_are_saved_as_one_atomic_numbered_set(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app.project_state.update(loaded=True, root=str(root))
            images = [valid_png(b"a"), valid_png(b"b")]
            candidates = [
                {"key": "url-1", "url": "url-1", "tag": "IMG"},
                {"key": "url-2", "url": "url-2", "tag": "IMG"},
            ]

            with patch.object(app, "download_result_image", side_effect=images):
                paths = app.persist_result_images({"id": 1}, candidates, 9, 2)

            self.assertEqual([Path(value).name for value in paths], ["9_01.png", "9_02.png"])
            self.assertTrue(all(Path(value).is_file() for value in paths))


if __name__ == "__main__":
    unittest.main()
