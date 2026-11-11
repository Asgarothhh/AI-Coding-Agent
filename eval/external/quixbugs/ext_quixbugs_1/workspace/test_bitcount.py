"""QuixBugs-derived tests for bitcount."""

import unittest

from bitcount import bitcount


class TestBitcount(unittest.TestCase):
    def test_case_01(self):
        self.assertEqual(bitcount(127), 7)

    def test_case_02(self):
        self.assertEqual(bitcount(128), 1)

    def test_case_03(self):
        self.assertEqual(bitcount(3005), 9)

    def test_case_04(self):
        self.assertEqual(bitcount(13), 3)

    def test_case_05(self):
        self.assertEqual(bitcount(14), 3)

    def test_case_06(self):
        self.assertEqual(bitcount(27), 4)

    def test_case_07(self):
        self.assertEqual(bitcount(834), 4)

    def test_case_08(self):
        self.assertEqual(bitcount(254), 7)

    def test_case_09(self):
        self.assertEqual(bitcount(256), 1)


if __name__ == "__main__":
    unittest.main()
