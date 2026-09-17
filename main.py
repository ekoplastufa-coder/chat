import os
from datetime import date, datetime
from typing import Any, Dict, List, Optional
import httpx
from fastapi import FastAPI, HTTPException, Query

app = FastAPI(title="Pro100 Ozon Analytics", version="1.0.0")
BASE = "https://api-seller.ozon.ru"
CLIENT_ID = os.getenv("OZON_CLIENT_ID", "")
API_KEY = os.getenv("OZON_API_KEY", "")

def headers():
    if not CLIENT_ID or not API_KEY:
        raise HTTPException(503, "OZON_CLIENT_ID/OZON_API_KEY are not configured")
    return {"Client-Id": CLIENT_ID, "Api-Key": API_KEY, "Content-Type": "application/json"}

async def post(path: str, payload: Dict[str, Any]):
    async with httpx.AsyncClient(timeout=90) as client:
        r = await client.post(BASE + path, headers=headers(), json=payload)
    if r.status_code >= 400:
        raise HTTPException(r.status_code, {"path": path, "response": r.text[:3000]})
    return r.json()

@app.get("/")
def root():
    return {"service": "pro100-ozon-analytics", "status": "ok"}

@app.get("/health")
def health():
    return {"status": "ok", "ozon_credentials_configured": bool(CLIENT_ID and API_KEY)}

@app.get("/products")
async def products(limit: int = Query(100, ge=1, le=1000)):
    return await post("/v3/product/list", {"filter": {"visibility": "ALL"}, "last_id": "", "limit": limit})

async def finance_ops(date_from: str, date_to: str) -> List[Dict[str, Any]]:
    ops, page, page_size = [], 1, 1000
    while True:
        payload = {
            "filter": {
                "date": {"from": date_from + "T00:00:00.000Z", "to": date_to + "T23:59:59.999Z"},
                "operation_type": [], "posting_number": "", "transaction_type": "all"
            },
            "page": page, "page_size": page_size
        }
        data = await post("/v3/finance/transaction/list", payload)
        result = data.get("result", {}) or {}
        batch = result.get("operations", []) or []
        ops.extend(batch)
        page_count = int(result.get("page_count") or 0)
        if not batch or (page_count and page >= page_count) or len(batch) < page_size:
            break
        page += 1
        if page > 500:
            break
    return ops

def aggregate(ops):
    agg = {}
    for op in ops:
        items = op.get("items") or [{"sku": "NO_SKU", "name": "Operations without SKU"}]
        divisor = max(len(items), 1)
        amount = float(op.get("amount") or 0)
        accrual = float(op.get("accruals_for_sale") or 0)
        commission = float(op.get("sale_commission") or 0)
        services = sum(float(x.get("price") or 0) for x in (op.get("services") or []))
        for item in items:
            sku = str(item.get("sku") or "NO_SKU")
            row = agg.setdefault(sku, {"sku": sku, "name": item.get("name") or "", "operations": 0,
                                       "amount": 0.0, "accruals_for_sale": 0.0,
                                       "sale_commission": 0.0, "services": 0.0})
            row["operations"] += 1
            row["amount"] += amount / divisor
            row["accruals_for_sale"] += accrual / divisor
            row["sale_commission"] += commission / divisor
            row["services"] += services / divisor
    rows = list(agg.values())
    for r in rows:
        for k in ("amount", "accruals_for_sale", "sale_commission", "services"):
            r[k] = round(r[k], 2)
        base = abs(r["accruals_for_sale"])
        r["effective_ozon_pct"] = round(abs(r["sale_commission"] + r["services"]) / base * 100, 2) if base else None
    rows.sort(key=lambda x: abs(x["amount"]), reverse=True)
    return rows

@app.get("/finance")
async def finance(date_from: str, date_to: str, raw: bool = False):
    try:
        datetime.strptime(date_from, "%Y-%m-%d")
        datetime.strptime(date_to, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(400, "Dates must be YYYY-MM-DD")
    ops = await finance_ops(date_from, date_to)
    return {"date_from": date_from, "date_to": date_to, "operations_count": len(ops),
            "data": ops if raw else aggregate(ops)}

@app.get("/finance/september")
async def september(raw: bool = False):
    return await finance("2026-09-01", date.today().isoformat(), raw)

@app.get("/sku/{sku}")
async def sku(sku: str, date_from: str = "2026-09-01", date_to: Optional[str] = None):
    result = await finance(date_from, date_to or date.today().isoformat(), False)
    rows = [r for r in result["data"] if str(r["sku"]) == str(sku)]
    if not rows:
        raise HTTPException(404, "SKU not found for selected period")
    return rows[0]
