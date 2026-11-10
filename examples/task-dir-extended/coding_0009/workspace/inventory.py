def apply_discount(price, pct):
    return round(price * (100 - pct) / 100)


def parse_sku(sku):
    prefix, num = sku.split('-')
    return prefix, num


def top_k(items, k):
    return sorted(items, key=lambda x: x[1], reverse=True)[:k]


def dedupe_preserve_order(xs):
    return sorted(set(xs))


def flatten(nested):
    return [x for sub in nested for x in sub]


def is_palindrome(s):
    return s == s[::-1]


def moving_average(xs, w):
    return [sum(xs[i:i + w]) / w for i in range(len(xs) - w + 1)]


def word_frequencies(text):
    counts = {}
    for word in text.split():
        counts[word] = counts.get(word, 0) + 1
    return counts
