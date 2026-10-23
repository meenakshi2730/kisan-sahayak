
import unittest

from agents.price import get_mandi_prices


class TestMandiPriceAgent(unittest.TestCase):

    def test_tomato_prices_are_sorted_descending(self):
        result = get_mandi_prices("tomato")

        self.assertEqual(result["status"], "success")
        prices = [item["price_per_kg"] for item in result["prices"]]
        self.assertEqual(prices, sorted(prices, reverse=True))

    def test_unknown_crop_returns_fallback(self):
        result = get_mandi_prices("potato")

        self.assertEqual(result["status"], "fallback")
        self.assertEqual(result["prices"], [])

    def test_empty_crop_returns_fallback(self):
        result = get_mandi_prices("")

        self.assertEqual(result["status"], "fallback")
        self.assertEqual(result["prices"], [])

    def test_crop_matching_is_case_insensitive(self):
        result = get_mandi_prices("TOMATO")

        self.assertEqual(result["status"], "success")


if __name__ == "__main__":
    unittest.main()
