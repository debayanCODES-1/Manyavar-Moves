import asyncio
import io
import json
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request
from openpyxl import Workbook

from backend.app.demo_data import build_demo_tables
import backend.main as api
from test_stock_mover import fixture


def workbook_bytes(tables):
    workbook = Workbook()
    workbook.remove(workbook.active)
    for name, rows in tables.items():
        sheet = workbook.create_sheet(name)
        if not rows:
            sheet.append(["empty"])
            continue
        columns = list(rows[0])
        sheet.append(columns)
        for row in rows:
            sheet.append([row.get(column) for column in columns])
    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()


def api_fixture():
    tables = fixture()
    tables["items"][0].update({
        "size": "M", "mrp": 10000, "cost_price": 4000,
        "launch_date": "2025-01-01",
    })
    for store in tables["stores"]:
        store["state"] = "Uttar Pradesh"
    for line in tables["bill_lines"]:
        line["selling_price"] = 7000
    tables["stock_snapshot"][2]["on_hand"] = 20
    weekly_rows = []
    for row in tables["stock_weekly"]:
        for week_end in ("2026-10-17", "2026-10-24", "2026-10-31"):
            weekly_rows.append({**row, "week_ending": week_end, "days_in_stock": 7})
    tables["stock_weekly"] = weekly_rows
    return tables


class APIUploadTests(unittest.TestCase):
    def test_public_demo_viewer_can_sign_in_without_token_but_write_roles_require_it(self):
        with patch.object(api, "DEMO_MODE", True), \
                patch.object(api, "APPROVAL_TOKEN", "team-access-token"), \
                patch.object(api, "SESSION_SECRET", "test-session-secret"):
            viewer = api.demo_login(api.DemoLoginRequest(role="viewer"))
            self.assertEqual(viewer.status_code, 200)
            self.assertEqual(json.loads(viewer.body)["user"]["permissions"], ["read"])

            with self.assertRaises(HTTPException) as raised:
                api.demo_login(api.DemoLoginRequest(role="stock_manager"))
            self.assertEqual(raised.exception.status_code, 401)

            manager = api.demo_login(api.DemoLoginRequest(
                role="stock_manager", token="team-access-token",
            ))
            self.assertEqual(manager.status_code, 200)

    def test_vercel_run_recalculates_the_persisted_dataset_without_a_blob_source(self):
        tables = api_fixture()
        state = {
            "source_name": "Generated synthetic demo data",
            "source_blob_path": None,
            "tables": tables,
            "is_demo_data": True,
        }
        with patch.object(api, "IS_VERCEL", True), \
                patch.object(api, "state", state), \
                patch.object(api, "_run_analysis_tables") as analyze, \
                patch.object(api, "_summary", return_value={"has_data": True}):
            result = asyncio.run(api.run())

        analyze.assert_called_once_with(
            tables, "Generated synthetic demo data", is_demo_data=True,
        )
        self.assertTrue(result["ok"])
        self.assertTrue(result["has_data"])

    def test_signed_session_accepts_valid_token_and_rejects_tampering(self):
        with patch.object(api, "SESSION_SECRET", "test-session-secret"):
            response = api._session_response("analyst", "Test Analyst")
            token = json.loads(response.body)["sessionToken"]

            def request_with_token(value):
                return Request({
                    "type": "http",
                    "headers": [(b"authorization", f"Bearer {value}".encode())],
                })

            self.assertEqual(api._session_from_request(request_with_token(token))["role"], "analyst")
            tampered = f"{token[:-1]}{'A' if token[-1] != 'A' else 'B'}"
            self.assertIsNone(api._session_from_request(request_with_token(tampered)))

    def test_state_payload_round_trips_dates_and_restores_required_defaults(self):
        generated_at = datetime(2026, 10, 31, tzinfo=timezone.utc)
        with patch.object(api, "state", {
            "generated_at": generated_at,
            "tables": {"calendar": [{"date": date(2026, 10, 31)}]},
        }):
            payload = api._database_payload()
            self.assertEqual(payload["generated_at"], generated_at.isoformat())
            self.assertEqual(payload["tables"]["calendar"][0]["date"], "2026-10-31")

            api._restore_database_payload(payload)
            self.assertEqual(api.state["generated_at"], generated_at.isoformat())
            self.assertEqual(api.state["tables"]["calendar"][0]["date"], "2026-10-31")
            self.assertEqual(api.state["approvals"], {})
            self.assertEqual(api.state["workflow_audit"], [])
            self.assertIsNone(api.state["source_blob_path"])

    def test_api_shared_secret_protects_backend_routes_but_not_health(self):
        previous = dict(api.state)
        try:
            with patch.object(api, "API_SHARED_SECRET", "gateway-test-secret"), \
                    TestClient(api.app) as client:
                self.assertEqual(client.get("/health").status_code, 200)
                self.assertEqual(client.get("/api/summary").status_code, 401)
                response = client.get(
                    "/api/summary",
                    headers={"x-stockmover-api-secret": "gateway-test-secret"},
                )
                self.assertEqual(response.status_code, 200)
        finally:
            api.state.clear()
            api.state.update(previous)

    def test_zero_sales_without_recent_stock_exposure_is_unknown_not_dormant(self):
        source = api_fixture()
        source["bills"] = []
        source["bill_lines"] = []
        source["stock_weekly"] = [
            {**row, "days_in_stock": 0} for row in source["stock_weekly"]
        ]
        analytics = api._build_analytics(source)
        store = next(row for row in analytics["products"]["KUR-01-M"]["store_performance"]
                     if row["store_id"] == "REC")
        self.assertIsNone(store["recent_velocity"])
        self.assertEqual(store["sell_through_grade"], "UNKNOWN")
        self.assertFalse(store["unsold_with_exposure"])

    def test_store_sell_grade_compares_velocity_only_after_recorded_exposure(self):
        analytics = api._build_analytics(api_fixture())
        metrics = {
            row["store_id"]: row
            for row in analytics["products"]["KUR-01-M"]["store_performance"]
        }
        self.assertGreater(metrics["DON"]["recent_velocity"], metrics["REC"]["recent_velocity"])
        self.assertEqual(metrics["DON"]["sell_through_grade"], "A")
        self.assertEqual(metrics["REC"]["sell_through_grade"], "C")

    def test_festival_stock_risk_requires_seven_days_without_sales_and_exposure(self):
        source = api_fixture()
        source["bills"] = []
        source["bill_lines"] = []
        source["calendar"] = [{
            "date": "2026-10-29", "state": "Uttar Pradesh",
            "festival": "Durga Puja", "is_wedding_date": 0,
        }]
        source["stock_snapshot"][0]["on_hand"] = 5
        analytics = api._build_analytics(source)
        store = next(row for row in analytics["products"]["KUR-01-M"]["store_performance"]
                     if row["store_id"] == "REC")
        self.assertEqual(store["stock_risk"], "seasonal_7_day_no_sale")

        source["stock_weekly"] = [
            {**row, "days_in_stock": 0} for row in source["stock_weekly"]
        ]
        analytics = api._build_analytics(source)
        store = next(row for row in analytics["products"]["KUR-01-M"]["store_performance"]
                     if row["store_id"] == "REC")
        self.assertIsNone(store["stock_risk"])

    def test_dead_stock_requires_70_days_and_recent_stock_exposure(self):
        source = api_fixture()
        source["bills"] = []
        source["bill_lines"] = []
        for row in source["stock_snapshot"]:
            if row["location_id"] == "REC" and row["sku_id"] == "KUR-01-M":
                row["on_hand"] = 5
                row["oldest_stock_received_on"] = "2026-07-01"
        analytics = api._build_analytics(source)
        store = next(row for row in analytics["products"]["KUR-01-M"]["store_performance"]
                     if row["store_id"] == "REC")
        self.assertEqual(store["stock_risk"], "dead_stock_70_day_no_sale")
        self.assertEqual(store["sell_through_grade"], "WATCH")

    def test_normal_season_signal_uses_exposure_after_a_newer_design_launch(self):
        source = api_fixture()
        source["bills"] = []
        source["bill_lines"] = []
        source["items"].append({
            "sku_id": "KUR-02-M", "style_id": "KUR-02", "style_name": "New Kurta",
            "category": "Kurta Set", "size": "M", "mrp": 11000,
            "cost_price": 4500, "launch_date": "2026-10-20", "is_packaging": 0,
        })
        analytics = api._build_analytics(source)
        donor = next(row for row in analytics["products"]["KUR-01-M"]["store_performance"]
                     if row["store_id"] == "DON")
        self.assertEqual(donor["stock_risk"], "no_sales_after_new_design")
        self.assertEqual(donor["successor_launch_date"], "2026-10-20")

    def test_margin_uses_recent_discounted_selling_price_and_similarity_uses_available_fields(self):
        source = api_fixture()
        source["items"].append({
            "sku_id": "KUR-02-M", "style_id": "KUR-02", "style_name": "New Kurta",
            "category": "Kurta Set", "size": "M", "mrp": 11000,
            "cost_price": 4500, "launch_date": "2026-09-01", "is_packaging": 0,
        })
        source["bills"].append({
            "bill_id": "B-SIM", "bill_date": "2026-10-20", "store_id": "REC",
        })
        source["bill_lines"].append({
            "bill_id": "B-SIM", "sku_id": "KUR-02-M", "qty": 20,
            "selling_price": 8000,
        })
        source["stock_snapshot"].append({
            "as_of_date": "2026-10-31", "location_id": "REC",
            "sku_id": "KUR-02-M", "on_hand": 2, "in_transit": 0,
        })
        source["stock_weekly"].extend([
            {"week_ending": week, "store_id": "REC", "sku_id": "KUR-02-M",
             "closing_on_hand": 2, "days_in_stock": 7}
            for week in ("2026-10-17", "2026-10-24", "2026-10-31")
        ])
        analytics = api._build_analytics(source)
        product = analytics["products"]["KUR-01-M"]
        self.assertEqual(product["average_selling_price"], 7000)
        self.assertEqual(product["unit_gross_margin_estimate"], 3000)
        self.assertAlmostEqual(product["gross_margin_pct_estimate"], 42.86, places=2)

        previous = dict(api.state)
        try:
            api.state["approvals"] = {}
            api._run_analysis_tables(source, "test catalogue")
            similar = api.product_detail("KUR-01-M")["similar_products"]
            self.assertEqual(similar[0]["sku_id"], "KUR-02-M")
            self.assertFalse(similar[0]["material_match_available"])
            self.assertEqual(similar[0]["demand_store"], "REC")
            self.assertTrue(api.state["moves"])
            self.assertIsNotNone(api.state["moves"][0]["estimated_gross_profit_14d"])
        finally:
            api.state.clear()
            api.state.update(previous)

    def test_packaging_lines_do_not_rank_as_merchandise_sales(self):
        source = api_fixture()
        source["items"].append({
            "sku_id": "PKG-BAG", "style_id": "PKG-BAG", "style_name": "Carry Bag",
            "category": "Packaging", "size": "FS", "mrp": 0, "is_packaging": 1,
        })
        source["bills"].append({
            "bill_id": "B-PACK", "bill_date": "2026-10-25", "store_id": "REC",
        })
        source["bill_lines"].append({
            "bill_id": "B-PACK", "sku_id": "PKG-BAG", "qty": 1, "selling_price": 0,
        })
        analytics = api._build_analytics(source)
        self.assertEqual(analytics["products"]["PKG-BAG"]["total_sold_units"], 0)
        package_bill = next(row for row in analytics["bill_summaries"]
                            if row["bill_id"] == "B-PACK")
        self.assertEqual(package_bill["units"], 0)

    def test_empty_data_directory_automatically_serves_calculated_demo_moves(self):
        previous = dict(api.state)
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            try:
                with patch.object(api, "DATA_DIR", data_dir), \
                        TestClient(api.app) as client:
                    summary = client.get("/api/summary").json()
                    self.assertTrue(summary["has_data"])
                    self.assertTrue(summary["is_demo_data"])
                    self.assertEqual(summary["stores"], 20)
                    self.assertEqual(summary["skus"], 385)
                    self.assertEqual(summary["model"]["status"], "trained")
                    moves = client.get("/api/recommendations?decision=pending").json()["items"]
                    self.assertTrue(moves)
                    self.assertEqual(moves[0]["from_location"], "WH-KOL")
                    stores = client.get("/api/stores").json()["items"]
                    lucknow = next(row for row in stores if row["store_id"] == "LKO-01")
                    self.assertIn("Lucknow", lucknow["address"])
                    self.assertIn(lucknow["sell_through_grade"], {"A", "B", "C", "WATCH", "UNKNOWN"})
                    self.assertNotEqual(lucknow["sell_through_grade"], lucknow["tier"])
                    expected_store_stock = sum(
                        row["on_hand"] + row["in_transit"]
                        for row in api.state["tables"]["stock_snapshot"]
                        if row["location_id"] == "LKO-01"
                    )
                    self.assertEqual(lucknow["stock_units"], expected_store_stock)
                    self.assertTrue(lucknow["store_locator_url"])
                    self.assertTrue(moves[0]["to_store_address"])
                    self.assertTrue(moves[0]["to_store_maps_url"])
                    all_moves = client.get("/api/recommendations?limit=100").json()["items"]
                    store_transfer = next(
                        move for move in all_moves
                        if move["move_type"] == "STORE_TO_STORE"
                        and move["from_location"] == "GAY-02"
                        and move["to_location"] == "GAY-01"
                    )
                    self.assertGreater(store_transfer["sell_probability_pct"], 0)
                    self.assertIn("Bazaza Road", store_transfer["from_store_address"])
                    self.assertIn("G.B. Road", store_transfer["to_store_address"])
                    self.assertTrue(store_transfer["route_maps_url"])
                    focused_product = client.get(
                        f"/api/products/{store_transfer['sku_id']}?store_id=GAY-02"
                    ).json()
                    self.assertEqual(
                        focused_product["placement_suggestion"]["source_location"], "GAY-02",
                    )
                    self.assertEqual(
                        focused_product["placement_suggestion"]["destination_location"], "GAY-01",
                    )
                    products = client.get("/api/products?limit=500").json()["items"]
                    self.assertEqual(len(products), 385)
                    for product in products:
                        self.assertIn(product["sell_through_grade"], {
                            "A", "B", "C", "WATCH", "UNKNOWN", "N/A",
                        })
                        suggestion = product["placement_suggestion"]
                        self.assertIn(suggestion["status"], {
                            "ready_for_review", "placement_lead", "monitor", "not_applicable",
                        })
                        if suggestion.get("destination_location"):
                            self.assertIn(suggestion["destination_location"], {
                                store["store_id"] for store in stores
                            })
            finally:
                api.state.clear()
                api.state.update(previous)

    def test_workbook_upload_calculates_and_exposes_summary(self):
        source = api_fixture()
        content = workbook_bytes(source)
        previous = dict(api.state)
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            with patch.object(api, "DATA_DIR", data_dir), \
                    TestClient(api.app) as client:
                response = client.post(
                    "/api/upload",
                    files={"file": ("sample.xlsx", content,
                                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
                )
                self.assertEqual(response.status_code, 200, response.text)
                self.assertTrue(response.json()["has_data"])
                self.assertEqual(response.json()["stores"], 2)
                self.assertEqual(client.get("/api/summary").status_code, 200)
                self.assertEqual(client.get("/api/stores").json()["items"][0]["store_id"], "DON")
                catalog = client.get("/api/products?sort=units&limit=5").json()
                self.assertEqual(catalog["total"], 1)
                self.assertEqual(catalog["items"][0]["sku_id"], "KUR-01-M")
                self.assertIn("Fabric, colour, and care details", catalog["items"][0]["description"])
                product = client.get("/api/products/KUR-01-M").json()
                self.assertEqual(len(product["product"]["store_performance"]), 2)
                self.assertEqual(len(product["sales_trend"]), 3)
                self.assertEqual(client.get("/api/stores/REC").status_code, 200)
                self.assertEqual(client.get(
                    "/api/stores/REC",
                    headers={"x-stockmover-role": "area_manager",
                             "x-stockmover-area-manager": "AM-DON"},
                ).status_code, 403)
                area_headers = {"x-stockmover-role": "area_manager",
                                "x-stockmover-area-manager": "AM-REC"}
                scoped_product = client.get(
                    "/api/products/KUR-01-M", headers=area_headers,
                ).json()["product"]
                self.assertEqual(len(scoped_product["store_performance"]), 1)
                self.assertEqual(scoped_product["total_sold_units"], 18)
                self.assertEqual(client.get("/api/summary", headers=area_headers).json()["stores"], 1)
                bills = client.get("/api/bill-analysis?store_id=REC").json()
                self.assertEqual(bills["summary"]["bills"], 1)
                pending = client.get("/api/recommendations?decision=pending").json()["items"]
                self.assertTrue(pending)
                with patch.dict("os.environ", {"GEMINI_API_KEY": "", "GOOGLE_API_KEY": ""}):
                    local_answer = client.post(
                        "/api/ask", json={"question": "What should move next?"},
                    )
                self.assertEqual(local_answer.status_code, 200, local_answer.text)
                self.assertEqual(local_answer.json()["provider"], "local_computed")
                self.assertIn(pending[0]["sku_id"], local_answer.json()["answer"])
                with patch.dict("os.environ", {"GEMINI_API_KEY": "test-key"}), \
                        patch("backend.main.urllib.request.urlopen",
                              side_effect=api.urllib.error.URLError("offline")):
                    fallback_answer = client.post(
                        "/api/ask", json={"question": "What is in the warehouse?"},
                    )
                self.assertEqual(fallback_answer.status_code, 200, fallback_answer.text)
                self.assertEqual(fallback_answer.json()["provider"], "local_computed")
                self.assertIn("fallback_reason", fallback_answer.json())
                logistics = client.get("/api/logistics").json()
                candidate = logistics["next_move"]
                self.assertEqual(candidate["id"], pending[0]["id"])
                self.assertEqual(logistics["ongoing"], [])
                decision = client.patch(
                    f"/api/recommendations/{candidate['id']}",
                    json={"status": "approved", "note": "Reviewed by merchandising"},
                    headers={"x-stockmover-user": "Rule Validator",
                             "x-stockmover-role": "validator"},
                )
                self.assertEqual(decision.status_code, 422)
                decision = client.patch(
                    f"/api/recommendations/{candidate['id']}",
                    json={"status": "approved", "validated": True,
                          "validated_qty": candidate["qty"],
                          "note": "Reviewed by merchandising"},
                    headers={"x-stockmover-user": "Rule Validator",
                             "x-stockmover-role": "validator"},
                )
                self.assertEqual(decision.status_code, 200, decision.text)
                self.assertEqual(decision.json()["item"]["decision"], "approved")
                self.assertEqual(len(client.get("/api/logistics").json()["ongoing"]), 1)
                wrong_receipt = client.post(
                    f"/api/logistics/{candidate['id']}/complete",
                    json={"received_qty": candidate["qty"] - 1,
                          "sku_verified": True, "route_verified": True},
                    headers={"x-stockmover-user": "Rule Validator",
                             "x-stockmover-role": "validator"},
                )
                self.assertEqual(wrong_receipt.status_code, 422)
                unchecked_receipt = client.post(
                    f"/api/logistics/{candidate['id']}/complete",
                    json={"received_qty": candidate["qty"],
                          "sku_verified": True, "route_verified": False},
                    headers={"x-stockmover-user": "Rule Validator",
                             "x-stockmover-role": "validator"},
                )
                self.assertEqual(unchecked_receipt.status_code, 422)
                completed = client.post(
                    f"/api/logistics/{candidate['id']}/complete",
                    json={"received_qty": candidate["qty"],
                          "sku_verified": True, "route_verified": True,
                          "note": "Receipt checked against the move"},
                    headers={"x-stockmover-user": "Rule Validator",
                             "x-stockmover-role": "validator"},
                )
                self.assertEqual(completed.status_code, 200, completed.text)
                completed_list = client.get("/api/logistics").json()["completed"]
                self.assertEqual(len(completed_list), 1)
                self.assertEqual(completed_list[0]["completion"]["received_qty"], candidate["qty"])
                self.assertEqual(completed_list[0]["completion"]["actor"], "Rule Validator")
                current_moves = api.state["moves"]
                api.state["moves"] = []
                try:
                    recovered = client.get("/api/logistics").json()["completed"]
                    self.assertEqual(len(recovered), 1)
                    self.assertEqual(recovered[0]["id"], candidate["id"])
                finally:
                    api.state["moves"] = current_moves
                persisted = api.state["approvals"][candidate["id"]]
                self.assertEqual(persisted["actor"], "Rule Validator")
                self.assertIn("completed_at", persisted["completion"])
                self.assertIn("move_snapshot", persisted)
                with sqlite3.connect(data_dir / "manyavar.sqlite3") as connection:
                    events = connection.execute(
                        "SELECT event_type FROM workflow_audit WHERE move_id = ? ORDER BY id",
                        (candidate["id"],),
                    ).fetchall()
                self.assertEqual(
                    [row[0] for row in events],
                    ["decision_approved", "logistics_completed"],
                )
        api.state.clear()
        api.state.update(previous)

    def test_legacy_json_decisions_migrate_to_sqlite(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            data_dir.joinpath("decisions.json").write_text(json.dumps({
                "move-1": {"status": "approved", "updated_at": "2026-10-31T12:00:00Z"},
            }), encoding="utf-8")
            with patch.object(api, "DATA_DIR", data_dir):
                decisions = api._read_decisions()
            self.assertEqual(decisions["move-1"]["status"], "approved")
            with sqlite3.connect(data_dir / "manyavar.sqlite3") as connection:
                row = connection.execute(
                    "SELECT payload_json FROM workflow_decisions WHERE move_id = ?",
                    ("move-1",),
                ).fetchone()
            self.assertEqual(json.loads(row[0])["status"], "approved")

    def test_synthetic_demo_matches_network_and_catalogue_size(self):
        tables = build_demo_tables()
        self.assertEqual(len(tables["stores"]), 20)
        self.assertEqual(len(tables["area_managers"]), 6)
        self.assertEqual(len(tables["items"]), 385)
        self.assertEqual(len(tables["salespeople"]), 100)
        self.assertGreater(len(tables["stock_weekly"]), 10_000)
        self.assertTrue(all(
            row["store_name"]
            and row["address"]
            and row["location_source"] == "Manyavar official store locator"
            and row["store_locator_url"].startswith("https://www.manyavar.com/")
            and row["google_maps_url"].startswith("https://www.google.com/maps/dir/")
            for row in tables["stores"]
        ))


if __name__ == "__main__":
    unittest.main()
