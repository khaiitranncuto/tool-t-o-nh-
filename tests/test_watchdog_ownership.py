import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import browser_watchdog


class WatchdogOwnershipTests(unittest.TestCase):
    def make_files(self, directory: Path, token: str):
        config = directory / "accounts.json"
        owner = directory / "watchdog-owner.json"
        config.write_text(
            json.dumps({"accounts": [{"id": 1, "port": 50001}, {"id": 2, "port": 50002}]}),
            encoding="utf-8",
        )
        owner.write_text(json.dumps({"pid": 10, "token": token}), encoding="utf-8")
        return config, owner

    def test_old_instance_cannot_close_new_instance_browsers(self):
        with tempfile.TemporaryDirectory() as value:
            config, owner = self.make_files(Path(value), "new-token")
            with patch.object(browser_watchdog, "close_exact_profile_gracefully") as close:
                browser_watchdog.cleanup(config, owner, "old-token")
            close.assert_not_called()

    def test_cleanup_stops_if_new_server_takes_ownership_mid_run(self):
        with tempfile.TemporaryDirectory() as value:
            config, owner = self.make_files(Path(value), "old-token")
            closed = []

            def close_first(account):
                closed.append(account["id"])
                owner.write_text(json.dumps({"pid": 20, "token": "new-token"}), encoding="utf-8")

            with patch.object(browser_watchdog, "close_exact_profile_gracefully", side_effect=close_first):
                browser_watchdog.cleanup(config, owner, "old-token")
            self.assertEqual(closed, [1])


if __name__ == "__main__":
    unittest.main()
