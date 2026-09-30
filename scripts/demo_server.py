"""Click through the UI without an API key.

Runs the real app on http://localhost:8765 with a throwaway database, a sample PO, and the Claude calls
replaced by canned responses keyed off the files in samples/ (see make_sample_invoice.py):
  * sample_invoice.pdf         -> built-in invoice with deliberate problems
  * sample_bill_of_lading.pdf  -> an unseen type: a schema is "designed", then reused for the next one
  * anything else              -> a generic "unrecognised document" with a one-field schema
Not for real documents.

    python scripts/demo_server.py
"""
import asyncio
import base64
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
tmp = Path(tempfile.mkdtemp(prefix="docparse-demo-"))
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{(tmp / 'demo.db').as_posix()}"
os.environ["UPLOAD_DIR"] = str(tmp / "uploads")
os.environ["ERP_WEBHOOK_URL"] = ""
sys.path.insert(0, str(ROOT))

import uvicorn  # noqa: E402

from app import dynamic_schema, llm  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.doc_schemas import Classification, InvoiceData  # noqa: E402
from app.main import app  # noqa: E402
from app.models import PurchaseOrder  # noqa: E402

BOL_SCHEMA = [
    {"key": "bol_number", "label": "B/L number", "type": "string", "description": "Bill of lading number",
     "required": True, "role": "document_number", "columns": []},
    {"key": "carrier", "label": "Carrier", "type": "string", "description": "Shipping line issuing the B/L",
     "required": True, "role": "party", "columns": []},
    {"key": "issue_date", "label": "Issue date", "type": "date", "description": "", "required": True,
     "role": "document_date", "columns": []},
    {"key": "shipper", "label": "Shipper", "type": "string", "description": "", "required": True,
     "role": "counterparty", "columns": []},
    {"key": "consignee", "label": "Consignee", "type": "string", "description": "", "required": True,
     "role": "none", "columns": []},
    {"key": "port_of_loading", "label": "Port of loading", "type": "string", "description": "", "required": True,
     "role": "none", "columns": []},
    {"key": "port_of_discharge", "label": "Port of discharge", "type": "string", "description": "",
     "required": True, "role": "none", "columns": []},
    {"key": "vessel_voyage", "label": "Vessel / voyage", "type": "string", "description": "", "required": False,
     "role": "none", "columns": []},
    {"key": "freight_terms", "label": "Freight terms", "type": "string", "description": "Prepaid or collect",
     "required": False, "role": "none", "columns": []},
    {"key": "shipped_on_board", "label": "Shipped on board", "type": "date", "description": "", "required": False,
     "role": "none", "columns": []},
    {"key": "cargo", "label": "Cargo", "type": "table", "description": "Goods shipped", "required": True,
     "role": "none", "columns": [
         {"key": "marks", "label": "Marks", "type": "string", "role": "none"},
         {"key": "description", "label": "Description", "type": "string", "role": "description"},
         {"key": "packages", "label": "Packages", "type": "integer", "role": "quantity"},
         {"key": "gross_weight_kg", "label": "Gross weight (kg)", "type": "number", "role": "none"}]},
]
BOL_VALUES = {
    "bol_number": ("MAEU-123", 0.99), "carrier": ("Maersk Line", 0.98), "issue_date": ("2026-09-05", 0.97),
    "shipper": ("Acme Exports Pvt Ltd, Mumbai", 0.95), "consignee": ("Nordic Machinery AB, Gothenburg", 0.94),
    "port_of_loading": ("Nhava Sheva (INNSA)", 0.62), "port_of_discharge": ("Gothenburg (SEGOT)", 0.93),
    "vessel_voyage": ("MAERSK KENSINGTON / 238W", 0.9), "freight_terms": ("Prepaid", 0.96),
    "shipped_on_board": ("2026-09-05", 0.91),
    "cargo": ([{"marks": "AE-01", "description": "Machine parts", "packages": 12, "gross_weight_kg": 3400.0},
               {"marks": "AE-02", "description": "Spare bearings", "packages": 4, "gross_weight_kg": 310.5}], 0.88),
}
GENERIC_SCHEMA = [{"key": "title", "label": "Title", "type": "string", "description": "", "required": True,
                   "role": "none", "columns": []}]


def _kind(block) -> str:
    raw = base64.b64decode(block["source"]["data"])
    return "invoice" if b"INVOICE" in raw else "bill_of_lading" if b"BILL OF LADING" in raw else "other"


async def classify(block, catalog):
    await asyncio.sleep(1.2)
    kind = _kind(block)
    if kind == "invoice":
        return Classification(doc_type="invoice", display_name="Invoice", purpose="Supplier bill",
                              confidence=0.97, reasoning="Titled 'Invoice'."), {"model": "demo-stub"}
    if kind == "bill_of_lading":
        return Classification(doc_type="bill_of_lading", display_name="Bill of lading",
                              purpose="Carrier's receipt for goods shipped and the contract of carriage.",
                              confidence=0.95, reasoning="Carrier B/L with ports and cargo."), {"model": "demo-stub"}
    return Classification(doc_type="unrecognised_document", display_name="Unrecognised document",
                          purpose="Demo mode cannot read real documents.", confidence=0.4,
                          reasoning="Demo stub."), {"model": "demo-stub"}


async def generate_schema(block, cls):
    await asyncio.sleep(1.5)
    return dynamic_schema.sanitize_schema(BOL_SCHEMA if _kind(block) == "bill_of_lading" else GENERIC_SCHEMA), \
        {"model": "demo-stub"}


async def extract(block, spec):
    await asyncio.sleep(1.5)
    return InvoiceData(
        invoice_number="INV-1001", invoice_date="2026-09-01", due_date="2026-10-01",
        vendor_name="Acme Supplies Inc", vendor_address="12 Industrial Rd, Pune", currency="USD", po_number="PO-7",
        subtotal=110.0, tax_amount=11.0, total_amount=125.0, payment_terms="Net 30",
        line_items=[
            {"description": "Steel bolts M8", "sku": "BLT-M8", "quantity": 500, "unit_price": 0.12, "amount": 60.0},
            {"description": "Washers", "quantity": 120, "unit_price": 0.5, "amount": 50.0},
        ],
        low_confidence_fields=["due_date"],
    ), {"model": "demo-stub"}


async def extract_dynamic(block, schema, display_name):
    await asyncio.sleep(1.5)
    values = BOL_VALUES if _kind(block) == "bill_of_lading" else {"title": ("(demo mode)", 0.3)}
    raw = {}
    for s in schema:
        v, c = values.get(s["key"], (None, 0.5))
        raw[s["key"]] = {"rows": v or [], "confidence": c} if s["type"] == "table" else {"value": v, "confidence": c}
    return raw, {"model": "demo-stub"}


llm.classify, llm.generate_schema, llm.extract, llm.extract_dynamic = classify, generate_schema, extract, extract_dynamic


async def seed():
    await init_db()
    async with SessionLocal() as s:
        s.add(PurchaseOrder(po_number="PO-7", vendor_name="ACME Supplies LLC", currency="USD", total_amount=121.0,
                            line_items=[{"description": "Steel bolts M8", "sku": "BLT-M8", "quantity": 500,
                                         "unit_price": 0.12, "amount": 60.0},
                                        {"description": "Washers", "sku": None, "quantity": 100,
                                         "unit_price": 0.5, "amount": 50.0}]))
        await s.commit()


if __name__ == "__main__":
    (tmp / "uploads").mkdir(parents=True, exist_ok=True)
    asyncio.run(seed())
    print(f"Demo data in {tmp}")
    uvicorn.run(app, port=8765)
