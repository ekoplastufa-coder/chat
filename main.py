import os
import secrets
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

app = FastAPI(title="Pro100 Ozon Analytics", version="3.0.0")

BASE = "https://api-seller.ozon.ru"
CLIENT_ID = os.getenv("OZON_CLIENT_ID", "")
API_KEY = os.getenv("OZON_API_KEY", "")
CONNECTION_TOKEN = os.getenv("OZON_CONNECTION_TOKEN", "")
connection_bearer = HTTPBearer(auto_error=False)


# =========================================================
# OZON API
# =========================================================

def headers():
    if not CLIENT_ID or not API_KEY:
        raise HTTPException(
            503,
            "OZON_CLIENT_ID/OZON_API_KEY are not configured"
        )

    return {
        "Client-Id": CLIENT_ID,
        "Api-Key": API_KEY,
        "Content-Type": "application/json",
    }


async def post(path: str, payload: Dict[str, Any], *, timeout: float = 120):
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            BASE + path,
            headers=headers(),
            json=payload,
        )

    if response.status_code >= 400:
        raise HTTPException(
            response.status_code,
            {
                "path": path,
                "response": response.text[:5000],
            },
        )

    return response.json()


def money(value):
    """
    Ozon money object:
    {"amount": "123.45", "currency": "RUB"}

    Also accepts ordinary int/float/string.
    """
    if value is None:
        return 0.0

    if isinstance(value, dict):
        value = value.get("amount", 0)

    if isinstance(value, (int, float)):
        return float(value)

    try:
        return float(
            str(value)
            .replace(" ", "")
            .replace(",", ".")
        )
    except Exception:
        return 0.0


# =========================================================
# BASIC
# =========================================================

@app.get("/")
def root():
    return {
        "service": "pro100-ozon-analytics",
        "version": "3.0.0",
        "status": "ok",
    }


@app.get("/health")
def health():
    return {
        "status": "ok",
        "version": "3.0.0",
        "ozon_credentials_configured": bool(
            CLIENT_ID and API_KEY
        ),
    }


# =========================================================
# PRODUCTS
# =========================================================

@app.get("/connection-check", operation_id="ozon_check_connection")
async def ozon_check_connection(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(connection_bearer),
):
    if not CONNECTION_TOKEN:
        raise HTTPException(503, "Connection check is unavailable")
    if not credentials or not secrets.compare_digest(
        credentials.credentials.encode(), CONNECTION_TOKEN.encode()
    ):
        raise HTTPException(
            401, "Invalid credentials", headers={"WWW-Authenticate": "Bearer"}
        )

    try:
        data = await post(
            "/v3/product/list",
            {"filter": {"visibility": "ALL"}, "last_id": "", "limit": 1},
            timeout=10,
        )
    except HTTPException as exc:
        if not CLIENT_ID or not API_KEY:
            raise HTTPException(503, "Ozon credentials are not configured") from exc
        raise HTTPException(502, {"ozon_status": exc.status_code}) from exc
    except httpx.TimeoutException as exc:
        raise HTTPException(504, "Ozon did not respond in time") from exc
    except (httpx.RequestError, ValueError) as exc:
        raise HTTPException(502, "Invalid or unavailable Ozon response") from exc

    result = data.get("result") if isinstance(data, dict) else None
    items = result.get("items") if isinstance(result, dict) else None
    if not isinstance(items, list) or len(items) > 1 or (
        items and not isinstance(items[0], dict)
    ):
        raise HTTPException(502, "Unexpected Ozon catalog response")

    return {
        "ozon_status": 200,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "offer_id": items[0].get("offer_id") if items else None,
    }


async def get_products_all():
    """
    Load product_id / offer_id / sku mapping.
    """

    result = []
    last_id = ""

    for _ in range(1000):

        data = await post(
            "/v3/product/list",
            {
                "filter": {
                    "visibility": "ALL"
                },
                "last_id": last_id,
                "limit": 1000,
            },
        )

        block = data.get("result", {}) or {}

        items = block.get("items", []) or []

        result.extend(items)

        new_last_id = block.get("last_id", "")

        if not items or not new_last_id:
            break

        if new_last_id == last_id:
            break

        last_id = new_last_id

    return result


@app.get("/products")
async def products():
    items = await get_products_all()

    return {
        "total": len(items),
        "items": items,
    }


async def product_maps():

    products = await get_products_all()

    sku_map = {}
    offer_map = {}

    for product in products:

        offer_id = str(
            product.get("offer_id") or ""
        )

        sku = product.get("sku")

        if sku is not None:
            sku_map[str(sku)] = {
                "offer_id": offer_id,
                "product_id": product.get("product_id"),
            }

        if offer_id:
            offer_map[offer_id.lower()] = product

    return sku_map, offer_map


# =========================================================
# FINANCE
# =========================================================

async def finance_day(day: str):

    rows = []
    last_id = ""

    for _ in range(1000):

        payload = {
            "date": day,
            "limit": 1000,
            "last_id": last_id,
        }

        data = await post(
            "/v1/finance/accrual/by-day",
            payload,
        )

        result = data.get("result", data) or {}

        batch = (
            result.get("accruals")
            or result.get("items")
            or result.get("operations")
            or []
        )

        if not batch:
            break

        for row in batch:

            if isinstance(row, dict):
                row["_finance_date"] = day
                rows.append(row)

        new_last_id = (
            result.get("last_id")
            or data.get("last_id")
            or ""
        )

        if not new_last_id:
            break

        if new_last_id == last_id:
            break

        last_id = new_last_id

    return rows


async def finance_period(
    date_from: str,
    date_to: str,
):

    start = datetime.strptime(
        date_from,
        "%Y-%m-%d",
    ).date()

    finish = datetime.strptime(
        date_to,
        "%Y-%m-%d",
    ).date()

    if finish < start:
        raise HTTPException(
            400,
            "date_to must be >= date_from",
        )

    if (finish - start).days > 366:
        raise HTTPException(
            400,
            "Maximum period is 366 days",
        )

    rows = []

    current = start

    while current <= finish:

        rows.extend(
            await finance_day(
                current.isoformat()
            )
        )

        current += timedelta(days=1)

    return rows


# =========================================================
# AGGREGATION
# =========================================================

def empty_sku(sku):

    return {
        "sku": str(sku),
        "offer_id": "",
        "product_id": None,

        "quantity": 0,

        # Sale price / turnover
        "sales": 0.0,

        # Ozon commission
        "commission": 0.0,

        # Delivery / logistics
        "logistics": 0.0,

        # ITEM fees
        "item_fees": 0.0,

        # Other charges
        "other": 0.0,

        # Total Ozon deductions
        "ozon_total": 0.0,

        # Remaining after Ozon
        "after_ozon": 0.0,

        # Effective Ozon %
        "ozon_pct": None,

        "posting_operations": 0,
        "item_operations": 0,
    }


def aggregate_finance(rows):

    result = {}

    # -----------------------------------------------------
    # POSTING operations
    # -----------------------------------------------------

    for row in rows:

        category = str(
            row.get("accrued_category") or ""
        ).upper()

        posting = row.get("posting")

        if category != "POSTING":
            continue

        if not isinstance(posting, dict):
            continue

        products = posting.get("products", []) or []

        for product in products:

            if not isinstance(product, dict):
                continue

            sku = product.get("sku")

            if sku is None:
                continue

            sku = str(sku)

            item = result.setdefault(
                sku,
                empty_sku(sku),
            )

            item["posting_operations"] += 1

            quantity = int(
                product.get("quantity") or 1
            )

            item["quantity"] += quantity

            # -------------------------------
            # COMMISSION
            # -------------------------------

            commission_block = product.get(
                "commission"
            ) or {}

            sale_amount = money(
                commission_block.get(
                    "sale_amount"
                )
            )

            sale_price = money(
                commission_block.get(
                    "sale_price"
                )
            )

            seller_price = money(
                commission_block.get(
                    "seller_price"
                )
            )

            commission = money(
                commission_block.get(
                    "sale_commission"
                )
            )

            if sale_amount:
                revenue = abs(sale_amount)

            elif sale_price:
                revenue = abs(sale_price)

            else:
                revenue = abs(seller_price)

            item["sales"] += revenue

            item["commission"] += abs(
                commission
            )

            # -------------------------------
            # DELIVERY
            # -------------------------------

            delivery = product.get(
                "delivery"
            ) or {}

            delivery_total = money(
                delivery.get(
                    "total_accrued"
                )
            )

            # total_accrued already represents
            # delivery services total.
            item["logistics"] += abs(
                delivery_total
            )

    # -----------------------------------------------------
    # ITEM fees
    # -----------------------------------------------------

    for row in rows:

        category = str(
            row.get("accrued_category") or ""
        ).upper()

        if category != "ITEM":
            continue

        item_fees = row.get(
            "item_fees"
        )

        if not isinstance(item_fees, dict):
            continue

        groups = item_fees.get(
            "fees",
            []
        ) or []

        for group in groups:

            if not isinstance(group, dict):
                continue

            sku = group.get("sku")

            if sku is None:
                continue

            sku = str(sku)

            item = result.setdefault(
                sku,
                empty_sku(sku),
            )

            item["item_operations"] += 1

            fees = group.get(
                "fees",
                []
            ) or []

            for fee in fees:

                if not isinstance(fee, dict):
                    continue

                amount = money(
                    fee.get("accrued")
                )

                item["item_fees"] += abs(
                    amount
                )

    # -----------------------------------------------------
    # FINAL CALCULATION
    # -----------------------------------------------------

    output = []

    for item in result.values():

        item["ozon_total"] = (
            item["commission"]
            + item["logistics"]
            + item["item_fees"]
            + item["other"]
        )

        item["after_ozon"] = (
            item["sales"]
            - item["ozon_total"]
        )

        if item["sales"] > 0:

            item["ozon_pct"] = (
                item["ozon_total"]
                / item["sales"]
                * 100
            )

        for key in [
            "sales",
            "commission",
            "logistics",
            "item_fees",
            "other",
            "ozon_total",
            "after_ozon",
        ]:

            item[key] = round(
                item[key],
                2,
            )

        if item["ozon_pct"] is not None:

            item["ozon_pct"] = round(
                item["ozon_pct"],
                2,
            )

        output.append(item)

    output.sort(
        key=lambda x: x["sales"],
        reverse=True,
    )

    return output


# =========================================================
# ADD OFFER IDS
# =========================================================

async def attach_offer_ids(rows):

    sku_map, _ = await product_maps()

    for row in rows:

        data = sku_map.get(
            str(row["sku"])
        )

        if data:

            row["offer_id"] = data.get(
                "offer_id",
                "",
            )

            row["product_id"] = data.get(
                "product_id"
            )

    return rows


# =========================================================
# FINANCE ENDPOINT
# =========================================================

@app.get("/finance")
async def finance(
    date_from: str,
    date_to: str,
    raw: bool = False,
):

    try:

        datetime.strptime(
            date_from,
            "%Y-%m-%d",
        )

        datetime.strptime(
            date_to,
            "%Y-%m-%d",
        )

    except ValueError:

        raise HTTPException(
            400,
            "Dates must be YYYY-MM-DD",
        )

    rows = await finance_period(
        date_from,
        date_to,
    )

    if raw:

        return {
            "date_from": date_from,
            "date_to": date_to,
            "operations_count": len(rows),
            "data": rows,
        }

    aggregated = aggregate_finance(
        rows
    )

    aggregated = await attach_offer_ids(
        aggregated
    )

    totals = {
        "sales": round(
            sum(x["sales"] for x in aggregated),
            2,
        ),
        "commission": round(
            sum(x["commission"] for x in aggregated),
            2,
        ),
        "logistics": round(
            sum(x["logistics"] for x in aggregated),
            2,
        ),
        "item_fees": round(
            sum(x["item_fees"] for x in aggregated),
            2,
        ),
        "ozon_total": round(
            sum(x["ozon_total"] for x in aggregated),
            2,
        ),
        "after_ozon": round(
            sum(x["after_ozon"] for x in aggregated),
            2,
        ),
    }

    if totals["sales"]:

        totals["ozon_pct"] = round(
            totals["ozon_total"]
            / totals["sales"]
            * 100,
            2,
        )

    else:

        totals["ozon_pct"] = None

    return {
        "date_from": date_from,
        "date_to": date_to,
        "operations_count": len(rows),
        "sku_count": len(aggregated),
        "totals": totals,
        "data": aggregated,
    }


# =========================================================
# SEARCH BY OFFER ID
# =========================================================

@app.get("/offer/{offer_id}")
async def offer(
    offer_id: str,
    date_from: str = "2026-09-01",
    date_to: Optional[str] = None,
):

    if date_to is None:
        date_to = date.today().isoformat()

    result = await finance(
        date_from,
        date_to,
        False,
    )

    needle = offer_id.strip().lower()

    matches = []

    for row in result["data"]:

        current = str(
            row.get("offer_id") or ""
        ).lower()

        if needle in current:
            matches.append(row)

    return {
        "query": offer_id,
        "date_from": date_from,
        "date_to": date_to,
        "matches": matches,
    }


# =========================================================
# SEPTEMBER
# =========================================================

@app.get("/finance/september")
async def september():

    end = min(
        date.today(),
        date(2026, 9, 30),
    )

    return await finance(
        "2026-09-01",
        end.isoformat(),
        False,
    )
