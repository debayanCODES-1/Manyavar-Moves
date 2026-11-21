# Manyavar Stock Mover

Stock Mover turns the hackathon stock, sales, and rules data into a prioritized
movement list for warehouse replenishment, store rebalancing, and aged-stock
pullbacks. Every output row includes a dispatch/arrival date and a one-line
reason.

## Run

Use the supplied Excel workbook:

```bash
python stock_mover.py /path/to/stock_mover.xlsx -o recommendations.csv
```

Or use a directory containing one CSV per table, with filenames matching the
table names below:

```bash
python stock_mover.py /path/to/csv-tables -o recommendations.csv
```

Use `-o recommendations.json` for JSON output. Excel input requires
`openpyxl` (`python -m pip install openpyxl`); CSV input uses only the Python
standard library. Run the focused tests with `python -m unittest discover -s
tests`.

Required tables: `stores`, `area_managers`, `items`, `bills`, `bill_lines`, `stock_snapshot`,
`stock_weekly`, `min_display`, `lead_times`, and `flagship_protected`.
`warehouse_inbound` is optional. CSV headers use the column names in the data
guide; headers and sheet/file names are case- and punctuation-insensitive.
`stock`/`stock_snapshots` can be used as aliases for `stock_snapshot`.

The rules tables need these fields (accepted alternatives are in parentheses):

* `min_display`: `tier`, `category`, and `min_display_units` (`min_units`,
  `minimum_units`, or `units`).
* `lead_times`: `from_type`, `to_type`, and `lead_time_days` (`lead_days`,
  `days`, or `lead_time`). Types must identify warehouse/store endpoints.
* `flagship_protected`: `store_id` and `style_id`.

## Decision policy

* Velocity uses bill-line units over the 12-week stock-observation window,
  divided by the SKU's in-stock days from those weekly snapshots. If there are sales but no recorded
  in-stock days, it uses a 90-day denominator. A zero-sales SKU with no in-stock
  days has unknown demand (zero computed velocity), so it is not treated as
  evidence of slow demand.
* Projected store stock includes on-hand and in-transit units. Only on-hand
  units can be dispatched. Warehouse planned inbound is available for future
  dispatch; the output date is moved to the earliest expected receipt when that
  supply is needed.
* Replenishment aims for lead time plus 30 days of SKU cover, and fills any
  unmet category display minimum. Capacity is respected and warehouse supply
  goes to the most urgent shortages first. Priority is P1 when current cover is
  within the relevant lead time; otherwise it is P2.
* Store-to-store moves cannot use protected flagship styles, must preserve the
  category display minimum, and leave the donor at or above 30 days of cover.
  Store-to-warehouse pullbacks are limited to stock above the category minimum.
* Output is prioritized and capped to each affected area manager's
  `max_alerts_per_week`; a store-to-store move counts against both managers.
* Aged pullbacks use the oldest received date and default to 120 days. Adjust
  this policy with `--age-days N`. This is a transparent operating heuristic,
  not a threshold specified by the brief.

The source brief did not include the actual workbook/CSV files. The analyzer
therefore produces a live recommendation list when run against the provided
dataset, but no real store/SKU recommendations are checked into this repository.
