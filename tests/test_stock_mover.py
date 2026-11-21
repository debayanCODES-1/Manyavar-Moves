import unittest
from datetime import date, timedelta

from stock_mover import (
    WAREHOUSE, build_lead_time_overrides, build_recommendations, limit_manager_alerts,
)
from backend.app.optimizer import optimize_proposals
from backend.app.forecast import forecast_velocity


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
        "rules": [{
            "rule": "Approval",
            "detail": "Moves worth more than Rs 1,00,000 at MRP require human approval.",
        }],
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
        self.assertIn("sell_probability_pct", transfer)
        self.assertGreater(transfer["sell_probability_pct"], 0)
        self.assertGreater(transfer["store_affinity_score"], 0)
        self.assertIn("Store fit:", transfer["reason"])
        checks = {check["rule"]: check for check in transfer["validation_checks"]}
        self.assertEqual(checks["Store capacity"]["status"], "pass")
        self.assertEqual(checks["Minimum display"]["status"], "pass")
        self.assertEqual(checks["S++ style protection"]["status"], "pass")
        self.assertEqual(checks["Donor cover"]["status"], "pass")
        self.assertEqual(checks["Route lead time"]["status"], "pass")

    def test_stale_stock_moves_to_store_with_matching_size_and_material_demand(self):
        data = fixture(receiver_stock=0, donor_stock=100)
        data["items"][0].update({"size": "M", "material": "Cotton Silk"})
        data["bills"] = []
        data["bill_lines"] = []
        data["stock_weekly"] = [
            {"store_id": "REC", "sku_id": "KUR-01-M", "days_in_stock": 0},
            {"store_id": "DON", "sku_id": "KUR-01-M", "days_in_stock": 90},
            {"store_id": "REC", "sku_id": "KUR-02-M", "days_in_stock": 90},
        ]
        data["items"].append({
            "sku_id": "KUR-02-M", "style_id": "KUR-02", "category": "Kurta Set",
            "size": "M", "material": "Cotton Silk", "is_packaging": 0,
        })
        data["bills"].append({
            "bill_id": "B-REC-PEER", "bill_date": "2026-10-15", "store_id": "REC",
        })
        data["bill_lines"].append({
            "bill_id": "B-REC-PEER", "sku_id": "KUR-02-M", "qty": 20,
        })
        data["stock_snapshot"][1]["oldest_stock_received_on"] = (
            AS_OF - timedelta(days=150)).isoformat()

        moves = build_recommendations(data)
        transfer = next(move for move in moves if move["move_type"] == "STORE_TO_STORE")
        self.assertEqual(transfer["from_location"], "DON")
        self.assertEqual(transfer["to_location"], "REC")
        self.assertGreater(transfer["daily_velocity"], 0)
        self.assertIn("matching material/cloth", transfer["demand_basis"])
        self.assertIn("70+ day no-demand stock", transfer["reason"])
        self.assertEqual(transfer["donor_cover_after_days"], "")
        self.assertTrue(transfer["stale_no_demand_redeployment"])
        checks = {check["rule"]: check for check in transfer["validation_checks"]}
        self.assertEqual(checks["Donor cover"]["status"], "evidence")

    def test_flagship_protected_style_is_never_transferred_out(self):
        moves = build_recommendations(fixture(protected=True))
        self.assertFalse(any(m["from_location"] == "DON" for m in moves))

    def test_store_capacity_limits_receipt(self):
        moves = build_recommendations(fixture(receiver_capacity=5))
        transfer = next(m for m in moves if m["move_type"] == "STORE_TO_STORE")
        self.assertEqual(transfer["qty"], 5)
        self.assertEqual(transfer["target_capacity_remaining_units"], 5)

    def test_capacity_remaining_counts_all_other_skus_at_destination(self):
        data = fixture(receiver_capacity=10)
        data["items"].append({
            "sku_id": "JKT-01-M", "style_id": "JKT-01", "category": "Jacket",
            "is_packaging": 0,
        })
        data["min_display"].extend([
            {"tier": "A", "category": "Jacket", "min_display_units": 0},
            {"tier": "S++", "category": "Jacket", "min_display_units": 0},
        ])
        data["stock_snapshot"].append({
            "as_of_date": "2026-10-31", "location_id": "REC", "sku_id": "JKT-01-M",
            "on_hand": 7, "in_transit": 0,
        })
        moves = build_recommendations(data)
        transfer = next(move for move in moves if move["move_type"] == "STORE_TO_STORE")
        self.assertEqual(transfer["qty"], 3)
        self.assertEqual(transfer["target_capacity_remaining_units"], 3)

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
        data = fixture(alert_limit=0)
        candidates = build_recommendations(data)
        self.assertTrue(candidates)
        self.assertEqual(limit_manager_alerts(candidates, data), [])

    def test_kolkata_warehouse_routes_use_store_zone_lead_times(self):
        data = fixture()
        data["stores"][0].update(city="Patna", zone="East")
        data["stores"][1].update(city="Lucknow", zone="North")
        data["stock_snapshot"][2]["on_hand"] = 100
        data["lead_times"] = [
            {"route": "Warehouse WH-KOL to store, East zone (Bihar)", "days": 3},
            {"route": "Warehouse WH-KOL to store, North zone (UP)", "days": 4},
            {"route": "Warehouse WH-KOL to store, South zone (Karnataka)", "days": 6},
            {"route": "Store to store, same city", "days": 2},
            {"route": "Store to store, same zone, different city", "days": 4},
            {"route": "Store to store, different zone", "days": 7},
        ]
        lead_times = build_lead_time_overrides(data)

        self.assertEqual(lead_times[WAREHOUSE, "REC"], 3)
        self.assertEqual(lead_times[WAREHOUSE, "DON"], 4)
        self.assertEqual(lead_times["REC", WAREHOUSE], 3)
        moves = build_recommendations(data, lead_time_overrides=lead_times)
        warehouse_move = next(move for move in moves if move["move_type"] == "WAREHOUSE_TO_STORE")
        self.assertEqual(warehouse_move["from_location"], WAREHOUSE)
        self.assertEqual(warehouse_move["to_location"], "REC")
        self.assertEqual(warehouse_move["arrival_date"], "2026-11-03")

    def test_milp_optimizer_keeps_feasible_recommendations(self):
        data = fixture()
        proposals = build_recommendations(data)
        optimized, metadata = optimize_proposals(proposals, data)
        self.assertEqual(metadata["optimizer"], "scipy_milp")
        self.assertEqual(len(optimized), len(proposals))

    def test_forecast_falls_back_when_history_is_not_model_ready(self):
        velocity, metadata = forecast_velocity(fixture())
        self.assertEqual(metadata["status"], "fallback")
        self.assertTrue(all(value == 0 for value in velocity.values()))


if __name__ == "__main__":
    unittest.main()
