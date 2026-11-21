"""Select the best safe subset of rule-engine proposals with a binary MILP."""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Any

from stock_mover import as_number, display_category, field


def optimize_proposals(moves: list[dict[str, Any]], tables: dict[str, list[dict[str, Any]]]
                       ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not moves:
        return [], {"optimizer": "scipy_milp", "status": "optimal", "candidate_count": 0}
    try:
        import numpy as np
        from scipy.optimize import Bounds, LinearConstraint, milp
        from scipy.sparse import lil_matrix
    except ImportError:
        return _greedy_limit(moves, tables), {
            "optimizer": "rule_engine_fallback",
            "status": "fallback",
            "reason": "Install scipy to optimize recommendation selection with MILP.",
            "candidate_count": len(moves),
        }

    stores = {str(field(row, "store_id", default="")): row for row in tables["stores"]}
    items = {str(field(row, "sku_id", default="")): row for row in tables["items"]}
    managers = {
        str(field(row, "area_manager_id", default="")):
        max(0, int(as_number(field(row, "max_alerts_per_week", "max_alerts"))))
        for row in tables["area_managers"]
    }
    snapshot = defaultdict(float)
    categories = defaultdict(float)
    for row in tables["stock_snapshot"]:
        location, sku = (str(field(row, "location_id", default="")),
                         str(field(row, "sku_id", default="")))
        units = as_number(field(row, "on_hand")) + as_number(field(row, "in_transit"))
        snapshot[location, sku] += units
        if location in stores and sku in items:
            category = display_category(field(items[sku], "category", default=""))
            categories[location, category] += units

    rule_floors = {}
    for row in tables["min_display"]:
        tier = str(field(row, "tier", "store_tier", default=""))
        category = str(field(row, "category", default=""))
        amount = field(row, "min_display_units", "minimum_display_units", "minimum_units",
                       "min_units", "units", "min_display")
        if tier and category and amount is not None:
            rule_floors[tier, category] = as_number(amount)

    # Each candidate is either accepted in full or rejected. All limits below are
    # network-wide constraints, so individually safe proposals cannot add up to a violation.
    constraints: dict[tuple[str, ...], list[tuple[int, float]]] = defaultdict(list)
    limits: dict[tuple[str, ...], float] = {}
    scores = []
    score_components = []
    for idx, move in enumerate(moves):
        source, target, sku = move["from_location"], move["to_location"], move["sku_id"]
        category, qty = display_category(move["category"]), int(move["qty"])
        item = items.get(sku, {})
        margin = max(0.0, as_number(field(item, "mrp")) - as_number(field(item, "cost_price")))
        velocity = max(0.0, as_number(move.get("daily_velocity")))
        lead_days = max(0.0, as_number(
            move.get("arrival_lead_days"),
            as_number(move.get("lead_time_days")),
        ))
        if not lead_days and move.get("dispatch_date") and move.get("arrival_date"):
            lead_days = max(0, (date.fromisoformat(move["arrival_date"])
                                - date.fromisoformat(move["dispatch_date"])).days)
        horizon = max(1.0, 14.0 - lead_days)
        expected_units = min(qty, velocity * horizon)
        sell_probability = max(0.03, min(0.96,
                                         as_number(move.get("sell_probability_pct")) / 100.0))
        affinity = max(0.0, min(100.0, as_number(move.get("store_affinity_score"))))
        urgency = 1.0 / max(1.0, as_number(move.get("donor_cover_after_days"), 60))
        if move["move_type"] == "STORE_TO_WAREHOUSE":
            age_days = max(0.0, as_number(move.get("stock_age_days")))
            age_factor = min(3.0, max(1.0, age_days / 120.0))
            recovery_proxy = max(0.0, as_number(field(item, "cost_price"))) * qty * age_factor * 0.1
            score_components.append({"ageing_recovery_proxy": recovery_proxy})
            scores.append(max(0.01, recovery_proxy))
        elif move["move_type"] == "STORE_TO_STORE":
            donor_velocity = max(0.0, as_number(move.get("donor_daily_velocity")))
            incremental_units = min(qty, max(0.0, velocity - donor_velocity) * horizon)
            opportunity = (
                margin * incremental_units * (1.0 + urgency)
                * (0.5 + sell_probability)
                * (1.0 + affinity / 100.0)
            )
            age_days = max(0.0, as_number(move.get("stock_age_days")))
            recovery_proxy = 0.0
            if age_days >= 120:
                age_factor = min(3.0, max(1.0, age_days / 120.0))
                recovery_proxy = max(0.0, as_number(field(item, "cost_price"))) * qty * age_factor * 0.1
            score_components.append({
                "margin_opportunity": opportunity,
                "ageing_recovery_proxy": recovery_proxy,
            })
            scores.append(max(0.01, opportunity + recovery_proxy))
        else:
            opportunity = (
                margin * expected_units * (1.0 + urgency)
                * (0.5 + sell_probability)
                * (1.0 + affinity / 100.0)
            )
            score_components.append({"margin_opportunity": opportunity})
            scores.append(max(0.01, opportunity))

        if source in stores:
            constraints["source", source, sku].append((idx, qty))
            tier = str(field(stores[source], "tier", default=""))
            floor = rule_floors.get((tier, category), 0)
            constraints["display", source, category].append((idx, qty))
            limits["display", source, category] = max(
                0, categories[source, category] - floor)
        if target in stores:
            constraints["capacity", target].append((idx, qty))
            cap = as_number(field(stores[target], "stock_capacity_units", "capacity_units"))
            initial_total = sum(snapshot[target, store_sku] for store_sku in items)
            limits["capacity", target] = max(0, cap - initial_total)
        if source == "WH-KOL":
            constraints["warehouse", sku].append((idx, qty))
        if move["move_type"] == "STORE_TO_STORE":
            rate = max(0.0, as_number(move.get("donor_daily_velocity")))
            projected = snapshot[source, sku]
            cover_allowance = max(0, projected - int(rate * 30 + 0.999))
            constraints["cover", source, sku].append((idx, qty))
            limits["cover", source, sku] = cover_allowance
        involved_managers = {
            str(field(stores[location], "area_manager_id", default=""))
            for location in (source, target) if location in stores
        }
        for manager in involved_managers:
            constraints["manager", manager].append((idx, 1))
            limits["manager", manager] = managers.get(manager, 0)

    warehouse_due = defaultdict(float)
    for row in tables["warehouse_inbound"]:
        warehouse_due[str(field(row, "sku_id", default=""))] += as_number(field(row, "qty"))
    for row in tables["stock_snapshot"]:
        if str(field(row, "location_id", default="")) == "WH-KOL":
            warehouse_due[str(field(row, "sku_id", default=""))] += as_number(field(row, "on_hand"))
            warehouse_due[str(field(row, "sku_id", default=""))] += as_number(field(row, "in_transit"))
    for key, entries in constraints.items():
        if key[0] == "source":
            _, location, sku = key
            limits.setdefault(key, snapshot[location, sku])
        elif key[0] == "warehouse":
            _, sku = key
            limits[key] = warehouse_due[sku]

    rows = list(constraints)
    matrix = lil_matrix((len(rows), len(moves)), dtype=float)
    upper = np.empty(len(rows), dtype=float)
    for row_idx, constraint_key in enumerate(rows):
        for variable_idx, coefficient in constraints[constraint_key]:
            matrix[row_idx, variable_idx] += coefficient
        upper[row_idx] = limits.get(constraint_key, float("inf"))
    result = milp(
        c=-np.asarray(scores),
        integrality=np.ones(len(moves), dtype=int),
        bounds=Bounds(np.zeros(len(moves)), np.ones(len(moves))),
        constraints=LinearConstraint(matrix.tocsr(), np.zeros(len(rows)), upper),
        options={"time_limit": 8.0},
    )
    if result.x is None:
        raise RuntimeError(f"MILP failed to return a feasible recommendation selection: {result.message}")
    selected_indices = [idx for idx in range(len(moves)) if result.x[idx] >= 0.5]
    selected = [moves[idx] for idx in selected_indices]
    selected_scores = {
        kind: round(sum(component.get(kind, 0) for idx, component in enumerate(score_components)
                        if idx in selected_indices), 2)
        for kind in ("margin_opportunity", "ageing_recovery_proxy")
    }
    return selected, {
        "optimizer": "scipy_milp",
        "status": "optimal" if result.success else "feasible_time_limit",
        "objective": "lead-time-adjusted gross-margin uplift plus an ageing-risk recovery proxy, under operating constraints",
        "selected_score_components": selected_scores,
        "candidate_count": len(moves),
        "selected_count": len(selected),
    }


def _greedy_limit(moves: list[dict[str, Any]], tables: dict[str, list[dict[str, Any]]]
                  ) -> list[dict[str, Any]]:
    stores = {str(field(row, "store_id", default="")): row for row in tables["stores"]}
    limits = {
        str(field(row, "area_manager_id", default="")):
        max(0, int(as_number(field(row, "max_alerts_per_week", "max_alerts"))))
        for row in tables["area_managers"]
    }
    items = {str(field(row, "sku_id", default="")): row for row in tables["items"]}
    def score(move: dict[str, Any]) -> float:
        item = items.get(move["sku_id"], {})
        qty = int(move["qty"])
        if move["move_type"] == "STORE_TO_WAREHOUSE":
            age_factor = min(3.0, max(1.0, as_number(move.get("stock_age_days")) / 120.0))
            return max(0.01, as_number(field(item, "cost_price")) * qty * age_factor * 0.1)
        margin = max(0.0, as_number(field(item, "mrp")) - as_number(field(item, "cost_price")))
        velocity = max(0.0, as_number(move.get("daily_velocity")))
        sell_probability = max(0.03, min(0.96,
                                         as_number(move.get("sell_probability_pct")) / 100.0))
        affinity = max(0.0, min(100.0, as_number(move.get("store_affinity_score"))))
        lead = 0
        if move.get("dispatch_date") and move.get("arrival_date"):
            lead = max(0, (date.fromisoformat(move["arrival_date"])
                           - date.fromisoformat(move["dispatch_date"])).days)
        horizon = max(1, 14 - lead)
        if move["move_type"] == "STORE_TO_STORE":
            velocity = max(0.0, velocity - as_number(move.get("donor_daily_velocity")))
            age_days = as_number(move.get("stock_age_days"))
            recovery = (as_number(field(item, "cost_price")) * qty
                        * min(3, max(1, age_days / 120)) * 0.1 if age_days >= 120 else 0)
        else:
            recovery = 0
        expected_units = min(qty, velocity * horizon)
        urgency = 1.0 / max(1.0, as_number(move.get("donor_cover_after_days"), 60))
        return max(
            0.01,
            margin * expected_units * (1.0 + urgency)
            * (0.5 + sell_probability)
            * (1.0 + affinity / 100.0)
            + recovery,
        )

    used = defaultdict(int)
    selected = []
    for move in sorted(moves, key=score, reverse=True):
        involved = {str(field(stores[loc], "area_manager_id", default=""))
                    for loc in (move["from_location"], move["to_location"]) if loc in stores}
        if any(used[manager] >= limits.get(manager, 0) for manager in involved):
            continue
        selected.append(move)
        for manager in involved:
            used[manager] += 1
    return selected
