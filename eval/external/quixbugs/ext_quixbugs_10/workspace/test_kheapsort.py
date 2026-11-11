"""QuixBugs-derived tests for kheapsort."""

import unittest

from kheapsort import kheapsort


class TestKheapsort(unittest.TestCase):
    def test_case_01(self):
        self.assertEqual(kheapsort([1, 2, 3, 4, 5], 0), [1, 2, 3, 4, 5])

    def test_case_02(self):
        self.assertEqual(kheapsort([3, 2, 1, 5, 4], 2), [1, 2, 3, 4, 5])

    def test_case_03(self):
        self.assertEqual(kheapsort([5, 4, 3, 2, 1], 4), [1, 2, 3, 4, 5])

    def test_case_04(self):
        self.assertEqual(kheapsort([3, 12, 5, 1, 6], 3), [1, 3, 5, 6, 12])


if __name__ == "__main__":
    unittest.main()
