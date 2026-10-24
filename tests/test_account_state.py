import unittest
from unittest.mock import patch

import app


class SequenceController:
    def __init__(self, states):
        self.states = list(states)
        self.index = 0

    def eval(self, _script):
        state = self.states[min(self.index, len(self.states) - 1)]
        self.index += 1
        return dict(state)

    def close(self):
        pass


class AccountStateTests(unittest.TestCase):
    def test_delayed_session_restore_wins_over_early_login_shell(self):
        controller = SequenceController([
            {"loginRequired": True, "authenticated": False, "verificationRequired": False},
            {"loginRequired": False, "authenticated": False, "verificationRequired": False},
            {
                "loginRequired": False,
                "authenticated": True,
                "verificationRequired": False,
                "email": "restored@example.com",
            },
        ])
        state = app.poll_flow_account_state(controller, timeout=1, interval=0)
        self.assertTrue(state["authenticated"])
        self.assertEqual(state["email"], "restored@example.com")

    def test_visible_verification_remains_decisive(self):
        state = app.poll_flow_account_state(
            SequenceController([{"verificationRequired": True, "authenticated": False}]),
            timeout=1,
            interval=0,
        )
        self.assertTrue(state["verificationRequired"])

    def test_unknown_page_is_not_falsely_marked_login_or_ready(self):
        account = {"id": 99, "name": "Account 99", "status": "LOGIN"}
        unknown = {
            "url": "https://flow.google.com/",
            "loginRequired": False,
            "verificationRequired": False,
            "authenticated": False,
            "email": "",
        }
        with (
            patch.object(app, "read_profile_email", return_value=""),
            patch.object(app, "flow_target", return_value={"url": unknown["url"]}),
            patch.object(app, "open_flow_controller", return_value=SequenceController([unknown])),
            patch.object(app, "poll_flow_account_state", return_value=unknown),
        ):
            app.refresh_account(account)
        self.assertEqual(account["status"], "OFFLINE")

    def test_hydrated_authenticated_page_is_ready_and_updates_email(self):
        account = {"id": 100, "name": "Account 100", "status": "LOGIN"}
        ready = {
            "url": "https://flow.google.com/",
            "loginRequired": False,
            "verificationRequired": False,
            "authenticated": True,
            "email": "linked@example.com",
        }

        def set_email(item, email):
            item["name"] = email

        with (
            patch.object(app, "read_profile_email", return_value=""),
            patch.object(app, "flow_target", return_value={"url": ready["url"]}),
            patch.object(app, "open_flow_controller", return_value=SequenceController([ready])),
            patch.object(app, "poll_flow_account_state", return_value=ready),
            patch.object(app, "set_account_email", side_effect=set_email),
        ):
            app.refresh_account(account)
        self.assertEqual(account["status"], "READY")
        self.assertEqual(account["name"], "linked@example.com")


if __name__ == "__main__":
    unittest.main()
