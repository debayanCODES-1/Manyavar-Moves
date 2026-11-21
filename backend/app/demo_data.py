"""Deterministic, synthetic Manyavar-shaped data for an immediately usable demo."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any
from urllib.parse import urlencode


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
    official_outlets = {
        "Lucknow": [
            ("Manyavar - Hazratganj", "Shop no 1, plot 31/56, 48, MG Road, Hazratganj, Lucknow, Uttar Pradesh 226001",
             "https://www.manyavar.com/en-in/store-india/uttar-pradesh/lucknow/manyavar-hazratganj-VFPLAFHG",
             "ChIJJYEmern9mzkRquhsbkWCsIg"),
            ("Manyavar and Mohey - Shahnajaf Road", "Rohit House, 1 Shahnajaf Road, Hazratganj, Lucknow, Uttar Pradesh 226001",
             "https://www.manyavar.com/en-in/store-india/uttar-pradesh/lucknow/manyavar-and-mohey-shahnajaf-rd-VFPLAAHG",
             "ChIJMRtSLQf9mzkRDalGLr-4OIE"),
            ("Manyavar - Aminabad", "166/3, Opp. Jhanday Wala Park, Aminabad, Lucknow, Uttar Pradesh 226018",
             "https://www.manyavar.com/en-in/store-india/uttar-pradesh/lucknow/manyavar-aminabad-VFPLHLA2",
             "ChIJaSNS3Vj9mzkRunbBZMMYKK8"),
            ("Manyavar - Kapoorthala Crossing Aliganj", "B 14, Sector F, Kapoorthala Crossing, Aliganj, Lucknow, Uttar Pradesh 226024",
             "https://www.manyavar.com/en-in/store-india/uttar-pradesh/lucknow/manyavar-kapoorthala-crossing-aliganj-VFPLAEKT",
             "ChIJA-jeFT1XmTkREc7AmjalQA8"),
        ],
        "Kanpur": [
            ("Manyavar and Mohey - Govind Nagar", "126/13/357-359, Chawla Market, Kanpur, Uttar Pradesh 208006",
             "https://www.manyavar.com/en-in/store-india/uttar-pradesh/kanpur/manyavar-and-mohey-govind-nagar-VFPLJSGN",
             "ChIJU7_ecN5HnDkR8odHvvJQfPE"),
            ("Manyavar and Mohey - PPN Market", "14/125, Civil Line Road, The Mall, PPN Market, Kanpur, Uttar Pradesh 208001",
             "https://www.manyavar.com/en-in/store-india/uttar-pradesh/kanpur/manyavar-and-mohey-ppn-market-VFPLJSK2",
             "ChIJR_o8rTw5nDkRbEt1tDFCipU"),
            ("Manyavar - Naveen Market", "Stall No. 13-14, Naveen Market, Kanpur, Uttar Pradesh 208001",
             "https://www.manyavar.com/en-in/store-india/uttar-pradesh/kanpur/manyavar-naveen-market-VFPLBCN3",
             "ChIJBbuqaKE4nDkR-7lTx54_EPI"),
        ],
        "Varanasi": [
            ("Manyavar - Shastri Nagar Chowraha", "Shop No. 12, 24, Sigra Crossing, Shastri Nagar, Sigra, Varanasi, Uttar Pradesh 221002",
             "https://www.manyavar.com/en-in/store-india/uttar-pradesh/varanasi/manyavar-shastri-nagar-chowraha-VFPLSRSS",
             "ChIJ49WljPwtjjkROXVwS43azXA"),
            ("Manyavar and Mohey - Sigra", "No. D 57/60B, Sigra, Varanasi, Uttar Pradesh 221010",
             "https://www.manyavar.com/en-in/store-india/uttar-pradesh/varanasi/manyavar-and-mohey-sigra-VFPLSR2S",
             "ChIJWUvSzUItjjkRBGFredX1Y2s"),
            ("Manyavar and Mohey - Maldahiya", "C 27/274 A-A1, Jai Singh Building, Maldahiya, Varanasi, Uttar Pradesh 221002",
             "https://www.manyavar.com/en-in/store-india/uttar-pradesh/varanasi/manyavar-and-mohey-maldahiya-VFPLSRS2",
             "ChIJzdLFmgkujjkRkXf_-HLdoVA"),
        ],
        "Patna": [
            ("Manyavar and Mohey - New Dak Bunglow Road", "G-4, N.P. Center, Patna, Bihar 800001",
             "https://www.manyavar.com/en-in/store-india/bihar/patna/manyavar-and-mohey-new-dak-bunglow-road-VFPLVFRA",
             "ChIJOxPpwVxY7TkRwK9QWy77pbE"),
            ("Manyavar and Mohey - Kankarbagh", "Holding No. 3015, Ward No. 35, Kankarbagh Circle-8A, Patna, Bihar 800020",
             "https://www.manyavar.com/en-in/store-india/bihar/patna/manyavar-and-mohey-kankarbagh-VFPLSSKB",
             "ChIJN9s55vBZ7TkR-ycsMrWfUPw"),
            ("Manyavar and Mohey - Boring Road", "1st & 2nd Floor, Savitri Commercial Complex, Boring Road, Patna, Bihar 800001",
             "https://www.manyavar.com/en-in/store-india/bihar/patna/manyavar-and-mohey-boring-road-1-VFPLVFUK",
             "ChIJedxqaTlZ7TkRpvhnwheLED8"),
            ("Manyavar and Mohey - City Centre Mall", "Unit CCPTC0201 & C0202, Second Floor, Block C, Utkarsh Sfatik Limited, Budh Marg, Lodipur, Patna, Bihar 800001",
             "https://www.manyavar.com/en-in/store-india/bihar/patna/manyavar-and-mohey-city-centre-mall-patna-2-VFPLLSP2",
             "ChIJ98FOtJg4nDkRo9LFr0LrfQc"),
        ],
        "Gaya": [
            ("Manyavar and Mohey - GB Road", "Opposite East Gate of Zila School, beside Bata showroom, G.B. Road, Gaya, Bihar 823001",
             "https://www.manyavar.com/en-in/store-india/bihar/gaya/manyavar-and-mohey-gb-road-VFPLRMTG",
             "ChIJhWpeV78r8zkRzlhZmDLpr44"),
            ("Manyavar - Bazaza Road", "Manyavar showroom, near Chhatta Masjid, 1st Floor, Bazaza Road, Gaya, Bihar 823001",
             "https://www.manyavar.com/en-in/store-india/bihar/gaya/manyavar-bazaza-road-VFPLVFG1",
             "ChIJg_D67T0q8zkRNHw8I_QUTF0"),
        ],
        "Bengaluru": [
            ("Manyavar - Basavanagudi", "Shop No. G-1, #123, Prasanna Complex, Dr. DVG Road, Basavanagudi, Bengaluru, Karnataka 560004",
             "https://www.manyavar.com/en-in/store-india/karnataka/bengaluru/manyavar-basavanagudi-VFPLPRDR",
             "ChIJEwAAAO0VrjsRXuwGfEfTB9c"),
            ("Manyavar - Mantri Mall", "Mantri Square, Shop No. 57, First Floor, No. 1 Sampige Road, Malleshwaram, Bengaluru, Karnataka 560003",
             "https://www.manyavar.com/en-in/store-india/karnataka/bengaluru/manyavar-mantri-mall-VFPLVFMM",
             "ChIJ74cnACMWrjsRLDKc1i7_EJ8"),
            ("Manyavar and Mohey - Jaya Nagar", "No. 80 (Old No. 391), SMS Complex, 11th Main, 3rd Block, Jayanagar, Bengaluru, Karnataka 560011",
             "https://www.manyavar.com/en-in/store-india/karnataka/bengaluru/manyavar-and-mohey-jaya-nagar-VFPLNM03",
             "ChIJHUOxfp4VrjsRB7sFxbw9mD4"),
            ("Manyavar and Mohey - Jaya Nagar 4th Block", "No. 15, 11th Main Road, 4th Block, Jayanagar, Bengaluru, Karnataka 560011",
             "https://www.manyavar.com/en-in/store-india/karnataka/bengaluru/manyavar-and-mohey-jaya-nagar-VFPLMHVJ",
             "ChIJPcjOqBQVrjsRWlI66Muy2iU"),
        ],
    }
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
            outlet_name, address, locator_url, maps_place_id = official_outlets[city][local_index - 1]
            stores.append({
                "store_id": store_id,
                "store_name": outlet_name,
                "address": address,
                "location_source": "Manyavar official store locator",
                "store_locator_url": locator_url,
                "google_maps_url": "https://www.google.com/maps/dir/?" + urlencode({
                    "api": "1",
                    "destination": address,
                    "destination_place_id": maps_place_id,
                }),
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
        fabric = {
            "Sherwani": "Silk Blend",
            "Indo-Western": "Art Silk",
            "Kurta Set": "Cotton Silk",
            "Jacket": "Raw Silk",
            "Mojari": "Leather",
            "Safa": "Cotton",
            "Mala": "Bead",
            "Stole": "Silk",
            "Packaging": "Recycled Paper",
        }[category]
        cloth_type = {
            "Sherwani": "Sherwani",
            "Indo-Western": "Indo-Western",
            "Kurta Set": "Kurta Set",
            "Jacket": "Layered Jacket",
            "Mojari": "Footwear",
            "Safa": "Headwear",
            "Mala": "Jewellery",
            "Stole": "Drape",
            "Packaging": "Packaging",
        }[category]
        colour = ("Ivory", "Maroon", "Teal", "Gold", "Midnight")[style_number % 5]
        for size in sizes:
            sku_id = f"{style_id}-{size}"
            mrp = base_price + (style_number % 9) * 250 if base_price else 0
            item = {
                "sku_id": sku_id,
                "style_id": style_id,
                "style_name": f"Demo {category} Style {style_number:02}",
                "category": category,
                "size": size,
                "cloth_type": cloth_type,
                "fabric": fabric,
                "material": fabric,
                "colour": colour,
                "care": "Dry clean" if category not in {"Mojari", "Packaging"} else "Wipe clean",
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

    # Keep one synthetic stale-stock case with proven local demand visible in the demo.
    rebalance_item = next(
        item for item in items
        if item["category"] == "Kurta Set" and item["size"] == "M"
    )
    rebalance_sku = rebalance_item["sku_id"]
    bill_store = {bill["bill_id"]: bill["store_id"] for bill in bills}
    bill_lines = [
        line for line in bill_lines
        if not (line["sku_id"] == rebalance_sku
                and bill_store.get(line["bill_id"]) == "GAY-02")
    ]
    for row in stock_snapshot:
        if row["sku_id"] != rebalance_sku:
            continue
        if row["location_id"] == "GAY-01":
            row.update(on_hand=0, in_transit=0, oldest_stock_received_on="")
        elif row["location_id"] == "GAY-02":
            row.update(on_hand=40, in_transit=0,
                       oldest_stock_received_on=(as_of - timedelta(days=183)).isoformat())
        elif row["location_id"] == "WH-KOL":
            row.update(on_hand=0, in_transit=0)

    week_ends = sorted({row["week_ending"] for row in stock_weekly})
    stock_weekly = [
        row for row in stock_weekly
        if not (row["sku_id"] == rebalance_sku
                and row["store_id"] in {"GAY-01", "GAY-02"})
    ]
    for week_end in week_ends:
        for store_id, closing_stock in (("GAY-01", 0), ("GAY-02", 40)):
            stock_weekly.append({
                "week_ending": week_end,
                "store_id": store_id,
                "sku_id": rebalance_sku,
                "closing_on_hand": closing_stock,
                "days_in_stock": 7,
            })
    for index, week_end in enumerate(week_ends[-6:], start=1):
        bill_id = f"DEMO-REBAL-{index:02}"
        bills.append({
            "bill_id": bill_id,
            "bill_date": (date.fromisoformat(week_end) - timedelta(days=2)).isoformat(),
            "store_id": "GAY-01",
            "salesperson_id": "DEMO-15-01",
            "customer_id": f"DEMO-C-REBAL-{index:02}",
        })
        bill_lines.append({
            "bill_id": bill_id,
            "sku_id": rebalance_sku,
            "qty": 5,
            "mrp": rebalance_item["mrp"],
            "selling_price": round(rebalance_item["mrp"] * 0.9),
        })

    for sku_index, item in enumerate(items):
        stock_snapshot.append({
            "as_of_date": as_of.isoformat(),
            "location_id": "WH-KOL",
            "sku_id": item["sku_id"],
            "on_hand": 0 if item["sku_id"] == rebalance_sku else 8 + sku_index % 17,
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
    warehouse_inbound = [
        row for row in warehouse_inbound if row["sku_id"] != rebalance_sku
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
