"""QuixBugs-derived tests for gcd."""

import unittest

from gcd import gcd


class TestGcd(unittest.TestCase):
    def test_case_01(self):
        self.assertEqual(gcd(17, 0), 17)

    def test_case_02(self):
        self.assertEqual(gcd(13, 13), 13)

    def test_case_03(self):
        self.assertEqual(gcd(37, 600), 1)

    def test_case_04(self):
        self.assertEqual(gcd(20, 100), 20)

    def test_case_05(self):
        self.assertEqual(gcd(624129, 2061517), 18913)

    def test_case_06(self):
        self.assertEqual(gcd(3, 12), 3)


if __name__ == "__main__":
    unittest.main()
