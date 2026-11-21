"""Availability-corrected demand forecasts with an optional LightGBM Tweedie model."""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import date, timedelta
from typing import Any

from stock_mover import as_date, as_number, field


def _fallback_velocity(tables: dict[str, list[dict[str, Any]]]) -> dict[tuple[str, str], float]:
    sales = defaultdict(float)
    bill_dates = {
        str(field(row, "bill_id", default="")): (
            str(field(row, "store_id", default="")),
            as_date(field(row, "bill_date")))
        for row in tables.get("bills", [])
    }
    weekly_ends = [as_date(field(row, "week_ending")) for row in tables.get("stock_weekly", [])]
    weekly_ends = [value for value in weekly_ends if value]
    if weekly_ends:
        first_week = min(weekly_ends) - timedelta(days=6)
        last_week = max(weekly_ends)
        for line in tables.get("bill_lines", []):
            store, sold_on = bill_dates.get(str(field(line, "bill_id", default="")), ("", None))
            if sold_on and first_week <= sold_on <= last_week:
                sales[store, str(field(line, "sku_id", default=""))] += as_number(field(line, "qty"))

    stock_days = defaultdict(float)
    for row in tables.get("stock_weekly", []):
        stock_days[str(field(row, "store_id", default="")),
                   str(field(row, "sku_id", default=""))] += as_number(field(row, "days_in_stock"))
    observation_days = ((max(weekly_ends) - min(weekly_ends)).days + 7) if weekly_ends else 84
    return {
        pair: (sales[pair] / stock_days[pair] if stock_days[pair] > 0
               else sales[pair] / max(1, observation_days))
        for pair in set(sales) | set(stock_days)
    }


def _calendar_flags(tables: dict[str, list[dict[str, Any]]]) -> tuple[set[tuple[date, str]], set[tuple[date, str]]]:
    festivals: set[tuple[date, str]] = set()
    weddings: set[tuple[date, str]] = set()
    for row in tables.get("calendar", []):
        day = as_date(field(row, "date"))
        state = str(field(row, "state", default="")).strip().lower()
        if not day:
            continue
        if field(row, "festival"):
            festivals.add((day, state))
        if as_number(field(row, "is_wedding_date")):
            weddings.add((day, state))
    return festivals, weddings


def forecast_velocity(tables: dict[str, list[dict[str, Any]]],
                      horizon_days: int = 14) -> tuple[dict[tuple[str, str], float], dict[str, Any]]:
    """Return forecast daily units by store/SKU and explain which model was used."""
    baseline = _fallback_velocity(tables)
    try:
        import numpy as np
        import lightgbm as lgb
        import sklearn  # noqa: F401
    except ImportError:
        return baseline, {
            "model": "availability_corrected_baseline",
            "status": "fallback",
            "reason": "Install backend requirements to enable the LightGBM Tweedie model.",
            "training_rows": 0,
            "forecast_horizon_days": horizon_days,
        }

    stores = {str(field(row, "store_id", default="")): row for row in tables.get("stores", [])}
    items = {str(field(row, "sku_id", default="")): row for row in tables.get("items", [])}
    weekly = tables.get("stock_weekly", [])
    week_ends = sorted({as_date(field(row, "week_ending")) for row in weekly if as_date(field(row, "week_ending"))})
    if not stores or not items or len(week_ends) < 4:
        return baseline, {
            "model": "availability_corrected_baseline",
            "status": "fallback",
            "reason": "At least four stock weeks and valid store/item tables are needed for model fitting.",
            "training_rows": 0,
            "forecast_horizon_days": horizon_days,
        }

    state_by_store = {
        store_id: str(field(row, "state", default="")).strip().lower()
        for store_id, row in stores.items()
    }
    item_codes = {value: idx for idx, value in enumerate(sorted({
        str(field(item, "category", default="")) for item in items.values()
    }))}
    tier_codes = {value: idx for idx, value in enumerate(sorted({
        str(field(store, "tier", default="")) for store in stores.values()
    }))}
    store_codes = {value: idx for idx, value in enumerate(sorted(stores))}
    sku_codes = {value: idx for idx, value in enumerate(sorted(items))}
    style_ids = {sku: str(field(item, "style_id", default=sku))
                 for sku, item in items.items()}
    style_codes = {value: idx for idx, value in enumerate(sorted(set(style_ids.values())))}
    festivals, weddings = _calendar_flags(tables)
    calendar_rows = tables.get("calendar", [])
    as_of = max((as_date(field(row, "as_of_date")) for row in tables.get("stock_snapshot", [])
                 if as_date(field(row, "as_of_date"))), default=week_ends[-1])

    bill_dates = {}
    bill_stores = {}
    for row in tables.get("bills", []):
        bill_id = str(field(row, "bill_id", default=""))
        bill_dates[bill_id] = as_date(field(row, "bill_date"))
        bill_stores[bill_id] = str(field(row, "store_id", default=""))
    sales_by_week = defaultdict(float)
    for line in tables.get("bill_lines", []):
        bill_id = str(field(line, "bill_id", default=""))
        sold_on = bill_dates.get(bill_id)
        if not sold_on:
            continue
        week_end = sold_on + timedelta(days=(5 - sold_on.weekday()) % 7)
        store_id = bill_stores.get(bill_id, "")
        sku_id = str(field(line, "sku_id", default=""))
        sales_by_week[store_id, sku_id, week_end] += as_number(field(line, "qty"))

    footfall_by_day = defaultdict(float)
    for row in tables.get("footfall_daily", []):
        day = as_date(field(row, "date"))
        store_id = str(field(row, "store_id", default=""))
        if day:
            footfall_by_day[store_id, day] += as_number(field(row, "footfall"))
    staff_by_day = defaultdict(float)
    for row in tables.get("roster_daily", []):
        day = as_date(field(row, "date"))
        store_id = str(field(row, "store_id", default=""))
        if day and str(field(row, "status", default="")).casefold() == "present":
            staff_by_day[store_id, day] += 1

    available = defaultdict(float)
    for row in weekly:
        pair = (str(field(row, "store_id", default="")),
                str(field(row, "sku_id", default="")),
                as_date(field(row, "week_ending")))
        available[pair] += as_number(field(row, "days_in_stock"))

    def features(store_id: str, sku_id: str, week_end: date, in_stock_days: float,
                 lag_rate: float, style_rate: float, footfall: float,
                 staff_presence: float) -> list[float]:
        store, item = stores[store_id], items[sku_id]
        state = state_by_store[store_id]
        festivals_in_week = sum((week_end - timedelta(days=offset), state) in festivals
                                for offset in range(7))
        weddings_in_week = sum((week_end - timedelta(days=offset), state) in weddings
                               for offset in range(7))
        price = as_number(field(item, "mrp"))
        return [
            store_codes[store_id], sku_codes[sku_id],
            style_codes[style_ids[sku_id]],
            item_codes[str(field(item, "category", default=""))],
            tier_codes[str(field(store, "tier", default=""))],
            as_number(field(item, "is_core_style")),
            math.log1p(price), week_end.month, week_end.weekday(),
            math.sin(2 * math.pi * week_end.timetuple().tm_yday / 365.25),
            math.cos(2 * math.pi * week_end.timetuple().tm_yday / 365.25),
            festivals_in_week, weddings_in_week, min(7.0, in_stock_days),
            math.log1p(max(0, lag_rate)), math.log1p(max(0, style_rate)),
            math.log1p(max(0, footfall)), math.log1p(max(0, staff_presence)),
        ]

    X, y, sample_weeks, sample_exposure = [], [], [], []
    historical_rates = defaultdict(list)
    store_footfall_history = defaultdict(list)
    store_staff_history = defaultdict(list)
    for store_id in stores:
        for week_end in week_ends:
            dates = [week_end - timedelta(days=offset) for offset in range(7)]
            store_footfall_history[store_id].append(
                sum(footfall_by_day[store_id, day] for day in dates) / 7)
            store_staff_history[store_id].append(
                sum(staff_by_day[store_id, day] for day in dates) / 7)
    style_sales = defaultdict(float)
    style_availability = defaultdict(float)
    for store_id in stores:
        for sku_id in items:
            style_id = style_ids[sku_id]
            for week_end in week_ends:
                style_sales[store_id, style_id, week_end] += sales_by_week[store_id, sku_id, week_end]
                style_availability[store_id, style_id, week_end] = max(
                    style_availability[store_id, style_id, week_end],
                    available[store_id, sku_id, week_end])
    style_history = defaultdict(list)
    style_weekly_rate = {}
    for store_id in stores:
        for style_id in set(style_ids.values()):
            for week_end in week_ends:
                days = style_availability[store_id, style_id, week_end]
                rate = style_sales[store_id, style_id, week_end] / days if days > 0 else 0
                style_weekly_rate[store_id, style_id, week_end] = rate
                style_history[store_id, style_id].append(rate)

    for store_id in stores:
        for sku_id in items:
            for week_end in week_ends:
                days = available[store_id, sku_id, week_end]
                if days <= 0:
                    continue
                rate = sales_by_week[store_id, sku_id, week_end] / days
                history = historical_rates[store_id, sku_id]
                style_rates = [style_weekly_rate[store_id, style_ids[sku_id], previous]
                               for previous in week_ends if previous < week_end][-4:]
                week_idx = week_ends.index(week_end)
                X.append(features(store_id, sku_id, week_end, days,
                                  sum(history[-4:]) / max(1, len(history[-4:])) if history else 0,
                                  sum(style_rates) / max(1, len(style_rates)),
                                  store_footfall_history[store_id][week_idx],
                                  store_staff_history[store_id][week_idx]))
                y.append(rate)
                sample_weeks.append(week_end)
                sample_exposure.append(days)
                history.append(rate)

    if len(y) < 100:
        return baseline, {
            "model": "availability_corrected_baseline",
            "status": "fallback",
            "reason": "Not enough available SKU-week observations for a stable pooled model.",
            "training_rows": len(y),
            "forecast_horizon_days": horizon_days,
        }

    def new_model():
        return lgb.LGBMRegressor(
            objective="tweedie", tweedie_variance_power=1.3,
            n_estimators=120, learning_rate=0.04, num_leaves=20,
            min_child_samples=40, verbosity=-1, random_state=42,
        )

    validation_start = week_ends[-2]
    train_mask = np.asarray([week < validation_start for week in sample_weeks])
    validation_mask = ~train_mask
    validation_metrics = {}
    if train_mask.sum() >= 100 and validation_mask.any():
        validation_model = new_model()
        x_array = np.asarray(X, dtype=float)
        y_array = np.asarray(y, dtype=float)
        exposure_array = np.asarray(sample_exposure, dtype=float)
        validation_model.fit(x_array[train_mask], y_array[train_mask],
                             sample_weight=exposure_array[train_mask],
                             categorical_feature=[0, 1, 2, 3, 4])
        predicted = np.maximum(0, validation_model.predict(x_array[validation_mask]))
        actual = y_array[validation_mask]
        validation_exposure = exposure_array[validation_mask]
        validation_metrics = {
            "holdout_weeks": 2,
            "mae_units_per_available_day": round(float(np.average(
                np.abs(actual - predicted), weights=validation_exposure)), 4),
            "wape": round(float(np.sum(np.abs(actual - predicted) * validation_exposure) /
                                 max(1e-8, np.sum(actual * validation_exposure))), 4),
        }
        recent_rate_prediction = np.maximum(0, np.expm1(x_array[validation_mask, 14]))
        validation_metrics["naive_wape"] = round(float(np.sum(
            np.abs(actual - recent_rate_prediction) * validation_exposure) /
            max(1e-8, np.sum(actual * validation_exposure))), 4)

    model = new_model()
    model.fit(np.asarray(X, dtype=float), np.asarray(y, dtype=float),
              sample_weight=np.asarray(sample_exposure, dtype=float),
              categorical_feature=[0, 1, 2, 3, 4])

    last_rates = {
        pair: sum(rates[-4:]) / len(rates[-4:])
        for pair, rates in historical_rates.items() if rates
    }
    use_learned_forecast = not validation_metrics or (
        validation_metrics["wape"] < validation_metrics["naive_wape"])
    forecast_source = "lightgbm_tweedie" if use_learned_forecast else "availability_corrected_recent_rate"
    future_dates = [as_of + timedelta(days=offset) for offset in range(1, horizon_days + 1)]
    last_style_rates = {
        pair: sum(rates[-4:]) / len(rates[-4:])
        for pair, rates in style_history.items() if rates
    }
    last_footfall = {store_id: sum(values[-4:]) / len(values[-4:])
                     for store_id, values in store_footfall_history.items() if values}
    last_staff_presence = {store_id: sum(values[-4:]) / len(values[-4:])
                           for store_id, values in store_staff_history.items() if values}
    forecast_rows = []
    forecast_pairs = []
    for store_id in stores:
        for sku_id in items:
            pair = store_id, sku_id
            for future_day in future_dates:
                week_end = future_day + timedelta(days=(5 - future_day.weekday()) % 7)
                style_pair = store_id, style_ids[sku_id]
                features_row = features(store_id, sku_id, week_end, 7.0,
                                        last_rates.get(pair, 0),
                                        last_style_rates.get(style_pair, 0),
                                        last_footfall.get(store_id, 0),
                                        last_staff_presence.get(store_id, 0))
                forecast_rows.append(features_row)
                forecast_pairs.append(pair)
    forecasts = defaultdict(list)
    for pair, predicted in zip(forecast_pairs, model.predict(np.asarray(forecast_rows, dtype=float))):
        forecasts[pair].append(max(0.0, float(predicted)))
    result = ({pair: sum(values) / len(values) for pair, values in forecasts.items()}
              if use_learned_forecast else last_rates)
    return result, {
        "model": "lightgbm_tweedie",
        "status": "trained",
        "forecast_source": forecast_source,
        "objective": "Tweedie (variance power 1.3)",
        "training_rows": len(y),
        "forecast_horizon_days": horizon_days,
        "validation": validation_metrics,
        "features": ["availability_days", "category", "store_tier", "store", "SKU",
                     "style_id", "core_style", "price", "calendar_seasonality", "festival",
                     "wedding_day", "recent_velocity", "pooled_style_velocity",
                     "door_footfall", "staff_presence"],
        "style_pooling": True,
        "calendar_rows_used": len(calendar_rows),
    }
