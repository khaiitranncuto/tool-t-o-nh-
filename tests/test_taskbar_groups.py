import unittest

import app


class TaskbarGroupTests(unittest.TestCase):
    def test_each_account_has_a_stable_unique_app_id(self):
        first = app.taskbar_app_id({"id": 1})
        second = app.taskbar_app_id({"id": 2})
        self.assertEqual(first, "TOOLV1RING.Flow.Account.001")
        self.assertEqual(second, "TOOLV1RING.Flow.Account.002")
        self.assertNotEqual(first, second)


if __name__ == "__main__":
    unittest.main()
