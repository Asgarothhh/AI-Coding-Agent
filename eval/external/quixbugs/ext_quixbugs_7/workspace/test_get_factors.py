"""QuixBugs-derived tests for get_factors."""

import unittest

from get_factors import get_factors


class TestGetFactors(unittest.TestCase):
    def test_case_01(self):
        self.assertEqual(get_factors(1), [])

    def test_case_02(self):
        self.assertEqual(get_factors(100), [2, 2, 5, 5])

    def test_case_03(self):
        self.assertEqual(get_factors(101), [101])

    def test_case_04(self):
        self.assertEqual(get_factors(104), [2, 2, 2, 13])

    def test_case_05(self):
        self.assertEqual(get_factors(2), [2])

    def test_case_06(self):
        self.assertEqual(get_factors(3), [3])

    def test_case_07(self):
        self.assertEqual(get_factors(17), [17])

    def test_case_08(self):
        self.assertEqual(get_factors(63), [3, 3, 7])

    def test_case_09(self):
        self.assertEqual(get_factors(74), [2, 37])

    def test_case_10(self):
        self.assertEqual(get_factors(73), [73])

    def test_case_11(self):
        self.assertEqual(get_factors(9837), [3, 3, 1093])


if __name__ == "__main__":
    unittest.main()
