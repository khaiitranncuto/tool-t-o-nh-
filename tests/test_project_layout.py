import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app


class ProjectLayoutTests(unittest.TestCase):
    def setUp(self):
        self.old_project_state = dict(app.project_state)
        self.old_last_manifest = app.config.get("last_project_manifest")

    def tearDown(self):
        app.project_state.clear()
        app.project_state.update(self.old_project_state)
        if self.old_last_manifest is None:
            app.config.pop("last_project_manifest", None)
        else:
            app.config["last_project_manifest"] = self.old_last_manifest

    @staticmethod
    def manifest_data():
        return {
            "version": 1,
            "project_name": "Demo",
            "segments": [{"id": 1, "content": "Một prompt", "prompt": ""}],
        }

    def test_new_project_has_two_folders_and_root_manifest(self):
        with tempfile.TemporaryDirectory() as value, patch.object(app, "save_config"):
            root = Path(value) / "Demo"
            manifest = root / "prompts.json"
            app.write_project_manifest(self.manifest_data(), manifest)
            data = app.activate_project(manifest)

            self.assertTrue((root / "01_AUDIO").is_dir())
            self.assertTrue((root / "02_IMAGE").is_dir())
            self.assertTrue(manifest.is_file())
            self.assertFalse((root / "02_PROMPT").exists())
            self.assertFalse((root / "03_IMAGE").exists())
            self.assertEqual(data["segments"][0]["image"], "02_IMAGE/1.png")

    def test_legacy_project_is_loaded_into_canonical_layout(self):
        with tempfile.TemporaryDirectory() as value, patch.object(app, "save_config"):
            root = Path(value) / "Legacy"
            legacy_manifest = root / "02_PROMPT" / "prompts.json"
            legacy_image = root / "03_IMAGE" / "1.png"
            legacy_image.parent.mkdir(parents=True)
            legacy_image.write_bytes(b"legacy-image")
            data = self.manifest_data()
            data["segments"][0]["image"] = "03_IMAGE/1.png"
            app.write_project_manifest(data, legacy_manifest)

            migrated = app.activate_project(legacy_manifest)

            canonical_manifest = root / "prompts.json"
            self.assertEqual(app.project_state["manifest"], str(canonical_manifest))
            self.assertTrue(canonical_manifest.is_file())
            self.assertEqual((root / "02_IMAGE" / "1.png").read_bytes(), b"legacy-image")
            self.assertFalse((root / "02_PROMPT").exists())
            self.assertFalse((root / "03_IMAGE").exists())
            saved = json.loads(canonical_manifest.read_text(encoding="utf-8"))
            self.assertEqual(saved["segments"][0]["image"], "02_IMAGE/1.png")
            self.assertEqual(migrated["segments"][0]["image"], "02_IMAGE/1.png")


if __name__ == "__main__":
    unittest.main()
