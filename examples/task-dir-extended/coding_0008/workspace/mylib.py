def normalize_spaces(s):
    return ' '.join(s.split(' '))


def running_max(xs):
    out = []
    best = 0
    for x in xs:
        if x > best:
            best = x
        out.append(best)
    return out


def chunk(xs, n):
    return [xs[i:i + n] for i in range(0, len(xs) - n + 1, n)]


def count_vowels(s):
    return sum(1 for ch in s if ch in 'aeiou')


def safe_div(a, b):
    return a / b


def merge_sorted(a, b):
    return a + b


def is_balanced(s):
    return s.count('(') == s.count(')')


def fib(n):
    a, b = 0, 1
    for _ in range(n - 1):
        a, b = b, a + b
    return a
