import base64
import unittest
from unittest.mock import patch

import app


class _FakeController:
    instances = []
    eval_result = None

    def __init__(self, *_args, **_kwargs):
        self.calls = []
        self.closed = False
        self.__class__.instances.append(self)

    def eval(self, expression):
        self.calls.append(("Runtime.evaluate", expression))
        return self.__class__.eval_result

    def close(self):
        self.closed = True


class ResultImageDownloadTests(unittest.TestCase):
    def setUp(self):
        _FakeController.instances = []

    def test_blob_result_is_read_as_original_bytes_inside_page(self):
        expected = b"\x89PNG\r\n\x1a\n" + (b"x" * 10_000)
        encoded = base64.b64encode(expected).decode("ascii")
        _FakeController.eval_result = {"ok": True, "data": f"data:image/png;base64,{encoded}"}

        with patch.object(app, "flow_target", return_value={"id": "flow"}), \
             patch.object(app, "open_flow_controller", side_effect=_FakeController):
            actual = app.download_result_image(
                {"id": 1}, {"url": "blob:https://flow.google.com/result", "tag": "IMG", "tagIndex": 4}
            )

        self.assertEqual(expected, actual)
        self.assertTrue(_FakeController.instances[0].closed)

    def test_failure_never_falls_back_to_page_screenshot(self):
        _FakeController.eval_result = {"ok": False, "detail": "blob expired"}

        with patch.object(app, "flow_target", return_value={"id": "flow"}), \
             patch.object(app, "open_flow_controller", side_effect=_FakeController), patch.object(app.time, "sleep"):
            with self.assertRaises(app.FlowTaskError):
                app.download_result_image(
                    {"id": 1}, {"url": "blob:https://flow.google.com/expired", "tag": "IMG", "tagIndex": 4}
                )

        self.assertTrue(all(method == "Runtime.evaluate" for instance in _FakeController.instances for method, _ in instance.calls))

    def test_low_resolution_flow_ui_crop_is_not_a_valid_result(self):
        # This matches the old viewport captures (for example 370x208) that
        # contained Flow controls and a large black area.
        cropped = (
            b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" +
            (370).to_bytes(4, "big") + (208).to_bytes(4, "big") + b"x" * 10_000
        )
        self.assertTrue(app.valid_image_bytes(cropped))
        self.assertFalse(app.valid_result_image_bytes(cropped))

    def test_signed_flow_content_host_is_allowed_for_original_download(self):
        self.assertTrue(app.trusted_flow_image_url("https://flow-content.google/image/result?Signature=x"))
        self.assertFalse(app.trusted_flow_image_url("https://flow-content.google.evil.example/image/result"))


if __name__ == "__main__":
    unittest.main()
