from datetime import date
from decimal import Decimal
import unittest

from services.orchestration.app.retrieval.analytics_view import fill_daily_series, number


class AnalyticsProjectionTests(unittest.TestCase):
    def test_fill_daily_series_includes_zero_days_and_json_safe_numbers(self):
        rows = [
            {
                "day": date(2026, 8, 23),
                "queries": 2,
                "tokens": Decimal("125"),
                "cost_usd": Decimal("0.00125"),
            }
        ]

        series = fill_daily_series(rows, 3, end_date=date(2026, 8, 24))

        self.assertEqual(
            series,
            [
                {"date": "2026-08-22", "queries": 0, "tokens": 0, "cost_usd": 0.0},
                {"date": "2026-08-23", "queries": 2, "tokens": 125, "cost_usd": 0.00125},
                {"date": "2026-08-24", "queries": 0, "tokens": 0, "cost_usd": 0.0},
            ],
        )

    def test_number_preserves_zero_and_only_defaults_missing_values(self):
        self.assertEqual(number(0, 99), 0)
        self.assertEqual(number(Decimal("1.5")), 1.5)
        self.assertEqual(number(None, 99), 99)


if __name__ == "__main__":
    unittest.main()
