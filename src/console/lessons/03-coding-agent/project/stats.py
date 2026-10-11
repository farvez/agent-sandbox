"""Small statistics helpers. Two of them have bugs: the tests in test_stats.py show which."""


def mean(values):
    return sum(values) / len(values)


def median(values):
    ordered = sorted(values)
    return ordered[len(ordered) // 2]


def mode(values):
    counts = {}
    for v in values:
        counts[v] = counts.get(v, 0) + 1
    return max(counts, key=counts.get)


def variance(values):
    m = mean(values)
    return sum((v - m) ** 2 for v in values) / len(values) - 1
