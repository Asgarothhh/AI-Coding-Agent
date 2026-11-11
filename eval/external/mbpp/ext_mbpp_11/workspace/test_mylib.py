"""MBPP-derived tests for ext_mbpp_11."""

import unittest

from mylib import text_lowercase_underscore


class TestMyLib(unittest.TestCase):
    def test_case_01(self):
        assert text_lowercase_underscore("aab_cbbbc")==(True)

    def test_case_02(self):
        assert text_lowercase_underscore("aab_Abbbc")==(False)

    def test_case_03(self):
        assert text_lowercase_underscore("Aaab_abbbc")==(False)


if __name__ == "__main__":
    unittest.main()
