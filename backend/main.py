"""FastAPI service for workbook ingestion, forecasts, move decisions and approvals."""

from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Header, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from stock_mover import (  # noqa: E402
    DEFAULT_AGE_DAYS, WAREHOUSE, approval_threshold_from_rules, as_date, as_number,
    build_recommendations, field, load_tables, validate_proposal_selection,
)
from backend.app.forecast import forecast_velocity  # noqa: E402
from backend.app.optimizer import optimize_proposals  # noqa: E402

DATA_DIR = Path(os.environ.get("DATA_DIR", ROOT / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DECISIONS_PATH = DATA_DIR / "decisions.json"

app = FastAPI(title="Manyavar Stock Mover API", version="1.0.0")
state: dict[str, Any] = {
    "tables": None, "moves": [], "model": {}, "optimizer": {},
    "source_name": None, "generated_at": None, "approvals": {},
    "products": {}, "sales_weekly": {}, "stock_weekly": {}, "bill_summaries": [],
    "sku_store_sales_weekly": {}, "sku_store_stock_weekly": {},
}


class Decision(BaseModel):
    status: str = Field(pattern="^(approved|rejected|pending)$")
    note: str = Field(default="", max_length=500)


def _product_description(item: dict[str, Any]) -> str:
    name = str(field(item, "style_name", default=field(item, "style_id", default="This design")))
    category = str(field(item, "category", default="occasionwear"))
    size = str(field(item, "size", default=""))
    price = as_number(field(item, "mrp"))
    assortment = "year-round core collection" if as_number(field(item, "is_core_style")) else "seasonal fashion collection"
    packaging = as_number(field(item, "is_packaging"))
    if packaging:
        return f"{name} is a packaging SKU in size {size or 'free size'}, listed at {rupee(price)}. It is tracked separately from merchandise demand."
    size_copy = f" in size {size}" if size else ""
    return (
        f"{name} is a {category} design from Manyavar's {assortment}{size_copy}. "
        f"The catalogue lists this variant at {rupee(price)} MRP. It is suited to "
        "wedding celebrations, festive gatherings, and occasion dressing. "
        "Fabric, colour, and care details are not specified in the source catalogue."
    )


def rupee(value: float) -> str:
    return f"₹{value:,.0f}"


class Question(BaseModel):
    question: str = Field(min_length=3, max_length=500)


def _read_decisions() -> dict[str, Any]:
    try:
        return json.loads(DECISIONS_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not read persisted approval decisions: {exc}") from exc


def _write_decisions() -> None:
    temporary = DECISIONS_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(state["approvals"], indent=2), encoding="utf-8")
    temporary.replace(DECISIONS_PATH)


def _read_source(path: Path) -> dict[str, list[dict[str, Any]]]:
    if path.suffix.lower() == ".zip":
        with tempfile.TemporaryDirectory(prefix="manyavar-csv-") as directory:
            folder = Path(directory)
            with zipfile.ZipFile(path) as archive:
                csv_files = [entry for entry in archive.infolist()
                             if not entry.is_dir() and entry.filename.lower().endswith(".csv")]
                if not csv_files:
                    raise ValueError("CSV archive contains no .csv tables.")
                if len(csv_files) > 40 or sum(entry.file_size for entry in csv_files) > 500 * 1024 * 1024:
                    raise ValueError("CSV archive exceeds the file-count or expanded-size limit.")
                for entry in csv_files:
                    basename = Path(entry.filename).name
                    if not basename:
                        continue
                    (folder / basename).write_bytes(archive.read(entry))
            return load_tables(folder)
    return load_tables(path)


def _route_lead_overrides(tables: dict[str, list[dict[str, Any]]]) -> dict[tuple[str, str], int]:
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
            if "warehouse" in route and "store" in route and any(alias in route for alias in aliases):
                return days
        return max((days for route, days in routes if "warehouse" in route and "store" in route),
                   default=0)

    def store_days(source: dict[str, Any], target: dict[str, Any]) -> int:
        same_city = str(field(source, "city", default="")).casefold() == str(
            field(target, "city", default="")).casefold()
        same_zone = str(field(source, "zone", default="")).casefold() == str(
            field(target, "zone", default="")).casefold()
        if same_city:
            selector = "same city"
        elif same_zone:
            selector = "same zone"
        else:
            selector = "different zone"
        for route, days in routes:
            if "store to store" in route and selector in route:
                if selector != "same zone" or "different city" in route:
                    return days
        return max((days for route, days in routes if "store to store" in route), default=0)

    overrides = {}
    for store_id, store in stores.items():
        zone = str(field(store, "zone", default=""))
        route_days = warehouse_days(zone)
        overrides[WAREHOUSE, store_id] = route_days
        # The guide contains warehouse-to-zone transit times, so return trips use the same leg.
        overrides[store_id, WAREHOUSE] = route_days
        for target_id, target in stores.items():
            if target_id != store_id:
                overrides[store_id, target_id] = store_days(store, target)
    return overrides


def _build_analytics(tables: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    items = {str(field(row, "sku_id", default="")): row for row in tables["items"]}
    stores = {str(field(row, "store_id", default="")): row for row in tables["stores"]}
    bills = {
        str(field(row, "bill_id", default="")): {
            "bill_id": str(field(row, "bill_id", default="")),
            "store_id": str(field(row, "store_id", default="")),
            "bill_date": as_date(field(row, "bill_date", "date")),
            "salesperson_id": str(field(row, "salesperson_id", default="")),
            "revenue": 0.0, "units": 0, "sku_units": defaultdict(int),
            "line_count": 0,
        }
        for row in tables["bills"]
    }
    weekly_dates = sorted({
        parsed for row in tables["stock_weekly"]
        if (parsed := as_date(field(row, "week_ending"))) is not None
    })
    as_of = max(weekly_dates, default=_date_range(tables)[1] or date.today())
    recent_start = as_of - timedelta(days=83)
    units_by_sku = defaultdict(int)
    revenue_by_sku = defaultdict(float)
    recent_units_by_sku = defaultdict(int)
    recent_revenue_by_sku = defaultdict(float)
    units_by_store_sku = defaultdict(int)
    revenue_by_store_sku = defaultdict(float)
    recent_units_by_store_sku = defaultdict(int)
    recent_revenue_by_store_sku = defaultdict(float)
    sales_weekly = defaultdict(lambda: defaultdict(int))
    store_sales_weekly = defaultdict(lambda: defaultdict(int))
    sku_store_sales_weekly = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    for line in tables["bill_lines"]:
        sku = str(field(line, "sku_id", default=""))
        bill = bills.get(str(field(line, "bill_id", default="")))
        if bill is None or sku not in items:
            continue
        if as_number(field(items[sku], "is_packaging")):
            continue
        qty = int(as_number(field(line, "qty")))
        revenue = qty * as_number(field(line, "selling_price"))
        units_by_sku[sku] += qty
        revenue_by_sku[sku] += revenue
        bill["revenue"] += revenue
        bill["units"] += qty
        bill["line_count"] += 1
        bill["sku_units"][sku] += qty
        bill_date = bill["bill_date"]
        store_id = bill["store_id"]
        if store_id in stores:
            units_by_store_sku[store_id, sku] += qty
            revenue_by_store_sku[store_id, sku] += revenue
        if bill_date:
            week_ending = bill_date + timedelta(days=(5 - bill_date.weekday()) % 7)
            week_key = week_ending.isoformat()
            sales_weekly[sku][week_key] += qty
            if store_id in stores:
                store_sales_weekly[store_id][week_key] += qty
                sku_store_sales_weekly[store_id][sku][week_key] += qty
            if recent_start <= bill_date <= as_of:
                recent_units_by_sku[sku] += qty
                recent_revenue_by_sku[sku] += revenue
                if store_id in stores:
                    recent_units_by_store_sku[store_id, sku] += qty
                    recent_revenue_by_store_sku[store_id, sku] += revenue

    snapshot = defaultdict(lambda: {"on_hand": 0, "in_transit": 0, "oldest": None})
    for row in tables["stock_snapshot"]:
        location = str(field(row, "location_id", default=""))
        sku = str(field(row, "sku_id", default=""))
        stock = snapshot[location, sku]
        stock["on_hand"] += int(as_number(field(row, "on_hand")))
        stock["in_transit"] += int(as_number(field(row, "in_transit")))
        received = as_date(field(row, "oldest_stock_received_on"))
        if received and (stock["oldest"] is None or received < stock["oldest"]):
            stock["oldest"] = received

    availability_days = defaultdict(int)
    stock_weekly = defaultdict(lambda: defaultdict(int))
    store_stock_weekly = defaultdict(lambda: defaultdict(int))
    sku_store_stock_weekly = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    for row in tables["stock_weekly"]:
        store_id = str(field(row, "store_id", default=""))
        sku = str(field(row, "sku_id", default=""))
        week = as_date(field(row, "week_ending"))
        if week is None:
            continue
        week_key = week.isoformat()
        stock_weekly[sku][week_key] += int(as_number(field(row, "closing_on_hand")))
        store_stock_weekly[store_id][week_key] += int(as_number(field(row, "closing_on_hand")))
        sku_store_stock_weekly[store_id][sku][week_key] += int(
            as_number(field(row, "closing_on_hand"))
        )
        if recent_start <= week <= as_of:
            availability_days[store_id, sku] += int(as_number(field(row, "days_in_stock")))

    product_index = {}
    for sku, item in items.items():
        store_performance = []
        store_stock_units = warehouse_units = recent_exposure_days = 0
        max_age_days = 0
        for store_id, store in stores.items():
            stock = snapshot[store_id, sku]
            on_hand = stock["on_hand"]
            in_transit = stock["in_transit"]
            projected = on_hand + in_transit
            recent_sold = recent_units_by_store_sku[store_id, sku]
            days_in_stock = availability_days[store_id, sku]
            recent_exposure_days += days_in_stock
            rate = recent_sold / days_in_stock if days_in_stock else None
            age_days = max(0, (as_of - stock["oldest"]).days) if stock["oldest"] else None
            if age_days is not None:
                max_age_days = max(max_age_days, age_days)
            store_stock_units += projected
            store_performance.append({
                "store_id": store_id,
                "city": field(store, "city", default=""),
                "tier": field(store, "tier", default=""),
                "sales_units": units_by_store_sku[store_id, sku],
                "sales_revenue": round(revenue_by_store_sku[store_id, sku], 2),
                "recent_units": recent_sold,
                "recent_revenue": round(recent_revenue_by_store_sku[store_id, sku], 2),
                "recent_in_stock_days": days_in_stock,
                "recent_velocity": round(rate, 3) if rate is not None else None,
                "on_hand": on_hand,
                "in_transit": in_transit,
                "stock_units": projected,
                "cover_days": round(projected / rate, 1) if rate and rate > 0 else None,
                "stock_age_days": age_days,
                "unsold_with_exposure": projected > 0 and recent_sold == 0 and days_in_stock > 0,
            })
        warehouse = snapshot[WAREHOUSE, sku]
        warehouse_units = warehouse["on_hand"] + warehouse["in_transit"]
        store_performance.sort(
            key=lambda row: (-row["recent_units"], -row["sales_units"], row["store_id"]))
        best_recent = next((row for row in store_performance if row["recent_units"] > 0), None)
        best_historical = next((row for row in store_performance if row["sales_units"] > 0), None)
        unsold_locations = [
            row for row in store_performance if row["unsold_with_exposure"]
        ]
        product_index[sku] = {
            **item,
            "sku_id": sku,
            "description": _product_description(item),
            "total_sold_units": units_by_sku[sku],
            "sales_revenue": round(revenue_by_sku[sku], 2),
            "recent_sold_units": recent_units_by_sku[sku],
            "recent_revenue": round(recent_revenue_by_sku[sku], 2),
            "recent_velocity": round(recent_units_by_sku[sku] / recent_exposure_days, 3)
            if recent_exposure_days else None,
            "store_stock_units": store_stock_units,
            "warehouse_units": warehouse_units,
            "network_stock_units": store_stock_units + warehouse_units,
            "aged_stock_days_max": max_age_days,
            "oldest_stock_age_days": max_age_days,
            "best_selling_store": best_historical["store_id"] if best_historical else None,
            "best_recent_store": best_recent["store_id"] if best_recent else None,
            "unsold_locations": unsold_locations,
            "store_performance": store_performance,
        }

    bill_summaries = []
    for bill in bills.values():
        if not bill["bill_date"]:
            continue
        sku_units = bill["sku_units"]
        best_sku = max(sku_units, key=sku_units.get) if sku_units else None
        bill_summaries.append({
            "bill_id": bill["bill_id"],
            "store_id": bill["store_id"],
            "bill_date": bill["bill_date"].isoformat(),
            "salesperson_id": bill["salesperson_id"],
            "revenue": round(bill["revenue"], 2),
            "units": bill["units"],
            "line_count": bill["line_count"],
            "top_sku_id": best_sku,
            "top_sku_name": field(items.get(best_sku, {}), "style_name", default=best_sku or ""),
        })
    bill_summaries.sort(key=lambda row: (-row["revenue"], row["bill_date"]))
    recent_weeks = [week.isoformat() for week in weekly_dates if recent_start <= week <= as_of]
    return {
        "products": product_index,
        "sales_weekly": {sku: dict(values) for sku, values in sales_weekly.items()},
        "stock_weekly": {sku: dict(values) for sku, values in stock_weekly.items()},
        "store_sales_weekly": {store: dict(values) for store, values in store_sales_weekly.items()},
        "store_stock_weekly": {store: dict(values) for store, values in store_stock_weekly.items()},
        "sku_store_sales_weekly": {
            store: {sku: dict(values) for sku, values in by_sku.items()}
            for store, by_sku in sku_store_sales_weekly.items()
        },
        "sku_store_stock_weekly": {
            store: {sku: dict(values) for sku, values in by_sku.items()}
            for store, by_sku in sku_store_stock_weekly.items()
        },
        "bill_summaries": bill_summaries,
        "recent_start": recent_start.isoformat(),
        "as_of": as_of.isoformat(),
        "weeks": recent_weeks,
    }


def _run_analysis(path: Path, source_name: str | None = None) -> None:
    tables = _read_source(path)
    velocity, model_meta = forecast_velocity(tables)
    age_threshold = int(os.environ.get("AGE_THRESHOLD_DAYS", DEFAULT_AGE_DAYS))
    raw_moves = build_recommendations(tables, age_threshold=age_threshold,
                                     velocity_override=velocity,
                                     lead_time_overrides=_route_lead_overrides(tables))
    optimized, optimizer_meta = optimize_proposals(raw_moves, tables)
    validate_proposal_selection(optimized, tables)
    analytics = _build_analytics(tables)
    item_by_sku = {str(field(row, "sku_id", default="")): row for row in tables["items"]}
    approval_limit = approval_threshold_from_rules(tables["rules"])
    enriched = []
    for move in optimized:
        item = item_by_sku.get(move["sku_id"], {})
        move = dict(move)
        move.update({
            "style_name": str(field(item, "style_name", default=move["style_id"])),
            "size": str(field(item, "size", default="")),
            "description": _product_description(item),
            "mrp": as_number(field(item, "mrp")),
            "cost_price": as_number(field(item, "cost_price")),
            "move_value_mrp": round(move["qty"] * as_number(field(item, "mrp")), 2),
            "approval_threshold_mrp": approval_limit,
            "requires_human_approval": move["qty"] * as_number(field(item, "mrp")) > approval_limit,
        })
        encoded = "|".join(str(move.get(column, "")) for column in (
            "move_type", "from_location", "to_location", "sku_id", "qty", "arrival_date"))
        move["id"] = hashlib.sha256(encoded.encode()).hexdigest()[:16]
        move["decision"] = state["approvals"].get(move["id"], {}).get("status", "pending")
        move["decision_note"] = state["approvals"].get(move["id"], {}).get("note", "")
        move["decision_actor"] = state["approvals"].get(move["id"], {}).get("actor", "")
        move["decision_role"] = state["approvals"].get(move["id"], {}).get("role", "")
        enriched.append(move)
    state.update({
        "tables": tables, "moves": enriched, "model": model_meta,
        "optimizer": optimizer_meta, "source_name": source_name or path.name,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "approval_threshold_mrp": approval_limit,
        **analytics,
    })


def _require_data() -> dict[str, list[dict[str, Any]]]:
    tables = state.get("tables")
    if tables is None:
        raise HTTPException(409, "Upload the provided workbook or a ZIP of CSV tables to begin.")
    return tables


def _ensure_store_scope(store_id: str | None, actor_role: str | None,
                        area_manager_id: str | None) -> None:
    if actor_role != "area_manager" or not store_id:
        return
    store = next((row for row in state["tables"]["stores"]
                  if str(field(row, "store_id", default="")) == store_id), None)
    if store and str(field(store, "area_manager_id", default="")) != area_manager_id:
        raise HTTPException(403, "Area managers can only view their assigned stores.")


def _area_store_ids(area_manager_id: str | None) -> set[str]:
    return {
        str(field(row, "store_id", default="")) for row in state["tables"]["stores"]
        if str(field(row, "area_manager_id", default="")) == area_manager_id
    }


def _scoped_product(product: dict[str, Any], store_ids: set[str]) -> dict[str, Any]:
    metrics = [row for row in product["store_performance"] if row["store_id"] in store_ids]
    exposure_days = sum(row["recent_in_stock_days"] for row in metrics)
    recent_units = sum(row["recent_units"] for row in metrics)
    historical = max(metrics, key=lambda row: row["sales_units"], default=None)
    recent = max(metrics, key=lambda row: row["recent_units"], default=None)
    store_stock = sum(row["stock_units"] for row in metrics)
    warehouse_units = product["warehouse_units"]
    return {
        **product,
        "total_sold_units": sum(row["sales_units"] for row in metrics),
        "sales_revenue": round(sum(row["sales_revenue"] for row in metrics), 2),
        "recent_sold_units": recent_units,
        "recent_revenue": round(sum(row["recent_revenue"] for row in metrics), 2),
        "recent_velocity": round(recent_units / exposure_days, 3) if exposure_days else None,
        "store_stock_units": store_stock,
        "network_stock_units": store_stock + warehouse_units,
        "oldest_stock_age_days": max(
            (row["stock_age_days"] or 0 for row in metrics), default=0),
        "aged_stock_days_max": max(
            (row["stock_age_days"] or 0 for row in metrics), default=0),
        "best_selling_store": historical["store_id"] if historical and historical["sales_units"] else None,
        "best_recent_store": recent["store_id"] if recent and recent["recent_units"] else None,
        "unsold_locations": [row for row in metrics if row["unsold_with_exposure"]],
        "store_performance": metrics,
    }


def _date_range(tables: dict[str, list[dict[str, Any]]]) -> tuple[date | None, date | None]:
    days = [as_date(field(row, "as_of_date")) for row in tables["stock_snapshot"]]
    days = [day for day in days if day]
    return min(days, default=None), max(days, default=None)


def _summary(tables: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    stores = {str(field(row, "store_id", default="")): row for row in tables["stores"]}
    items = {str(field(row, "sku_id", default="")): row for row in tables["items"]}
    stock_total = defaultdict(float)
    store_total = defaultdict(float)
    category_units = defaultdict(float)
    for row in tables["stock_snapshot"]:
        loc = str(field(row, "location_id", default=""))
        sku = str(field(row, "sku_id", default=""))
        units = as_number(field(row, "on_hand")) + as_number(field(row, "in_transit"))
        stock_total[loc] += units
        if loc in stores:
            store_total[loc] += units
            category_units[str(field(items.get(sku, {}), "category", default=""))] += units

    bills = {str(field(row, "bill_id", default="")): row for row in tables["bills"]}
    revenue = 0.0
    units_sold = 0
    for line in tables["bill_lines"]:
        qty = as_number(field(line, "qty"))
        revenue += qty * as_number(field(line, "selling_price"))
        sku = str(field(line, "sku_id", default=""))
        if not as_number(field(items.get(sku, {}), "is_packaging")):
            units_sold += int(qty)

    all_moves = state["moves"]
    pending = [row for row in all_moves if row["decision"] == "pending"]
    date_min, date_max = _date_range(tables)
    store_summaries = _store_summaries(tables, store_total)
    total_footfall = sum(row["footfall"] for row in store_summaries)
    total_recent_bills = sum(row["recent_bills"] for row in store_summaries)
    return {
        "has_data": True,
        "source_name": state["source_name"],
        "generated_at": state["generated_at"],
        "period_start": date_min.isoformat() if date_min else None,
        "period_end": date_max.isoformat() if date_max else None,
        "stores": len(stores),
        "skus": len(items),
        "stock_units": int(sum(store_total.values())),
        "warehouse_units": int(stock_total[WAREHOUSE]),
        "revenue": round(revenue, 2),
        "units_sold": units_sold,
        "footfall": total_footfall,
        "visit_to_bill_pct": round(100 * total_recent_bills / total_footfall, 2)
        if total_footfall else 0,
        "recommendations": len(all_moves),
        "pending": len(pending),
        "approved": sum(row["decision"] == "approved" for row in all_moves),
        "rejected": sum(row["decision"] == "rejected" for row in all_moves),
        "approval_threshold_mrp": state["approval_threshold_mrp"],
        "warehouse_to_store": sum(row["move_type"] == "WAREHOUSE_TO_STORE" for row in pending),
        "store_to_store": sum(row["move_type"] == "STORE_TO_STORE" for row in pending),
        "store_to_warehouse": sum(row["move_type"] == "STORE_TO_WAREHOUSE" for row in pending),
        "model": state["model"],
        "optimizer": state["optimizer"],
        "category_stock": [{"category": name, "units": int(units)}
                           for name, units in sorted(category_units.items())],
        "stores_summary": store_summaries,
    }


def _store_summaries(tables: dict[str, list[dict[str, Any]]],
                     store_total: dict[str, float] | None = None) -> list[dict[str, Any]]:
    store_rows = {str(field(row, "store_id", default="")): row for row in tables["stores"]}
    sku_items = {str(field(row, "sku_id", default="")): row for row in tables["items"]}
    sales = defaultdict(float)
    stock_days = defaultdict(float)
    sold_units = defaultdict(float)
    footfall = defaultdict(float)
    recent_bill_count = defaultdict(int)
    present_shifts = defaultdict(int)
    roster_days = defaultdict(set)
    bill_store = {str(field(row, "bill_id", default="")): str(field(row, "store_id", default=""))
                  for row in tables["bills"]}
    footfall_dates = [as_date(field(row, "date")) for row in tables.get("footfall_daily", [])]
    footfall_dates = [day for day in footfall_dates if day]
    footfall_start, footfall_end = min(footfall_dates, default=None), max(footfall_dates, default=None)
    for row in tables.get("footfall_daily", []):
        footfall[str(field(row, "store_id", default=""))] += as_number(field(row, "footfall"))
    for bill in tables["bills"]:
        bill_date = as_date(field(bill, "bill_date", "date"))
        store_id = str(field(bill, "store_id", default=""))
        if bill_date and footfall_start and footfall_start <= bill_date <= footfall_end:
            recent_bill_count[store_id] += 1
    for row in tables.get("roster_daily", []):
        store_id = str(field(row, "store_id", default=""))
        day = as_date(field(row, "date"))
        if not day:
            continue
        roster_days[store_id].add(day)
        if str(field(row, "status", default="")).casefold() == "present":
            present_shifts[store_id] += 1
    for row in tables["bill_lines"]:
        store = bill_store.get(str(field(row, "bill_id", default="")), "")
        sku = str(field(row, "sku_id", default=""))
        if as_number(field(sku_items.get(sku, {}), "is_packaging")):
            continue
        qty = as_number(field(row, "qty"))
        sales[store] += qty * as_number(field(row, "selling_price"))
        sold_units[store] += qty
    for row in tables["stock_weekly"]:
        stock_days[str(field(row, "store_id", default="")),
                   str(field(row, "sku_id", default=""))] += as_number(field(row, "days_in_stock"))
    rows = []
    for store_id, store in store_rows.items():
        tier = str(field(store, "tier", default=""))
        capacity = as_number(field(store, "stock_capacity_units", "capacity_units"))
        units = store_total.get(store_id, 0) if store_total is not None else 0
        rows.append({
            "store_id": store_id, "city": field(store, "city", default=""),
            "state": field(store, "state", default=""), "tier": tier,
            "area_manager_id": field(store, "area_manager_id", default=""),
            "stock_units": int(units), "capacity": int(capacity),
            "capacity_pct": round(100 * units / capacity, 1) if capacity else 0,
            "sales_revenue": round(sales[store_id], 2),
            "units_sold": int(sold_units[store_id]),
            "footfall": int(footfall[store_id]),
            "recent_bills": recent_bill_count[store_id],
            "visit_to_bill_pct": round(100 * recent_bill_count[store_id] / footfall[store_id], 2)
            if footfall[store_id] else 0,
            "avg_staff_present": round(present_shifts[store_id] / len(roster_days[store_id]), 1)
            if roster_days[store_id] else 0,
            "covering_skus": sum(
                stock_days[store_id, sku] > 0 for sku in sku_items),
        })
    return sorted(rows, key=lambda row: (-row["sales_revenue"], row["store_id"]))


def _insights(tables: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    insights = []
    for move in state["moves"]:
        if move["decision"] != "pending":
            continue
        label = f"{move['sku_id']} · {move['to_location']}"
        if move["priority"] == "P1":
            insights.append({
                "severity": "urgent", "title": "Short cover at destination",
                "detail": f"{label}: {move['daily_velocity']} units/day. {move['reason']}",
                "move_id": move["id"],
            })
    snapshot = tables["stock_snapshot"]
    stores = {str(field(row, "store_id", default="")): row for row in tables["stores"]}
    totals = defaultdict(float)
    for row in snapshot:
        location = str(field(row, "location_id", default=""))
        if location in stores:
            totals[location] += as_number(field(row, "on_hand")) + as_number(field(row, "in_transit"))
    for store_id, store in stores.items():
        capacity = as_number(field(store, "stock_capacity_units", "capacity_units"))
        pct = totals[store_id] / capacity if capacity else 0
        if pct >= 0.9:
            insights.append({
                "severity": "watch", "title": "Store near capacity",
                "detail": f"{store_id} is at {pct:.0%} of its stated unit capacity.",
                "store_id": store_id, "move_id": None,
            })
    if not insights:
        insights.append({
            "severity": "positive", "title": "No urgent exceptions",
            "detail": "No pending P1 move or store above 90% of capacity was detected.",
            "store_id": None, "move_id": None,
        })
    return insights[:12]


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "has_data": state["tables"] is not None}


@app.post("/api/upload")
async def upload(file: UploadFile = File(...)) -> dict[str, Any]:
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in {".xlsx", ".zip"}:
        raise HTTPException(415, "Upload an .xlsx workbook or .zip containing one CSV per table.")
    safe_name = f"dataset-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}{suffix}"
    saved = DATA_DIR / safe_name
    try:
        content = await file.read(200 * 1024 * 1024 + 1)
        if len(content) > 200 * 1024 * 1024:
            raise HTTPException(413, "File exceeds the 200 MB upload limit.")
        if not content:
            raise HTTPException(400, "Uploaded file is empty.")
        saved.write_bytes(content)
        _run_analysis(saved, file.filename)
        return {"ok": True, **_summary(state["tables"])}
    except HTTPException:
        raise
    except (OSError, ValueError, KeyError, zipfile.BadZipFile) as exc:
        saved.unlink(missing_ok=True)
        raise HTTPException(422, f"Could not analyse this dataset: {exc}") from exc


@app.post("/api/run")
def run() -> dict[str, Any]:
    source = state.get("source_name")
    if not source:
        raise HTTPException(409, "Upload a dataset before running recommendations.")
    candidates = sorted(DATA_DIR.glob("dataset-*"), key=lambda path: path.stat().st_mtime, reverse=True)
    if not candidates:
        raise HTTPException(409, "The uploaded source file is no longer available.")
    try:
        _run_analysis(candidates[0], source)
        return {"ok": True, **_summary(state["tables"])}
    except (OSError, ValueError, KeyError) as exc:
        raise HTTPException(422, f"Analysis failed: {exc}") from exc


@app.get("/api/summary")
def summary(
    actor_role: str | None = Header(None, alias="x-stockmover-role"),
    area_manager_id: str | None = Header(None, alias="x-stockmover-area-manager"),
) -> dict[str, Any]:
    if state["tables"] is None:
        return {"has_data": False, "source_name": None, "generated_at": None}
    tables = _require_data()
    result = _summary(tables)
    if actor_role != "area_manager":
        return result
    managed = _area_store_ids(area_manager_id)
    result["stores_summary"] = [row for row in result["stores_summary"]
                                if row["store_id"] in managed]
    result["stores"] = len(result["stores_summary"])
    result["stock_units"] = sum(row["stock_units"] for row in result["stores_summary"])
    result["revenue"] = round(sum(row["sales_revenue"] for row in result["stores_summary"]), 2)
    result["units_sold"] = sum(row["units_sold"] for row in result["stores_summary"])
    result["footfall"] = sum(row["footfall"] for row in result["stores_summary"])
    result["visit_to_bill_pct"] = round(
        100 * sum(row["recent_bills"] for row in result["stores_summary"]) / result["footfall"], 2
    ) if result["footfall"] else 0
    category_units = defaultdict(int)
    item_by_sku = {str(field(row, "sku_id", default="")): row for row in tables["items"]}
    for row in tables["stock_snapshot"]:
        store_id = str(field(row, "location_id", default=""))
        if store_id in managed:
            category = str(field(item_by_sku.get(str(field(row, "sku_id", default="")), {}),
                                 "category", default=""))
            category_units[category] += int(
                as_number(field(row, "on_hand")) + as_number(field(row, "in_transit"))
            )
    result["category_stock"] = [{"category": key, "units": value}
                                for key, value in sorted(category_units.items())]
    move_scope = [row for row in state["moves"]
                  if row["from_location"] in managed or row["to_location"] in managed]
    pending = [row for row in move_scope if row["decision"] == "pending"]
    result.update({
        "recommendations": len(move_scope),
        "pending": len(pending),
        "approved": sum(row["decision"] == "approved" for row in move_scope),
        "rejected": sum(row["decision"] == "rejected" for row in move_scope),
        "warehouse_to_store": sum(row["move_type"] == "WAREHOUSE_TO_STORE" for row in pending),
        "store_to_store": sum(row["move_type"] == "STORE_TO_STORE" for row in pending),
        "store_to_warehouse": sum(row["move_type"] == "STORE_TO_WAREHOUSE" for row in pending),
    })
    return result


@app.get("/api/recommendations")
def recommendations(
    move_type: str | None = None,
    decision: str | None = None,
    priority: str | None = None,
    search: str | None = None,
    actor_role: str | None = Header(None, alias="x-stockmover-role"),
    area_manager_id: str | None = Header(None, alias="x-stockmover-area-manager"),
    limit: int = Query(100, ge=1, le=500),
) -> dict[str, Any]:
    _require_data()
    result = state["moves"]
    if actor_role == "area_manager" and area_manager_id:
        stores_for_manager = _area_store_ids(area_manager_id)
        result = [row for row in result
                  if row["from_location"] in stores_for_manager or row["to_location"] in stores_for_manager]
    if move_type:
        result = [row for row in result if row["move_type"] == move_type]
    if decision:
        result = [row for row in result if row["decision"] == decision]
    if priority:
        result = [row for row in result if row["priority"] == priority]
    if search:
        needle = search.casefold()
        result = [row for row in result if needle in " ".join(
            str(row.get(key, "")) for key in ("sku_id", "style_name", "category",
                                               "from_location", "to_location", "reason")).casefold()]
    return {"total": len(result), "items": result[:limit]}


@app.patch("/api/recommendations/{move_id}")
def decide(
    move_id: str,
    decision: Decision,
    actor: str | None = Header(None, alias="x-stockmover-user"),
    actor_role: str | None = Header(None, alias="x-stockmover-role"),
    area_manager_id: str | None = Header(None, alias="x-stockmover-area-manager"),
) -> dict[str, Any]:
    _require_data()
    move = next((row for row in state["moves"] if row["id"] == move_id), None)
    if move is None:
        raise HTTPException(404, "Recommendation not found.")
    if actor_role == "area_manager":
        managed_stores = _area_store_ids(area_manager_id)
        if move["from_location"] not in managed_stores and move["to_location"] not in managed_stores:
            raise HTTPException(403, "Area managers can only decide moves involving their assigned stores.")
    state["approvals"][move_id] = {
        "status": decision.status, "note": decision.note,
        "actor": actor or "Merchandising team", "role": actor_role or "unspecified",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    _write_decisions()
    move["decision"] = decision.status
    move["decision_note"] = decision.note
    move["decision_actor"] = actor or "Merchandising team"
    move["decision_role"] = actor_role or "unspecified"
    return {"ok": True, "item": move}


@app.get("/api/stores")
def stores(
    actor_role: str | None = Header(None, alias="x-stockmover-role"),
    area_manager_id: str | None = Header(None, alias="x-stockmover-area-manager"),
) -> dict[str, Any]:
    tables = _require_data()
    items = _store_summaries(tables)
    if actor_role == "area_manager":
        items = [row for row in items if row["area_manager_id"] == area_manager_id]
    return {"items": items}


@app.get("/api/products")
def products(
    q: str | None = None,
    category: str | None = None,
    store_id: str | None = None,
    actor_role: str | None = Header(None, alias="x-stockmover-role"),
    area_manager_id: str | None = Header(None, alias="x-stockmover-area-manager"),
    sort: str = Query("recent", pattern="^(recent|units|revenue|stock|age|name)$"),
    unsold_only: bool = False,
    limit: int = Query(100, ge=1, le=500),
) -> dict[str, Any]:
    _require_data()
    _ensure_store_scope(store_id, actor_role, area_manager_id)
    managed = _area_store_ids(area_manager_id) if actor_role == "area_manager" else None
    result = []
    needle = q.casefold().strip() if q else ""
    for product in state["products"].values():
        if managed is not None:
            product = _scoped_product(product, managed)
        if category and str(field(product, "category", default="")).casefold() != category.casefold():
            continue
        if needle and needle not in " ".join(
            str(product.get(key, "")) for key in ("sku_id", "style_id", "style_name", "category", "size")
        ).casefold():
            continue
        store_metrics = next(
            (row for row in product["store_performance"] if row["store_id"] == store_id), None
        ) if store_id else None
        if store_id and store_metrics is None:
            continue
        if unsold_only and not (
            store_metrics["unsold_with_exposure"] if store_metrics
            else product["unsold_locations"]
        ):
            continue
        result.append({
            **product,
            "store_metrics": store_metrics,
            "description": product["description"],
        })
    sort_fields = {
        "recent": lambda row: row["store_metrics"]["recent_units"] if row["store_metrics"]
        else row["recent_sold_units"],
        "units": lambda row: row["store_metrics"]["sales_units"] if row["store_metrics"]
        else row["total_sold_units"],
        "revenue": lambda row: row["store_metrics"]["sales_revenue"] if row["store_metrics"]
        else row["sales_revenue"],
        "stock": lambda row: row["store_metrics"]["stock_units"] if row["store_metrics"]
        else row["network_stock_units"],
        "age": lambda row: row["store_metrics"]["stock_age_days"] or 0 if row["store_metrics"]
        else row["aged_stock_days_max"],
        "name": lambda row: str(field(row, "style_name", default="")).casefold(),
    }
    result.sort(key=sort_fields[sort], reverse=sort != "name")
    return {"total": len(result), "items": result[:limit], "as_of": state["as_of"]}


@app.get("/api/analytics/meta")
def analytics_meta() -> dict[str, Any]:
    tables = _require_data()
    return {
        "categories": sorted({
            str(field(row, "category", default="")) for row in tables["items"]
            if field(row, "category", default="")
        }),
        "as_of": state["as_of"],
        "recent_start": state["recent_start"],
    }


@app.get("/api/products/{sku_id}")
def product_detail(
    sku_id: str,
    actor_role: str | None = Header(None, alias="x-stockmover-role"),
    area_manager_id: str | None = Header(None, alias="x-stockmover-area-manager"),
) -> dict[str, Any]:
    _require_data()
    source_product = state["products"].get(sku_id)
    if source_product is None:
        raise HTTPException(404, "SKU not found in the uploaded catalogue.")
    managed = _area_store_ids(area_manager_id) if actor_role == "area_manager" else None
    product = _scoped_product(source_product, managed) if managed is not None else source_product
    weeks = state["weeks"]
    if managed is None:
        sales = state["sales_weekly"].get(sku_id, {})
        stock = state["stock_weekly"].get(sku_id, {})
    else:
        sales = defaultdict(int)
        stock = defaultdict(int)
        for store in managed:
            for week, units in state["sku_store_sales_weekly"].get(store, {}).get(sku_id, {}).items():
                sales[week] += units
            for week, units in state["sku_store_stock_weekly"].get(store, {}).get(sku_id, {}).items():
                stock[week] += units
    transfer_moves = [
        row for row in state["moves"] if row["sku_id"] == sku_id
        and row["move_type"] in {"STORE_TO_STORE", "WAREHOUSE_TO_STORE"}
        and (managed is None or row["from_location"] in managed or row["to_location"] in managed)
    ]
    return {
        "product": product,
        "sales_trend": [{"week": week, "units": sales.get(week, 0)} for week in weeks],
        "stock_trend": [{"week": week, "units": stock.get(week, 0)} for week in weeks],
        "transfer_recommendations": transfer_moves,
    }


@app.get("/api/stores/{store_id}")
def store_detail(
    store_id: str,
    actor_role: str | None = Header(None, alias="x-stockmover-role"),
    area_manager_id: str | None = Header(None, alias="x-stockmover-area-manager"),
) -> dict[str, Any]:
    tables = _require_data()
    store = next((row for row in tables["stores"]
                  if str(field(row, "store_id", default="")) == store_id), None)
    if store is None:
        raise HTTPException(404, "Store not found.")
    _ensure_store_scope(store_id, actor_role, area_manager_id)
    summaries = {row["store_id"]: row for row in _store_summaries(tables)}
    performance = []
    for product in state["products"].values():
        metric = next((row for row in product["store_performance"]
                       if row["store_id"] == store_id), None)
        if metric:
            performance.append({**product, "store_metrics": metric})
    best_sellers = sorted(performance, key=lambda row: (
        -row["store_metrics"]["recent_units"], -row["store_metrics"]["sales_units"]))[:10]
    dormant = sorted(
        (row for row in performance if row["store_metrics"]["unsold_with_exposure"]),
        key=lambda row: (-int(row["store_metrics"]["stock_age_days"] or 0),
                         -row["store_metrics"]["stock_units"]),
    )[:20]
    weeks = state["weeks"]
    sales = state["store_sales_weekly"].get(store_id, {})
    stock = state["store_stock_weekly"].get(store_id, {})
    category_stock = defaultdict(int)
    items = {str(field(row, "sku_id", default="")): row for row in tables["items"]}
    for row in tables["stock_snapshot"]:
        if str(field(row, "location_id", default="")) != store_id:
            continue
        item = items.get(str(field(row, "sku_id", default="")), {})
        category_stock[str(field(item, "category", default="Other"))] += int(
            as_number(field(row, "on_hand")) + as_number(field(row, "in_transit"))
        )
    return {
        "store": summaries[store_id],
        "category_stock": [{"category": key, "units": value}
                           for key, value in sorted(category_stock.items())],
        "best_sellers": best_sellers,
        "dormant_stock": dormant,
        "weekly_trend": [{
            "week": week, "sales_units": sales.get(week, 0), "stock_units": stock.get(week, 0),
        } for week in weeks],
    }


@app.get("/api/bill-analysis")
def bill_analysis(
    store_id: str | None = None,
    actor_role: str | None = Header(None, alias="x-stockmover-role"),
    area_manager_id: str | None = Header(None, alias="x-stockmover-area-manager"),
) -> dict[str, Any]:
    tables = _require_data()
    known_stores = {str(field(row, "store_id", default="")): row for row in tables["stores"]}
    if store_id and store_id not in known_stores:
        raise HTTPException(404, "Store not found.")
    _ensure_store_scope(store_id, actor_role, area_manager_id)
    allowed_stores = {
        key for key, row in known_stores.items()
        if actor_role != "area_manager"
        or str(field(row, "area_manager_id", default="")) == area_manager_id
    }
    bills = [
        row for row in state["bill_summaries"]
        if row["bill_date"] >= state["recent_start"]
        and (row["store_id"] in allowed_stores)
        and (not store_id or row["store_id"] == store_id)
    ]
    bills.sort(key=lambda row: (-row["revenue"], row["bill_date"]))
    store_products = []
    dormant = []
    scope_ids = allowed_stores if actor_role == "area_manager" else None
    for product in state["products"].values():
        if scope_ids is not None:
            product = _scoped_product(product, scope_ids)
        metrics = [row for row in product["store_performance"]
                   if row["store_id"] in allowed_stores
                   and (not store_id or row["store_id"] == store_id)]
        if not store_id:
            aggregate = {
                "recent_units": sum(row["recent_units"] for row in metrics),
                "sales_units": sum(row["sales_units"] for row in metrics),
            }
            if aggregate["recent_units"] > 0:
                store_products.append({**product, "store_metrics": aggregate})
            candidate_dormant = [row for row in metrics if row["unsold_with_exposure"]]
        else:
            metric = metrics[0] if metrics else None
            if metric is None:
                continue
            if metric["recent_units"] > 0:
                store_products.append({**product, "store_metrics": metric})
            candidate_dormant = [metric] if metric["unsold_with_exposure"] else []
        for metric in candidate_dormant:
            target_store = metric["store_id"]
            best_other = next((candidate for candidate in product["store_performance"]
                               if candidate["store_id"] != target_store
                               and candidate["recent_units"] > 0
                               and candidate["store_id"] in allowed_stores), None)
            transfer = next((move for move in state["moves"]
                             if move["sku_id"] == product["sku_id"]
                             and move["to_location"] == target_store
                             and move["move_type"] == "STORE_TO_STORE"
                             and move["decision"] == "pending"), None)
            dormant.append({
                **product,
                "store_metrics": metric,
                "best_other_store": best_other,
                "recommended_move": transfer,
            })
    store_products.sort(key=lambda row: (
        -row["store_metrics"]["recent_units"], -row["store_metrics"]["sales_units"]))
    dormant.sort(key=lambda row: (
        -int(row["store_metrics"]["stock_age_days"] or 0),
        -row["store_metrics"]["stock_units"]))
    total_revenue = sum(row["revenue"] for row in bills)
    total_units = sum(row["units"] for row in bills)
    return {
        "store_id": store_id,
        "period_start": state["recent_start"],
        "period_end": state["as_of"],
        "summary": {
            "bills": len(bills),
            "revenue": round(total_revenue, 2),
            "units": total_units,
            "average_bill_value": round(total_revenue / len(bills), 2) if bills else 0,
            "average_units_per_bill": round(total_units / len(bills), 2) if bills else 0,
        },
        "top_products": store_products[:10],
        "dormant_stock": dormant[:20],
        "top_bills": bills[:15],
    }


@app.get("/api/insights")
def insights(
    actor_role: str | None = Header(None, alias="x-stockmover-role"),
    area_manager_id: str | None = Header(None, alias="x-stockmover-area-manager"),
) -> dict[str, Any]:
    tables = _require_data()
    items = _insights(tables)
    if actor_role == "area_manager":
        managed = _area_store_ids(area_manager_id)
        allowed_move_ids = {row["id"] for row in state["moves"]
                            if row["from_location"] in managed or row["to_location"] in managed}
        items = [row for row in items
                 if row.get("move_id") in allowed_move_ids or row.get("store_id") in managed]
    return {"items": items}


@app.get("/api/export")
def export(
    format: str = Query("csv", pattern="^(csv|json)$"),
    actor_role: str | None = Header(None, alias="x-stockmover-role"),
    area_manager_id: str | None = Header(None, alias="x-stockmover-area-manager"),
) -> StreamingResponse:
    _require_data()
    moves = state["moves"]
    if actor_role == "area_manager":
        managed = _area_store_ids(area_manager_id)
        moves = [row for row in moves
                 if row["from_location"] in managed or row["to_location"] in managed]
    if format == "json":
        content = json.dumps(moves, indent=2)
        return StreamingResponse(io.StringIO(content), media_type="application/json",
                                 headers={"Content-Disposition": 'attachment; filename="moves.json"'})
    buffer = io.StringIO()
    columns = ["priority", "move_type", "from_location", "to_location", "sku_id",
               "style_name", "size", "category", "qty", "dispatch_date", "arrival_date",
               "daily_velocity", "donor_daily_velocity", "donor_cover_after_days",
               "stock_age_days", "move_value_mrp", "decision", "decision_actor",
               "decision_role", "decision_note", "reason"]
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(moves)
    return StreamingResponse(io.StringIO(buffer.getvalue()), media_type="text/csv",
                             headers={"Content-Disposition": 'attachment; filename="moves.csv"'})


@app.post("/api/ask")
def ask(
    question: Question,
    actor_role: str | None = Header(None, alias="x-stockmover-role"),
    area_manager_id: str | None = Header(None, alias="x-stockmover-area-manager"),
) -> dict[str, Any]:
    tables = _require_data()
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise HTTPException(503, "Gemini is not configured. Set GEMINI_API_KEY in the backend environment.")
    metrics = summary(actor_role, area_manager_id)
    pending_moves = [row for row in state["moves"] if row["decision"] == "pending"]
    if actor_role == "area_manager":
        managed = _area_store_ids(area_manager_id)
        pending_moves = [row for row in pending_moves
                         if row["from_location"] in managed or row["to_location"] in managed]
    context = {
        "summary": {key: metrics[key] for key in (
            "stores", "skus", "stock_units", "warehouse_units", "revenue",
            "pending", "approved", "model", "optimizer")},
        "pending_recommendations": [{
            key: row[key] for key in ("move_type", "from_location", "to_location",
                                      "sku_id", "qty", "priority", "reason")
        } for row in pending_moves][:100],
    }
    body = json.dumps({
        "system_instruction": {"parts": [{"text":
            "You are a stock-operations copilot for a fashion retailer. Explain the supplied "
            "computed data and recommendations clearly. Never invent quantities, break rules, "
            "or claim you ran an optimization; the provided values are authoritative. "
            "If the data does not answer a question, say what is missing."}]},
        "contents": [{"parts": [{"text": json.dumps(context) + "\nQuestion: " + question.question}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 600},
    }).encode()
    request = urllib.request.Request(
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent",
        data=body, headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
        method="POST")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            result = json.loads(response.read())
        text = result["candidates"][0]["content"]["parts"][0]["text"]
        return {"answer": text, "provider": "Gemini", "computed_values_are_authoritative": True}
    except (urllib.error.URLError, KeyError, IndexError, json.JSONDecodeError) as exc:
        raise HTTPException(502, f"Gemini request failed: {exc}") from exc


async def _scheduled_refresh() -> None:
    interval_hours = max(0, int(os.environ.get("AUTO_RUN_INTERVAL_HOURS", "24")))
    if interval_hours == 0:
        return
    while True:
        await asyncio.sleep(interval_hours * 3600)
        files = sorted(DATA_DIR.glob("dataset-*"), key=lambda path: path.stat().st_mtime, reverse=True)
        if files:
            try:
                _run_analysis(files[0], state.get("source_name"))
            except (OSError, ValueError, KeyError, RuntimeError) as exc:
                print(f"Scheduled recommendation run failed: {exc}", file=sys.stderr)


@app.on_event("startup")
async def startup() -> None:
    state["approvals"] = _read_decisions()
    latest = sorted(DATA_DIR.glob("dataset-*"), key=lambda path: path.stat().st_mtime, reverse=True)
    if latest:
        try:
            _run_analysis(latest[0], latest[0].name)
        except Exception as exc:
            print(f"Could not load persisted dataset: {exc}", file=sys.stderr)
    asyncio.create_task(_scheduled_refresh())
