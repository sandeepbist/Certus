from datetime import date, timedelta
from decimal import Decimal


def number(value, default=0):
    """Convert PostgreSQL numeric values into JSON-safe numbers without inventing data."""
    if value is None:
        return default
    if isinstance(value, Decimal):
        return float(value)
    return value


def fill_daily_series(rows, days: int, end_date: date | None = None):
    end = end_date or date.today()
    by_day = {row["day"]: row for row in rows}
    return [
        {
            "date": current.isoformat(),
            "queries": int(number(by_day.get(current, {}).get("queries"), 0)),
            "tokens": int(number(by_day.get(current, {}).get("tokens"), 0)),
            "cost_usd": float(number(by_day.get(current, {}).get("cost_usd"), 0.0)),
        }
        for current in (end - timedelta(days=offset) for offset in range(days - 1, -1, -1))
    ]
