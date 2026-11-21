import unittest
from datetime import date, timedelta

from backend.app.forecast import forecast_velocity


class ForecastTests(unittest.TestCase):
    def test_tweedie_forecast_trains_and_reports_temporal_holdout(self):
        stores = [{"store_id": f"S{i}", "tier": "A", "state": "Uttar Pradesh"}
                  for i in range(2)]
        items = [{
            "sku_id": f"KUR-{i:02}-M", "style_id": f"KUR-{i:02}",
            "category": "Kurta Set", "mrp": 5000, "is_core_style": 1,
        } for i in range(20)]
        weeks = [date(2026, 9, 26) + timedelta(days=7 * i) for i in range(6)]
        stock_weekly = [{
            "store_id": store["store_id"], "sku_id": item["sku_id"],
            "week_ending": week.isoformat(), "days_in_stock": 6,
        } for store in stores for item in items for week in weeks]
        bills, lines = [], []
        for store_idx, store in enumerate(stores):
            for item_idx, item in enumerate(items):
                for week_idx, week in enumerate(weeks):
                    bill_id = f"B-{store_idx}-{item_idx}-{week_idx}"
                    bills.append({
                        "bill_id": bill_id, "store_id": store["store_id"],
                        "bill_date": (week - timedelta(days=1)).isoformat(),
                    })
                    lines.append({
                        "bill_id": bill_id, "sku_id": item["sku_id"],
                        "qty": (store_idx + item_idx + week_idx) % 4,
                    })
        tables = {
            "stores": stores, "items": items, "stock_weekly": stock_weekly,
            "bills": bills, "bill_lines": lines,
            "stock_snapshot": [{"as_of_date": "2026-10-31"}], "calendar": [],
        }
        velocity, metadata = forecast_velocity(tables)
        self.assertEqual(metadata["model"], "lightgbm_tweedie")
        self.assertEqual(metadata["style_pooling"], True)
        self.assertEqual(metadata["validation"]["holdout_weeks"], 2)
        self.assertEqual(len(velocity), len(stores) * len(items))
        self.assertTrue(all(value >= 0 for value in velocity.values()))


if __name__ == "__main__":
    unittest.main()
