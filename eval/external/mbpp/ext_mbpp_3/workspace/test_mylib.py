"""MBPP-derived tests for ext_mbpp_3."""

import unittest

from mylib import heap_queue_largest


class TestMyLib(unittest.TestCase):
    def test_case_01(self):
        assert heap_queue_largest( [25, 35, 22, 85, 14, 65, 75, 22, 58],3)==[85, 75, 65]

    def test_case_02(self):
        assert heap_queue_largest( [25, 35, 22, 85, 14, 65, 75, 22, 58],2)==[85, 75]

    def test_case_03(self):
        assert heap_queue_largest( [25, 35, 22, 85, 14, 65, 75, 22, 58],5)==[85, 75, 65, 58, 35]


if __name__ == "__main__":
    unittest.main()
