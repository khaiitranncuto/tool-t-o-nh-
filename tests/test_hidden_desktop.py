import os
import unittest

import app


@unittest.skipUnless(os.name == "nt", "Windows hidden desktop only")
class HiddenDesktopTests(unittest.TestCase):
    def tearDown(self):
        app.close_hidden_desktop()

    def test_private_desktop_can_host_a_real_process(self):
        command = os.environ.get("ComSpec", r"C:\Windows\System32\cmd.exe")
        process = app.create_process_on_hidden_desktop([command, "/d", "/c", "exit", "0"])
        try:
            self.assertEqual(process.desktop_mode, "HIDDEN_DESKTOP")
            self.assertEqual(process.wait(timeout=10), 0)
        finally:
            process.close()
