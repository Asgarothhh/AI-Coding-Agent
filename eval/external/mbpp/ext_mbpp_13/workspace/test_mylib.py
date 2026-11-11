"""MBPP-derived tests for ext_mbpp_13."""

import unittest

from mylib import remove_dirty_chars


class TestMyLib(unittest.TestCase):
    def test_case_01(self):
        assert remove_dirty_chars("probasscurve", "pros") == 'bacuve'

    def test_case_02(self):
        assert remove_dirty_chars("digitalindia", "talent") == 'digiidi'

    def test_case_03(self):
        assert remove_dirty_chars("exoticmiles", "toxic") == 'emles'


if __name__ == "__main__":
    unittest.main()
