"""QuixBugs-derived tests for hanoi."""

import unittest

from hanoi import hanoi


class TestHanoi(unittest.TestCase):
    def test_case_01(self):
        self.assertEqual(hanoi(0, 1, 3), [])

    def test_case_02(self):
        self.assertEqual(hanoi(1, 1, 3), [[1, 3]])

    def test_case_03(self):
        self.assertEqual(hanoi(2, 1, 3), [[1, 2], [1, 3], [2, 3]])

    def test_case_04(self):
        self.assertEqual(hanoi(3, 1, 3), [[1, 3], [1, 2], [3, 2], [1, 3], [2, 1], [2, 3], [1, 3]])

    def test_case_05(self):
        self.assertEqual(hanoi(4, 1, 3), [[1, 2], [1, 3], [2, 3], [1, 2], [3, 1], [3, 2], [1, 2], [1, 3], [2, 3], [2, 1], [3, 1], [2, 3], [1, 2], [1, 3], [2, 3]])

    def test_case_06(self):
        self.assertEqual(hanoi(2, 1, 2), [[1, 3], [1, 2], [3, 2]])

    def test_case_07(self):
        self.assertEqual(hanoi(2, 1, 1), [[1, 2], [1, 1], [2, 1]])

    def test_case_08(self):
        self.assertEqual(hanoi(2, 3, 1), [[3, 2], [3, 1], [2, 1]])


if __name__ == "__main__":
    unittest.main()
