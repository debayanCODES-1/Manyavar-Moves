import unittest
from datetime import date, timedelta

from stock_mover import build_recommendations


AS_OF = date(2026, 10, 31)


def fixture(receiver_stock=0, receiver_transit=0, donor_stock=100, protected=False,
            receiver_capacity=120, alert_limit=5):
    protected_rows = [{"store_id": "DON", "rank": 1, "style_id": "KUR-01"}] if protected else []
    return {
        "stores": [
            {"store_id": "REC", "tier": "A", "stock_capacity_units": receiver_capacity,
             "area_manager_id": "AM-REC"},
            {"store_id": "DON", "tier": "S++", "stock_capacity_units": 200,
             "area_manager_id": "AM-DON"},
        ],
        "area_managers": [
            {"area_manager_id": "AM-REC", "max_alerts_per_week": alert_limit},
            {"area_manager_id": "AM-DON", "max_alerts_per_week": alert_limit},
        ],
        "items": [{"sku_id": "KUR-01-M", "style_id": "KUR-01", "category": "Kurta Set",
                   "is_packaging": 0, "is_core_style": 1}],
        "bills": [
            {"bill_id": "B-REC", "bill_date": "2026-10-15", "store_id": "REC"},
            {"bill_id": "B-DON", "bill_date": "2026-10-15", "store_id": "DON"},
        ],
        "bill_lines": [
            {"bill_id": "B-REC", "sku_id": "KUR-01-M", "qty": 18},
            {"bill_id": "B-DON", "sku_id": "KUR-01-M", "qty": 90},
        ],
        "stock_snapshot": [
            {"as_of_date": "2026-10-31", "location_id": "REC", "sku_id": "KUR-01-M",
             "on_hand": receiver_stock, "in_transit": receiver_transit},
            {"as_of_date": "2026-10-31", "location_id": "DON", "sku_id": "KUR-01-M",
             "on_hand": donor_stock, "in_transit": 0},
            {"as_of_date": "2026-10-31", "location_id": "WH-KOL", "sku_id": "KUR-01-M",
             "on_hand": 0, "in_transit": 0},
        ],
        "stock_weekly": [
            {"store_id": "REC", "sku_id": "KUR-01-M", "days_in_stock": 90},
            {"store_id": "DON", "sku_id": "KUR-01-M", "days_in_stock": 90},
        ],
        "min_display": [{"tier": "A", "category": "Kurta Set", "min_display_units": 0},
                        {"tier": "S++", "category": "Kurta Set", "min_display_units": 10}],
        "lead_times": [
            {"from_type": "warehouse", "to_type": "store", "lead_time_days": 3},
            {"from_type": "store", "to_type": "store", "lead_time_days": 2},
            {"from_type": "store", "to_type": "warehouse", "lead_time_days": 2},
        ],
        "flagship_protected": protected_rows,
        "warehouse_inbound": [],
    }


class StockMoverTests(unittest.TestCase):
    def test_transit_is_included_in_recipient_stock(self):
        moves = build_recommendations(fixture(receiver_transit=8))
        self.assertFalse(any(m["to_location"] == "REC" for m in moves))

    def test_store_transfer_preserves_donor_cover_and_display_floor(self):
        moves = build_recommendations(fixture())
        transfer = next(m for m in moves if m["move_type"] == "STORE_TO_STORE")
        self.assertEqual(transfer["qty"], 7)
        self.assertGreaterEqual(transfer["donor_cover_after_days"], 30)

    def test_flagship_protected_style_is_never_transferred_out(self):
        moves = build_recommendations(fixture(protected=True))
        self.assertFalse(any(m["from_location"] == "DON" for m in moves))

    def test_store_capacity_limits_receipt(self):
        moves = build_recommendations(fixture(receiver_capacity=5))
        transfer = next(m for m in moves if m["move_type"] == "STORE_TO_STORE")
        self.assertEqual(transfer["qty"], 5)

    def test_old_stock_is_pulled_back_but_minimum_display_remains(self):
        data = fixture(receiver_stock=0, donor_stock=30)
        data["bills"] = []
        data["bill_lines"] = []
        data["stock_weekly"] = []
        data["stock_snapshot"][1]["oldest_stock_received_on"] = (
            AS_OF - timedelta(days=150)).isoformat()
        moves = build_recommendations(data)
        pullback = next(m for m in moves if m["move_type"] == "STORE_TO_WAREHOUSE")
        self.assertEqual(pullback["qty"], 20)
        self.assertIn("150-day-old", pullback["reason"])

    def test_manager_weekly_alert_limit_is_respected(self):
        self.assertEqual(build_recommendations(fixture(alert_limit=0)), [])


if __name__ == "__main__":
    unittest.main()
