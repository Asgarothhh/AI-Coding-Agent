import unittest

import mylib as m


class T(unittest.TestCase):
    def test_normalize_spaces(self):
        self.assertEqual(m.normalize_spaces('  a\t b\n c  '), 'a b c')
        self.assertEqual(m.normalize_spaces(''), '')

    def test_running_max(self):
        self.assertEqual(m.running_max([1, 3, 2, 5]), [1, 3, 3, 5])
        self.assertEqual(m.running_max([-5, -2, -9]), [-5, -2, -2])
        self.assertEqual(m.running_max([]), [])

    def test_chunk(self):
        self.assertEqual(m.chunk([1, 2, 3, 4, 5], 2), [[1, 2], [3, 4], [5]])
        self.assertEqual(m.chunk([1, 2], 3), [[1, 2]])
        self.assertEqual(m.chunk([], 2), [])

    def test_count_vowels(self):
        self.assertEqual(m.count_vowels('AeIou x'), 5)
        self.assertEqual(m.count_vowels('XYZ'), 0)

    def test_safe_div(self):
        self.assertEqual(m.safe_div(7, 2), 3.5)
        self.assertIsNone(m.safe_div(1, 0))

    def test_merge_sorted(self):
        self.assertEqual(m.merge_sorted([1, 3, 5], [2, 4, 6]), [1, 2, 3, 4, 5, 6])
        self.assertEqual(m.merge_sorted([], [1, 2]), [1, 2])

    def test_is_balanced(self):
        self.assertTrue(m.is_balanced('(a[b]{c})'))
        self.assertFalse(m.is_balanced('([)]'))
        self.assertFalse(m.is_balanced(')('))
        self.assertTrue(m.is_balanced(''))

    def test_fib(self):
        self.assertEqual(m.fib(0), 0)
        self.assertEqual(m.fib(1), 1)
        self.assertEqual(m.fib(7), 13)


if __name__ == '__main__':
    unittest.main()
