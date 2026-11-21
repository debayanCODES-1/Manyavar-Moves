"""FastAPI service for workbook ingestion, forecasts, move decisions and approvals."""

from __future__ import annotations

import asyncio
import base64
import csv
import hashlib
import hmac
import io
import json
import os
import re
import sqlite3
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Any
from urllib.parse import urlencode

from fastapi import FastAPI, File, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from stock_mover import (  # noqa: E402
    DEFAULT_AGE_DAYS, DEFAULT_MIN_COVER_DAYS, WAREHOUSE,
    approval_threshold_from_rules, as_date, as_number,
    build_lead_time_overrides, build_recommendations, field, load_tables,
    validate_proposal_selection,
)
from backend.app.demo_data import build_demo_tables  # noqa: E402
from backend.app.forecast import forecast_velocity  # noqa: E402
from backend.app.optimizer import optimize_proposals  # noqa: E402

IS_VERCEL = os.environ.get("VERCEL") == "1"
DATABASE_URL = os.environ.get("DATABASE_URL", "")
SESSION_SECRET = os.environ.get("APP_SESSION_SECRET", "")
APPROVAL_TOKEN = os.environ.get("APPROVAL_TOKEN", "")
DEMO_MODE = os.environ.get("DEMO_MODE", "true").lower() != "false"
CRON_SECRET = os.environ.get("CRON_SECRET", "")
DATA_DIR = Path(os.environ.get(
    "DATA_DIR",
    "/tmp/manyavar-stock-mover" if IS_VERCEL else ROOT / "data",
))
DATA_DIR.mkdir(parents=True, exist_ok=True)
API_SHARED_SECRET = os.environ.get("API_SHARED_SECRET", "")

app = FastAPI(title="Manyavar Stock Mover API", version="1.0.0")
_state_request_lock = asyncio.Lock()
_database_ready = False
_ROLE_PERMISSIONS = {
    "admin": ("read", "upload", "run", "approve", "ask"),
    "stock_manager": ("read", "upload", "run", "approve", "ask"),
    "validator": ("read", "approve", "ask"),
    "area_manager": ("read", "approve", "ask"),
    "analyst": ("read", "ask"),
    "viewer": ("read",),
}
state: dict[str, Any] = {
    "tables": None, "moves": [], "model": {}, "optimizer": {},
    "source_name": None, "generated_at": None, "approvals": {},
    "source_blob_path": None, "workflow_audit": [],
    "is_demo_data": False,
    "products": {}, "sales_weekly": {}, "stock_weekly": {}, "bill_summaries": [],
    "sku_store_sales_weekly": {}, "sku_store_stock_weekly": {},
}


def _encode_urlsafe(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode_urlsafe(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _session_from_request(request: Request) -> dict[str, Any] | None:
    authorization = request.headers.get("authorization", "")
    bearer = authorization[7:] if authorization.startswith("Bearer ") else ""
    token = request.cookies.get("stock_mover_session") or bearer
    try:
        payload, signature = token.rsplit(".", 1)
        if not SESSION_SECRET:
            return None
        expected = _encode_urlsafe(hmac.new(
            SESSION_SECRET.encode(), payload.encode(), hashlib.sha256,
        ).digest())
        if not hmac.compare_digest(signature, expected):
            return None
        session = json.loads(_decode_urlsafe(payload))
        if session.get("expires", 0) <= int(datetime.now(timezone.utc).timestamp() * 1000):
            return None
        role = session.get("role")
        if role not in _ROLE_PERMISSIONS:
            return None
        return {
            "name": str(session.get("name") or "Merchandising team"),
            "role": role,
            "area_manager_id": session.get("area_manager_id") or None,
            "permissions": list(_ROLE_PERMISSIONS[role]),
        }
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


def _session_response(role: str, name: str, area_manager_id: str | None = None) -> JSONResponse:
    payload = _encode_urlsafe(json.dumps({
        "name": name,
        "role": role,
        "area_manager_id": area_manager_id,
        "expires": int(datetime.now(timezone.utc).timestamp() * 1000) + 8 * 60 * 60 * 1000,
    }, separators=(",", ":")).encode())
    signature = _encode_urlsafe(hmac.new(
        SESSION_SECRET.encode(), payload.encode(), hashlib.sha256,
    ).digest())
    token = f"{payload}.{signature}"
    return JSONResponse(
        content={
            "ok": True,
            "user": {
                "name": name,
                "role": role,
                "area_manager_id": area_manager_id,
                "permissions": list(_ROLE_PERMISSIONS[role]),
            },
            "sessionToken": token,
        },
        headers={
            "set-cookie": (
                f"stock_mover_session={token}; HttpOnly; Secure; SameSite=Lax; "
                "Path=/; Max-Age=28800"
            ),
        },
    )


def _permission_for(method: str, path: str) -> str | None:
    if path in {"/api/upload", "/api/upload-blob"} and method == "POST":
        return "upload"
    if path == "/api/run" and method == "POST":
        return "run"
    if path == "/api/ask" and method == "POST":
        return "ask"
    if path.startswith("/api/recommendations/") and method == "PATCH":
        return "approve"
    if path.startswith("/api/logistics/") and method == "POST":
        return "approve"
    return None


def _json_default(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if hasattr(value, "item"):
        return value.item()
    raise TypeError(f"Value of type {type(value).__name__} cannot be persisted.")


def _database_payload() -> dict[str, Any]:
    return json.loads(json.dumps(state, default=_json_default))


def _restore_database_payload(payload: dict[str, Any]) -> None:
    state.clear()
    state.update(payload)
    state.setdefault("approvals", {})
    state.setdefault("workflow_audit", [])
    state.setdefault("source_blob_path", None)


async def _ensure_database_schema(connection: Any) -> None:
    await connection.execute("""
        CREATE TABLE IF NOT EXISTS stock_mover_app_state (
            state_key TEXT PRIMARY KEY,
            payload JSONB NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)


async def _load_database_payload(connection: Any) -> bool:
    await _ensure_database_schema(connection)
    cursor = await connection.execute(
        "SELECT payload FROM stock_mover_app_state WHERE state_key = 'current'"
    )
    row = await cursor.fetchone()
    if not row:
        return False
    _restore_database_payload(row[0])
    return True


async def _save_database_payload(connection: Any) -> None:
    from psycopg.types.json import Jsonb

    await connection.execute(
        """
        INSERT INTO stock_mover_app_state (state_key, payload, updated_at)
        VALUES ('current', %s, NOW())
        ON CONFLICT (state_key) DO UPDATE
        SET payload = EXCLUDED.payload, updated_at = EXCLUDED.updated_at
        """,
        (Jsonb(_database_payload()),),
    )


@app.middleware("http")
async def require_gateway_secret(request: Request, call_next):
    if API_SHARED_SECRET and request.url.path.startswith("/api/"):
        supplied = request.headers.get("x-stockmover-api-secret", "")
        if not hmac.compare_digest(supplied, API_SHARED_SECRET):
            return JSONResponse(status_code=401, content={"detail": "Gateway authentication required."})
    return await call_next(request)


@app.middleware("http")
async def vercel_request_middleware(request: Request, call_next):
    if not IS_VERCEL or not request.url.path.startswith("/api/"):
        return await call_next(request)

    path = request.url.path
    auth_routes = {
        "/api/auth/status", "/api/auth/demo-login",
        "/api/auth/login", "/api/auth/logout",
    }
    if path in auth_routes:
        return await call_next(request)

    user = _session_from_request(request)
    if path != "/api/health" and path != "/api/cron/run" and (DEMO_MODE or APPROVAL_TOKEN):
        if not user:
            return JSONResponse(
                status_code=401,
                content={"detail": "Sign in with a merchandising account to access this resource."},
            )
        permission = _permission_for(request.method, path)
        if permission and permission not in user["permissions"]:
            return JSONResponse(
                status_code=403,
                content={"detail": f"Your {user['role']} role cannot {permission} this resource."},
            )
        forwarded_headers = [
            (key, value) for key, value in request.scope["headers"]
            if key.lower() not in {
                b"x-stockmover-user", b"x-stockmover-role", b"x-stockmover-area-manager",
            }
        ]
        forwarded_headers.extend([
            (b"x-stockmover-user", user["name"].encode()),
            (b"x-stockmover-role", user["role"].encode()),
        ])
        if user["area_manager_id"]:
            forwarded_headers.append((
                b"x-stockmover-area-manager", user["area_manager_id"].encode(),
            ))
        request.scope["headers"] = forwarded_headers

    is_mutation = (
        request.method in {"POST", "PATCH", "PUT", "DELETE"}
        or path == "/api/cron/run"
    )
    try:
        import psycopg

        async with _state_request_lock:
            async with await psycopg.AsyncConnection.connect(
                DATABASE_URL, connect_timeout=8,
            ) as connection:
                async with connection.transaction():
                    if is_mutation:
                        await connection.execute("SELECT pg_advisory_xact_lock(724106202)")
                    loaded = await _load_database_payload(connection)
                    if not loaded:
                        state["approvals"] = {}
                        _run_analysis_tables(
                            build_demo_tables(), "Generated synthetic demo data",
                            is_demo_data=True,
                        )
                    response = await call_next(request)
                    if is_mutation and response.status_code < 400:
                        await _save_database_payload(connection)
                    return response
    except psycopg.Error as exc:
        print(f"Vercel state database request failed: {exc}", file=sys.stderr)
        return JSONResponse(
            status_code=503,
            content={"detail": "Persistent app storage is temporarily unavailable."},
        )


class DemoLoginRequest(BaseModel):
    role: str
    area_manager_id: str | None = None
    token: str = ""


class TokenLoginRequest(BaseModel):
    token: str


@app.get("/api/auth/status")
def auth_status(request: Request) -> dict[str, Any]:
    user = _session_from_request(request)
    required = DEMO_MODE or bool(APPROVAL_TOKEN)
    return {
        "required": required,
        "demoMode": DEMO_MODE,
        "authenticated": not required or user is not None,
        "user": user,
        "roles": [
            {"id": role, "name": label}
            for role, label in (
                ("admin", "Demo Admin"),
                ("stock_manager", "Stock Manager"),
                ("validator", "Rule Validator"),
                ("area_manager", "Area Manager"),
                ("analyst", "Sales Analyst"),
                ("viewer", "Read-only Viewer"),
            )
        ],
        "tokenRequired": bool(APPROVAL_TOKEN),
    }


@app.post("/api/auth/demo-login")
def demo_login(payload: DemoLoginRequest) -> JSONResponse:
    if not DEMO_MODE:
        raise HTTPException(404, "Demo sign-in is disabled.")
    if payload.role not in _ROLE_PERMISSIONS:
        raise HTTPException(400, "Choose a valid demo role.")
    if (
        APPROVAL_TOKEN
        and payload.role != "viewer"
        and not hmac.compare_digest(payload.token, APPROVAL_TOKEN)
    ):
        raise HTTPException(401, "A valid team access token is required for this role.")
    if payload.role == "area_manager" and not re.fullmatch(
        r"AM-\d{2}", payload.area_manager_id or "",
    ):
        raise HTTPException(400, "Select a valid area manager account.")
    area_manager_id = payload.area_manager_id if payload.role == "area_manager" else None
    role_names = {
        "admin": "Demo Admin",
        "stock_manager": "Stock Manager",
        "validator": "Rule Validator",
        "area_manager": "Area Manager",
        "analyst": "Sales Analyst",
        "viewer": "Read-only Viewer",
    }
    name = role_names[payload.role]
    if area_manager_id:
        name = f"{name} · {area_manager_id}"
    return _session_response(payload.role, name, area_manager_id)


@app.post("/api/auth/login")
def token_login(payload: TokenLoginRequest) -> JSONResponse:
    if not APPROVAL_TOKEN:
        raise HTTPException(404, "Token sign-in is not configured.")
    if not hmac.compare_digest(payload.token, APPROVAL_TOKEN):
        raise HTTPException(401, "That access token was not recognised.")
    return _session_response("admin", "Administrator")


@app.post("/api/auth/logout", status_code=204)
def auth_logout() -> Response:
    return Response(
        status_code=204,
        headers={"set-cookie": (
            "stock_mover_session=; HttpOnly; Secure; SameSite=Lax; "
            "Path=/; Max-Age=0"
        )},
    )


class Decision(BaseModel):
    status: str = Field(pattern="^(approved|rejected|pending)$")
    note: str = Field(default="", max_length=500)
    validated: bool = False
    validated_qty: int | None = Field(default=None, ge=0)


class LogisticsCompletion(BaseModel):
    received_qty: int = Field(ge=0)
    sku_verified: bool
    route_verified: bool
    note: str = Field(default="", max_length=500)


class BlobUploadRequest(BaseModel):
    pathname: str
    filename: str


async def _download_blob_to_temp(pathname: str, suffix: str) -> Path:
    from vercel import blob

    temporary = tempfile.NamedTemporaryFile(
        prefix="dataset-", suffix=suffix, dir=DATA_DIR, delete=False,
    )
    path = Path(temporary.name)
    size = 0
    try:
        with temporary:
            async with blob.stream_blob(pathname, access="private") as download:
                if download.metadata.size and download.metadata.size > 200 * 1024 * 1024:
                    raise HTTPException(413, "File exceeds the 200 MB upload limit.")
                async for chunk in download:
                    size += len(chunk)
                    if size > 200 * 1024 * 1024:
                        raise HTTPException(413, "File exceeds the 200 MB upload limit.")
                    temporary.write(chunk)
        if size == 0:
            raise HTTPException(400, "Uploaded file is empty.")
        return path
    except HTTPException:
        path.unlink(missing_ok=True)
        raise
    except blob.BlobNotFoundError as exc:
        path.unlink(missing_ok=True)
        raise HTTPException(404, "Uploaded workbook was not found in private storage.") from exc
    except (blob.BlobError, OSError) as exc:
        path.unlink(missing_ok=True)
        raise HTTPException(502, f"Could not retrieve the uploaded workbook: {exc}") from exc


def _product_description(item: dict[str, Any]) -> str:
    name = str(field(item, "style_name", default=field(item, "style_id", default="This design")))
    category = str(field(item, "category", default="occasionwear"))
    size = str(field(item, "size", default=""))
    price = as_number(field(item, "mrp"))
    cloth_type = str(field(item, "cloth_type", "fabric", default=""))
    material = str(field(item, "material", "fabric", default=""))
    colour = str(field(item, "colour", "color", default=""))
    care = str(field(item, "care", "care_instructions", default=""))
    assortment = "year-round core collection" if as_number(field(item, "is_core_style")) else "seasonal fashion collection"
    packaging = as_number(field(item, "is_packaging"))
    if packaging:
        return f"{name} is a packaging SKU in size {size or 'free size'}, listed at {rupee(price)}. It is tracked separately from merchandise demand."
    size_copy = f" in size {size}" if size else ""
    detail_parts = []
    if cloth_type:
        detail_parts.append(f"cloth type {cloth_type}")
    if material:
        detail_parts.append(f"material {material}")
    if colour:
        detail_parts.append(f"colour {colour}")
    detail_copy = f" The catalogue identifies {'; '.join(detail_parts)}." if detail_parts else ""
    care_copy = f" Care: {care}." if care else ""
    return (
        f"{name} is a {category} design from Manyavar's {assortment}{size_copy}. "
        f"The catalogue lists this variant at {rupee(price)} MRP. It is suited to "
        f"wedding celebrations, festive gatherings, and occasion dressing.{detail_copy}{care_copy}"
        " Fabric, colour, and care details are not inferred beyond catalogue fields."
    )


def rupee(value: float) -> str:
    return f"₹{value:,.0f}"


class Question(BaseModel):
    question: str = Field(min_length=3, max_length=500)


def _read_decisions() -> dict[str, Any]:
    legacy_path = DATA_DIR / "decisions.json"
    try:
        legacy = json.loads(legacy_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        legacy = {}
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not read legacy approval decisions: {exc}") from exc

    connection = None
    try:
        connection = sqlite3.connect(DATA_DIR / "manyavar.sqlite3", timeout=10)
        with connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS workflow_decisions (
                    move_id TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS workflow_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    move_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    role TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    details_json TEXT NOT NULL
                )
            """)
            stored = connection.execute(
                "SELECT COUNT(*) FROM workflow_decisions"
            ).fetchone()[0]
            if not stored and legacy:
                connection.executemany(
                    "INSERT INTO workflow_decisions (move_id, payload_json, updated_at) "
                    "VALUES (?, ?, ?)",
                    [
                        (move_id, json.dumps(payload), str(payload.get("updated_at", "")))
                        for move_id, payload in legacy.items()
                    ],
                )
            rows = connection.execute(
                "SELECT move_id, payload_json FROM workflow_decisions"
            ).fetchall()
        return {move_id: json.loads(payload) for move_id, payload in rows}
    except (OSError, sqlite3.Error, json.JSONDecodeError, TypeError, AttributeError) as exc:
        raise RuntimeError(f"Could not read persisted approval decisions: {exc}") from exc
    finally:
        if connection is not None:
            connection.close()


def _write_decisions(move_id: str | None = None, event_type: str | None = None) -> None:
    if IS_VERCEL:
        if move_id and event_type:
            payload = state["approvals"][move_id]
            state["workflow_audit"].append({
                "move_id": move_id,
                "event_type": event_type,
                "actor": str(payload.get("actor", "")),
                "role": str(payload.get("role", "")),
                "occurred_at": str(
                    payload.get("updated_at") or datetime.now(timezone.utc).isoformat()
                ),
                "details": payload,
            })
        return
    connection = None
    try:
        connection = sqlite3.connect(DATA_DIR / "manyavar.sqlite3", timeout=10)
        with connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS workflow_decisions (
                    move_id TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS workflow_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    move_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    role TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    details_json TEXT NOT NULL
                )
            """)
            for decision_id, payload in state["approvals"].items():
                connection.execute(
                    "INSERT INTO workflow_decisions (move_id, payload_json, updated_at) "
                    "VALUES (?, ?, ?) ON CONFLICT(move_id) DO UPDATE SET "
                    "payload_json=excluded.payload_json, updated_at=excluded.updated_at",
                    (decision_id, json.dumps(payload), str(payload.get("updated_at", ""))),
                )
            if move_id and event_type:
                payload = state["approvals"][move_id]
                occurred_at = str(payload.get("updated_at") or datetime.now(timezone.utc).isoformat())
                connection.execute(
                    "INSERT INTO workflow_audit "
                    "(move_id, event_type, actor, role, occurred_at, details_json) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (move_id, event_type, str(payload.get("actor", "")),
                     str(payload.get("role", "")), occurred_at, json.dumps(payload)),
                )
    except (OSError, sqlite3.Error, TypeError, KeyError) as exc:
        raise RuntimeError(f"Could not persist approval decisions: {exc}") from exc
    finally:
        if connection is not None:
            connection.close()


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


def _build_analytics(tables: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    items = {str(field(row, "sku_id", default="")): row for row in tables["items"]}
    stores = {str(field(row, "store_id", default="")): row for row in tables["stores"]}
    launch_dates = {
        sku: as_date(field(item, "launch_date"))
        for sku, item in items.items()
    }
    launches_by_assortment: dict[tuple[str, str], list[tuple[str, date]]] = defaultdict(list)
    for sku, item in items.items():
        launch = launch_dates[sku]
        category = str(field(item, "category", default="")).casefold()
        size = str(field(item, "size", default="")).casefold()
        style_id = str(field(item, "style_id", default=""))
        if launch and category and size and not as_number(field(item, "is_packaging")):
            launches_by_assortment[category, size].append((style_id, launch))
    calendar_events: dict[str, set[date]] = defaultdict(set)
    for row in tables.get("calendar", []):
        event_date = as_date(field(row, "date"))
        festival = str(field(row, "festival", default="")).casefold()
        seasonal_festival = any(term in festival for term in (
            "durga", "navratri", "navaratri", "wedding", "vivah",
        ))
        if event_date and (
            as_number(field(row, "is_wedding_date")) or seasonal_festival
        ):
            calendar_events[str(field(row, "state", default="")).casefold()].add(event_date)
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
    successor_launch = {}
    for sku, item in items.items():
        own_launch = launch_dates[sku]
        category = str(field(item, "category", default="")).casefold()
        size = str(field(item, "size", default="")).casefold()
        style_id = str(field(item, "style_id", default=""))
        candidates = [
            launch for other_style, launch in launches_by_assortment[category, size]
            if own_launch and other_style != style_id and own_launch < launch <= as_of
        ]
        successor_launch[sku] = max(candidates, default=None)
    units_by_sku = defaultdict(int)
    revenue_by_sku = defaultdict(float)
    recent_units_by_sku = defaultdict(int)
    recent_revenue_by_sku = defaultdict(float)
    units_by_store_sku = defaultdict(int)
    revenue_by_store_sku = defaultdict(float)
    recent_units_by_store_sku = defaultdict(int)
    recent_revenue_by_store_sku = defaultdict(float)
    last_sale_by_store_sku = {}
    sales_since_successor_by_store_sku = defaultdict(int)
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
            if store_id in stores and qty > 0:
                key = (store_id, sku)
                last_sale_by_store_sku[key] = max(
                    bill_date, last_sale_by_store_sku.get(key, date.min)
                )
                design_launch = successor_launch.get(sku)
                if design_launch and bill_date >= design_launch:
                    sales_since_successor_by_store_sku[key] += qty

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
    successor_exposure_days = defaultdict(int)
    latest_week_exposure = {}
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
        if store_id in stores:
            exposure = int(as_number(field(row, "days_in_stock")))
            launch = successor_launch.get(sku)
            if launch and week >= launch:
                possible_days = min(7, (week - launch).days + 1)
                successor_exposure_days[store_id, sku] += min(exposure, possible_days)
            current = latest_week_exposure.get((store_id, sku))
            if current is None or week > current[0]:
                latest_week_exposure[store_id, sku] = (week, exposure)
            elif week == current[0]:
                latest_week_exposure[store_id, sku] = (
                    week, min(7, current[1] + exposure)
                )

    product_index = {}
    for sku, item in items.items():
        store_performance = []
        store_stock_units = warehouse_units = recent_exposure_days = 0
        max_age_days = 0
        recent_price = (
            recent_revenue_by_sku[sku] / recent_units_by_sku[sku]
            if recent_units_by_sku[sku] else None
        )
        historical_price = (
            revenue_by_sku[sku] / units_by_sku[sku] if units_by_sku[sku] else None
        )
        margin_price = (
            recent_price if recent_price is not None
            else historical_price if historical_price is not None
            else as_number(field(item, "mrp"))
        )
        raw_cost_price = field(item, "cost_price", default=None)
        cost_price = as_number(raw_cost_price)
        unit_margin = (
            margin_price - cost_price
            if margin_price is not None and raw_cost_price not in (None, "") else None
        )
        margin_basis = (
            "recent realized selling price (post-discount)"
            if recent_price is not None
            else "all-time realized selling price (post-discount)"
            if historical_price is not None
            else "MRP estimate; no realized selling price recorded"
        )
        if raw_cost_price in (None, ""):
            margin_basis += "; cost price not supplied, margin unavailable"
        design_launch = successor_launch.get(sku)
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
            last_sale = last_sale_by_store_sku.get((store_id, sku))
            days_since_sale = (as_of - last_sale).days if last_sale else None
            last_week = latest_week_exposure.get((store_id, sku))
            exposed_last_week = bool(last_week and last_week[1] > 0)
            seasonal_window = any(
                as_of - timedelta(days=6) <= event <= as_of
                for event in calendar_events[str(field(store, "state", default="")).casefold()]
            )
            no_sale_for_seven_days = (
                projected > 0 and exposed_last_week
                and (last_sale is None or days_since_sale >= 7)
            )
            dead_stock_confirmed = (
                projected > 0
                and days_in_stock > 0
                and recent_sold == 0
                and (
                    days_since_sale is not None and days_since_sale >= 70
                    or last_sale is None and age_days is not None and age_days >= 70
                )
            )
            design_exposure = successor_exposure_days[store_id, sku]
            no_sale_after_design = (
                projected > 0 and design_launch is not None and design_exposure > 0
                and sales_since_successor_by_store_sku[store_id, sku] == 0
            )
            if dead_stock_confirmed:
                stock_risk = "dead_stock_70_day_no_sale"
                stock_risk_reason = (
                    f"No recorded sale for at least 70 days"
                    f"{f' ({days_since_sale} days)' if days_since_sale is not None else ''}, "
                    f"with {days_in_stock} recent in-stock days and {projected} unit(s) held."
                )
            elif seasonal_window and no_sale_for_seven_days:
                stock_risk = "seasonal_7_day_no_sale"
                stock_risk_reason = (
                    "No recorded sale for at least 7 days during a calendar-marked "
                    "festival or wedding window, with recent stock exposure."
                )
            elif no_sale_after_design:
                stock_risk = "no_sales_after_new_design"
                stock_risk_reason = (
                    f"No recorded sales across {design_exposure} in-stock days since "
                    f"a newer design launched on {design_launch.isoformat()}."
                )
            else:
                stock_risk = None
                stock_risk_reason = None
            store_stock_units += projected
            store_recent_price = (
                recent_revenue_by_store_sku[store_id, sku]
                / recent_units_by_store_sku[store_id, sku]
                if recent_units_by_store_sku[store_id, sku] else None
            )
            store_performance.append({
                "store_id": store_id,
                "store_name": field(store, "store_name", "name", default=store_id),
                "city": field(store, "city", default=""),
                "address": field(store, "address", "full_address", default=""),
                "store_locator_url": field(store, "store_locator_url", default=""),
                "google_maps_url": field(store, "google_maps_url", default=""),
                "tier": field(store, "tier", default=""),
                "sales_units": units_by_store_sku[store_id, sku],
                "sales_revenue": round(revenue_by_store_sku[store_id, sku], 2),
                "recent_units": recent_sold,
                "recent_revenue": round(recent_revenue_by_store_sku[store_id, sku], 2),
                "average_selling_price": round(store_recent_price, 2)
                if store_recent_price is not None else None,
                "recent_in_stock_days": days_in_stock,
                "recent_velocity": round(rate, 3) if rate is not None else None,
                "on_hand": on_hand,
                "in_transit": in_transit,
                "stock_units": projected,
                "cover_days": round(projected / rate, 1) if rate and rate > 0 else None,
                "stock_age_days": age_days,
                "last_sale_date": last_sale.isoformat() if last_sale else None,
                "days_since_sale": days_since_sale,
                "successor_launch_date": design_launch.isoformat() if design_launch else None,
                "exposure_days_since_successor": design_exposure,
                "seasonal_window_active": seasonal_window,
                "stock_risk": stock_risk,
                "stock_risk_reason": stock_risk_reason,
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
            "average_selling_price": round(margin_price, 2) if margin_price is not None else None,
            "unit_gross_margin_estimate": round(unit_margin, 2)
            if unit_margin is not None else None,
            "gross_margin_pct_estimate": round(100 * unit_margin / margin_price, 2)
            if unit_margin is not None and margin_price else None,
            "margin_basis": margin_basis,
            "recent_velocity": round(recent_units_by_sku[sku] / recent_exposure_days, 3)
            if recent_exposure_days else None,
            "recent_in_stock_days": recent_exposure_days,
            "store_stock_units": store_stock_units,
            "warehouse_units": warehouse_units,
            "network_stock_units": store_stock_units + warehouse_units,
            "aged_stock_days_max": max_age_days,
            "oldest_stock_age_days": max_age_days,
            "best_selling_store": best_historical["store_id"] if best_historical else None,
            "best_recent_store": best_recent["store_id"] if best_recent else None,
            "unsold_locations": unsold_locations,
            "stock_risk_locations": [
                row for row in store_performance if row["stock_risk"]
            ],
            "successor_launch_date": design_launch.isoformat() if design_launch else None,
            "store_performance": store_performance,
        }

    product_cohorts: dict[tuple[str, str], list[float]] = defaultdict(list)
    store_cohorts: dict[tuple[str, str], list[float]] = defaultdict(list)
    for product in product_index.values():
        cohort = (
            str(field(product, "category", default="")).casefold(),
            str(field(product, "size", default="")).casefold(),
        )
        if as_number(field(product, "is_packaging")):
            continue
        product_rate = product["recent_velocity"]
        if product_rate is not None and product["recent_in_stock_days"] > 0:
            product_cohorts[cohort].append(product_rate)
        for metric in product["store_performance"]:
            rate = metric["recent_velocity"]
            if rate is not None and metric["recent_in_stock_days"] > 0:
                store_cohorts[cohort].append(rate)

    product_thresholds = {
        cohort: _velocity_thresholds(values) for cohort, values in product_cohorts.items()
    }
    store_thresholds = {
        cohort: _velocity_thresholds(values) for cohort, values in store_cohorts.items()
    }
    for product in product_index.values():
        cohort = (
            str(field(product, "category", default="")).casefold(),
            str(field(product, "size", default="")).casefold(),
        )
        if as_number(field(product, "is_packaging")):
            product["sell_through_grade"] = "N/A"
            product["sell_through_grade_basis"] = "Packaging is excluded from merchandise velocity grading."
            continue
        median, upper_quartile = product_thresholds.get(cohort, (None, None))
        product["sell_through_grade"] = _velocity_grade(
            product["recent_velocity"], product["recent_in_stock_days"],
            product["recent_sold_units"], median, upper_quartile,
        )
        product["sell_through_grade_basis"] = (
            "Relative to the same category and size: A is at/above the cohort upper quartile, "
            "B is at/above the cohort median, C is positive but below the median; WATCH means "
            "zero recent sales with recorded exposure, UNKNOWN means no recorded exposure."
        )
        median, upper_quartile = store_thresholds.get(cohort, (None, None))
        for metric in product["store_performance"]:
            metric["sell_through_grade"] = _velocity_grade(
                metric["recent_velocity"], metric["recent_in_stock_days"],
                metric["recent_units"], median, upper_quartile,
            )

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
    _run_analysis_tables(_read_source(path), source_name or path.name)


def _run_analysis_tables(tables: dict[str, list[dict[str, Any]]],
                         source_name: str, is_demo_data: bool = False) -> None:
    velocity, model_meta = forecast_velocity(tables)
    age_threshold = int(os.environ.get("AGE_THRESHOLD_DAYS", DEFAULT_AGE_DAYS))
    raw_moves = build_recommendations(tables, age_threshold=age_threshold,
                                     velocity_override=velocity,
                                     lead_time_overrides=build_lead_time_overrides(tables))
    optimized, optimizer_meta = optimize_proposals(raw_moves, tables)
    validate_proposal_selection(optimized, tables)
    analytics = _build_analytics(tables)
    item_by_sku = {str(field(row, "sku_id", default="")): row for row in tables["items"]}
    store_by_id = {str(field(row, "store_id", default="")): row for row in tables["stores"]}
    approval_limit = approval_threshold_from_rules(tables["rules"])
    enriched = []
    for move in optimized:
        item = item_by_sku.get(move["sku_id"], {})
        product = analytics["products"].get(move["sku_id"], {})
        move = dict(move)
        mrp = as_number(field(item, "mrp"))
        raw_cost_price = field(item, "cost_price", default=None)
        cost_price = as_number(raw_cost_price)
        average_price = product.get("average_selling_price")
        if average_price is None:
            average_price = mrp
        unit_margin = (
            average_price - cost_price
            if average_price is not None and raw_cost_price not in (None, "") else None
        )
        margin_pct = 100 * unit_margin / average_price if unit_margin is not None and average_price else None
        arrival = as_date(move.get("arrival_date"))
        lead_days = max(0, (arrival - as_date(analytics["as_of"])).days) if arrival else 0
        selling_days = max(0, 14 - lead_days)
        expected_units = min(
            int(move.get("qty", 0)),
            max(0.0, as_number(move.get("daily_velocity"))) * selling_days,
        )
        move.update({
            "style_name": str(field(item, "style_name", default=move["style_id"])),
            "size": str(field(item, "size", default="")),
            "description": _product_description(item),
            "mrp": mrp,
            "cost_price": cost_price,
            "move_value_mrp": round(move["qty"] * mrp, 2),
            "estimated_unit_gross_margin": round(unit_margin, 2)
            if unit_margin is not None else None,
            "estimated_gross_margin_pct": round(margin_pct, 2)
            if margin_pct is not None else None,
            "estimated_sell_through_units_14d": round(expected_units, 2),
            "estimated_gross_profit_14d": round(unit_margin * expected_units, 2)
            if unit_margin is not None else None,
            "estimated_gross_profit_if_all_units_sell": round(unit_margin * move["qty"], 2)
            if unit_margin is not None else None,
            "margin_basis": product.get("margin_basis", "MRP estimate; no realized selling price recorded")
            + ("; cost price not supplied, margin unavailable" if raw_cost_price in (None, "") else ""),
            "profit_estimate_note": (
                "Directional gross-margin estimate before tax and overhead; uses the product's "
                "observed selling price when available, otherwise MRP."
            ),
            "approval_threshold_mrp": approval_limit,
            "requires_human_approval": move["qty"] * mrp > approval_limit,
        })
        for endpoint in ("from", "to"):
            location_id = move[f"{endpoint}_location"]
            store = store_by_id.get(location_id, {})
            move[f"{endpoint}_store_name"] = str(field(
                store, "store_name", "name", default=location_id))
            move[f"{endpoint}_store_city"] = str(field(store, "city", default=""))
            move[f"{endpoint}_store_state"] = str(field(store, "state", default=""))
            move[f"{endpoint}_store_address"] = str(field(store, "address", "full_address", default=""))
            move[f"{endpoint}_store_locator_url"] = str(field(store, "store_locator_url", default=""))
            move[f"{endpoint}_store_maps_url"] = str(field(store, "google_maps_url", default=""))
        if move["from_store_address"] and move["to_store_address"]:
            move["route_maps_url"] = "https://www.google.com/maps/dir/?" + urlencode({
                "api": "1",
                "origin": move["from_store_address"],
                "destination": move["to_store_address"],
            })
        encoded = "|".join(str(move.get(column, "")) for column in (
            "move_type", "from_location", "to_location", "sku_id", "qty", "arrival_date"))
        move["id"] = hashlib.sha256(encoded.encode()).hexdigest()[:16]
        approval = state["approvals"].get(move["id"], {})
        move["decision"] = approval.get("status", "pending")
        move["decision_note"] = approval.get("note", "")
        move["decision_actor"] = approval.get("actor", "")
        move["decision_role"] = approval.get("role", "")
        move["decision_validated"] = bool(approval.get("validated"))
        move["logistics_status"] = (
            "awaiting_approval" if move["decision"] == "pending"
            else "declined" if move["decision"] == "rejected"
            else "completed" if approval.get("completion")
            else "ongoing"
        )
        move["completion"] = approval.get("completion")
        enriched.append(move)
    state.update({
        "tables": tables, "moves": enriched, "model": model_meta,
        "optimizer": optimizer_meta, "source_name": source_name,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "is_demo_data": is_demo_data,
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
    historical_units = sum(row["sales_units"] for row in metrics)
    historical_revenue = sum(row["sales_revenue"] for row in metrics)
    recent_revenue = sum(row["recent_revenue"] for row in metrics)
    selling_price = (
        recent_revenue / recent_units if recent_units
        else historical_revenue / historical_units if historical_units
        else as_number(product.get("mrp"))
    )
    has_cost_price = product.get("cost_price") not in (None, "")
    unit_margin = (
        selling_price - as_number(product.get("cost_price"))
        if selling_price is not None and has_cost_price else None
    )
    warehouse_units = product["warehouse_units"]
    return {
        **product,
        "total_sold_units": historical_units,
        "sales_revenue": round(historical_revenue, 2),
        "recent_sold_units": recent_units,
        "recent_revenue": round(recent_revenue, 2),
        "average_selling_price": round(selling_price, 2) if selling_price is not None else None,
        "unit_gross_margin_estimate": round(unit_margin, 2)
        if unit_margin is not None else None,
        "gross_margin_pct_estimate": round(100 * unit_margin / selling_price, 2)
        if unit_margin is not None and selling_price else None,
        "margin_basis": (
            "recent realized selling price (post-discount)" if recent_units
            else "all-time realized selling price (post-discount)" if historical_units
            else "MRP estimate; no realized selling price recorded"
        ) + ("; cost price not supplied, margin unavailable" if not has_cost_price else ""),
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


def _best_pending_outbound_move(
    store_id: str,
    sku_id: str,
    allowed_target_ids: set[str] | None = None,
) -> dict[str, Any] | None:
    candidates = [
        move for move in state["moves"]
        if move["move_type"] == "STORE_TO_STORE"
        and move["from_location"] == store_id
        and move["sku_id"] == sku_id
        and move["decision"] == "pending"
        and (allowed_target_ids is None or move["to_location"] in allowed_target_ids)
    ]
    return max(candidates, key=lambda move: (
        as_number(move.get("estimated_gross_profit_14d")),
        as_number(move.get("sell_probability_pct")),
        -((as_date(move.get("arrival_date")) or date.max)
          - (as_date(state.get("as_of")) or date.min)).days,
    ), default=None)


def _similar_products(product: dict[str, Any], exclude_store_id: str | None = None,
                      allowed_store_ids: set[str] | None = None
                      ) -> list[dict[str, Any]]:
    category = str(product.get("category", "")).casefold()
    size = str(product.get("size", "")).casefold()
    price = as_number(product.get("mrp"))
    style_id = str(product.get("style_id", ""))
    material = str(product.get("material", product.get("fabric", ""))).strip().casefold()
    cloth_type = str(product.get("cloth_type", "")).strip().casefold()
    colour = str(product.get("colour", product.get("color", ""))).strip().casefold()
    if not category or not size or not price or as_number(product.get("is_packaging")):
        return []

    matches = []
    for candidate in state["products"].values():
        candidate_price = as_number(candidate.get("mrp"))
        candidate_stores = [
            row for row in candidate["store_performance"]
            if allowed_store_ids is None or row["store_id"] in allowed_store_ids
        ]
        recent_units = sum(row["recent_units"] for row in candidate_stores)
        if (
            candidate["sku_id"] == product.get("sku_id")
            or str(candidate.get("style_id", "")) == style_id
            or str(candidate.get("category", "")).casefold() != category
            or str(candidate.get("size", "")).casefold() != size
            or as_number(candidate.get("is_packaging"))
            or not candidate_price
            or recent_units <= 0
        ):
            continue
        price_difference = abs(candidate_price - price) / price
        if price_difference > 0.3 or candidate["recent_sold_units"] <= 0:
            continue
        candidate_material = str(candidate.get("material", candidate.get("fabric", ""))).strip().casefold()
        candidate_cloth = str(candidate.get("cloth_type", "")).strip().casefold()
        candidate_colour = str(candidate.get("colour", candidate.get("color", ""))).strip().casefold()
        material_match = bool(
            material and candidate_material and material == candidate_material
        )
        cloth_match = bool(
            cloth_type and candidate_cloth and cloth_type == candidate_cloth
        )
        colour_match = bool(
            colour and candidate_colour and colour == candidate_colour
        )
        match_parts = ["same category and size", "MRP within 30%"]
        if material_match:
            match_parts.append("same material")
        if cloth_match:
            match_parts.append("same cloth type")
        if colour_match:
            match_parts.append("same colour")
        demand = [
            row for row in candidate_stores
            if row["store_id"] != exclude_store_id and row["recent_units"] > 0
        ]
        best_demand = max(demand, key=lambda row: row["recent_units"], default=None)
        matches.append({
            "sku_id": candidate["sku_id"],
            "style_id": candidate.get("style_id"),
            "style_name": candidate.get("style_name"),
            "category": candidate.get("category"),
            "size": candidate.get("size"),
            "mrp": candidate_price,
            "price_difference_pct": round(price_difference * 100, 1),
            "recent_sold_units": recent_units,
            "demand_store": best_demand["store_id"] if best_demand else None,
            "demand_store_recent_units": best_demand["recent_units"] if best_demand else 0,
            "recent_velocity": candidate["recent_velocity"],
            "similarity_basis": "; ".join(match_parts),
            "material_match_available": material_match,
            "cloth_type_match": cloth_match,
            "colour_match": colour_match,
            "fabric": candidate.get("fabric", candidate.get("material", "")),
            "cloth_type": candidate.get("cloth_type", ""),
            "colour": candidate.get("colour", candidate.get("color", "")),
        })
    matches.sort(key=lambda row: (
        -row["demand_store_recent_units"], -row["recent_sold_units"],
        row["price_difference_pct"], row["sku_id"],
    ))
    return matches[:5]


def _placement_suggestion(
    product: dict[str, Any],
    store_summaries: dict[str, dict[str, Any]],
    allowed_store_ids: set[str] | None = None,
    focus_store_id: str | None = None,
) -> dict[str, Any]:
    if as_number(product.get("is_packaging")):
        return {
            "status": "not_applicable",
            "reason": "Packaging is tracked separately and is not allocated as merchandise.",
        }

    def destination_allowed(location: str) -> bool:
        return allowed_store_ids is None or location in allowed_store_ids

    selected_moves = [
        move for move in state["moves"]
        if move["sku_id"] == product.get("sku_id")
        and move["move_type"] in {"STORE_TO_STORE", "WAREHOUSE_TO_STORE"}
        and move["decision"] == "pending"
        and destination_allowed(move["to_location"])
        and (move["from_location"] == "WH-KOL" or destination_allowed(move["from_location"]))
        and (focus_store_id is None or focus_store_id in {
            move["from_location"], move["to_location"],
        })
    ]
    if selected_moves:
        move = max(selected_moves, key=lambda row: (
            as_number(row.get("estimated_gross_profit_14d")),
            as_number(row.get("sell_probability_pct")),
        ))
        return {
            "status": "ready_for_review",
            "is_executable": True,
            "source_location": move["from_location"],
            "source_store_name": move.get("from_store_name", move["from_location"]),
            "destination_location": move["to_location"],
            "destination_store_name": move.get("to_store_name", move["to_location"]),
            "destination_city": move.get("to_store_city", ""),
            "destination_address": move.get("to_store_address", ""),
            "destination_maps_url": move.get("to_store_maps_url", ""),
            "route_maps_url": move.get("route_maps_url", ""),
            "qty": move["qty"],
            "arrival_date": move["arrival_date"],
            "sell_fit_pct": move.get("sell_probability_pct"),
            "daily_velocity": move.get("daily_velocity"),
            "reason": move["reason"],
            "move_id": move["id"],
        }

    performance = [
        row for row in product.get("store_performance", [])
        if destination_allowed(str(row["store_id"]))
    ]
    direct_targets = []
    for metric in performance:
        if focus_store_id and metric["store_id"] == focus_store_id:
            continue
        rate = as_number(metric.get("recent_velocity"))
        store = store_summaries.get(str(metric["store_id"]), {})
        free_capacity = max(0, as_number(store.get("capacity")) - as_number(store.get("stock_units")))
        if rate <= 0 or metric.get("recent_in_stock_days", 0) <= 0 or free_capacity <= 0:
            continue
        cover = metric["stock_units"] / rate
        direct_targets.append((cover <= DEFAULT_MIN_COVER_DAYS,
                               rate / (1 + cover / DEFAULT_MIN_COVER_DAYS),
                               rate, metric, cover))

    evidence = "direct SKU sales"
    target = None
    target_cover = None
    target_rate = 0.0
    similar_match = None
    if direct_targets:
        _, _, target_rate, target, target_cover = max(direct_targets, key=lambda row: row[:3])
    else:
        for similar in _similar_products(
            product, exclude_store_id=focus_store_id,
            allowed_store_ids=allowed_store_ids,
        ):
            peer_product = state["products"].get(str(similar["sku_id"]), {})
            target_candidates = []
            for metric in peer_product.get("store_performance", []):
                location = str(metric["store_id"])
                store = store_summaries.get(location, {})
                free_capacity = max(
                    0, as_number(store.get("capacity")) - as_number(store.get("stock_units"))
                )
                rate = as_number(metric.get("recent_velocity"))
                if (
                    not destination_allowed(location)
                    or location == focus_store_id
                    or metric["recent_units"] <= 0
                    or rate <= 0
                    or free_capacity <= 0
                ):
                    continue
                cover = as_number(metric.get("stock_units")) / rate
                target_candidates.append((
                    cover <= DEFAULT_MIN_COVER_DAYS,
                    rate / (1 + cover / DEFAULT_MIN_COVER_DAYS),
                    metric["recent_units"], metric, rate, cover,
                ))
            if target_candidates:
                _, _, _, target, target_rate, target_cover = max(
                    target_candidates, key=lambda row: row[:3],
                )
                similar_match = similar
                evidence = "comparable SKU sales"
                break

    if target is None:
        return {
            "status": "monitor",
            "is_executable": False,
            "source_location": None,
            "destination_location": None,
            "reason": (
                "No store has verified direct or comparable demand and free capacity; "
                "keep this SKU under review rather than inventing a transfer."
            ),
        }

    source_candidates = [
        row for row in performance
        if (focus_store_id is None or row["store_id"] == focus_store_id)
        and row["store_id"] != target["store_id"]
        and row["stock_units"] > 0
        and row["recent_in_stock_days"] >= 7
        and row["recent_units"] == 0
    ]
    source = max(source_candidates, key=lambda row: (
        row.get("stock_risk") == "dead_stock_70_day_no_sale",
        as_number(row.get("stock_age_days")),
        as_number(row.get("stock_units")),
    ), default=None)
    target_store = store_summaries.get(str(target["store_id"]), {})
    direct_reason = (
        f"{target.get('store_name') or target['store_id']} recorded "
        f"{int(as_number(target.get('recent_units', 0))):,} recent units at "
        f"{target_rate:.3f} units per in-stock day, with about "
        f"{target_cover:.1f} days of cover."
        if evidence == "direct SKU sales"
        else (
            f"Comparable {similar_match['sku_id']} demand was observed at "
            f"{target.get('store_name') or target['store_id']} "
            f"({int(as_number(target.get('recent_units'))):,} recent units; "
            f"{similar_match['similarity_basis']})."
        )
    )
    if target_cover is not None and target_cover > DEFAULT_MIN_COVER_DAYS:
        direct_reason += " Destination already exceeds 30 days of cover; treat this as a placement lead, not a dispatch."
    else:
        direct_reason += " Confirm a safe donor quantity and rules before dispatch."
    return {
        "status": "placement_lead",
        "is_executable": False,
        "source_location": source["store_id"] if source else None,
        "source_store_name": source.get("store_name") if source else None,
        "source_stock_units": source.get("stock_units") if source else None,
        "destination_location": target["store_id"],
        "destination_store_name": target.get("store_name", target["store_id"]),
        "destination_city": target.get("city", ""),
        "destination_address": target.get("address", ""),
        "destination_maps_url": target.get("google_maps_url", ""),
        "destination_capacity_remaining_units": max(
            0, as_number(target_store.get("capacity")) - as_number(target_store.get("stock_units"))
        ),
        "destination_cover_days": round(target_cover, 1) if target_cover is not None else None,
        "daily_velocity": round(target_rate, 3),
        "evidence": evidence,
        "similar_sku_id": similar_match.get("sku_id") if similar_match else None,
        "reason": direct_reason,
    }


def _date_range(tables: dict[str, list[dict[str, Any]]]) -> tuple[date | None, date | None]:
    days = [as_date(field(row, "as_of_date")) for row in tables["stock_snapshot"]]
    days = [day for day in days if day]
    return min(days, default=None), max(days, default=None)


def _velocity_grade(
    velocity: float | None,
    exposure_days: float,
    sold_units: float,
    median: float | None,
    upper_quartile: float | None,
) -> str:
    if exposure_days <= 0:
        return "UNKNOWN"
    if sold_units <= 0:
        return "WATCH"
    if upper_quartile is not None and velocity is not None and velocity >= upper_quartile:
        return "A"
    if median is not None and velocity is not None and velocity >= median:
        return "B"
    return "C"


def _velocity_thresholds(values: list[float]) -> tuple[float | None, float | None]:
    positive = sorted(value for value in values if value > 0)
    if not positive:
        return None, None
    upper_index = min(len(positive) - 1, int(len(positive) * 0.75 + 0.999) - 1)
    return median(positive), positive[upper_index]


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
        "is_demo_data": state["is_demo_data"],
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
    if store_total is None:
        store_total = defaultdict(float)
        for stock in tables["stock_snapshot"]:
            location = str(field(stock, "location_id", default=""))
            if location in store_rows:
                store_total[location] += (
                    as_number(field(stock, "on_hand"))
                    + as_number(field(stock, "in_transit"))
                )
    sku_items = {str(field(row, "sku_id", default="")): row for row in tables["items"]}
    sales = defaultdict(float)
    stock_days = defaultdict(float)
    sold_units = defaultdict(float)
    recent_sold_units = defaultdict(float)
    recent_stock_days = defaultdict(float)
    footfall = defaultdict(float)
    recent_bill_count = defaultdict(int)
    total_bill_count = defaultdict(int)
    present_shifts = defaultdict(int)
    roster_days = defaultdict(set)
    bill_store = {str(field(row, "bill_id", default="")): str(field(row, "store_id", default=""))
                  for row in tables["bills"]}
    bill_dates = {
        str(field(row, "bill_id", default="")): as_date(field(row, "bill_date", "date"))
        for row in tables["bills"]
    }
    stock_dates = [
        as_date(field(row, "week_ending")) for row in tables.get("stock_weekly", [])
    ]
    stock_dates = [day for day in stock_dates if day]
    as_of = max(stock_dates, default=None)
    recent_start = as_of - timedelta(days=83) if as_of else None
    footfall_dates = [as_date(field(row, "date")) for row in tables.get("footfall_daily", [])]
    footfall_dates = [day for day in footfall_dates if day]
    footfall_start, footfall_end = min(footfall_dates, default=None), max(footfall_dates, default=None)
    for row in tables.get("footfall_daily", []):
        footfall[str(field(row, "store_id", default=""))] += as_number(field(row, "footfall"))
    for bill in tables["bills"]:
        bill_date = as_date(field(bill, "bill_date", "date"))
        store_id = str(field(bill, "store_id", default=""))
        total_bill_count[store_id] += 1
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
        bill_date = bill_dates.get(str(field(row, "bill_id", default="")))
        if recent_start and as_of and bill_date and recent_start <= bill_date <= as_of:
            recent_sold_units[store] += qty
    for row in tables["stock_weekly"]:
        store_id = str(field(row, "store_id", default=""))
        sku = str(field(row, "sku_id", default=""))
        days = as_number(field(row, "days_in_stock"))
        stock_days[store_id, sku] += days
        week_ending = as_date(field(row, "week_ending"))
        if recent_start and as_of and week_ending and recent_start <= week_ending <= as_of:
            recent_stock_days[store_id] += days
    store_velocity_values = [
        recent_sold_units[store_id] / recent_stock_days[store_id]
        for store_id in store_rows
        if recent_stock_days[store_id] > 0 and recent_sold_units[store_id] > 0
    ]
    velocity_median, velocity_upper = _velocity_thresholds(store_velocity_values)
    rows = []
    for store_id, store in store_rows.items():
        tier = str(field(store, "tier", default=""))
        capacity = as_number(field(store, "stock_capacity_units", "capacity_units"))
        units = store_total.get(store_id, 0) if store_total is not None else 0
        rows.append({
            "store_id": store_id, "city": field(store, "city", default=""),
            "state": field(store, "state", default=""), "tier": tier,
            "store_name": field(store, "store_name", "name", default=""),
            "address": field(store, "address", "full_address", default=""),
            "location_source": field(store, "location_source", default=""),
            "store_locator_url": field(store, "store_locator_url", default=""),
            "google_maps_url": field(store, "google_maps_url", default=""),
            "area_manager_id": field(store, "area_manager_id", default=""),
            "stock_units": int(units), "capacity": int(capacity),
            "capacity_pct": round(100 * units / capacity, 1) if capacity else 0,
            "sales_revenue": round(sales[store_id], 2),
            "units_sold": int(sold_units[store_id]),
            "recent_units_sold": int(recent_sold_units[store_id]),
            "recent_in_stock_days": int(recent_stock_days[store_id]),
            "recent_velocity": round(
                recent_sold_units[store_id] / recent_stock_days[store_id], 3
            ) if recent_stock_days[store_id] else None,
            "sell_through_grade": _velocity_grade(
                recent_sold_units[store_id] / recent_stock_days[store_id]
                if recent_stock_days[store_id] else None,
                recent_stock_days[store_id], recent_sold_units[store_id],
                velocity_median, velocity_upper,
            ),
            "sell_through_grade_basis": (
                "Relative to the network median and upper quartile of recent units per "
                "recorded SKU in-stock day; zero sales with exposure is WATCH, no exposure is UNKNOWN."
            ),
            "bill_count": total_bill_count[store_id],
            "average_bill_value": round(sales[store_id] / total_bill_count[store_id], 2)
            if total_bill_count[store_id] else 0,
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
    return {
        "status": "ok",
        "has_data": state["tables"] is not None,
        "is_demo_data": state["is_demo_data"],
    }


@app.get("/api/health")
def api_health() -> dict[str, Any]:
    return health()


@app.get("/api/cron/run")
async def scheduled_run(request: Request) -> dict[str, Any]:
    if not CRON_SECRET:
        raise HTTPException(503, "Scheduled refresh is not configured.")
    supplied = request.headers.get("authorization", "")
    if not hmac.compare_digest(supplied, f"Bearer {CRON_SECRET}"):
        raise HTTPException(401, "Scheduled refresh authorization failed.")
    source = state.get("source_name")
    if IS_VERCEL and state.get("source_blob_path"):
        temporary = await _download_blob_to_temp(
            state["source_blob_path"], Path(source or "").suffix.lower(),
        )
        try:
            await asyncio.to_thread(_run_analysis, temporary, source)
        finally:
            temporary.unlink(missing_ok=True)
    elif state.get("tables") is not None:
        await asyncio.to_thread(
            _run_analysis_tables,
            state["tables"],
            source or "Generated synthetic demo data",
            is_demo_data=bool(state.get("is_demo_data")),
        )
    else:
        raise HTTPException(409, "There is no loaded dataset to refresh.")
    return {"ok": True, **_summary(state["tables"])}


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
        await asyncio.to_thread(_run_analysis, saved, file.filename)
        if IS_VERCEL:
            from vercel import blob

            stored = await blob.put_blob(
                f"datasets/{safe_name}", content, access="private",
                content_type=file.content_type or "application/octet-stream",
                add_random_suffix=True,
            )
            state["source_blob_path"] = stored.pathname
        return {"ok": True, **_summary(state["tables"])}
    except HTTPException:
        raise
    except (OSError, ValueError, KeyError, zipfile.BadZipFile) as exc:
        saved.unlink(missing_ok=True)
        raise HTTPException(422, f"Could not analyse this dataset: {exc}") from exc
    finally:
        if IS_VERCEL:
            saved.unlink(missing_ok=True)


@app.post("/api/upload-blob")
async def upload_blob(payload: BlobUploadRequest) -> dict[str, Any]:
    suffix = Path(payload.filename).suffix.lower()
    if suffix not in {".xlsx", ".zip"}:
        raise HTTPException(415, "Upload an .xlsx workbook or .zip containing one CSV per table.")
    if not payload.pathname.startswith("datasets/") or ".." in Path(payload.pathname).parts:
        raise HTTPException(400, "Invalid private workbook path.")
    temporary = await _download_blob_to_temp(payload.pathname, suffix)
    try:
        await asyncio.to_thread(_run_analysis, temporary, payload.filename)
        state["source_blob_path"] = payload.pathname
        return {"ok": True, **_summary(state["tables"])}
    except (OSError, ValueError, KeyError, zipfile.BadZipFile) as exc:
        raise HTTPException(422, f"Could not analyse this dataset: {exc}") from exc
    finally:
        temporary.unlink(missing_ok=True)


@app.post("/api/run")
async def run() -> dict[str, Any]:
    source = state.get("source_name")
    if not source:
        raise HTTPException(409, "Upload a dataset before running recommendations.")
    if IS_VERCEL and state.get("source_blob_path"):
        temporary = await _download_blob_to_temp(
            state["source_blob_path"], Path(source).suffix.lower(),
        )
        try:
            await asyncio.to_thread(_run_analysis, temporary, source)
            return {"ok": True, **_summary(state["tables"])}
        except (OSError, ValueError, KeyError) as exc:
            raise HTTPException(422, f"Analysis failed: {exc}") from exc
        finally:
            temporary.unlink(missing_ok=True)
    if IS_VERCEL:
        tables = state.get("tables")
        if tables is None:
            raise HTTPException(409, "The current dataset is no longer available.")
        await asyncio.to_thread(
            _run_analysis_tables,
            tables,
            source,
            is_demo_data=bool(state.get("is_demo_data")),
        )
        return {"ok": True, **_summary(tables)}
    candidates = sorted(DATA_DIR.glob("dataset-*"), key=lambda path: path.stat().st_mtime, reverse=True)
    if not candidates:
        if state.get("is_demo_data") and state.get("tables") is not None:
            _run_analysis_tables(state["tables"], "Generated synthetic demo data", is_demo_data=True)
            return {"ok": True, **_summary(state["tables"])}
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


@app.get("/api/logistics")
def logistics(
    actor_role: str | None = Header(None, alias="x-stockmover-role"),
    area_manager_id: str | None = Header(None, alias="x-stockmover-area-manager"),
) -> dict[str, Any]:
    _require_data()
    current_moves = {row["id"]: row for row in state["moves"]}
    awaiting = [row for row in state["moves"] if row["decision"] == "pending"]
    active_moves = []
    for move_id, approval in state["approvals"].items():
        if approval.get("status") != "approved":
            continue
        move = current_moves.get(move_id) or approval.get("move_snapshot")
        if not move:
            continue
        move = dict(move)
        move["decision"] = "approved"
        move["completion"] = approval.get("completion")
        move["logistics_status"] = "completed" if approval.get("completion") else "ongoing"
        active_moves.append(move)
    if actor_role == "area_manager" and area_manager_id:
        stores_for_manager = _area_store_ids(area_manager_id)
        awaiting = [row for row in awaiting
                    if row["from_location"] in stores_for_manager
                    or row["to_location"] in stores_for_manager]
        active_moves = [row for row in active_moves
                        if row["from_location"] in stores_for_manager
                        or row["to_location"] in stores_for_manager]
    ongoing = [row for row in active_moves if row["logistics_status"] == "ongoing"]
    completed = [row for row in active_moves if row["logistics_status"] == "completed"]
    completed.sort(
        key=lambda row: row["completion"].get("completed_at", ""), reverse=True,
    )
    return {
        "next_move": awaiting[0] if awaiting else None,
        "awaiting_count": len(awaiting),
        "ongoing": ongoing,
        "completed": completed,
    }


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
    if move["logistics_status"] == "completed" and decision.status != "approved":
        raise HTTPException(409, "A completed logistics move cannot have its approval changed.")
    if decision.status == "approved" and (
        not decision.validated or decision.validated_qty != move["qty"]
    ):
        raise HTTPException(
            422, "Validate the displayed SKU, route and exact recommended quantity before approval."
        )
    state["approvals"][move_id] = {
        "status": decision.status, "note": decision.note,
        "actor": actor or "Merchandising team", "role": actor_role or "unspecified",
        "validated": decision.status == "approved",
        "validated_qty": decision.validated_qty if decision.status == "approved" else None,
        "validated_at": datetime.now(timezone.utc).isoformat()
        if decision.status == "approved" else None,
        "completion": move.get("completion") if decision.status == "approved" else None,
        "move_snapshot": dict(move) if decision.status == "approved" else None,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    _write_decisions(move_id, f"decision_{decision.status}")
    move["decision"] = decision.status
    move["decision_note"] = decision.note
    move["decision_actor"] = actor or "Merchandising team"
    move["decision_role"] = actor_role or "unspecified"
    move["decision_validated"] = decision.status == "approved"
    move["logistics_status"] = (
        "awaiting_approval" if decision.status == "pending"
        else "declined" if decision.status == "rejected"
        else "completed" if state["approvals"][move_id]["completion"]
        else "ongoing"
    )
    return {"ok": True, "item": move}


@app.post("/api/logistics/{move_id}/complete")
def complete_logistics(
    move_id: str,
    completion: LogisticsCompletion,
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
            raise HTTPException(403, "Area managers can only validate moves involving their assigned stores.")
    if move["decision"] != "approved":
        raise HTTPException(409, "Approve the recommendation before recording logistics completion.")
    if move["logistics_status"] == "completed":
        raise HTTPException(409, "This logistics move is already completed.")
    if completion.received_qty != move["qty"]:
        raise HTTPException(422, "Received quantity must match the validated recommendation quantity.")
    if not completion.sku_verified or not completion.route_verified:
        raise HTTPException(422, "Verify the SKU and route against the recommendation before completion.")

    completed_at = datetime.now(timezone.utc).isoformat()
    approval = state["approvals"][move_id]
    approval["completion"] = {
        "received_qty": completion.received_qty,
        "sku_verified": completion.sku_verified,
        "route_verified": completion.route_verified,
        "note": completion.note,
        "actor": actor or "Merchandising team",
        "role": actor_role or "unspecified",
        "completed_at": completed_at,
    }
    approval["updated_at"] = completed_at
    _write_decisions(move_id, "logistics_completed")
    move["completion"] = approval["completion"]
    move["logistics_status"] = "completed"
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
    tables = _require_data()
    _ensure_store_scope(store_id, actor_role, area_manager_id)
    managed = _area_store_ids(area_manager_id) if actor_role == "area_manager" else None
    store_summaries = {
        row["store_id"]: row for row in _store_summaries(tables)
    }
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
            "placement_suggestion": _placement_suggestion(
                product, store_summaries, managed, store_id,
            ),
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
    store_id: str | None = None,
    actor_role: str | None = Header(None, alias="x-stockmover-role"),
    area_manager_id: str | None = Header(None, alias="x-stockmover-area-manager"),
) -> dict[str, Any]:
    _require_data()
    _ensure_store_scope(store_id, actor_role, area_manager_id)
    source_product = state["products"].get(sku_id)
    if source_product is None:
        raise HTTPException(404, "SKU not found in the uploaded catalogue.")
    managed = _area_store_ids(area_manager_id) if actor_role == "area_manager" else None
    product = _scoped_product(source_product, managed) if managed is not None else source_product
    store_summaries = {
        row["store_id"]: row for row in _store_summaries(state["tables"])
    }
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
        "placement_suggestion": _placement_suggestion(
            product, store_summaries, managed, store_id,
        ),
        "sales_trend": [{"week": week, "units": sales.get(week, 0)} for week in weeks],
        "stock_trend": [{"week": week, "units": stock.get(week, 0)} for week in weeks],
        "transfer_recommendations": transfer_moves,
        "similar_products": _similar_products(product, allowed_store_ids=managed),
        "similarity_notice": (
            "Matches use category, size, and a 30% MRP band. When catalogue fields exist, "
            "material, cloth type and colour are also used as explicit match signals."
        ),
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
    stock_risk = sorted(
        (row for row in performance if row["store_metrics"]["stock_risk"]),
        key=lambda row: (-(row["store_metrics"]["days_since_sale"] or 0),
                         -row["store_metrics"]["stock_units"]),
    )
    for product in stock_risk:
        product["similar_demand"] = _similar_products(product, exclude_store_id=store_id)
        product["recommended_move"] = _best_pending_outbound_move(
            store_id, str(product["sku_id"]),
            _area_store_ids(area_manager_id) if actor_role == "area_manager" else None,
        )
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
    store_bills = [
        row for row in state["bill_summaries"]
        if row["store_id"] == store_id and row["bill_date"] >= state["recent_start"]
    ]
    store_bills.sort(key=lambda row: (row["bill_date"], row["bill_id"]), reverse=True)
    return {
        "store": summaries[store_id],
        "category_stock": [{"category": key, "units": value}
                           for key, value in sorted(category_stock.items())],
        "best_sellers": best_sellers,
        "dormant_stock": dormant,
        "stock_risk": stock_risk[:20],
        "recent_bills": store_bills[:15],
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
            transfer = _best_pending_outbound_move(
                target_store, str(product["sku_id"]), allowed_stores
                if actor_role == "area_manager" else None)
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


def _local_answer(question: str, actor_role: str | None,
                  area_manager_id: str | None) -> dict[str, Any]:
    metrics = summary(actor_role, area_manager_id)
    query = question.casefold()
    model = metrics["model"]
    prefix = "Synthetic demo data: " if metrics.get("is_demo_data") else ""
    pending_moves = [row for row in state["moves"] if row["decision"] == "pending"]
    if actor_role == "area_manager":
        managed = _area_store_ids(area_manager_id)
        pending_moves = [row for row in pending_moves
                         if row["from_location"] in managed or row["to_location"] in managed]

    if any(word in query for word in ("model", "forecast", "train", "accuracy", "wape")):
        validation = model.get("validation", {})
        if validation.get("wape") is not None:
            answer = (
                f"The {model.get('model', 'demand')} model is {model.get('status', 'not run')}. "
                f"It trained on {int(as_number(model.get('training_rows'))):,} observed "
                f"SKU-week rows. Its two-week holdout WAPE was {validation['wape']:.1%}, "
                f"compared with {validation.get('naive_wape', 0):.1%} for the recent-rate "
                "baseline. The selected forecast source is "
                f"{model.get('forecast_source', model.get('model', 'baseline'))}."
            )
        else:
            answer = (
                f"The model is using {model.get('model', 'the available baseline')}: "
                f"{model.get('reason', 'no validation metrics are available yet')} "
                f"It has {int(as_number(model.get('training_rows'))):,} training rows."
            )
    elif any(word in query for word in ("next", "recommend", "move", "transfer", "should")):
        if pending_moves:
            move = pending_moves[0]
            answer = (
                f"Next rule-checked move: {move['sku_id']} ({move['style_name']}), "
                f"{move['qty']} units from {move['from_location']} to {move['to_location']}; "
                f"expected {move['arrival_date']}. Priority {move['priority']}. "
                f"{move['reason']} A person must validate it before approval."
            )
        else:
            answer = "There are no pending rule-checked moves in your current view."
    elif "warehouse" in query or "wh-kol" in query:
        warehouse_moves = [row for row in pending_moves
                           if row["move_type"] == "WAREHOUSE_TO_STORE"]
        answer = (
            f"WH-KOL has {int(as_number(metrics['warehouse_units'])):,} units on hand or in transit. "
            f"{len(warehouse_moves)} pending recommendation(s) use warehouse stock."
        )
        if warehouse_moves:
            move = warehouse_moves[0]
            answer += (
                f" The highest-ranked is {move['sku_id']}: {move['qty']} units to "
                f"{move['to_location']}, arriving {move['arrival_date']}."
            )
    elif any(term in query for term in ("top store", "best store", "highest sales", "top selling")):
        leaders = sorted(
            metrics["stores_summary"], key=lambda row: row["sales_revenue"], reverse=True,
        )[:3]
        answer = "Top stores by recorded sales: " + "; ".join(
            f"{row['store_id']} ({rupee(row['sales_revenue'])})" for row in leaders
        ) + "."
    else:
        store = next(
            (row for row in metrics["stores_summary"] if row["store_id"].casefold() in query),
            None,
        )
        if store:
            answer = (
                f"{store['store_id']} ({store['city']}, {store['tier']}) has "
                f"{int(as_number(store['stock_units'])):,} units, is at "
                f"{store['capacity_pct']}% of capacity, and recorded "
                f"{rupee(store['sales_revenue'])} in sales. Area manager: "
                f"{store['area_manager_id']}."
            )
        else:
            city_stores = [
                row for row in metrics["stores_summary"] if row["city"].casefold() in query
            ]
            if city_stores:
                total_stock = sum(row["stock_units"] for row in city_stores)
                total_revenue = sum(row["sales_revenue"] for row in city_stores)
                answer = (
                    f"{city_stores[0]['city']} has {len(city_stores)} store(s), "
                    f"{int(total_stock):,} units, and {rupee(total_revenue)} in recorded sales."
                )
            else:
                managed = _area_store_ids(area_manager_id) if actor_role == "area_manager" else None
                product = None
                for sku_id, source_product in state["products"].items():
                    item = (
                        _scoped_product(source_product, managed)
                        if managed is not None else source_product
                    )
                    if (sku_id.casefold() in query
                            or str(item.get("style_id", "")).casefold() in query
                            or str(item.get("style_name", "")).casefold() in query):
                        product = item
                        break
                if product:
                    velocity = product["recent_velocity"]
                    answer = (
                        f"{product['sku_id']} ({product.get('style_name', product['sku_id'])}, "
                        f"{product.get('category', 'uncategorized')}) has "
                        f"{int(as_number(product['store_stock_units'])):,} store units and "
                        f"{int(as_number(product['warehouse_units'])):,} WH-KOL units. "
                        f"Recent availability-corrected velocity is "
                        f"{velocity if velocity is not None else 'unknown'} units per day."
                    )
                else:
                    answer = (
                        f"The current network has {metrics['stores']} stores and {metrics['skus']} "
                        f"SKUs, {int(as_number(metrics['stock_units'])):,} store units, "
                        f"{metrics['pending']} pending moves, and {rupee(metrics['revenue'])} "
                        "in recorded sales. Ask about a store, SKU, warehouse, recommendation, "
                        "or forecast for more detail."
                    )

    return {
        "answer": prefix + answer,
        "provider": "local_computed",
        "computed_values_are_authoritative": True,
    }


@app.post("/api/ask")
def ask(
    question: Question,
    actor_role: str | None = Header(None, alias="x-stockmover-role"),
    area_manager_id: str | None = Header(None, alias="x-stockmover-area-manager"),
) -> dict[str, Any]:
    _require_data()
    local_result = _local_answer(question.question, actor_role, area_manager_id)
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        return local_result
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
        candidates = result.get("candidates") if isinstance(result, dict) else None
        content = candidates[0].get("content") if candidates and isinstance(candidates[0], dict) else None
        parts = content.get("parts") if isinstance(content, dict) else None
        text = parts[0].get("text") if parts and isinstance(parts[0], dict) else None
        if not isinstance(text, str) or not text.strip():
            return {**local_result, "fallback_reason": "Gemini returned no usable answer."}
        return {"answer": text, "provider": "Gemini", "computed_values_are_authoritative": True}
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        return {
            **local_result,
            "fallback_reason": f"Gemini is unavailable ({exc}); returned a computed local answer.",
        }


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
        elif state.get("is_demo_data") and state.get("tables") is not None:
            try:
                _run_analysis_tables(state["tables"], "Generated synthetic demo data",
                                     is_demo_data=True)
            except (ValueError, KeyError, RuntimeError) as exc:
                print(f"Scheduled demo recommendation run failed: {exc}", file=sys.stderr)


@app.on_event("startup")
async def startup() -> None:
    if IS_VERCEL:
        if not DATABASE_URL or not SESSION_SECRET or not APPROVAL_TOKEN or not CRON_SECRET:
            raise RuntimeError(
                "Vercel requires DATABASE_URL, APP_SESSION_SECRET, APPROVAL_TOKEN, and CRON_SECRET."
            )
        import psycopg

        try:
            async with await psycopg.AsyncConnection.connect(
                DATABASE_URL, connect_timeout=8,
            ) as connection:
                async with connection.transaction():
                    await connection.execute("SELECT pg_advisory_xact_lock(724106202)")
                    loaded = await _load_database_payload(connection)
                    if not loaded:
                        state["approvals"] = {}
                        state["workflow_audit"] = []
                        _run_analysis_tables(
                            build_demo_tables(), "Generated synthetic demo data",
                            is_demo_data=True,
                        )
                        await _save_database_payload(connection)
        except psycopg.Error as exc:
            raise RuntimeError(f"Could not initialize persistent app storage: {exc}") from exc
        return
    state["approvals"] = _read_decisions()
    latest = sorted(DATA_DIR.glob("dataset-*"), key=lambda path: path.stat().st_mtime, reverse=True)
    if latest:
        try:
            _run_analysis(latest[0], latest[0].name)
        except Exception as exc:
            print(f"Could not load persisted dataset: {exc}", file=sys.stderr)
    else:
        try:
            _run_analysis_tables(build_demo_tables(), "Generated synthetic demo data",
                                 is_demo_data=True)
        except (OSError, ValueError, KeyError, RuntimeError) as exc:
            print(f"Could not calculate the synthetic demo dataset: {exc}", file=sys.stderr)
    asyncio.create_task(_scheduled_refresh())
