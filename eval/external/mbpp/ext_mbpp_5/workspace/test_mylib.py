"""MBPP-derived tests for ext_mbpp_5."""

import unittest

from mylib import find_char_long


class TestMyLib(unittest.TestCase):
    def test_case_01(self):
        assert set(find_char_long('Please move back to stream')) == set(['Please', 'move', 'back', 'stream'])

    def test_case_02(self):
        assert set(find_char_long('Jing Eco and Tech')) == set(['Jing', 'Tech'])

    def test_case_03(self):
        assert set(find_char_long('Jhingai wulu road Zone 3')) == set(['Jhingai', 'wulu', 'road', 'Zone'])


if __name__ == "__main__":
    unittest.main()
