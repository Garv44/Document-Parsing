"""Approval and ERP routing.

Approving a job always writes an immutable Record (the reviewed structured data). Then:
  * the type has an ERP mapping  -> the record is exported      (job: exported)
  * no mapping (a discovered type nobody has configured yet)
                                 -> the record is held          (job: approved, record: awaiting_configuration)
Once an operator configures a mapping for that type (PUT /api/doc-types/{slug}), held records can be
exported with export().

Built-in types map to fixed record types (ap_bill / expense / purchase_order) with specialised payloads.
A configured discovered type exports a generic payload: its schema-keyed fields and tables.

Delivery: ERP_WEBHOOK_URL set -> POST JSON (bearer ERP_API_KEY); otherwise the built-in mock ERP table.
"""
from datetime import datetime, timezone

import httpx
from sqlalchemy import select

from app import config, type_registry
from app.db import SessionLocal
from app.doc_schemas import BUILTIN_TYPES
from app.models import ErpExport, Job, JobStatus, PurchaseOrder, Record, RecordErpStatus


class ExportBlocked(Exception):
    pass


def build_payload(job: Job, record_type: str) -> dict:
    d = job.data or {}
    source = {"filename": job.filename, "sha256": job.file_sha256, "human_corrected": job.corrected,
              "document_type": job.doc_type, "schema_source": job.schema_source}
    spec = BUILTIN_TYPES.get(job.doc_type)
    if spec is None:
        schema = job.extraction_schema or []
        return {
            "record_type": record_type,
            "external_id": job.id,
            "document_type": {"id": job.doc_type, "name": job.doc_type_name},
            "fields": {s["key"]: d.get(s["key"]) for s in schema if s["type"] != "table"},
            "tables": {s["key"]: d.get(s["key"]) or [] for s in schema if s["type"] == "table"},
            "source": source,
        }
    return {
        "record_type": record_type,
        "external_id": job.id,
        "document_number": d.get(spec.number_field),
        "document_date": d.get(spec.date_field),
        "due_date": d.get("due_date"),
        "party": {"name": d.get(spec.party_field), "tax_id": d.get("vendor_tax_id"),
                  "address": d.get("vendor_address")},
        "po_number": d.get("po_number"),
        "currency": d.get("currency"),
        "subtotal": d.get("subtotal"),
        "tax_amount": d.get("tax_amount"),
        "total_amount": d.get("total_amount"),
        "payment_terms": d.get("payment_terms"),
        "lines": [
            {"line_no": i + 1, **{k: li.get(k) for k in ("sku", "description", "quantity", "unit_price", "amount")}}
            for i, li in enumerate(d.get("line_items") or [])
        ],
        "source": {**source, "po_match_status": (job.po_match or {}).get("status")},
    }


async def _deliver(payload: dict) -> tuple[bool, int | None, str]:
    if not config.ERP_WEBHOOK_URL:
        return True, None, "stored in mock ERP"
    headers = {"Authorization": f"Bearer {config.ERP_API_KEY}"} if config.ERP_API_KEY else {}
    try:
        async with httpx.AsyncClient(timeout=20) as http:
            r = await http.post(config.ERP_WEBHOOK_URL, json=payload, headers=headers)
        return r.is_success, r.status_code, r.text[:2000]
    except httpx.HTTPError as e:
        return False, None, f"{type(e).__name__}: {e}"


async def _export(s, job: Job, record: Record, record_type: str) -> ErpExport:
    payload = build_payload(job, record_type)
    ok, status, body = await _deliver(payload)
    export = ErpExport(job_id=job.id, target=config.ERP_WEBHOOK_URL or "mock-erp", payload=payload,
                       success=ok, response_status=status, response_body=body)
    s.add(export)
    await s.flush()
    record.erp_export_id = export.id
    if ok:
        record.erp_status = RecordErpStatus.EXPORTED
        job.status = JobStatus.EXPORTED
        if job.doc_type == "purchase_order":
            await _register_po(s, job)
    else:
        record.erp_status = RecordErpStatus.EXPORT_FAILED
    return export


async def approve(job_id: str, override_errors: bool = False) -> tuple[Record, ErpExport | None]:
    """Store the reviewed record; export it if the document type has an ERP mapping."""
    async with SessionLocal() as s:
        job = await s.get(Job, job_id)
        if job is None:
            raise ExportBlocked("Job not found.")
        if job.status not in JobStatus.REVIEWABLE:
            raise ExportBlocked(f"Job is '{job.status}' and cannot be approved.")
        if not job.data or not job.extraction_schema:
            raise ExportBlocked("No extracted data to approve.")
        errors = [i for i in job.issues or [] if i["severity"] == "error"]
        if errors and not override_errors:
            raise ExportBlocked(f"{len(errors)} validation error(s) must be fixed or explicitly overridden.")

        record = Record(job_id=job.id, doc_type=job.doc_type, doc_type_name=job.doc_type_name,
                        schema=job.extraction_schema, data=job.data, field_confidence=job.field_confidence,
                        human_corrected=job.corrected, errors_overridden=bool(errors),
                        erp_status=RecordErpStatus.AWAITING_CONFIGURATION)
        s.add(record)
        job.status = JobStatus.APPROVED
        job.approved_at = datetime.now(timezone.utc)

        export = None
        record_type = await type_registry.erp_record_type(s, job.doc_type)
        if record_type:
            export = await _export(s, job, record, record_type)
        await s.commit()
        return record, export


async def export(job_id: str) -> ErpExport:
    """Export an already-approved record (held for configuration, or a failed delivery being retried)."""
    async with SessionLocal() as s:
        job = await s.get(Job, job_id)
        record = await s.scalar(select(Record).where(Record.job_id == job_id))
        if job is None or record is None or job.status != JobStatus.APPROVED:
            raise ExportBlocked("Only approved records that have not been exported can be exported.")
        record_type = await type_registry.erp_record_type(s, job.doc_type)
        if not record_type:
            raise ExportBlocked(f"No ERP mapping is configured for document type '{job.doc_type}'. "
                                "Configure one on the Document types screen first.")
        exp = await _export(s, job, record, record_type)
        await s.commit()
        return exp


async def _register_po(s, job: Job) -> None:
    d = job.data
    if not d.get("po_number"):
        return
    po = await s.scalar(select(PurchaseOrder).where(PurchaseOrder.po_number == d["po_number"]))
    if po is None:
        po = PurchaseOrder(po_number=d["po_number"])
        s.add(po)
    po.vendor_name = d.get("vendor_name") or ""
    po.currency = d.get("currency")
    po.order_date = d.get("order_date")
    po.total_amount = d.get("total_amount")
    po.line_items = d.get("line_items") or []
    po.source, po.source_job_id = "document", job.id
