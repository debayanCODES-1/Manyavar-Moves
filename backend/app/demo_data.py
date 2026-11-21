"""Deterministic, synthetic Manyavar-shaped data for an immediately usable demo."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any


def build_demo_tables() -> dict[str, list[dict[str, Any]]]:
    categories = [
        ("Sherwani", "SHR", 8999),
        ("Indo-Western", "IW", 7499),
        ("Kurta Set", "KUR", 3999),
        ("Jacket", "JKT", 4999),
        ("Mojari", "MOJ", 2199),
        ("Safa", "SAF", 1299),
        ("Mala", "MAL", 999),
        ("Stole", "STL", 1599),
        ("Packaging", "PKG", 0),
    ]
    sizes = ("S", "M", "L", "XL", "XXL")
    city_rows = [
        ("Lucknow", "Uttar Pradesh", "North", "AM-01", 4),
        ("Kanpur", "Uttar Pradesh", "North", "AM-02", 3),
        ("Varanasi", "Uttar Pradesh", "North", "AM-03", 3),
        ("Patna", "Bihar", "East", "AM-04", 4),
        ("Gaya", "Bihar", "East", "AM-05", 2),
        ("Bengaluru", "Karnataka", "South", "AM-06", 4),
    ]
    stores: list[dict[str, Any]] = []
    area_managers: list[dict[str, Any]] = []
    store_specs = []
    store_number = 0
    for city, region, zone, manager_id, count in city_rows:
        area_managers.append({
            "area_manager_id": manager_id,
            "name": f"Demo Manager {manager_id[-2:]}",
            "city": city,
            "max_alerts_per_week": 5,
        })
        for local_index in range(1, count + 1):
            store_number += 1
            prefix = {"Lucknow": "LKO", "Kanpur": "KAN", "Varanasi": "VAR",
                      "Patna": "PAT", "Gaya": "GAY", "Bengaluru": "BLR"}[city]
            tier = ("S++", "S+", "A", "B")[(store_number - 1) % 4]
            store_id = f"{prefix}-{local_index:02}"
            stores.append({
                "store_id": store_id,
                "city": city,
                "state": region,
                "zone": zone,
                "tier": tier,
                "size_sqft": 1800 + (store_number % 6) * 650,
                "stock_capacity_units": 2000 + (store_number % 5) * 250,
                "opened_on": "2018-05-01",
                "area_manager_id": manager_id,
            })
            store_specs.append((store_number - 1, store_id, tier, manager_id))

    items: list[dict[str, Any]] = []
    for style_number in range(1, 78):
        category, prefix, base_price = categories[(style_number - 1) % len(categories)]
        style_id = f"{prefix}-{style_number:02}"
        for size in sizes:
            sku_id = f"{style_id}-{size}"
            mrp = base_price + (style_number % 9) * 250 if base_price else 0
            item = {
                "sku_id": sku_id,
                "style_id": style_id,
                "style_name": f"Demo {category} Style {style_number:02}",
                "category": category,
                "size": size,
                "mrp": mrp,
                "cost_price": round(mrp * 0.38),
                "launch_date": "2025-08-01",
                "is_core_style": int(style_number % 3 != 0),
                "is_packaging": int(category == "Packaging"),
            }
            items.append(item)

    display_categories = {
        "Accessories (Safa/Mala/Stole)" if category in {"Safa", "Mala", "Stole"} else category
        for category, _, _ in categories
        if category != "Packaging"
    }
    minimums = [
        {"tier": tier, "category": category, "min_display_units": 2 if tier == "S++" else 1}
        for tier in ("S++", "S+", "A", "B")
        for category in sorted(display_categories)
    ]
    protected = []
    for _, store_id, tier, _ in store_specs:
        if tier == "S++":
            protected.extend({
                "store_id": store_id, "rank": rank, "style_id": item["style_id"],
            } for rank, item in enumerate(items[::5][:20], start=1))

    as_of = date(2026, 10, 31)
    week_ends = [date(2026, 8, 15) + timedelta(days=7 * index) for index in range(12)]
    stock_snapshot: list[dict[str, Any]] = []
    stock_weekly: list[dict[str, Any]] = []
    bills: list[dict[str, Any]] = []
    bill_lines: list[dict[str, Any]] = []
    created_bill_ids: set[str] = set()
    footfall_daily: list[dict[str, Any]] = []
    roster_daily: list[dict[str, Any]] = []
    salespeople: list[dict[str, Any]] = []
    for store_index, store_id, _, manager_id in store_specs:
        staff_ids = [f"DEMO-{store_index + 1:02}-{n:02}" for n in range(1, 6)]
        for staff_index, salesperson_id in enumerate(staff_ids):
            salespeople.append({
                "salesperson_id": salesperson_id,
                "store_id": store_id,
                "name": f"Demo Associate {store_index + 1:02}-{staff_index + 1:02}",
                "join_date": "2024-11-01",
            })

        for sku_index, item in enumerate(items):
            seed = (store_index * 17 + sku_index * 13) % 101
            has_stock_history = (store_index * 7 + sku_index) % 5 == 0
            on_hand = 0 if seed % 11 < 3 else 1 + seed % 9
            oldest_age = 35 + (seed * 19) % 170
            stock_snapshot.append({
                "as_of_date": as_of.isoformat(),
                "location_id": store_id,
                "sku_id": item["sku_id"],
                "on_hand": on_hand,
                "in_transit": int((seed + 2) % 23 == 0),
                "in_transit_arrives_on": "2026-11-03" if (seed + 2) % 23 == 0 else "",
                "oldest_stock_received_on": (as_of - timedelta(days=oldest_age)).isoformat()
                if on_hand else "",
            })
            if not has_stock_history:
                continue
            for week_index, week_end in enumerate(week_ends):
                week_seed = (store_index * 7 + sku_index * 3 + week_index * 5) % 97
                in_stock_days = 0 if week_seed % 13 == 0 else 7
                closing_stock = max(0, min(18, on_hand + (week_index % 5) - 2))
                stock_weekly.append({
                    "week_ending": week_end.isoformat(),
                    "store_id": store_id,
                    "sku_id": item["sku_id"],
                    "closing_on_hand": closing_stock,
                    "days_in_stock": in_stock_days,
                })
                if in_stock_days and week_seed % 7 < 3:
                    quantity = 1 + week_seed % 3
                    bill_id = f"DEMO-{store_index + 1:02}-{week_index + 1:02}"
                    if bill_id not in created_bill_ids:
                        bills.append({
                            "bill_id": bill_id,
                            "bill_date": (week_end - timedelta(days=2)).isoformat(),
                            "store_id": store_id,
                            "salesperson_id": staff_ids[week_seed % len(staff_ids)],
                            "customer_id": f"DEMO-C-{store_index + 1:02}-{week_index + 1:02}",
                        })
                        created_bill_ids.add(bill_id)
                    bill_lines.append({
                        "bill_id": bill_id,
                        "sku_id": item["sku_id"],
                        "qty": quantity,
                        "mrp": item["mrp"],
                        "selling_price": round(item["mrp"] * (0.9 if week_seed % 4 == 0 else 1)),
                    })

    for sku_index, item in enumerate(items):
        stock_snapshot.append({
            "as_of_date": as_of.isoformat(),
            "location_id": "WH-KOL",
            "sku_id": item["sku_id"],
            "on_hand": 8 + sku_index % 17,
            "in_transit": 0,
            "in_transit_arrives_on": "",
            "oldest_stock_received_on": (as_of - timedelta(days=18 + sku_index % 60)).isoformat(),
        })

    calendar = []
    for day_offset in range(84):
        current_day = as_of - timedelta(days=day_offset)
        for region in ("Uttar Pradesh", "Bihar", "Karnataka"):
            calendar.append({
                "date": current_day.isoformat(),
                "state": region,
                "festival": "Demo festival" if (day_offset + len(region)) % 23 == 0 else "",
                "is_wedding_date": int(day_offset % 6 == 0),
            })
        for store_index, (_, store_id, _, _) in enumerate(store_specs):
            footfall_daily.append({
                "date": current_day.isoformat(),
                "store_id": store_id,
                "footfall": 24 + (day_offset * 3 + store_index * 7) % 58,
            })
            for staff_index in range(5):
                roster_daily.append({
                    "date": current_day.isoformat(),
                    "store_id": store_id,
                    "salesperson_id": f"DEMO-{store_index + 1:02}-{staff_index + 1:02}",
                    "status": "Present" if (day_offset + staff_index + store_index) % 7 else "Off",
                })

    lead_times = [
        {"route": "Warehouse WH-KOL to store, East zone (Bihar)", "days": 3},
        {"route": "Warehouse WH-KOL to store, North zone (UP)", "days": 4},
        {"route": "Warehouse WH-KOL to store, South zone (Karnataka)", "days": 6},
        {"route": "Store to store, same city", "days": 2},
        {"route": "Store to store, same zone, different city", "days": 4},
        {"route": "Store to store, different zone", "days": 7},
    ]
    rules = [
        {"rule": "Human approval", "detail": "Moves worth more than Rs 1,00,000 at MRP require human approval."},
        {"rule": "Minimum donor cover", "detail": "Store donors retain at least 30 days of cover."},
        {"rule": "Ageing stock", "detail": "Review stock aged 120 days or more."},
    ]
    warehouse_inbound = [
        {"expected_date": "2026-11-05", "sku_id": item["sku_id"],
         "qty": 6 + index % 8, "status": "Planned"}
        for index, item in enumerate(items) if index % 7 == 0
    ]
    return {
        "stores": stores,
        "area_managers": area_managers,
        "items": items,
        "salespeople": salespeople,
        "roster_daily": roster_daily,
        "footfall_daily": footfall_daily,
        "bills": bills,
        "bill_lines": bill_lines,
        "stock_snapshot": stock_snapshot,
        "stock_weekly": stock_weekly,
        "warehouse_inbound": warehouse_inbound,
        "calendar": calendar,
        "rules": rules,
        "min_display": minimums,
        "lead_times": lead_times,
        "flagship_protected": protected,
    }
