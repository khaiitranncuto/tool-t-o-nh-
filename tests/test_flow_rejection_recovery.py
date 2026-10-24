import unittest
from unittest.mock import patch

import app


class FakeClickController:
    def __init__(self):
        self.calls = []

    def dispatch_mouse_event(self, event_type, x, y, button="left"):
        self.calls.append((event_type, x, y, button))

    def eval(self, _expression):
        return {"ok": True, "x": 120, "y": 240}


class FlowRejectionRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.old_scheduler = dict(app.config.get("scheduler", {}))
        app.config.setdefault("scheduler", {})["account_limit_cooldown_seconds"] = 3600
        app.batch_available_accounts.clear()

    def tearDown(self):
        app.config["scheduler"] = self.old_scheduler
        app.batch_available_accounts.clear()

    def test_generate_click_never_activates_or_restores_native_window(self):
        controller = FakeClickController()
        account = {"id": 1, "pid": 123}
        with (
            patch.object(app, "set_browser_window_cloaked", return_value=True),
            patch.object(app, "set_browser_window_hidden"),
            patch.object(app, "minimize_browser_window"),
            patch.object(app.time, "sleep"),
        ):
            app.click_generate_background(account, {"id": "target"}, controller)

        self.assertEqual([item[0] for item in controller.calls], ["move", "down", "up"])

    def test_unusual_activity_is_a_soft_error_and_rotates_immediately(self):
        failure = app.classify_flow_error(RuntimeError("hoạt động bất thường"), submitted=True)
        self.assertEqual(failure.kind, "AMBIGUOUS")

    def test_quota_places_account_on_long_cooldown_and_removes_it_from_batch(self):
        account = {
            "id": 4,
            "name": "test@example.com",
            "status": "READY",
            "runtime_state": "ACTIVE",
            "failure_count": 0,
        }
        app.batch_available_accounts.add(4)
        failure = app.classify_flow_error(RuntimeError("weekly quota usage limit"), submitted=True)

        with patch.object(app.time, "time", return_value=1000), patch.object(app, "save_config"):
            app.mark_account_problem(account, failure)

        self.assertEqual(account["status"], "READY")
        self.assertEqual(account["runtime_state"], "COOLDOWN")
        self.assertEqual(account["cooldown_until"], 4600)
        self.assertNotIn(4, app.batch_available_accounts)

    def test_account_in_cooldown_is_not_eligible_for_new_batch(self):
        old_accounts = app.config.get("accounts", [])
        account = {"id": 9, "status": "READY", "disabled": False, "cooldown_until": 1060}
        app.config["accounts"] = [account]
        try:
            with patch.object(app.time, "time", return_value=1000):
                self.assertEqual(app.eligible_accounts(), [])
            self.assertEqual(account["runtime_state"], "COOLDOWN")
        finally:
            app.config["accounts"] = old_accounts

    def test_flow_rejection_probe_includes_non_aria_cards(self):
        self.assertIn("document.createTreeWalker", app.GENERATION_STATE_JS)
        self.assertNotIn("body div,body section,body article", app.GENERATION_STATE_JS)
        self.assertIn("tiện ích trình duyệt sửa đổi trang", app.GENERATION_STATE_JS)
        self.assertIn("limitIssue?'COOLDOWN'", app.GENERATION_STATE_JS)
        self.assertIn("retryIssue?'AMBIGUOUS'", app.GENERATION_STATE_JS)

    def test_generation_without_activity_returns_prompt_before_full_timeout(self):
        app.config["scheduler"]["generation_stall_seconds"] = 60

        class AdvancingClock:
            def __init__(self):
                self.value = 0.0

            def __call__(self):
                self.value += 10.0
                return self.value

        clock = AdvancingClock()
        idle_state = {
            "resultCount": 0,
            "resultKeys": [],
            "errorKeys": [],
            "busy": False,
            "progressPercent": None,
            "accountIssue": False,
        }
        with (
            patch.object(app, "generation_state", return_value=idle_state),
            patch.object(app.time, "time", side_effect=clock),
            patch.object(app.time, "sleep"),
        ):
            with self.assertRaises(app.FlowTaskError) as raised:
                app.wait_for_generation({"id": 1}, idle_state, timeout=300)

        self.assertIn("không có tiến triển", str(raised.exception))
        self.assertLess(clock.value, 300)

    def test_playwright_timeout_can_be_shortened_for_shared_dispatch(self):
        controller = app.PlaywrightFlowController.__new__(app.PlaywrightFlowController)
        page = unittest.mock.Mock()
        controller.page = page

        controller.set_timeout(10)

        page.set_default_timeout.assert_called_once_with(10_000)


if __name__ == "__main__":
    unittest.main()
