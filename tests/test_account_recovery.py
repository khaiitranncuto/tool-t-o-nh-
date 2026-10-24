import unittest
from unittest.mock import patch

import app


class FakeRecoveryController:
    def __init__(self):
        self.calls = []

    def reload(self):
        self.calls.append("reload")

    def eval(self, _expression):
        return True

    def close(self):
        pass


class AccountRecoveryTests(unittest.TestCase):
    def setUp(self):
        app.accounts_needing_reload.clear()
        app.stop_event.clear()

    def tearDown(self):
        app.accounts_needing_reload.clear()

    def test_failed_account_reloads_before_opening_new_project(self):
        account = {"id": 7, "name": "test@example.com", "status": "READY"}
        controller = FakeRecoveryController()
        app.accounts_needing_reload.add(7)
        with (
            patch.object(app, "open_browser"),
            patch.object(app, "flow_target", return_value={"id": "target"}),
            patch.object(app, "open_flow_controller", return_value=controller),
            patch.object(app, "ensure_project_composer") as ensure,
            patch.object(app, "configure_flow_preferences") as configure,
            patch.object(app, "add_log"),
        ):
            app.open_clean_project(account)
        self.assertEqual(controller.calls[0], "reload")
        ensure.assert_called_once_with(controller, force_new=True)
        configure.assert_called_once_with(controller)
        self.assertNotIn(7, app.accounts_needing_reload)

    def test_healthy_account_opens_project_without_reload(self):
        account = {"id": 8, "name": "healthy@example.com", "status": "READY"}
        controller = FakeRecoveryController()
        with (
            patch.object(app, "open_browser"),
            patch.object(app, "flow_target", return_value={"id": "target"}),
            patch.object(app, "open_flow_controller", return_value=controller),
            patch.object(app, "ensure_project_composer") as ensure,
            patch.object(app, "configure_flow_preferences") as configure,
        ):
            app.open_clean_project(account)
        self.assertNotIn("reload", controller.calls)
        ensure.assert_called_once_with(controller, force_new=True)
        configure.assert_called_once_with(controller)


if __name__ == "__main__":
    unittest.main()
