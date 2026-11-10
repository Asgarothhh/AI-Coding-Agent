import unittest

import inventory as m


class T(unittest.TestCase):
    def test_apply_discount(self):
        self.assertEqual(m.apply_discount(103, 50), 51)
        self.assertEqual(m.apply_discount(100, 0), 100)
        self.assertEqual(m.apply_discount(100, 100), 0)

    def test_parse_sku(self):
        self.assertEqual(m.parse_sku('XYZ-42'), ('XYZ', 42))

    def test_top_k(self):
        self.assertEqual(
            m.top_k([('b', 10), ('a', 10), ('c', 12)], 2),
            [('c', 12), ('a', 10)],
        )
        self.assertEqual(m.top_k([], 2), [])

    def test_dedupe_preserve_order(self):
        self.assertEqual(
            m.dedupe_preserve_order(['b', 'a', 'b', 'c', 'a']),
            ['b', 'a', 'c'],
        )
        self.assertEqual(m.dedupe_preserve_order([]), [])

    def test_flatten(self):
        self.assertEqual(m.flatten([1, [2, [3, 4]], 5]), [1, 2, 3, 4, 5])
        self.assertEqual(m.flatten([]), [])

    def test_is_palindrome(self):
        self.assertTrue(m.is_palindrome('A man, a plan, a canal: Panama'))
        self.assertFalse(m.is_palindrome('race a car'))
        self.assertTrue(m.is_palindrome(''))

    def test_moving_average(self):
        self.assertEqual(m.moving_average([1, 2, 3, 4, 5], 2), [1, 2, 3, 4])
        self.assertEqual(m.moving_average([1, 2], 3), [])
        self.assertEqual(m.moving_average([], 2), [])

    def test_word_frequencies(self):
        self.assertEqual(
            m.word_frequencies('Hello, hello world!'),
            {'hello': 2, 'world': 1},
        )
        self.assertEqual(m.word_frequencies(''), {})


if __name__ == '__main__':
    unittest.main()
