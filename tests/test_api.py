import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from openpyxl import Workbook

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
    tables["stock_snapshot"][2]["on_hand"] = 20
    weekly_rows = []
    for row in tables["stock_weekly"]:
        for week_end in ("2026-10-17", "2026-10-24", "2026-10-31"):
            weekly_rows.append({**row, "week_ending": week_end, "days_in_stock": 7})
    tables["stock_weekly"] = weekly_rows
    return tables


class APIUploadTests(unittest.TestCase):
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
        self.assertFalse(store["unsold_with_exposure"])

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

    def test_workbook_upload_calculates_and_exposes_summary(self):
        source = api_fixture()
        content = workbook_bytes(source)
        previous = dict(api.state)
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            with patch.object(api, "DATA_DIR", data_dir), \
                    patch.object(api, "DECISIONS_PATH", data_dir / "decisions.json"), \
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
                decision = client.patch(
                    f"/api/recommendations/{pending[0]['id']}",
                    json={"status": "approved", "note": "Reviewed by merchandising"},
                    headers={"x-stockmover-user": "Rule Validator",
                             "x-stockmover-role": "validator"},
                )
                self.assertEqual(decision.status_code, 200, decision.text)
                self.assertEqual(decision.json()["item"]["decision"], "approved")
                persisted = api.state["approvals"][pending[0]["id"]]
                self.assertEqual(persisted["actor"], "Rule Validator")
        api.state.clear()
        api.state.update(previous)


if __name__ == "__main__":
    unittest.main()
