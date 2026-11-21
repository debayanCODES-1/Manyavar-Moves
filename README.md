# Manyavar Stock Mover

An approval-first inventory decision system for the Manyavar stock-movement
brief. The product combines a Node.js web/API gateway, a Python FastAPI data and
decision service, a LightGBM demand forecast, constrained move selection,
deterministic rule validation, persisted approvals, and an optional Gemini
merchandising copilot.

The UI pairs Manyavar's ivory and maroon identity with jade, indigo, and amber
data accents, glass panels, editorial stock photography, and loading states.
The images are illustrative stock photography, not product-specific catalogue
images. On a clean install the backend generates a clearly labeled synthetic
demo dataset and calculates its forecasts and recommendations; upload the
shared workbook or CSV bundle to replace it with provided data.

## Start locally

Requirements: Node.js 18+, Python 3.11+ (3.14 works with current wheels).

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r backend/requirements.txt
cp .env.example .env
```

Demo role sign-in is enabled by default for local showcases; anyone can choose
a demo role without a password. Add secrets only to the local, git-ignored
`.env` file and never commit API keys. In one terminal start FastAPI:

```bash
uvicorn backend.main:app --reload --env-file .env --host 127.0.0.1 --port 8000
```

In another terminal start the Node.js web server:

```bash
npm start
```

Node reads the same local `.env` file automatically.

Open [http://localhost:3000](http://localhost:3000) and choose a role. A
synthetic 20-store / 385-SKU dataset is available immediately if no uploaded
dataset exists; the same backend forecast, allocation, and validation pipeline
calculates its visible dashboard results. The dashboard identifies this data
as synthetic. Upload the workbook on the **Data & rules** page or from the
welcome banner to replace it. The API accepts an `.xlsx`
workbook or a `.zip` archive containing one `.csv` file per table. Required
tables are `stores`, `area_managers`, `items`, `bills`, `bill_lines`,
`stock_snapshot`, `stock_weekly`, `rules`, `min_display`, `lead_times`, and
`flagship_protected`. `salespeople`, `roster_daily`, `footfall_daily`,
`warehouse_inbound`, and `calendar` are optional; roster/footfall metrics
enrich store analytics and calendar features improve the learned forecast.
The upload is validated before its recommendations replace the current dataset.

The FastAPI process stores the last uploaded source, approval decisions, and
analysis state in `DATA_DIR` (default `./data`). It reloads the latest source
on restart. The daily scheduler recomputes from that most recently uploaded
snapshot; it does not fetch new ERP/WMS data. To make the inputs themselves
fully automatic, connect a scheduled export or inventory-system adapter to
the upload API. No source-system credentials or integration details were
provided in the brief, so none are assumed here.

### Docker

```bash
cp .env.example .env
# Set DEMO_MODE=false, APPROVAL_TOKEN, and GEMINI_API_KEY for a shared deployment.
docker compose up --build
```

Open [http://localhost:3000](http://localhost:3000). The named Docker volume
persists uploaded data and decisions.

## Product capabilities

* **Warehouse → store:** prioritized shortages, available warehouse stock,
  planned inbound supply, and dispatch recommendations from the single
  `WH-KOL` warehouse in Kolkata. Warehouse-to-store lead times follow the
  provided route table: 3 days to East, 4 to North, and 6 to South. Returns to
  the warehouse use the same zone-specific lane.
* **Store → store:** send stock from available donor surplus to uncovered
  demand; respect donor cover, category display minimums, S++ flagship style
  protections, capacity, and lead time.
* **Ageing stock:** identify units past the configurable 120-day operating
  heuristic and pull back stock above the store's category display floor when
  the rebalancing pass found no safe destination. Adjust via
  `AGE_THRESHOLD_DAYS` or the standalone CLI `--age-days` option.
* **Explainable review:** each move has urgency, from/to locations, SKU and
  style, quantity, dispatch/arrival dates, demand velocity, and a concise
  reason. The Logistics dashboard surfaces the next automation-ranked move,
  separates approved in-progress moves from completed receipts, and keeps a
  completion audit. A person must validate the displayed recommendation before
  approval, then verify the SKU, route, and exact received quantity before
  marking logistics complete. These records survive restart. Completion is a
  human-reported receipt, not a physical shipment signal from an ERP/WMS;
  connecting an execution integration is still required for live tracking.
* **Role-based demo access:** admin, stock manager, validator, area manager,
  analyst, and read-only viewer sessions have distinct permissions. Area
  managers select their AM account and are limited to that area for store
  views and decisions. Approval records retain the acting role and name.
* **Product intelligence:** browse every SKU with generated copy based only on
  catalogue fields, product revenue and units, recent availability-corrected
  velocity, current store/warehouse stock, oldest-unit age, weekly trends, and
  a same-SKU comparison across stores. Missing stock exposure is shown as
  unknown rather than interpreted as weak demand.
* **Bill analyzer:** compare recent top sellers, largest bills, and
  exposure-confirmed unsold stock by store. It links a dormant SKU to another
  selling store and shows a “ready move” only when the rule engine has an
  actual pending safe recommendation.
* **Store detail:** drill into recent weekly sales/stock, top products,
  exposure-confirmed dormant inventory, capacity, and category position.
* **Network view:** inventory, warehouse stock, historic sales value, store
  capacity, category balance, recommendations, and operational exceptions.
* **Availability-aware demand:** use the trailing observed stock weeks and
  availability exposure so a zero-sale size with zero availability is not
  treated as proof of weak demand. Pool style-level signal across sizes.
* **Festival and wedding context:** calendar dates, category, tier, price,
  style, and recent velocity feed the optional pooled model.
* **Manager attention:** respect each area manager's `max_alerts_per_week`.
* **Optional Gemini Q&A:** ask questions about the computed recommendation set.
  Gemini receives aggregate stock and the move list, and is instructed not to
  invent arithmetic or authorize changes. If Gemini is unconfigured or
  unavailable, the backend answers from computed network, SKU, store, move,
  and forecast data.
* **Daily recompute:** `AUTO_RUN_INTERVAL_HOURS` defaults to 24 and recomputes
  recommendations from the persisted latest dataset. Set it to `0` to disable.

## Model and decision design

The system keeps prediction, optimization, and rule enforcement distinct:

1. **Forecast.** `backend/app/forecast.py` trains a pooled LightGBM regressor
   with a Tweedie objective on observed store/SKU/week availability-corrected
   sales. Category, style, SKU, tier, price, calendar seasonality, festival,
   wedding, recent SKU velocity, and recent pooled style velocity are model
   features. It reports a time-ordered two-week holdout MAE/WAPE. If there are
   too few observations or the model dependency is missing, it visibly falls
   back to sales divided by in-stock days; this is not represented as a trained
   model.
2. **Allocation.** `stock_mover.py` builds feasible warehouse, lateral-transfer,
   and age-return candidates in urgency order. `backend/app/optimizer.py` uses
   a binary SciPy MILP to select a high expected gross-margin subset under
   aggregate supply, category display, capacity, 30-day donor-cover, and
   manager-alert limits. Pullbacks receive a modest ageing-risk recovery proxy,
   not a claim of realized profit. The optimizer considers the full candidate
   pool before applying each manager's alert cap. If SciPy is unavailable, the service uses its safe
   priority fallback. This is a practical candidate-selection adaptation of a
   network-allocation MIP, not a claim to reproduce Zara's proprietary system
   or the full stochastic optimization from the paper.
3. **Validator.** The deterministic Python validator checks protected S++
   styles, capacity, category display minimums, available stock, and minimum
   donor cover before a recommendation can reach the API. Transit is included
   in projected store cover and excluded from dispatchable on-hand units.
4. **Explanation.** The recommendation engine writes the reason from computed
   values. Gemini is only a question-answering aid and is never the source of
   move quantities or rule decisions.

The ageing threshold (120 days) is an explicit configurable heuristic because
the brief does not define one. The provided data also does not define
style-specific size-run/display policy, so the engine uses the supplied
tier/category display minimum rather than inventing a required size assortment.

## Configuration

| Variable | Purpose | Default |
|---|---|---|
| `GEMINI_API_KEY` | Gemini API key for the optional Q&A feature | unset |
| `DEMO_MODE` | Enable passwordless, role-based demo sessions | `true` |
| `APPROVAL_TOKEN` | Optional gate for demo sign-in, or required team token when demo mode is off | unset |
| `APP_SESSION_SECRET` | HMAC secret for the HttpOnly, 8-hour session cookie | random per process in local mode |
| `AUTO_RUN_INTERVAL_HOURS` | Recompute interval for the latest uploaded snapshot | `24` |
| `AGE_THRESHOLD_DAYS` | Ageing-stock pullback heuristic threshold | `120` |
| `DATA_DIR` | Persistent uploads and approval decisions | `./data` |
| `FASTAPI_URL` | Node gateway's FastAPI upstream | `http://127.0.0.1:8000` |
| `PORT` | Node gateway listen port | `3000` |

Demo mode is intentionally open and is not production authentication. For any
shared deployment, set `DEMO_MODE=false`, a long random `APPROVAL_TOKEN`, a
separate random `APP_SESSION_SECRET`, and terminate HTTPS at a trusted reverse proxy.
The FastAPI port should remain private; the included Compose setup exposes only
the Node gateway.

## API

The gateway proxies FastAPI routes. Useful endpoints:

* `GET /api/health`, `GET /api/summary`, `GET /api/stores`,
  `GET /api/insights`
* `GET /api/products`, `GET /api/products/{sku_id}`,
  `GET /api/analytics/meta`, `GET /api/stores/{store_id}`,
  `GET /api/bill-analysis?store_id=...`
* `POST /api/upload` (`multipart/form-data`, field `file`), `POST /api/run`
* `GET /api/recommendations` with optional `move_type`, `decision`, `priority`,
  `search`, and `limit` filters
* `PATCH /api/recommendations/{id}` with
  `{"status":"approved","validated":true,"validated_qty":12}` after reviewing
  the recommendation, or `{"status":"rejected","note":"..."}`
* `GET /api/logistics` for the next eligible recommendation, ongoing transfers,
  and human-validated completed transfers
* `POST /api/logistics/{id}/complete` to record a verified receipt
* `GET /api/export?format=csv|json`, `POST /api/ask`

In demo mode, the browser obtains an HttpOnly signed role session. Upload/run
is limited to admin and stock-manager roles; approvals are limited to admin,
stock manager, validator, and area manager. In production mode, token sign-in
creates an admin session; writes are rejected without a valid session.

## Tests

```bash
python -m unittest discover -s tests -v
node --check server.mjs
node --check public/app.js
```

Tests cover automatic synthetic demo startup and model training, Gemini-free
copilot answers, validated logistics completion, workbook upload, in-transit
demand, protected styles, display minimums, donor cover, capacity, alert limits,
Kolkata-to-zone warehouse routes, and safe optimizer behavior.

## Research references

The algorithm is inspired by the participant-provided research; it is adapted
to the explicit Manyavar data and rules rather than asserted to be an exact
reimplementation.

* Caro & Gallien (2010), *Inventory Management of a Fast-Fashion Retail
  Network*: [INFORMS](https://pubsonline.informs.org/doi/fpi/10.1287/opre.1090.0698),
  [MIT record](https://dspace.mit.edu/handle/1721.1/39810?show=full).
* M5/modern retail forecasting discussion:
  [arXiv:2310.13029](https://ar5iv.labs.arxiv.org/html/2310.13029) and
  [arXiv:2310.12809](https://arxiv.org/abs/2310.12809).
* Intermittent-demand methods:
  [arXiv:2103.08250](https://arxiv.org/abs/2103.08250).
* Retail replenishment and lateral transshipment:
  [arXiv:2410.18571](https://arxiv.org/abs/2410.18571).
* Deep RL is deliberately not used in the daily approval loop; it needs a
  validated simulator and a different operating objective:
  [arXiv:2306.11246](https://arxiv.org/abs/2306.11246).

## Dataset availability

Uploaded workbooks, approval history, and any local sample bundle live under
the git-ignored `data/` directory. They are intentionally excluded from source
control; load the hackathon dataset through the app to generate current
recommendations and analytics.
