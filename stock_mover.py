#!/usr/bin/env python3
"""Build a prioritized, rule-checked stock movement list from the hackathon data."""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any


WAREHOUSE = "WH-KOL"
DEFAULT_AGE_DAYS = 120
DEFAULT_MIN_COVER_DAYS = 30


def key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")


def as_number(value: Any, default: float = 0) -> float:
    if value in (None, ""):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def as_date(value: Any) -> date | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()[:10]
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return None


def load_tables(source: Path) -> dict[str, list[dict[str, Any]]]:
    tables: dict[str, list[dict[str, Any]]] = {}
    if source.is_dir():
        for path in sorted(source.glob("*.csv")):
            with path.open(newline="", encoding="utf-8-sig") as file:
                tables[key(path.stem)] = [
                    {key(k): v for k, v in row.items()} for row in csv.DictReader(file)
                ]
    elif source.suffix.lower() == ".xlsx":
        try:
            from openpyxl import load_workbook
        except ImportError as exc:
            raise ValueError("Reading .xlsx requires openpyxl; install it with `pip install openpyxl`.") from exc
        workbook = load_workbook(source, read_only=True, data_only=True)
        for sheet in workbook.worksheets:
            rows = sheet.iter_rows(values_only=True)
            headers = next(rows, ())
            tables[key(sheet.title)] = [
                {key(name): value for name, value in zip(headers, row) if name is not None}
                for row in rows
                if any(value is not None for value in row)
            ]
    else:
        raise ValueError("Input must be an .xlsx workbook or a directory of CSV files.")

    aliases = {
        "stock": "stock_snapshot",
        "stock_snapshots": "stock_snapshot",
        "min_display_rules": "min_display",
        "flagship_protections": "flagship_protected",
        "warehouse_deliveries": "warehouse_inbound",
    }
    for alias, canonical in aliases.items():
        if alias in tables and canonical not in tables:
            tables[canonical] = tables[alias]
    return tables


def require_tables(tables: dict[str, list[dict[str, Any]]]) -> None:
    required = ("stores", "area_managers", "items", "bills", "bill_lines", "stock_snapshot",
                "stock_weekly", "rules", "min_display", "lead_times", "flagship_protected")
    missing = [name for name in required if name not in tables]
    if missing:
        raise ValueError("Missing required table(s): " + ", ".join(missing))


def field(row: dict[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        normalized = key(name)
        if normalized in row and row[normalized] not in (None, ""):
            return row[normalized]
    return default


def min_display_by_tier(rows: list[dict[str, Any]]) -> dict[tuple[str, str], int]:
    result = {}
    for row in rows:
        tier = str(field(row, "tier", "store_tier", default="")).strip()
        category = str(field(row, "category", default="")).strip()
        units = field(row, "min_display_units", "minimum_display_units", "minimum_units",
                      "min_units", "units", "min_display")
        if tier and category and units is not None:
            result[tier, category] = int(as_number(units))
    return result


def display_category(value: Any) -> str:
    category = str(value or "").strip()
    if category.casefold() in {"safa", "mala", "stole"}:
        return "Accessories (Safa/Mala/Stole)"
    return category


def approval_threshold_from_rules(rows: list[dict[str, Any]]) -> int:
    for row in rows:
        rule = str(field(row, "rule", "name", default="")).casefold()
        detail = str(field(row, "detail", "description", default=""))
        if "approval" not in rule:
            continue
        match = re.search(r"(?:rs\.?|₹)\s*([\d,]+)", detail, re.IGNORECASE)
        if match:
            return int(match.group(1).replace(",", ""))
    return 100_000


def get_lead_days(rows: list[dict[str, Any]], source_type: str, target_type: str) -> int:
    for row in rows:
        origin = key(field(row, "from", "from_type", "from_location_type",
                           "source", "source_type", "origin_type", default=""))
        destination = key(field(row, "to", "to_type", "to_location_type",
                                "destination", "destination_type", "target_type", default=""))
        route = key(field(row, "move_type", "movement_type", "route", "movement", "name", default=""))
        endpoints_match = (origin and destination
                           and key(source_type) in origin and key(target_type) in destination)
        route_matches = (route and key(source_type) in route and key(target_type) in route)
        if endpoints_match or route_matches:
            days = field(row, "lead_time_days", "lead_days", "days", "lead_time", "value")
            return max(0, int(as_number(days)))
    raise ValueError(f"No lead-time rule found for {source_type} -> {target_type}.")


def build_lead_time_overrides(tables: dict[str, list[dict[str, Any]]]) -> dict[tuple[str, str], int]:
    """Resolve lead times for routes from the single Kolkata warehouse and between stores."""
    stores = {str(field(row, "store_id", default="")): row for row in tables["stores"]}
    routes = [
        (str(field(row, "route", "move_type", default="")).casefold(),
         max(0, int(as_number(field(row, "days", "lead_time_days", "lead_days")))))
        for row in tables["lead_times"]
    ]

    def warehouse_days(zone: str) -> int:
        aliases = {
            "north": ("north", "up"),
            "east": ("east", "bihar"),
            "south": ("south", "karnataka"),
        }.get(zone.casefold(), (zone.casefold(),))
        for route, days in routes:
            if "warehouse wh-kol" in route and "to store" in route \
                    and any(alias in route for alias in aliases):
                return days
        if not zone or not any(field(row, "route", "move_type", default="")
                               for row in tables["lead_times"]):
            return get_lead_days(tables["lead_times"], "warehouse", "store")
        raise ValueError(f"No Kolkata warehouse lead-time route for zone {zone}.")

    def store_days(source: dict[str, Any], target: dict[str, Any]) -> int:
        same_city = str(field(source, "city", default="")).casefold() == str(
            field(target, "city", default="")).casefold()
        same_zone = str(field(source, "zone", default="")).casefold() == str(
            field(target, "zone", default="")).casefold()
        selector = "same city" if same_city else "same zone" if same_zone else "different zone"
        for route, days in routes:
            if "store to store" in route and selector in route:
                if selector != "same zone" or "different city" in route:
                    return days
        if not any(field(row, "route", "move_type", default="")
                   for row in tables["lead_times"]):
            return get_lead_days(tables["lead_times"], "store", "store")
        raise ValueError(f"No store-to-store lead-time route for {selector}.")

    overrides = {}
    for store_id, store in stores.items():
        outbound_days = warehouse_days(str(field(store, "zone", default="")))
        overrides[WAREHOUSE, store_id] = outbound_days
        # Return transport follows the same Kolkata-to-store lane.
        overrides[store_id, WAREHOUSE] = outbound_days
        for target_id, target in stores.items():
            if target_id != store_id:
                overrides[store_id, target_id] = store_days(store, target)
    return overrides


def build_recommendations(tables: dict[str, list[dict[str, Any]]],
                          age_threshold: int = DEFAULT_AGE_DAYS,
                          velocity_override: dict[tuple[str, str], float] | None = None,
                          lead_time_overrides: dict[tuple[str, str], int] | None = None
                          ) -> list[dict[str, Any]]:
    require_tables(tables)
    stores = {str(r["store_id"]): r for r in tables["stores"]}
    items = {str(r["sku_id"]): r for r in tables["items"]}
    snapshot = tables["stock_snapshot"]
    as_of = max((as_date(field(r, "as_of_date")) for r in snapshot if as_date(field(r, "as_of_date"))),
                default=date.today())
    weekly_dates = [as_date(field(row, "week_ending")) for row in tables["stock_weekly"]]
    weekly_dates = [value for value in weekly_dates if value is not None and value <= as_of]
    sales_start = min(weekly_dates) - timedelta(days=6) if weekly_dates else as_of - timedelta(days=89)
    sales_end = max(weekly_dates) if weekly_dates else as_of
    observation_days = (sales_end - sales_start).days + 1

    bill_dates = {}
    for bill in tables["bills"]:
        bill_id = str(field(bill, "bill_id", default=""))
        bill_date = as_date(field(bill, "bill_date", "date"))
        if bill_id and bill_date and sales_start <= bill_date <= sales_end:
            bill_dates[bill_id] = (str(field(bill, "store_id")), bill_date)

    sold = defaultdict(float)
    for line in tables["bill_lines"]:
        bill_id = str(field(line, "bill_id", default=""))
        if bill_id in bill_dates:
            store_id, _ = bill_dates[bill_id]
            sku_id = str(field(line, "sku_id", default=""))
            sold[store_id, sku_id] += as_number(field(line, "qty"))

    in_stock_days = defaultdict(float)
    for row in tables["stock_weekly"]:
        store_id = str(field(row, "store_id", default=""))
        sku_id = str(field(row, "sku_id", default=""))
        in_stock_days[store_id, sku_id] += as_number(field(row, "days_in_stock"))

    velocity: dict[tuple[str, str], float] = {}
    for store_id in stores:
        for sku_id in items:
            days = in_stock_days[store_id, sku_id]
            quantity = sold[store_id, sku_id]
            if days > 0:
                velocity[store_id, sku_id] = quantity / days
            elif quantity > 0:
                velocity[store_id, sku_id] = quantity / observation_days
            else:
                velocity[store_id, sku_id] = 0.0
    if velocity_override is not None:
        for pair, rate in velocity_override.items():
            if pair in velocity and rate >= 0:
                velocity[pair] = rate

    # Transit is included in projected stock/cover, but only on-hand units can be dispatched.
    on_hand = defaultdict(float)
    transit = defaultdict(float)
    oldest = {}
    for row in snapshot:
        location = str(field(row, "location_id", default=""))
        sku_id = str(field(row, "sku_id", default=""))
        on_hand[location, sku_id] += as_number(field(row, "on_hand"))
        transit[location, sku_id] += as_number(field(row, "in_transit"))
        received = as_date(field(row, "oldest_stock_received_on"))
        if received and (location, sku_id) not in oldest:
            oldest[location, sku_id] = received
        elif received and received < oldest[location, sku_id]:
            oldest[location, sku_id] = received

    warehouse_supply: dict[str, list[tuple[date, float]]] = defaultdict(list)
    for sku_id in items:
        if on_hand[WAREHOUSE, sku_id] > 0:
            warehouse_supply[sku_id].append((as_of, on_hand[WAREHOUSE, sku_id]))
        if transit[WAREHOUSE, sku_id] > 0:
            transit_date = next((as_date(field(r, "in_transit_arrives_on"))
                                 for r in snapshot
                                 if str(field(r, "location_id", default="")) == WAREHOUSE
                                 and str(field(r, "sku_id", default="")) == sku_id
                                 and as_number(field(r, "in_transit")) > 0), None)
            warehouse_supply[sku_id].append((transit_date or as_of, transit[WAREHOUSE, sku_id]))
    for row in tables.get("warehouse_inbound", []):
        status = key(field(row, "status", default="planned"))
        expected = as_date(field(row, "expected_date"))
        sku_id = str(field(row, "sku_id", default=""))
        if status not in {"cancelled", "canceled", "received", "rejected"} and expected and sku_id in items:
            warehouse_supply[sku_id].append((expected, as_number(field(row, "qty"))))
    for sku_id in warehouse_supply:
        warehouse_supply[sku_id].sort(key=lambda entry: entry[0])

    protected = {
        (str(field(r, "store_id")), str(field(r, "style_id")))
        for r in tables["flagship_protected"]
    }
    approval_limit = approval_threshold_from_rules(tables["rules"])
    minimums = min_display_by_tier(tables["min_display"])
    merchandise_categories = {
        display_category(field(item, "category", default="")) for item in items.values()
        if not as_number(field(item, "is_packaging"))
    }
    tiers = {str(field(store, "tier", default="")) for store in stores.values()}
    missing_floors = sorted((tier, category) for tier in tiers for category in merchandise_categories
                            if (tier, category) not in minimums)
    if missing_floors:
        raise ValueError("Missing min_display rule(s) for: " + ", ".join(
            f"{tier}/{category}" for tier, category in missing_floors))
    wh_to_store_days = get_lead_days(tables["lead_times"], "warehouse", "store")
    store_to_store_days = get_lead_days(tables["lead_times"], "store", "store")
    try:
        store_to_wh_days = get_lead_days(tables["lead_times"], "store", "warehouse")
    except ValueError:
        store_to_wh_days = wh_to_store_days

    def lead_days(source: str, target: str, default: int) -> int:
        return (lead_time_overrides or {}).get((source, target), default)

    stock = defaultdict(float)
    for store_id in stores:
        for sku_id in items:
            stock[store_id, sku_id] = on_hand[store_id, sku_id] + transit[store_id, sku_id]

    category_stock = defaultdict(float)
    for (location, sku_id), units in stock.items():
        item = items.get(sku_id, {})
        category_stock[location, display_category(field(item, "category", default=""))] += units

    def category_floor(store_id: str, category: str) -> int:
        tier = str(field(stores[store_id], "tier", default=""))
        return minimums.get((tier, category), 0)

    capacity = {
        store_id: max(0, int(as_number(field(store, "stock_capacity_units", "capacity_units")) -
                             sum(stock[store_id, sku] for sku in items)))
        for store_id, store in stores.items()
    }

    needs: dict[tuple[str, str], int] = {}
    for store_id in stores:
        for sku_id, item in items.items():
            if as_number(field(item, "is_packaging")):
                continue
            category = display_category(field(item, "category", default=""))
            rate = velocity[store_id, sku_id]
            current = stock[store_id, sku_id]
            lead = lead_days(WAREHOUSE, store_id, wh_to_store_days)
            target = int((rate * (lead + DEFAULT_MIN_COVER_DAYS)) + 0.999)
            sku_need = max(0, target - int(current))
            needs[store_id, sku_id] = sku_need

        # A category display floor is spread over its selling SKUs, fastest first.
        categories = {display_category(field(i, "category", default="")) for i in items.values()
                      if not as_number(field(i, "is_packaging"))}
        for category in categories:
            floor_gap = max(0, category_floor(store_id, category) -
                            int(category_stock[store_id, category]))
            candidates = sorted(
                (sku for sku, item in items.items()
                 if display_category(field(item, "category", default="")) == category
                 and not as_number(field(item, "is_packaging"))),
                key=lambda sku: velocity[store_id, sku], reverse=True)
            for sku_id in candidates:
                if floor_gap <= 0:
                    break
                add = floor_gap
                needs[store_id, sku_id] = needs.get((store_id, sku_id), 0) + add
                floor_gap -= add

    recommendations: list[dict[str, Any]] = []
    def priority(store_id: str, sku_id: str, lead: int) -> tuple[float, float, str]:
        rate = velocity[store_id, sku_id]
        cover = stock[store_id, sku_id] / rate if rate > 0 else float("inf")
        urgency = rate / max(1.0, stock[store_id, sku_id] + 1)
        return (0 if cover <= lead else 1, -urgency, store_id)

    def add_move(kind: str, source: str, target: str, sku_id: str, qty: int,
                 lead: int, reason: str, cover_after: float | None = None,
                 dispatch: date | None = None, donor_velocity: float | None = None,
                 age_days: int | None = None) -> None:
        dispatch = dispatch or as_of
        arrival = dispatch + timedelta(days=lead)
        move_value_mrp = qty * as_number(field(items[sku_id], "mrp"))
        recommendations.append({
            "priority": "P1" if velocity.get((target, sku_id), 0) > 0 and
                        stock[target, sku_id] / max(velocity[target, sku_id], 0.0001) <= lead else "P2",
            "move_type": kind, "from_location": source, "to_location": target,
            "sku_id": sku_id, "style_id": str(field(items[sku_id], "style_id", default="")),
            "category": str(field(items[sku_id], "category", default="")),
            "qty": int(qty), "dispatch_date": dispatch.isoformat(),
            "arrival_date": arrival.isoformat(),
            "daily_velocity": round(velocity.get((target, sku_id), 0), 3),
            "donor_daily_velocity": round(donor_velocity, 3) if donor_velocity is not None else "",
            "donor_cover_after_days": "" if cover_after is None else round(cover_after, 1),
            "stock_age_days": age_days if age_days is not None else "",
            "move_value_mrp": round(move_value_mrp, 2),
            "approval_threshold_mrp": approval_limit,
            "requires_human_approval": move_value_mrp > approval_limit,
            "reason": reason,
        })

    # Allocate scarce warehouse units to the most urgent unmet demand first.
    for sku_id in items:
        category = display_category(field(items[sku_id], "category", default=""))
        if as_number(field(items[sku_id], "is_packaging")):
            continue
        recipients = sorted(
            (store_id for store_id in stores if needs.get((store_id, sku_id), 0) > 0),
            key=lambda store_id: priority(
                store_id, sku_id, lead_days(WAREHOUSE, store_id, wh_to_store_days)))
        for store_id in recipients:
            if capacity[store_id] <= 0:
                continue
            for idx, (available_date, available_units) in enumerate(warehouse_supply[sku_id]):
                if needs[store_id, sku_id] <= 0 or capacity[store_id] <= 0:
                    break
                qty = min(int(needs[store_id, sku_id]), int(available_units), capacity[store_id])
                if qty <= 0:
                    continue
                recipient_lead = lead_days(WAREHOUSE, store_id, wh_to_store_days)
                add_move("WAREHOUSE_TO_STORE", WAREHOUSE, store_id, sku_id, qty, recipient_lead,
                         f"Cover {round(stock[store_id, sku_id] / velocity[store_id, sku_id], 1) if velocity[store_id, sku_id] else 'unknown'} "
                         f"days against {round(velocity[store_id, sku_id], 2)} units/day; highest-priority "
                         f"unmet {category} demand within store capacity.",
                         dispatch=max(as_of, available_date))
                stock[store_id, sku_id] += qty
                category_stock[store_id, category] += qty
                capacity[store_id] -= qty
                needs[store_id, sku_id] -= qty
                warehouse_supply[sku_id][idx] = (available_date, available_units - qty)

    # Rebalance safe store surplus to remaining demand; protected flagship styles never leave.
    for sku_id, item in items.items():
        if as_number(field(item, "is_packaging")):
            continue
        category = display_category(field(item, "category", default=""))
        recipients = sorted(
            (store_id for store_id in stores if needs.get((store_id, sku_id), 0) > 0),
            key=lambda store_id: priority(store_id, sku_id, store_to_store_days))
        donors = sorted(stores, key=lambda store_id: (
            oldest.get((store_id, sku_id), as_of), -on_hand[store_id, sku_id]))
        for target in recipients:
            for donor in donors:
                if donor == target or needs.get((target, sku_id), 0) <= 0 or capacity[target] <= 0:
                    continue
                style_id = str(field(item, "style_id", default=""))
                if (donor, style_id) in protected:
                    continue
                rate = velocity[donor, sku_id]
                projected = stock[donor, sku_id]
                min_safe = int(rate * DEFAULT_MIN_COVER_DAYS + 0.999) if rate > 0 else int(projected)
                category_excess = int(category_stock[donor, category]) - category_floor(donor, category)
                safe_qty = min(int(on_hand[donor, sku_id]), int(projected) - min_safe, category_excess)
                qty = min(needs[target, sku_id], safe_qty, capacity[target])
                if qty <= 0:
                    continue
                after = (projected - qty) / rate if rate > 0 else float("inf")
                if after < DEFAULT_MIN_COVER_DAYS:
                    continue
                age = (as_of - oldest[(donor, sku_id)]).days if (donor, sku_id) in oldest else None
                age_note = f"; oldest stock is {age} days old" if age is not None else ""
                donor_lead = lead_days(donor, target, store_to_store_days)
                add_move("STORE_TO_STORE", donor, target, sku_id, qty, donor_lead,
                         f"Move surplus to a faster-selling store ({round(velocity[target, sku_id], 2)} "
                         f"vs {round(rate, 2)} units/day){age_note}; donor retains "
                         f"{round(after, 1)} days of cover and its category display floor.",
                         cover_after=after, donor_velocity=rate, age_days=age)
                stock[donor, sku_id] -= qty
                category_stock[donor, category] -= qty
                on_hand[donor, sku_id] -= qty
                stock[target, sku_id] += qty
                category_stock[target, category] += qty
                capacity[target] -= qty
                needs[target, sku_id] -= qty

    # Pull back truly aged stock only after checking no receiving store can use it.
    for (store_id, sku_id), received in sorted(oldest.items()):
        item = items.get(sku_id)
        if store_id not in stores or item is None or as_number(field(item, "is_packaging")):
            continue
        age = (as_of - received).days
        if age < age_threshold:
            continue
        category = display_category(field(item, "category", default=""))
        style_id = str(field(item, "style_id", default=""))
        if (store_id, style_id) in protected:
            continue
        safe_category_units = int(category_stock[store_id, category]) - category_floor(store_id, category)
        qty = min(int(on_hand[store_id, sku_id]), safe_category_units)
        if qty <= 0:
            continue
        return_lead = lead_days(store_id, WAREHOUSE, store_to_wh_days)
        add_move("STORE_TO_WAREHOUSE", store_id, WAREHOUSE, sku_id, qty, return_lead,
                 f"Pull back {age}-day-old stock after no eligible store demand remained; "
                 f"category display minimum is preserved.", age_days=age)
        stock[store_id, sku_id] -= qty
        category_stock[store_id, category] -= qty
        on_hand[store_id, sku_id] -= qty

    validate_recommendations(recommendations, stores, items, tables, minimums)
    recommendations.sort(key=lambda move: (
        0 if move["priority"] == "P1" else 1,
        move["arrival_date"], move["move_type"], move["sku_id"], move["to_location"]))
    return recommendations


def limit_manager_alerts(recommendations: list[dict[str, Any]],
                         tables: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    stores = {str(field(row, "store_id", default="")): row for row in tables["stores"]}
    manager_limits = {
        str(field(row, "area_manager_id")): max(0, int(as_number(
            field(row, "max_alerts_per_week", "max_alerts"))))
        for row in tables["area_managers"]
        if field(row, "area_manager_id") is not None
    }
    manager_alerts = defaultdict(int)
    selected = []
    for move in recommendations:
        managers = {
            str(field(stores[location], "area_manager_id"))
            for location in (move["from_location"], move["to_location"])
            if location in stores
        }
        if any(manager not in manager_limits or manager_alerts[manager] >= manager_limits[manager]
               for manager in managers):
            continue
        selected.append(move)
        for manager in managers:
            manager_alerts[manager] += 1
    return selected


def validate_proposal_selection(moves: list[dict[str, Any]],
                                tables: dict[str, list[dict[str, Any]]]) -> None:
    stores = {str(field(row, "store_id", default="")): row for row in tables["stores"]}
    items = {str(field(row, "sku_id", default="")): row for row in tables["items"]}
    minimums = {}
    for row in tables["min_display"]:
        tier = str(field(row, "tier", "store_tier", default=""))
        category = display_category(field(row, "category", default=""))
        value = field(row, "min_display_units", "minimum_display_units", "minimum_units",
                      "min_units", "units", "min_display")
        if tier and category and value is not None:
            minimums[tier, category] = int(as_number(value))
    validate_recommendations(moves, stores, items, tables, minimums)


def validate_recommendations(moves: list[dict[str, Any]], stores: dict[str, dict[str, Any]],
                             items: dict[str, dict[str, Any]], tables: dict[str, list[dict[str, Any]]],
                             minimums: dict[tuple[str, str], int]) -> None:
    projected = defaultdict(float)
    category_totals = defaultdict(float)
    for row in tables["stock_snapshot"]:
        location = str(field(row, "location_id", default=""))
        sku = str(field(row, "sku_id", default=""))
        units = as_number(field(row, "on_hand")) + as_number(field(row, "in_transit"))
        projected[location, sku] += units
        if location in stores and sku in items:
            category_totals[location, display_category(field(items[sku], "category", default=""))] += units
    protected = {(str(field(r, "store_id")), str(field(r, "style_id")))
                 for r in tables["flagship_protected"]}
    min_by_tier = minimums
    velocities = defaultdict(float)
    sold = defaultdict(float)
    for move in moves:
        source, target, sku = move["from_location"], move["to_location"], move["sku_id"]
        qty = move["qty"]
        if sku not in items or qty <= 0:
            raise ValueError("Generated an invalid move quantity or unknown SKU.")
        if target in stores:
            projected[target, sku] += qty
            category_totals[target, display_category(move["category"])] += qty
        if source in stores:
            projected[source, sku] -= qty
            category_totals[source, display_category(move["category"])] -= qty
        if move["move_type"] == "STORE_TO_STORE":
            if (source, move["style_id"]) in protected:
                raise ValueError(f"Protected flagship style would leave {source}: {sku}")
            if projected[source, sku] < -1e-8:
                raise ValueError(f"Store {source} would dispatch more {sku} than projected stock.")
            # The engine records cover after each donor allocation; enforce the published hard limit.
            if float(move["donor_cover_after_days"]) < DEFAULT_MIN_COVER_DAYS:
                raise ValueError(f"Store-to-store move leaves donor under 30 days of cover: {sku}")
        if source in stores:
            tier = str(field(stores[source], "tier", default=""))
            category = display_category(move["category"])
            floor = min_by_tier.get((tier, category), 0)
            if category_totals[source, category] < floor:
                raise ValueError(f"Move leaves {source} below its {move['category']} display minimum.")
        if target in stores:
            cap = as_number(field(stores[target], "stock_capacity_units", "capacity_units"))
            total = sum(projected[target, item_id] for item_id in items)
            if total > cap + 1e-8:
                raise ValueError(f"Move exceeds {target} stock capacity.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="Workbook (.xlsx) or directory of table CSV files")
    parser.add_argument("-o", "--output", type=Path, default=Path("recommendations.csv"))
    parser.add_argument("--age-days", type=int, default=DEFAULT_AGE_DAYS,
                        help=f"age threshold for pullbacks (default: {DEFAULT_AGE_DAYS})")
    args = parser.parse_args(argv)
    try:
        tables = load_tables(args.input)
        moves = limit_manager_alerts(
            build_recommendations(
                tables, age_threshold=args.age_days,
                lead_time_overrides=build_lead_time_overrides(tables),
            ), tables)
        if args.output.suffix.lower() == ".json":
            args.output.write_text(json.dumps(moves, indent=2), encoding="utf-8")
        else:
            columns = ["priority", "move_type", "from_location", "to_location", "sku_id",
                       "style_id", "category", "qty", "dispatch_date", "arrival_date",
                       "daily_velocity", "donor_daily_velocity", "donor_cover_after_days",
                       "stock_age_days", "move_value_mrp",
                       "approval_threshold_mrp", "requires_human_approval", "reason"]
            with args.output.open("w", newline="", encoding="utf-8") as file:
                writer = csv.DictWriter(file, fieldnames=columns)
                writer.writeheader()
                writer.writerows(moves)
        print(f"Wrote {len(moves)} recommendations to {args.output}")
        return 0
    except (OSError, ValueError, KeyError) as exc:
        print(f"stock-mover: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
