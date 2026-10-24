import unittest
from unittest.mock import patch

import app


class DispatchGapTests(unittest.TestCase):
    def setUp(self):
        self.old_scheduler = dict(app.config.get("scheduler", {}))
        app.config.setdefault("scheduler", {})["submit_gap"] = "5.5"
        app.last_global_dispatch_at = 0.0
        app.stop_event.clear()

    def tearDown(self):
        app.config["scheduler"] = self.old_scheduler
        app.last_global_dispatch_at = 0.0

    def test_global_gap_waits_from_last_real_generate(self):
        app.last_global_dispatch_at = 99.0
        with (
            patch.object(app.time, "monotonic", return_value=100.0),
            patch.object(app.stop_event, "wait", return_value=False) as wait,
        ):
            app.wait_for_global_dispatch_slot()
        wait.assert_called_once_with(4.5)

    def test_first_generate_in_batch_does_not_wait(self):
        with patch.object(app.stop_event, "wait", return_value=False) as wait:
            app.wait_for_global_dispatch_slot()
        wait.assert_not_called()

    def test_flow_gap_rejects_values_below_five_seconds(self):
        for value in ("0", "4.9", "1-4"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                app.parse_submit_gap(value)

    def test_competitor_compatible_default_gap_and_thread_count(self):
        self.assertEqual(app.parse_submit_gap(None), (5.0, 5.0, "5"))
        self.assertEqual(app.DEFAULT_ACTIVE_THREADS, 5)
        self.assertEqual(app.BROWSER_LAUNCH_GAP_SECONDS, 5.0)

    def test_thread_count_has_no_fixed_ceiling_or_sixty_forty_split(self):
        self.assertEqual(app.active_account_count(150, 99), 99)
        self.assertEqual(app.active_account_count(150, 999), 150)
        self.assertEqual(app.active_account_count(7, 7), 7)


if __name__ == "__main__":
    unittest.main()
