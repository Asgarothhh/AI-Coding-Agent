"""QuixBugs-derived tests for find_in_sorted."""

import unittest

from find_in_sorted import find_in_sorted


class TestFindInSorted(unittest.TestCase):
    def test_case_01(self):
        self.assertEqual(find_in_sorted([3, 4, 5, 5, 5, 5, 6], 5), 3)

    def test_case_02(self):
        self.assertEqual(find_in_sorted([1, 2, 3, 4, 6, 7, 8], 5), -1)

    def test_case_03(self):
        self.assertEqual(find_in_sorted([1, 2, 3, 4, 6, 7, 8], 4), 3)

    def test_case_04(self):
        self.assertEqual(find_in_sorted([2, 4, 6, 8, 10, 12, 14, 16, 18, 20], 18), 8)

    def test_case_05(self):
        self.assertEqual(find_in_sorted([3, 5, 6, 7, 8, 9, 12, 13, 14, 24, 26, 27], 0), -1)

    def test_case_06(self):
        self.assertEqual(find_in_sorted([3, 5, 6, 7, 8, 9, 12, 12, 14, 24, 26, 27], 12), 6)

    def test_case_07(self):
        self.assertEqual(find_in_sorted([24, 26, 28, 50, 59], 101), -1)


if __name__ == "__main__":
    unittest.main()
