import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app


class AccountProxyTests(unittest.TestCase):
    def test_normalize_proxy_accepts_vendor_formats(self):
        self.assertEqual(app.normalize_proxy("10.0.0.8:8080"), "http://10.0.0.8:8080")
        self.assertEqual(
            app.normalize_proxy("10.0.0.8:8080:user_name:p@ss"),
            "http://user_name:p%40ss@10.0.0.8:8080",
        )
        self.assertEqual(
            app.normalize_proxy("socks5://proxy.example:1080"),
            "socks5://proxy.example:1080",
        )

    def test_normalize_proxy_rejects_invalid_values(self):
        for value in ("proxy-without-port", "ftp://proxy.example:21", "http://proxy.example:99999"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                app.normalize_proxy(value)

    def test_authenticated_proxy_uses_safe_launch_argument_and_local_extension(self):
        with tempfile.TemporaryDirectory() as temporary:
            account = {
                "id": 12,
                "port": 9229,
                "profile_dir": str(Path(temporary) / "browser_profile"),
                "proxy": "http://alice:s3cret@proxy.example:8080",
            }
            arguments = app.browser_launch_args(account, "chrome.exe")

            self.assertIn("--proxy-server=http://proxy.example:8080", arguments)
            self.assertFalse(any("s3cret" in argument for argument in arguments))
            extension_argument = next(arg for arg in arguments if arg.startswith("--load-extension="))
            extension_dir = Path(extension_argument.split("=", 1)[1])
            manifest = json.loads((extension_dir / "manifest.json").read_text(encoding="utf-8"))
            background = (extension_dir / "background.js").read_text(encoding="utf-8")
            self.assertEqual(manifest["manifest_version"], 3)
            self.assertIn("webRequestAuthProvider", manifest["permissions"])
            self.assertIn("alice", background)
            self.assertIn("s3cret", background)

    def test_proxy_display_never_exposes_credentials(self):
        display = app.proxy_display("https://alice:s3cret@proxy.example:8443")
        self.assertEqual(display, "https://proxy.example:8443")
        self.assertNotIn("alice", display)
        self.assertNotIn("s3cret", display)

    def test_flow_url_supports_current_and_legacy_routes(self):
        self.assertTrue(app.is_flow_url("https://labs.google/fx/vi/tools/flow/project/abc"))
        self.assertTrue(app.is_flow_url("https://flow.google.com/project/abc"))
        self.assertFalse(app.is_flow_url("https://example.com/tools/flow"))

    def test_selected_flow_preferences_are_applied_in_order(self):
        class FakeController:
            def __init__(self):
                self.calls = []

            def dispatch_key_event(self, event_type, key):
                self.calls.append((event_type, key))

        original = dict(app.config.get("scheduler", {}))
        app.config["scheduler"].update(flow_model="nano-banana-pro", aspect_ratio="16:9", images_per_prompt=1)
        controller = FakeController()
        try:
            with (
                patch.object(app, "flow_preference_active", return_value=False),
                patch.object(app, "click_flow_setting", return_value=True) as click,
            ):
                app.configure_flow_preferences(controller)
            self.assertEqual(
                click.call_args_list,
                [
                    unittest.mock.call(controller, "control"),
                    unittest.mock.call(controller, "image"),
                    unittest.mock.call(controller, "model-dropdown"),
                    unittest.mock.call(controller, "model", "Nano Banana Pro"),
                    unittest.mock.call(controller, "control"),
                    unittest.mock.call(controller, "aspect", "16:9"),
                    unittest.mock.call(controller, "control"),
                    unittest.mock.call(controller, "outputs", "1"),
                ],
            )
            self.assertEqual(controller.calls, [("down", "Escape"), ("up", "Escape")])
        finally:
            app.config["scheduler"].clear()
            app.config["scheduler"].update(original)

    def test_flow_preferences_do_not_fail_when_requested_options_are_already_active(self):
        class FakeController:
            def dispatch_key_event(self, *_args):
                raise AssertionError("Không được đóng/mở lại menu khi mọi option đã đúng")

        original = dict(app.config.get("scheduler", {}))
        app.config["scheduler"].update(flow_model="nano-banana-2-lite", aspect_ratio="auto", images_per_prompt=1)
        try:
            with (
                patch.object(app, "flow_preference_active", return_value=True),
                patch.object(app, "click_flow_setting") as click,
            ):
                app.configure_flow_preferences(FakeController())
            click.assert_not_called()
        finally:
            app.config["scheduler"].clear()
            app.config["scheduler"].update(original)

    def test_settings_endpoint_keeps_user_thread_count_and_saves_flow_choices(self):
        original = dict(app.config.get("scheduler", {}))
        try:
            with patch.object(app, "save_config"):
                response = app.app.test_client().post(
                    "/api/settings",
                    json={
                        "active_threads": 99,
                        "submit_gap": "5-8",
                        "flow_model": "nano-banana-2-lite",
                        "aspect_ratio": "9:16",
                        "images_per_prompt": 4,
                        "browser_run_mode": "minimized_taskbar",
                    },
                )
            self.assertEqual(response.status_code, 200)
            scheduler = response.get_json()["scheduler"]
            self.assertEqual(scheduler["active_threads"], 99)
            self.assertEqual(scheduler["submit_gap"], "5-8")
            self.assertEqual(scheduler["flow_model"], "nano-banana-2-lite")
            self.assertEqual(scheduler["aspect_ratio"], "9:16")
            self.assertEqual(scheduler["images_per_prompt"], 4)
            self.assertEqual(scheduler["browser_run_mode"], "minimized_taskbar")
        finally:
            app.config["scheduler"].clear()
            app.config["scheduler"].update(original)

    def test_browser_run_mode_maps_to_windows_desktop_behavior(self):
        original = dict(app.config.get("scheduler", {}))
        try:
            app.config["scheduler"]["browser_run_mode"] = "hidden_desktop"
            self.assertEqual(app.desired_browser_desktop_mode(), "HIDDEN_DESKTOP")
            app.config["scheduler"]["browser_run_mode"] = "minimized_taskbar"
            self.assertEqual(app.desired_browser_desktop_mode(), "MINIMIZED_TASKBAR")
            self.assertEqual(app.desired_browser_desktop_mode(visible=True), "VISIBLE")
        finally:
            app.config["scheduler"].clear()
            app.config["scheduler"].update(original)

    def test_settings_endpoint_rejects_unknown_browser_run_mode(self):
        response = app.app.test_client().post(
            "/api/settings",
            json={"browser_run_mode": "unknown", "submit_gap": "5"},
        )
        self.assertEqual(response.status_code, 400)

    def test_settings_endpoint_rejects_non_positive_thread_count(self):
        response = app.app.test_client().post(
            "/api/settings",
            json={"active_threads": 0, "submit_gap": "5"},
        )
        self.assertEqual(response.status_code, 400)

    def test_account_api_creates_and_updates_complete_flow_identity(self):
        original_accounts = app.config.get("accounts", [])
        app.config["accounts"] = []
        browsers = [{"key": "chrome", "name": "Chrome", "path": "C:/Chrome/chrome.exe", "installed": True}]
        try:
            with (
                patch.object(app, "available_browsers", return_value=browsers),
                patch.object(app, "save_config"),
                patch.object(app, "stop_unresponsive_managed_browser"),
            ):
                client = app.app.test_client()
                created = client.post(
                    "/api/accounts",
                    json={
                        "label": "Account học tập",
                        "email": "student@example.com",
                        "browser_key": "chrome",
                        "proxy": "proxy.example:8080:user:pass",
                    },
                )
                self.assertEqual(created.status_code, 201)
                account = created.get_json()["account"]
                self.assertEqual(account["label"], "Account học tập")
                self.assertEqual(account["email"], "student@example.com")
                self.assertEqual(account["proxy_display"], "http://proxy.example:8080")

                updated = client.patch(
                    f"/api/accounts/{account['id']}",
                    json={**account, "label": "Account ưu tiên", "proxy": "socks5://proxy.example:1080"},
                )
                self.assertEqual(updated.status_code, 200)
                changed = updated.get_json()["account"]
                self.assertEqual(changed["name"], "Account ưu tiên")
                self.assertEqual(changed["proxy"], "socks5://proxy.example:1080")
        finally:
            app.config["accounts"] = original_accounts

    def test_playwright_is_the_only_engine_and_failure_is_explicit(self):
        primary = object()
        account = {"id": 51, "name": "primary", "port": 9511}
        with patch.object(app, "PlaywrightFlowController", return_value=primary) as playwright_controller:
            actual = app.open_flow_controller(account)
        self.assertIs(actual, primary)
        playwright_controller.assert_called_once_with(account)

        account = {"id": 52, "name": "broken", "port": 9512}
        with patch.object(app, "PlaywrightFlowController", side_effect=RuntimeError("driver unavailable")):
            with self.assertRaisesRegex(RuntimeError, "Playwright"):
                app.open_flow_controller(account, {"id": "page"})
        self.assertEqual(account["control_engine"], "PLAYWRIGHT_ERROR")

    def test_runtime_profile_clone_preserves_login_data_and_skips_locks(self):
        with tempfile.TemporaryDirectory() as temporary:
            profile = Path(temporary) / "account_007" / "browser_profile"
            default = profile / "Default"
            default.mkdir(parents=True)
            (default / "Preferences").write_text('{"account_info":[]}', encoding="utf-8")
            (default / "Cache").mkdir()
            (default / "Cache" / "cache.bin").write_bytes(b"cache")
            (profile / "SingletonLock").write_text("locked", encoding="utf-8")
            account = {
                "id": 7,
                "profile_dir": str(profile),
                "port": 9227,
                "helper_profile_dir": "",
                "helper_port": 0,
                "helper_pid": 0,
            }

            clone = Path(app.prepare_runtime_profile_clone(account))
            self.assertTrue((clone / "Default" / "Preferences").is_file())
            self.assertFalse((clone / "Default" / "Cache").exists())
            self.assertFalse((clone / "SingletonLock").exists())
            self.assertEqual(app.account_runtime_profile(account), str(clone))
            self.assertNotEqual(app.account_runtime_port(account), account["port"])

            self.assertTrue(app.cleanup_runtime_profile_clone(account))
            self.assertFalse(clone.exists())
            self.assertEqual(account["helper_profile_dir"], "")
            self.assertEqual(app.account_runtime_port(account), account["port"])


if __name__ == "__main__":
    unittest.main()
