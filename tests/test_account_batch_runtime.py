import queue
import unittest
from unittest.mock import patch

import app


class AccountBatchRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.old_accounts = app.config.get("accounts", [])
        self.old_jobs = list(app.jobs)
        self.old_timing = dict(app.batch_timing)
        app.batch_active_accounts.clear()
        app.batch_available_accounts.clear()
        app.accounts_needing_reload.clear()
        app.stop_event.clear()

    def tearDown(self):
        app.config["accounts"] = self.old_accounts
        app.jobs[:] = self.old_jobs
        app.batch_timing.update(self.old_timing)
        app.batch_active_accounts.clear()
        app.batch_available_accounts.clear()
        app.accounts_needing_reload.clear()
        app.stop_event.clear()

    def test_success_and_failure_counters_are_independent(self):
        account = {"id": 1, "success_count": 8, "failure_count": 3, "batch_completed": 0, "batch_failed": 0}

        app.record_batch_success(account)
        app.record_batch_success(account)
        app.record_batch_failure(account)

        self.assertEqual(account["batch_completed"], 2)
        self.assertEqual(account["batch_failed"], 1)
        self.assertEqual(account["success_count"], 10)
        self.assertEqual(account["failure_count"], 3)

    def test_failed_active_profile_closes_before_reserve_is_started(self):
        first = {"id": 1, "name": "first", "runtime_state": "ACTIVE", "disabled": False, "status": "READY"}
        reserve = {"id": 2, "name": "reserve", "runtime_state": "RESERVE", "disabled": False, "status": "READY"}
        pool = queue.Queue()
        pool.put(first)
        pool.put(reserve)
        app.batch_available_accounts.update({1, 2})
        events = []

        def run(account):
            events.append(("run", account["id"]))
            return account["id"] == 2

        def close(account):
            events.append(("close", account["id"]))
            return True

        with (
            patch.object(app, "run_account_worker", side_effect=run),
            patch.object(app, "close_account_runtime", side_effect=close),
            patch.object(app, "add_log"),
            patch.object(app.stop_event, "wait", return_value=False),
        ):
            app.run_worker_slot(1, pool)

        self.assertEqual(events[:4], [("run", 1), ("close", 1), ("run", 2), ("close", 2)])
        self.assertEqual(first["runtime_state"], "RESERVE")
        self.assertEqual(reserve["runtime_state"], "STANDBY")

    def test_only_initial_active_profiles_are_left_open(self):
        app.config["accounts"] = [
            {"id": 1, "name": "active"},
            {"id": 2, "name": "reserve"},
            {"id": 3, "name": "verify"},
        ]
        closed = []
        with (
            patch.object(app, "close_account_runtime", side_effect=lambda account: closed.append(account["id"]) or True),
            patch.object(app, "save_config"),
        ):
            app.close_inactive_batch_browsers({1})

        self.assertEqual(set(closed), {2, 3})

    def test_start_resets_all_visible_account_counters_before_response(self):
        accounts = [
            {"id": 1, "name": "one", "batch_completed": 9, "batch_failed": 1},
            {"id": 2, "name": "two", "batch_completed": 4, "batch_failed": 3},
        ]
        app.config["accounts"] = accounts
        app.jobs[:] = [{"id": 1, "status": "WAITING"}]

        with (
            patch.object(app, "eligible_accounts", return_value=accounts),
            patch.object(app.threading, "enumerate", return_value=[]),
            patch.object(app.threading, "Thread") as thread,
        ):
            response = app.app.test_client().post("/api/jobs/start", json={})

        self.assertEqual(response.status_code, 200)
        self.assertEqual([(a["batch_completed"], a["batch_failed"]) for a in accounts], [(0, 0), (0, 0)])
        thread.return_value.start.assert_called_once()

    def test_open_action_restores_the_selected_browser_window(self):
        account = {
            "id": 7, "name": "account@example.com", "profile_dir": "profile", "port": 9007,
            "status": "READY",
        }
        with (
            patch.object(app, "find_account", return_value=account),
            patch.object(app, "open_browser") as open_browser,
            patch.object(app, "show_browser_window", return_value=True) as show_window,
            patch.object(app, "save_config"),
        ):
            response = app.app.test_client().post("/api/accounts/7/open", json={})

        self.assertEqual(response.status_code, 200)
        open_browser.assert_called_once_with(account, visible=True)
        show_window.assert_called_once_with(account)

    def test_transient_project_preparation_retries_same_account_before_rotation(self):
        account = {
            "id": 7, "name": "account@example.com", "status": "READY",
            "runtime_state": "STANDBY", "failure_count": 4, "disabled": False,
        }
        app.jobs[:] = []
        with (
            patch.object(app, "open_clean_project", side_effect=[RuntimeError("temporary"), RuntimeError("temporary"), None]) as prepare,
            patch.object(app, "add_log"),
            patch.object(app.stop_event, "wait", return_value=False),
        ):
            completed = app.run_account_worker(account)

        self.assertTrue(completed)
        self.assertEqual(prepare.call_count, 3)
        self.assertEqual(account["failure_count"], 4)

    def test_project_preparation_failure_is_not_counted_as_image_failure(self):
        account = {
            "id": 8, "name": "account@example.com", "status": "READY",
            "runtime_state": "STANDBY", "failure_count": 6, "disabled": False,
        }
        with (
            patch.object(app, "open_clean_project", side_effect=RuntimeError("setting not ready")) as prepare,
            patch.object(app, "capture_flow_diagnostic"),
            patch.object(app, "save_config"),
            patch.object(app, "add_log"),
            patch.object(app.stop_event, "wait", return_value=False),
        ):
            completed = app.run_account_worker(account)

        self.assertFalse(completed)
        self.assertEqual(prepare.call_count, 3)
        self.assertEqual(account["failure_count"], 6)


if __name__ == "__main__":
    unittest.main()
