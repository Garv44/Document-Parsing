"""End-to-end API flows with the Claude calls stubbed out.

The fake LLM decides what a document "is" from a marker in the file bytes (TYPE:<slug>), so each test
can upload built-in and previously unseen document types through the real pipeline, DB and ERP layer.
"""
import asyncio
import base64
import itertools

import httpx
import pytest

from app import config, dynamic_schema, llm
from app.db import engine, init_db
from app.doc_schemas import Classification, InvoiceData, PurchaseOrderData, ReceiptData
from app.main import app

_seq = itertools.count()


def pdf(doc_type: str) -> bytes:
    """A unique fake PDF carrying a type marker (unique so duplicate-file checks don't interfere)."""
    return f"%PDF-1.4\nTYPE:{doc_type}\n#{next(_seq)}\n".encode()


INVOICE = InvoiceData(
    invoice_number="INV-1001", invoice_date="2026-09-01", due_date="2026-10-01", vendor_name="Acme Supplies Inc",
    currency="USD", po_number="PO-7", subtotal=110.0, tax_amount=11.0, total_amount=121.0,
    line_items=[{"description": "Steel bolts M8", "sku": "BLT-M8", "quantity": 500, "unit_price": 0.12, "amount": 60.0},
                {"description": "Washers", "quantity": 100, "unit_price": 0.5, "amount": 50.0}],
)
RECEIPT = ReceiptData(
    merchant_name="Corner Cafe", receipt_number="R-55", transaction_date="2026-09-10", payment_method="Visa",
    currency="USD", subtotal=10.0, tax_amount=1.0, tip_amount=2.0, total_amount=13.0,
    line_items=[{"description": "Flat white", "quantity": 2, "unit_price": 5.0, "amount": 10.0}],
)
PURCHASE_ORDER = PurchaseOrderData(
    po_number="PO-900", order_date="2026-09-02", buyer_name="Globex Manufacturing", vendor_name="Initech Parts",
    currency="USD", subtotal=200.0, tax_amount=0.0, total_amount=200.0,
    line_items=[{"description": "Gaskets", "sku": "GSK-1", "quantity": 100, "unit_price": 2.0, "amount": 200.0}],
)
BUILTIN_OUTPUT = {"invoice": INVOICE, "receipt": RECEIPT, "purchase_order": PURCHASE_ORDER}

# Types the platform has never seen: the schema Claude would design, and what it would extract.
BOL_SCHEMA = [
    {"key": "bol_number", "label": "B/L number", "type": "string", "description": "", "required": True,
     "role": "document_number", "columns": []},
    {"key": "carrier", "label": "Carrier", "type": "string", "description": "", "required": True, "role": "party",
     "columns": []},
    {"key": "shipper", "label": "Shipper", "type": "string", "description": "", "required": True,
     "role": "counterparty", "columns": []},
    {"key": "issue_date", "label": "Issue date", "type": "date", "description": "", "required": True,
     "role": "document_date", "columns": []},
    {"key": "port_of_loading", "label": "Port of loading", "type": "string", "description": "", "required": False,
     "role": "none", "columns": []},
    {"key": "cargo", "label": "Cargo", "type": "table", "description": "", "required": True, "role": "none",
     "columns": [{"key": "description", "label": "Description", "type": "string", "role": "description"},
                 {"key": "packages", "label": "Packages", "type": "integer", "role": "quantity"},
                 {"key": "gross_weight_kg", "label": "Gross weight (kg)", "type": "number", "role": "none"}]},
]
BOL_DATA = {"bol_number": ("MAEU-123", 0.98), "carrier": ("Maersk Line", 0.97), "shipper": ("Acme Exports", 0.95),
            "issue_date": ("2026-09-05", 0.9), "port_of_loading": ("Nhava Sheva", 0.45),
            "cargo": ([{"description": "Machine parts", "packages": 12, "gross_weight_kg": 3400.0}], 0.9)}

UTILITY_SCHEMA = [
    {"key": "account_number", "label": "Account", "type": "string", "description": "", "required": True,
     "role": "document_number", "columns": []},
    {"key": "provider", "label": "Provider", "type": "string", "description": "", "required": True, "role": "party",
     "columns": []},
    {"key": "charges", "label": "Charges", "type": "table", "description": "", "required": True, "role": "none",
     "columns": [{"key": "item", "label": "Item", "type": "string", "role": "description"},
                 {"key": "amount", "label": "Amount", "type": "number", "role": "line_amount"}]},
    {"key": "subtotal", "label": "Subtotal", "type": "number", "description": "", "required": False,
     "role": "subtotal", "columns": []},
    {"key": "tax", "label": "Tax", "type": "number", "description": "", "required": False, "role": "tax",
     "columns": []},
    {"key": "total", "label": "Total", "type": "number", "description": "", "required": True, "role": "total",
     "columns": []},
]
UTILITY_DATA = {"account_number": ("ACC-1", 0.99), "provider": ("City Power", 0.99),
                "charges": ([{"item": "Energy", "amount": 75.0}, {"item": "Standing", "amount": 10.0}], 0.95),
                "subtotal": (85.0, 0.99), "tax": (17.0, 0.99), "total": (120.0, 0.99)}  # 85 + 17 != 120

UNSEEN = {
    "bill_of_lading": ("Bill of lading", "Receipt for goods shipped by a carrier.", BOL_SCHEMA, BOL_DATA),
    "packing_list": ("Packing list", "Lists the contents of a shipment.", BOL_SCHEMA, BOL_DATA),
    "utility_bill": ("Utility bill", "A bill for energy consumption.", UTILITY_SCHEMA, UTILITY_DATA),
    "mystery_form": ("Mystery form", "Unclear.", BOL_SCHEMA, BOL_DATA),
    "customs_declaration": ("Customs declaration", "Declares goods to customs.", BOL_SCHEMA, BOL_DATA),
}


def _marker(block: dict) -> str:
    raw = base64.b64decode(block["source"]["data"])
    return raw.split(b"TYPE:")[1].split(b"\n")[0].decode()


@pytest.fixture
def fake_llm(monkeypatch):
    calls = {"classify": 0, "extract": 0, "design": 0, "extract_dynamic": 0, "confidence": {}, "catalogs": []}

    async def classify(block, catalog):
        calls["classify"] += 1
        calls["catalogs"].append([t.slug for t in catalog])
        slug = _marker(block)
        conf = calls["confidence"].get(slug, 0.95)
        name, purpose = UNSEEN[slug][:2] if slug in UNSEEN else (slug.replace("_", " ").title(), "built-in")
        return Classification(doc_type=slug, display_name=name, purpose=purpose, confidence=conf,
                              reasoning="stub"), {}

    async def extract(block, spec):
        calls["extract"] += 1
        return BUILTIN_OUTPUT[_marker(block)].model_copy(deep=True), {}

    async def generate_schema(block, cls):
        calls["design"] += 1
        return dynamic_schema.sanitize_schema(UNSEEN[_marker(block)][2]), {}

    async def extract_dynamic(block, schema, display_name):
        calls["extract_dynamic"] += 1
        values = UNSEEN[_marker(block)][3]
        raw = {}
        for s in schema:
            v, c = values.get(s["key"], (None, 0.5))
            raw[s["key"]] = {"rows": v, "confidence": c} if s["type"] == "table" else {"value": v, "confidence": c}
        return raw, {}

    for name, fn in [("classify", classify), ("extract", extract), ("generate_schema", generate_schema),
                     ("extract_dynamic", extract_dynamic)]:
        monkeypatch.setattr(llm, name, fn)
    return calls


def run(flow):
    """Run one test flow in a fresh event loop against the ASGI app (background tasks finish before
    each response returns under ASGITransport, so no polling is needed)."""
    async def wrapper():
        config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        await init_db()
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
                await flow(c)
        finally:
            await engine.dispose()
    asyncio.run(wrapper())


async def upload(c, content: bytes, name: str = "doc.pdf") -> dict:
    r = await c.post("/api/documents", files={"file": (name, content, "application/pdf")})
    assert r.status_code == 202, r.text
    return (await c.get(f"/api/jobs/{r.json()['job_id']}")).json()


def codes(job):
    return {i["code"] for i in job["issues"]}


# =========================================================================== built-in types


def test_invoice_full_flow(fake_llm):
    async def flow(c):
        r = await c.post("/api/purchase-orders", json={
            "po_number": "PO-7", "vendor_name": "ACME Supplies, LLC", "currency": "USD", "total_amount": 121.0,
            "line_items": [{"description": "Steel bolts M8", "sku": "BLT-M8", "quantity": 500, "unit_price": 0.12},
                           {"description": "Washers", "quantity": 100, "unit_price": 0.5}]})
        assert r.status_code == 201

        content = pdf("invoice")
        job = await upload(c, content, "inv.pdf")
        assert (job["doc_type"], job["type_kind"], job["schema_source"]) == ("invoice", "built_in", "built_in")
        assert any(s["key"] == "line_items" and s["type"] == "table" for s in job["schema"])
        assert job["po_match"]["status"] == "matched"
        assert job["status"] == "validated", job["issues"]
        assert job["erp_record_type"] == "ap_bill"

        r = await c.post(f"/api/jobs/{job['id']}/approve", json={})
        assert r.status_code == 200
        body = r.json()
        assert (body["status"], body["erp_status"], body["target"]) == ("exported", "exported", "mock-erp")
        assert (await c.post(f"/api/jobs/{job['id']}/approve", json={})).status_code == 409  # no double export
        exports = (await c.get("/api/erp/exports")).json()
        assert exports[0]["payload"]["record_type"] == "ap_bill" and exports[0]["payload"]["total_amount"] == 121.0
        records = (await c.get("/api/records?doc_type=invoice")).json()
        assert any(r["job_id"] == job["id"] and r["erp_status"] == "exported" for r in records)

        # same file again -> duplicate errors, approval blocked until overridden
        r = await c.post("/api/documents", files={"file": ("inv-copy.pdf", content, "application/pdf")})
        dup = (await c.get(f"/api/jobs/{r.json()['job_id']}")).json()
        assert {"duplicate_file", "duplicate_document"} <= codes(dup)
        assert dup["status"] == "needs_review"
        assert (await c.post(f"/api/jobs/{dup['id']}/approve", json={})).status_code == 409
        assert (await c.post(f"/api/jobs/{dup['id']}/reject")).json()["status"] == "rejected"
    run(flow)


def test_receipt_full_flow(fake_llm):
    async def flow(c):
        job = await upload(c, pdf("receipt"))
        assert (job["doc_type"], job["doc_type_name"], job["schema_source"]) == ("receipt", "Receipt", "built_in")
        assert job["data"]["tip_amount"] == 2.0
        assert job["status"] == "validated", job["issues"]  # 10 + 1 + 2 tip = 13
        r = await c.post(f"/api/jobs/{job['id']}/approve", json={})
        assert r.json()["status"] == "exported"
        payload = (await c.get("/api/erp/exports")).json()[0]["payload"]
        assert (payload["record_type"], payload["party"]["name"], payload["total_amount"]) == \
            ("expense", "Corner Cafe", 13.0)
    run(flow)


def test_purchase_order_document_becomes_matchable(fake_llm):
    async def flow(c):
        job = await upload(c, pdf("purchase_order"))
        assert (job["doc_type"], job["schema_source"]) == ("purchase_order", "built_in")
        assert job["status"] == "validated", job["issues"]
        r = await c.post(f"/api/jobs/{job['id']}/approve", json={})
        assert r.json()["status"] == "exported"
        assert (await c.get("/api/erp/exports")).json()[0]["payload"]["record_type"] == "purchase_order"
        pos = {p["po_number"]: p for p in (await c.get("/api/purchase-orders")).json()}
        assert pos["PO-900"]["source"] == "document" and pos["PO-900"]["source_job_id"] == job["id"]
        assert pos["PO-900"]["line_items"][0]["sku"] == "GSK-1"
    run(flow)


def test_review_correction_revalidates(fake_llm):
    async def flow(c):
        job = await upload(c, pdf("invoice"))
        data = job["data"]
        data["invoice_number"] = "INV-2002"
        data["total_amount"] = 999.0
        r = await c.put(f"/api/jobs/{job['id']}/data", json={"data": data})
        assert r.status_code == 200
        body = r.json()
        assert body["corrected"] is True
        assert "total_mismatch" in codes(body)
        assert body["extracted_data"]["total_amount"] == 121.0  # original kept for audit
    run(flow)


def test_uncertain_classification_is_flagged_not_blocked(fake_llm):
    fake_llm["confidence"]["invoice"] = 0.3

    async def flow(c):
        job = await upload(c, pdf("invoice"))
        assert job["status"] == "needs_review"
        assert job["issues"][0]["code"] == "classification_uncertain"
        assert fake_llm["extract"] == 1 and job["data"]["invoice_number"] == "INV-1001"  # still processed

        # reviewer confirms the type -> classification skipped, warning gone
        before = fake_llm["classify"]
        r = await c.post(f"/api/jobs/{job['id']}/reprocess", json={"doc_type": "invoice"})
        assert r.status_code == 202
        job = (await c.get(f"/api/jobs/{job['id']}")).json()
        assert fake_llm["classify"] == before and fake_llm["extract"] == 2
        assert job["doc_type_forced"] is True and "classification_uncertain" not in codes(job)
    run(flow)


def test_llm_failure_marks_job_failed(fake_llm, monkeypatch):
    async def boom(block, catalog):
        raise llm.LLMError("Anthropic API key missing or invalid.")
    monkeypatch.setattr(llm, "classify", boom)

    async def flow(c):
        job = await upload(c, pdf("invoice"))
        assert job["status"] == "failed" and "API key" in job["error"]
        assert (await c.post(f"/api/jobs/{job['id']}/reject")).status_code == 200
    run(flow)


def test_upload_rejects_bad_files(fake_llm):
    async def flow(c):
        r = await c.post("/api/documents", files={"file": ("a.txt", b"hello", "text/plain")})
        assert r.status_code == 415
        r = await c.post("/api/documents", files={"file": ("a.pdf", b"not a pdf", "application/pdf")})
        assert r.status_code == 415
    run(flow)


def test_builtin_types_cannot_be_reconfigured(fake_llm):
    async def flow(c):
        r = await c.put("/api/doc-types/invoice", json={"erp_record_type": "something_else"})
        assert r.status_code == 409
        types = {t["id"]: t for t in (await c.get("/api/doc-types")).json()}
        assert {k: types[k]["erp_record_type"] for k in ("invoice", "receipt", "purchase_order")} == \
            {"invoice": "ap_bill", "receipt": "expense", "purchase_order": "purchase_order"}
        assert all(types[k]["kind"] == "built_in" for k in ("invoice", "receipt", "purchase_order"))
    run(flow)


# =========================================================================== previously unseen types


def test_unseen_type_end_to_end(fake_llm):
    async def flow(c):
        job = await upload(c, pdf("bill_of_lading"), "bol.pdf")

        # 1-4: classified, schema designed, extracted with confidences
        assert (job["doc_type"], job["doc_type_name"], job["type_kind"], job["schema_source"]) == \
            ("bill_of_lading", "Bill of lading", "discovered", "generated")
        assert job["doc_type_purpose"] == "Receipt for goods shipped by a carrier."
        assert fake_llm["design"] == 1 and fake_llm["extract_dynamic"] == 1 and fake_llm["extract"] == 0
        assert [s["key"] for s in job["schema"]] == [s["key"] for s in BOL_SCHEMA]
        assert job["data"]["cargo"][0]["packages"] == 12
        assert job["field_confidence"]["port_of_loading"] == 0.45
        assert job["party"] == "Maersk Line" and job["doc_number"] == "MAEU-123"

        # 6: generic validation flags the low-confidence field
        assert job["status"] == "needs_review"
        assert [i["field"] for i in job["issues"] if i["code"] == "low_confidence"] == ["port_of_loading"]
        assert job["po_match"] is None

        # type registry now knows it - with no ERP mapping
        t = (await c.get("/api/doc-types/bill_of_lading")).json()
        assert (t["kind"], t["erp_record_type"], t["field_count"]) == ("discovered", None, len(BOL_SCHEMA))
        assert job["erp_record_type"] is None

        # 8: reviewer corrects; confidences become "verified"; re-validated clean
        data = job["data"] | {"port_of_loading": "Nhava Sheva (INNSA)"}
        r = await c.put(f"/api/jobs/{job['id']}/data", json={"data": data})
        assert r.status_code == 200, r.text
        job = r.json()
        assert job["corrected"] and job["data"]["port_of_loading"] == "Nhava Sheva (INNSA)"
        assert set(job["field_confidence"].values()) == {1.0}
        assert job["status"] == "validated", job["issues"]
        assert job["extracted_data"]["port_of_loading"]["value"] == "Nhava Sheva"  # original kept

        # bad corrections are rejected against the discovered schema
        bad = job["data"] | {"cargo": [{"description": "x", "packages": "a dozen", "gross_weight_kg": 1}]}
        assert (await c.put(f"/api/jobs/{job['id']}/data", json={"data": bad})).status_code == 422

        # 9 + ERP redesign: approval stores a record but does NOT export (no mapping)
        exports_before = len((await c.get("/api/erp/exports")).json())
        r = await c.post(f"/api/jobs/{job['id']}/approve", json={})
        assert r.status_code == 200
        assert (r.json()["status"], r.json()["erp_status"], r.json()["target"]) == \
            ("approved", "awaiting_configuration", None)
        assert len((await c.get("/api/erp/exports")).json()) == exports_before
        held = (await c.get("/api/records?erp_status=awaiting_configuration")).json()
        rec = next(r for r in held if r["job_id"] == job["id"])
        assert rec["doc_type"] == "bill_of_lading" and rec["data"]["carrier"] == "Maersk Line"
        assert [s["key"] for s in rec["schema"]] == [s["key"] for s in BOL_SCHEMA]  # 10: schema preserved
        assert (await c.get("/api/doc-types/bill_of_lading")).json()["records_awaiting_configuration"] >= 1

        # exporting before configuration is refused; record is final
        r = await c.post(f"/api/jobs/{job['id']}/export")
        assert r.status_code == 409 and "No ERP mapping" in r.json()["detail"]
        assert (await c.put(f"/api/jobs/{job['id']}/data", json={"data": data})).status_code == 409
        assert (await c.post(f"/api/jobs/{job['id']}/reject")).status_code == 409

        # operator configures routing -> held record can be exported with the generic payload
        r = await c.put("/api/doc-types/bill_of_lading", json={"erp_record_type": "Inbound Shipment"})
        assert r.json()["erp_record_type"] == "inbound_shipment"
        r = await c.post(f"/api/jobs/{job['id']}/export")
        assert r.status_code == 200 and r.json()["status"] == "exported"
        payload = (await c.get("/api/erp/exports")).json()[0]["payload"]
        assert payload["record_type"] == "inbound_shipment"
        assert payload["document_type"] == {"id": "bill_of_lading", "name": "Bill of lading"}
        assert payload["fields"]["bol_number"] == "MAEU-123" and payload["tables"]["cargo"][0]["packages"] == 12
        assert (await c.get(f"/api/jobs/{job['id']}")).json()["record"]["erp_status"] == "exported"
    run(flow)


def test_second_document_of_discovered_type_reuses_schema(fake_llm):
    async def flow(c):
        first = await upload(c, pdf("packing_list"))
        second = await upload(c, pdf("packing_list"))
        assert first["schema_source"] == "generated" and second["schema_source"] == "reused"
        assert fake_llm["design"] == 1  # schema designed once
        assert "packing_list" in fake_llm["catalogs"][-1]  # classifier was offered the discovered type
        assert second["schema"] == first["schema"]
        assert "duplicate_document" in codes(second)  # same number + party -> role-based duplicate check
    run(flow)


def test_low_confidence_discovery_is_processed_but_not_registered(fake_llm):
    fake_llm["confidence"]["mystery_form"] = 0.35

    async def flow(c):
        job = await upload(c, pdf("mystery_form"))
        assert job["schema_source"] == "generated" and job["data"]["carrier"] == "Maersk Line"
        assert job["issues"][0]["code"] == "classification_uncertain"
        assert "mystery_form" not in {t["id"] for t in (await c.get("/api/doc-types")).json()}
        # the job still carries its own schema and can be reviewed and approved as a record
        r = await c.post(f"/api/jobs/{job['id']}/approve", json={})
        assert r.json()["erp_status"] == "awaiting_configuration"
    run(flow)


def test_generic_validation_blocks_inconsistent_unseen_document(fake_llm):
    async def flow(c):
        job = await upload(c, pdf("utility_bill"))
        assert "total_mismatch" in codes(job)  # 85 + 17 != 120, found via field roles
        assert (await c.post(f"/api/jobs/{job['id']}/approve", json={})).status_code == 409
        r = await c.post(f"/api/jobs/{job['id']}/approve", json={"override_errors": True})
        assert r.status_code == 200 and r.json()["status"] == "approved"
        rec = next(r for r in (await c.get("/api/records?doc_type=utility_bill")).json() if r["job_id"] == job["id"])
        assert rec["errors_overridden"] is True
    run(flow)


def test_regenerate_schema_and_force_new_type(fake_llm):
    async def flow(c):
        job = await upload(c, pdf("customs_declaration"))
        assert fake_llm["design"] == 1
        r = await c.post(f"/api/jobs/{job['id']}/reprocess", json={"regenerate_schema": True})
        assert r.status_code == 202
        job = (await c.get(f"/api/jobs/{job['id']}")).json()
        assert fake_llm["design"] == 2 and job["schema_source"] == "generated"

        # reviewer re-labels an unseen document as another, brand-new type
        r = await c.post(f"/api/jobs/{job['id']}/reprocess",
                         json={"doc_type": "Export Declaration", "display_name": "Export declaration"})
        job = (await c.get(f"/api/jobs/{job['id']}")).json()
        assert (job["doc_type"], job["doc_type_name"], job["doc_type_forced"]) == \
            ("export_declaration", "Export declaration", True)
        assert "export_declaration" in {t["id"] for t in (await c.get("/api/doc-types")).json()}

        assert (await c.post(f"/api/jobs/{job['id']}/reprocess",
                             json={"doc_type": "invoice", "regenerate_schema": True})).status_code == 422
    run(flow)


# =========================================================================== UI support endpoints


def test_config_samples_and_run_summary_fields(fake_llm, monkeypatch, tmp_path):
    (tmp_path / "samples").mkdir()
    (tmp_path / "samples" / "a_invoice.pdf").write_bytes(pdf("invoice"))
    (tmp_path / "samples" / "b_bol.pdf").write_bytes(pdf("bill_of_lading"))
    (tmp_path / "samples" / "notes.txt").write_text("ignored")
    monkeypatch.setattr(config, "BASE_DIR", tmp_path)

    async def flow(c):
        cfg = (await c.get("/api/config")).json()
        assert ".pdf" in cfg["accepted_extensions"] and cfg["max_upload_mb"] == config.MAX_UPLOAD_MB

        r = await c.post("/api/samples")
        assert r.status_code == 202 and len(r.json()) == 2
        jobs = {j["id"]: j for j in (await c.get("/api/jobs")).json()}
        inv, bol = (jobs[x["job_id"]] for x in r.json())
        assert inv["filename"] == "a_invoice.pdf" and inv["file_size"] == (tmp_path / "samples" / "a_invoice.pdf").stat().st_size
        # filled values: 9 non-null invoice fields + line 1 (5 cells) + line 2 (4 cells, no sku)
        assert inv["field_count"] == 9 + 5 + 4
        assert bol["doc_type"] == "bill_of_lading" and bol["field_count"] == 5 + 3

    run(flow)


def test_samples_endpoint_without_samples(fake_llm, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "BASE_DIR", tmp_path)

    async def flow(c):
        r = await c.post("/api/samples")
        assert r.status_code == 404 and "make_sample_invoice" in r.json()["detail"]
    run(flow)


# =========================================================================== llm.extract_dynamic fallback


def test_extract_dynamic_falls_back_to_envelope(monkeypatch):
    schema = dynamic_schema.sanitize_schema(UTILITY_SCHEMA)
    seen = []

    async def fake_parse(content, model, effort, max_tokens):
        seen.append(model)
        if len(seen) == 1:
            raise llm.SchemaRejected("schema too complex")
        return dynamic_schema.Envelope.model_validate({
            "fields": [{"key": "total", "value": "USD 120.00", "confidence": 0.9}],
            "tables": [{"key": "charges", "confidence": 0.8,
                        "rows": [{"cells": [{"column": "item", "value": "Energy"},
                                            {"column": "amount", "value": "75"}]}]}]}), {"model": "stub"}

    monkeypatch.setattr(llm, "_parse", fake_parse)
    raw, usage = asyncio.run(llm.extract_dynamic({"type": "document"}, schema, "Utility bill"))
    assert seen[0].__name__ == "DynamicExtraction" and seen[1] is dynamic_schema.Envelope
    assert usage["fallback_format"] is True
    assert raw["total"] == {"value": 120.0, "confidence": 0.9}
    assert raw["charges"]["rows"] == [{"item": "Energy", "amount": 75.0}]
