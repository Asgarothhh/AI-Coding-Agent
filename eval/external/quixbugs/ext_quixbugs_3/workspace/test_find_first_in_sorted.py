"""QuixBugs-derived tests for find_first_in_sorted."""

import unittest

from find_first_in_sorted import find_first_in_sorted


class TestFindFirstInSorted(unittest.TestCase):
    def test_case_01(self):
        self.assertEqual(find_first_in_sorted([3, 4, 5, 5, 5, 5, 6], 5), 2)

    def test_case_02(self):
        self.assertEqual(find_first_in_sorted([3, 4, 5, 5, 5, 5, 6], 7), -1)

    def test_case_03(self):
        self.assertEqual(find_first_in_sorted([3, 4, 5, 5, 5, 5, 6], 2), -1)

    def test_case_04(self):
        self.assertEqual(find_first_in_sorted([3, 6, 7, 9, 9, 10, 14, 27], 14), 6)

    def test_case_05(self):
        self.assertEqual(find_first_in_sorted([0, 1, 6, 8, 13, 14, 67, 128], 80), -1)

    def test_case_06(self):
        self.assertEqual(find_first_in_sorted([0, 1, 6, 8, 13, 14, 67, 128], 67), 6)

    def test_case_07(self):
        self.assertEqual(find_first_in_sorted([0, 1, 6, 8, 13, 14, 67, 128], 128), 7)


if __name__ == "__main__":
    unittest.main()
