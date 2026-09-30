"""HTTP API + static review UI.

POST /api/documents               upload -> 202 {job_id}; processing continues in the background
GET  /api/jobs/{id}               poll status / results (includes the job's schema and field confidences)
PUT  /api/jobs/{id}/data          reviewer corrections -> re-validated (no LLM call)
POST /api/jobs/{id}/reprocess     re-run the LLM; optionally force a type or regenerate a discovered schema
POST /api/jobs/{id}/approve       store the reviewed record; export it if the type has an ERP mapping
POST /api/jobs/{id}/export        export a held record once its type has been configured
GET  /api/doc-types               built-in + discovered types; PUT /api/doc-types/{id} configures ERP routing
GET  /api/records                 reviewed structured records of any type
"""
import hashlib
import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import config, dynamic_schema, erp, pipeline, type_registry
from app.db import get_session, init_db
from app.doc_schemas import BUILTIN_TYPES, LineItem
from app.models import DocumentType, ErpExport, Job, JobStatus, PurchaseOrder, Record

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

MEDIA_TYPES = {
    ".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp", ".gif": "image/gif",
}
MAGIC = {"application/pdf": [b"%PDF"], "image/png": [b"\x89PNG"], "image/jpeg": [b"\xff\xd8\xff"],
         "image/webp": [b"RIFF"], "image/gif": [b"GIF87a", b"GIF89a"]}


@asynccontextmanager
async def lifespan(_: FastAPI):
    config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    await init_db()
    await pipeline.resume_pending()
    yield


app = FastAPI(title="DocParse - intelligent document processing", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=config.BASE_DIR / "app" / "static"), name="static")


@app.get("/", include_in_schema=False)
async def index():
    return RedirectResponse("/static/index.html")


# ------------------------------------------------------------------ serialisation


def _party(j: Job) -> str | None:
    for s in j.extraction_schema or []:
        if s.get("role") == "party":
            return (j.data or {}).get(s["key"])
    return None


def _currency(j: Job) -> str | None:
    for s in j.extraction_schema or []:
        if s.get("role") == "currency":
            return (j.data or {}).get(s["key"])
    return None


def _filled_fields(j: Job) -> int | None:
    """Number of extracted values: filled scalar fields plus filled table cells."""
    if not j.data or not j.extraction_schema:
        return None
    n = 0
    for spec in j.extraction_schema:
        v = j.data.get(spec["key"])
        if spec["type"] == "table":
            n += sum(1 for row in v or [] for c in spec["columns"] if row.get(c["key"]) not in (None, ""))
        elif v not in (None, ""):
            n += 1
    return n


def job_summary(j: Job) -> dict:
    counts = {"error": 0, "warning": 0, "info": 0}
    for i in j.issues or []:
        counts[i["severity"]] = counts.get(i["severity"], 0) + 1
    return {
        "id": j.id, "filename": j.filename, "file_size": j.file_size, "field_count": _filled_fields(j),
        "status": j.status, "doc_type": j.doc_type,
        "doc_type_name": j.doc_type_name, "type_kind": "built_in" if j.doc_type in BUILTIN_TYPES else
        ("discovered" if j.doc_type else None), "schema_source": j.schema_source,
        "doc_number": j.doc_number, "party": _party(j), "total_amount": j.total_amount, "currency": _currency(j),
        "issue_counts": counts, "error": j.error, "created_at": j.created_at, "updated_at": j.updated_at,
    }


async def job_detail(s: AsyncSession, j: Job) -> dict:
    record = await s.scalar(select(Record).where(Record.job_id == j.id))
    return {
        **job_summary(j), "media_type": j.media_type, "classification": j.classification,
        "doc_type_forced": j.doc_type_forced, "doc_type_purpose": j.doc_type_purpose,
        "schema": j.extraction_schema, "data": j.data, "field_confidence": j.field_confidence,
        "extracted_data": j.extracted_data, "corrected": j.corrected, "issues": j.issues or [],
        "po_match": j.po_match, "llm_usage": j.llm_usage, "approved_at": j.approved_at,
        "erp_record_type": await type_registry.erp_record_type(s, j.doc_type),
        "record": {"id": record.id, "erp_status": record.erp_status, "erp_export_id": record.erp_export_id}
        if record else None,
    }


async def _job_or_404(s: AsyncSession, job_id: str) -> Job:
    job = await s.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    return job


# ------------------------------------------------------------------ documents / jobs


async def _create_job(s: AsyncSession, background: BackgroundTasks, name: str, content: bytes) -> dict:
    ext = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
    media_type = MEDIA_TYPES.get(ext)
    if media_type is None:
        raise HTTPException(415, f"Unsupported file type. Allowed: {', '.join(MEDIA_TYPES)}")
    if len(content) > config.MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(413, f"File larger than {config.MAX_UPLOAD_MB} MB")
    if not any(content.startswith(m) for m in MAGIC[media_type]):
        raise HTTPException(415, "File contents do not match its extension")

    job = Job(filename=name, stored_path="", media_type=media_type, file_size=len(content),
              file_sha256=hashlib.sha256(content).hexdigest())
    s.add(job)
    await s.flush()
    path = config.UPLOAD_DIR / f"{job.id}{ext}"
    path.write_bytes(content)
    job.stored_path = str(path)
    await s.commit()

    background.add_task(pipeline.process_job, job.id)
    return {"job_id": job.id, "status": job.status, "poll": f"/api/jobs/{job.id}"}


@app.post("/api/documents", status_code=202)
async def upload(file: UploadFile, background: BackgroundTasks, s: AsyncSession = Depends(get_session)):
    return await _create_job(s, background, file.filename or "", await file.read())


@app.get("/api/config")
async def public_config():
    """Upload limits for the UI, so it never advertises formats the backend rejects."""
    return {"accepted_extensions": list(MEDIA_TYPES), "max_upload_mb": config.MAX_UPLOAD_MB}


@app.post("/api/samples", status_code=202)
async def upload_samples(background: BackgroundTasks, s: AsyncSession = Depends(get_session)):
    """Queue the bundled sample documents (samples/) - the UI's "Try a sample" button."""
    folder = config.BASE_DIR / "samples"
    files = sorted(p for p in folder.glob("*") if p.suffix.lower() in MEDIA_TYPES) if folder.is_dir() else []
    if not files:
        raise HTTPException(404, "No sample documents found. Run: python scripts/make_sample_invoice.py")
    return [await _create_job(s, background, p.name, p.read_bytes()) for p in files]


@app.get("/api/jobs")
async def list_jobs(status: str | None = None, doc_type: str | None = None, limit: int = 100,
                    s: AsyncSession = Depends(get_session)):
    q = select(Job).order_by(Job.created_at.desc()).limit(min(limit, 500))
    if status:
        q = q.where(Job.status == status)
    if doc_type:
        q = q.where(Job.doc_type == doc_type)
    return [job_summary(j) for j in (await s.scalars(q)).all()]


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str, s: AsyncSession = Depends(get_session)):
    return await job_detail(s, await _job_or_404(s, job_id))


@app.get("/api/jobs/{job_id}/file")
async def get_file(job_id: str, s: AsyncSession = Depends(get_session)):
    job = await _job_or_404(s, job_id)
    return FileResponse(job.stored_path, media_type=job.media_type, filename=job.filename,
                        content_disposition_type="inline")


class DataUpdate(BaseModel):
    data: dict[str, Any]


@app.put("/api/jobs/{job_id}/data")
async def update_data(job_id: str, body: DataUpdate, s: AsyncSession = Depends(get_session)):
    job = await _job_or_404(s, job_id)
    if job.status not in JobStatus.REVIEWABLE or not job.data or not job.extraction_schema:
        raise HTTPException(409, f"Job is '{job.status}' and cannot be edited")
    builtin = BUILTIN_TYPES.get(job.doc_type)
    model = builtin.model if builtin else dynamic_schema.values_model(job.extraction_schema)
    try:
        clean = model.model_validate(body.data).model_dump()
    except ValidationError as e:
        raise HTTPException(422, e.errors(include_url=False, include_context=False)) from e
    # The reviewer has checked every value, so the model's uncertainty markers no longer apply.
    if builtin:
        clean["low_confidence_fields"] = []
    else:
        job.field_confidence = {s_["key"]: 1.0 for s_ in job.extraction_schema}
    job.data, job.corrected = clean, True
    await s.commit()
    job = await pipeline.revalidate(job_id)
    return await job_detail(s, job)


class ReprocessRequest(BaseModel):
    doc_type: str | None = None  # force a type (built-in, discovered, or a new name); None = auto-classify
    display_name: str | None = None  # for a new type
    regenerate_schema: bool = False  # discovered types: design a fresh schema instead of reusing the stored one


@app.post("/api/jobs/{job_id}/reprocess", status_code=202)
async def reprocess(job_id: str, body: ReprocessRequest, background: BackgroundTasks,
                    s: AsyncSession = Depends(get_session)):
    job = await _job_or_404(s, job_id)
    if job.status in JobStatus.IN_PROGRESS:
        raise HTTPException(409, "Job is already processing")
    if job.status in JobStatus.FINAL:
        raise HTTPException(409, f"Job is '{job.status}'; its record is final")
    slug = dynamic_schema.normalize_slug(body.doc_type) if body.doc_type else None
    if body.regenerate_schema and (slug or job.doc_type) in BUILTIN_TYPES:
        raise HTTPException(422, "Built-in types use fixed schemas")
    job.doc_type_forced = bool(slug)
    if slug:
        job.doc_type = slug
        if body.display_name:
            job.doc_type_name = body.display_name.strip()[:128]
        else:
            known = await type_registry.get(s, slug)
            job.doc_type_name = known.display_name if known else slug.replace("_", " ").capitalize()
    job.regenerate_schema = body.regenerate_schema
    job.status, job.error, job.issues, job.po_match = JobStatus.QUEUED, None, None, None
    await s.commit()
    background.add_task(pipeline.process_job, job.id)
    return {"job_id": job.id, "status": job.status}


class ApproveRequest(BaseModel):
    override_errors: bool = False


@app.post("/api/jobs/{job_id}/approve")
async def approve(job_id: str, body: ApproveRequest, s: AsyncSession = Depends(get_session)):
    await _job_or_404(s, job_id)
    try:
        record, export = await erp.approve(job_id, body.override_errors)
    except erp.ExportBlocked as e:
        raise HTTPException(409, str(e)) from e
    return {"status": JobStatus.EXPORTED if export and export.success else JobStatus.APPROVED,
            "record_id": record.id, "erp_status": record.erp_status,
            "target": export.target if export else None,
            "erp_response": export.response_body if export and not export.success else None}


@app.post("/api/jobs/{job_id}/export")
async def export_job(job_id: str, s: AsyncSession = Depends(get_session)):
    await _job_or_404(s, job_id)
    try:
        exp = await erp.export(job_id)
    except erp.ExportBlocked as e:
        raise HTTPException(409, str(e)) from e
    if not exp.success:
        raise HTTPException(502, f"ERP rejected the record ({exp.response_status}): {exp.response_body}")
    return {"status": JobStatus.EXPORTED, "export_id": exp.id, "target": exp.target}


@app.post("/api/jobs/{job_id}/reject")
async def reject(job_id: str, s: AsyncSession = Depends(get_session)):
    job = await _job_or_404(s, job_id)
    if job.status not in JobStatus.REVIEWABLE | {JobStatus.FAILED}:
        raise HTTPException(409, f"Job is '{job.status}' and cannot be rejected")
    job.status = JobStatus.REJECTED
    await s.commit()
    return {"status": job.status}


@app.get("/api/stats")
async def stats(s: AsyncSession = Depends(get_session)):
    rows = (await s.scalars(select(Job.status))).all()
    out: dict[str, int] = {}
    for r in rows:
        out[r] = out.get(r, 0) + 1
    return {"total": len(rows), "by_status": out}


# ------------------------------------------------------------------ document types


@app.get("/api/doc-types")
async def doc_types(s: AsyncSession = Depends(get_session)):
    return await type_registry.summary(s)


@app.get("/api/doc-types/{slug}")
async def doc_type(slug: str, s: AsyncSession = Depends(get_session)):
    for t in await type_registry.summary(s):
        if t["id"] == slug:
            return t
    raise HTTPException(404, "Unknown document type")


class DocTypeUpdate(BaseModel):
    erp_record_type: str | None = None  # None/empty removes the mapping
    display_name: str | None = None


@app.put("/api/doc-types/{slug}")
async def update_doc_type(slug: str, body: DocTypeUpdate, s: AsyncSession = Depends(get_session)):
    if slug in BUILTIN_TYPES:
        raise HTTPException(409, "Built-in types have fixed ERP mappings")
    row = await s.get(DocumentType, slug)
    if row is None:
        raise HTTPException(404, "Unknown document type")
    rt = (body.erp_record_type or "").strip()
    row.erp_record_type = dynamic_schema.normalize_slug(rt) if rt else None
    if body.display_name and body.display_name.strip():
        row.display_name = body.display_name.strip()[:128]
    await s.commit()
    return await doc_type(slug, s)


# ------------------------------------------------------------------ reviewed records


@app.get("/api/records")
async def list_records(doc_type: str | None = None, erp_status: str | None = None, limit: int = 100,
                       s: AsyncSession = Depends(get_session)):
    q = select(Record).order_by(Record.approved_at.desc()).limit(min(limit, 500))
    if doc_type:
        q = q.where(Record.doc_type == doc_type)
    if erp_status:
        q = q.where(Record.erp_status == erp_status)
    return [{"id": r.id, "job_id": r.job_id, "doc_type": r.doc_type, "doc_type_name": r.doc_type_name,
             "data": r.data, "schema": r.schema, "field_confidence": r.field_confidence,
             "human_corrected": r.human_corrected, "errors_overridden": r.errors_overridden,
             "erp_status": r.erp_status, "approved_at": r.approved_at} for r in (await s.scalars(q)).all()]


# ------------------------------------------------------------------ purchase orders


class POIn(BaseModel):
    po_number: str
    vendor_name: str
    currency: str | None = None
    order_date: str | None = None
    total_amount: float | None = None
    line_items: list[LineItem] = []


@app.get("/api/purchase-orders")
async def list_pos(s: AsyncSession = Depends(get_session)):
    rows = (await s.scalars(select(PurchaseOrder).order_by(PurchaseOrder.created_at.desc()))).all()
    return [pipeline.po_to_dict(p) for p in rows]


@app.post("/api/purchase-orders", status_code=201)
async def create_po(body: POIn, s: AsyncSession = Depends(get_session)):
    if await s.scalar(select(PurchaseOrder).where(PurchaseOrder.po_number == body.po_number)):
        raise HTTPException(409, f"PO {body.po_number} already exists")
    po = PurchaseOrder(**body.model_dump(exclude={"line_items"}),
                       line_items=[li.model_dump() for li in body.line_items])
    s.add(po)
    await s.commit()
    return pipeline.po_to_dict(po)


@app.delete("/api/purchase-orders/{po_id}", status_code=204)
async def delete_po(po_id: int, s: AsyncSession = Depends(get_session)):
    await s.execute(delete(PurchaseOrder).where(PurchaseOrder.id == po_id))
    await s.commit()


# ------------------------------------------------------------------ ERP export log


@app.get("/api/erp/exports")
async def list_exports(limit: int = 50, s: AsyncSession = Depends(get_session)):
    rows = (await s.scalars(select(ErpExport).order_by(ErpExport.created_at.desc()).limit(limit))).all()
    return [{"id": e.id, "job_id": e.job_id, "target": e.target, "success": e.success,
             "response_status": e.response_status, "payload": e.payload, "created_at": e.created_at}
            for e in rows]
