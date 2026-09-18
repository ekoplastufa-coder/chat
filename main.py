import os
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

import httpx
from fastapi import FastAPI, HTTPException, Query

app = FastAPI(title="Pro100 Ozon Analytics", version="2.0.0")

BASE = "https://api-seller.ozon.ru"
CLIENT_ID = os.getenv("OZON_CLIENT_ID", "")
API_KEY = os.getenv("OZON_API_KEY", "")


def headers():
    if not CLIENT_ID or not API_KEY:
        raise HTTPException(
            503,
            "OZON_CLIENT_ID/OZON_API_KEY are not configured"
        )

    return {
        "Client-Id": CLIENT_ID,
        "Api-Key": API_KEY,
        "Content-Type": "application/json"
    }


async def post(path: str, payload: Dict[str, Any]):
    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(
            BASE + path,
            headers=headers(),
            json=payload
        )

    if r.status_code >= 400:
        raise HTTPException(
            r.status_code,
            {
                "path": path,
                "response": r.text[:5000]
            }
        )

    return r.json()


@app.get("/")
def root():
    return {
        "service": "pro100-ozon-analytics",
        "version": "2.0.0",
        "status": "ok"
    }


@app.get("/health")
def health():
    return {
        "status": "ok",
        "ozon_credentials_configured": bool(
            CLIENT_ID and API_KEY
        )
    }


# ---------------------------------------------------------
# PRODUCTS
# ---------------------------------------------------------

@app.get("/products")
async def products(
    limit: int = Query(100, ge=1, le=1000)
):
    return await post(
        "/v3/product/list",
        {
            "filter": {
                "visibility": "ALL"
            },
            "last_id": "",
            "limit": limit
        }
    )


# ---------------------------------------------------------
# NEW OZON FINANCE API
# /v1/finance/accrual/by-day
# ---------------------------------------------------------

async def finance_day(
    day: str
) -> List[Dict[str, Any]]:

    rows = []
    last_id = ""

    for _ in range(1000):

        payload = {
            "date": day,
            "limit": 1000,
            "last_id": last_id
        }

        data = await post(
            "/v1/finance/accrual/by-day",
            payload
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
    date_to: str
):

    start = datetime.strptime(
        date_from,
        "%Y-%m-%d"
    ).date()

    finish = datetime.strptime(
        date_to,
        "%Y-%m-%d"
    ).date()

    if finish < start:
        raise HTTPException(
            400,
            "date_to must be >= date_from"
        )

    if (finish - start).days > 366:
        raise HTTPException(
            400,
            "Maximum period is 366 days"
        )

    rows = []

    current = start

    while current <= finish:

        day = current.isoformat()

        day_rows = await finance_day(day)

        rows.extend(day_rows)

        current += timedelta(days=1)

    return rows


# ---------------------------------------------------------
# HELPERS
# ---------------------------------------------------------

def number(value):

    if value is None:
        return 0.0

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


def find_value(
    row: Dict[str, Any],
    names: List[str]
):

    for name in names:

        if name in row:
            return row[name]

    return None


def get_sku(row):

    candidates = [
        "sku",
        "product_id",
        "offer_id",
        "seller_sku"
    ]

    value = find_value(
        row,
        candidates
    )

    if value:
        return str(value)

    product = row.get("product")

    if isinstance(product, dict):

        for key in candidates:

            if product.get(key):
                return str(product[key])

    item = row.get("item")

    if isinstance(item, dict):

        for key in candidates:

            if item.get(key):
                return str(item[key])

    return "NO_SKU"


def get_name(row):

    value = find_value(
        row,
        [
            "name",
            "product_name",
            "item_name"
        ]
    )

    if value:
        return str(value)

    product = row.get("product")

    if isinstance(product, dict):

        return str(
            product.get("name")
            or product.get("product_name")
            or ""
        )

    return ""


# ---------------------------------------------------------
# AGGREGATION
# ---------------------------------------------------------

def aggregate(rows):

    result = {}

    for row in rows:

        sku = get_sku(row)

        item = result.setdefault(
            sku,
            {
                "sku": sku,
                "name": get_name(row),
                "operations": 0,
                "sales": 0.0,
                "commission": 0.0,
                "logistics": 0.0,
                "services": 0.0,
                "other": 0.0,
                "total_ozon": 0.0,
                "net_after_ozon": 0.0
            }
        )

        item["operations"] += 1

        sales = number(
            find_value(
                row,
                [
                    "accruals_for_sale",
                    "sale_amount",
                    "revenue",
                    "amount"
                ]
            )
        )

        commission = number(
            find_value(
                row,
                [
                    "sale_commission",
                    "commission",
                    "commission_amount"
                ]
            )
        )

        logistics = number(
            find_value(
                row,
                [
                    "delivery_charge",
                    "logistics",
                    "logistics_amount",
                    "delivery_amount"
                ]
            )
        )

        services = number(
            find_value(
                row,
                [
                    "services_amount",
                    "service_amount"
                ]
            )
        )

        item["sales"] += sales
        item["commission"] += commission
        item["logistics"] += logistics
        item["services"] += services

    output = []

    for item in result.values():

        ozon = (
            abs(item["commission"])
            + abs(item["logistics"])
            + abs(item["services"])
            + abs(item["other"])
        )

        item["total_ozon"] = ozon

        item["net_after_ozon"] = (
            item["sales"] - ozon
        )

        if item["sales"]:

            item["effective_ozon_pct"] = (
                ozon
                / abs(item["sales"])
                * 100
            )

        else:

            item["effective_ozon_pct"] = None

        for key in [
            "sales",
            "commission",
            "logistics",
            "services",
            "other",
            "total_ozon",
            "net_after_ozon"
        ]:

            item[key] = round(
                item[key],
                2
            )

        if item["effective_ozon_pct"] is not None:

            item["effective_ozon_pct"] = round(
                item["effective_ozon_pct"],
                2
            )

        output.append(item)

    output.sort(
        key=lambda x: abs(x["sales"]),
        reverse=True
    )

    return output


# ---------------------------------------------------------
# FINANCE ENDPOINT
# ---------------------------------------------------------

@app.get("/finance")
async def finance(
    date_from: str,
    date_to: str,
    raw: bool = False
):

    try:

        datetime.strptime(
            date_from,
            "%Y-%m-%d"
        )

        datetime.strptime(
            date_to,
            "%Y-%m-%d"
        )

    except ValueError:

        raise HTTPException(
            400,
            "Dates must be YYYY-MM-DD"
        )

    rows = await finance_period(
        date_from,
        date_to
    )

    return {
        "date_from": date_from,
        "date_to": date_to,
        "operations_count": len(rows),
        "data": (
            rows
            if raw
            else aggregate(rows)
        )
    }


# ---------------------------------------------------------
# SEPTEMBER 2026
# ---------------------------------------------------------

@app.get("/finance/september")
async def september(
    raw: bool = False
):

    today = date.today()

    end = min(
        today,
        date(2026, 9, 30)
    )

    return await finance(
        "2026-09-01",
        end.isoformat(),
        raw
    )


# ---------------------------------------------------------
# ONE SKU
# ---------------------------------------------------------

@app.get("/sku/{sku}")
async def sku(
    sku: str,
    date_from: str = "2026-09-01",
    date_to: Optional[str] = None
):

    result = await finance(
        date_from,
        date_to or date.today().isoformat(),
        False
    )

    rows = [
        row
        for row in result["data"]
        if str(row["sku"]) == str(sku)
    ]

    if not rows:

        raise HTTPException(
            404,
            "SKU not found for selected period"
        )

    return rows[0]
