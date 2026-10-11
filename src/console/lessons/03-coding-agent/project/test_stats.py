import unittest

from stats import mean, median, mode, variance


class StatsTest(unittest.TestCase):
    def test_mean(self):
        self.assertEqual(mean([1, 2, 3, 4]), 2.5)

    def test_median_odd(self):
        self.assertEqual(median([3, 1, 2]), 2)

    def test_median_even(self):
        self.assertEqual(median([4, 1, 3, 2]), 2.5)

    def test_mode(self):
        self.assertEqual(mode([1, 2, 2, 3]), 2)

    def test_variance(self):
        # Population variance: the mean of squared distances from the mean.
        self.assertEqual(variance([2, 4, 4, 4, 5, 5, 7, 9]), 4)


if __name__ == "__main__":
    unittest.main()
