"""The background pipeline.

    classify ─► built-in type?  ── yes ─► specialised schema ─► extract ─► specialised checks
                    │
                    no ─► discovered before? ── yes ─► reuse stored schema ─┐
                              │                                            ├─► extract with confidences
                              no ─► Claude designs a schema (saved) ───────┘      ─► generic checks

Runs in-process via FastAPI BackgroundTasks. Job state lives in the `jobs` table so the frontend can
poll it, and jobs interrupted by a restart are picked up again on startup (see resume_pending).
"""
import asyncio
import logging
from pathlib import Path

from sqlalchemy import select

from app import config, erp, llm, type_registry, validation
from app.db import SessionLocal
from app.doc_schemas import BUILTIN_TYPES, Classification, builtin_field_specs
from app.dynamic_schema import normalize_slug, split_extraction
from app.models import Job, JobStatus, PurchaseOrder, SchemaSource

log = logging.getLogger("pipeline")
_slots = asyncio.Semaphore(config.MAX_CONCURRENT_JOBS)


async def _set(job_id: str, **fields) -> Job:
    async with SessionLocal() as s:
        job = await s.get(Job, job_id)
        for k, v in fields.items():
            setattr(job, k, v)
        await s.commit()
        return job


async def process_job(job_id: str) -> None:
    async with _slots:
        try:
            await _run(job_id)
        except llm.LLMError as e:
            await _set(job_id, status=JobStatus.FAILED, error=str(e))
        except Exception as e:  # keep the worker alive; surface the error on the job
            log.exception("job %s crashed", job_id)
            await _set(job_id, status=JobStatus.FAILED, error=f"Unexpected error: {e}")


async def _run(job_id: str) -> None:
    async with SessionLocal() as s:
        job = await s.get(Job, job_id)
        catalog = await type_registry.catalog(s)
    block = llm.file_block(Path(job.stored_path), job.media_type)
    usage: list[dict] = []

    # 1. classify - skipped when a reviewer already set the type
    if job.doc_type_forced and job.doc_type:
        known = next((t for t in catalog if t.slug == job.doc_type), None)
        cls = Classification(doc_type=job.doc_type,
                             display_name=known.display_name if known else job.doc_type_name
                             or job.doc_type.replace("_", " ").capitalize(),
                             purpose=(known.purpose if known else job.doc_type_purpose) or "",
                             confidence=1.0, reasoning="Document type set by reviewer.")
    else:
        await _set(job_id, status=JobStatus.CLASSIFYING, error=None)
        cls, u = await llm.classify(block, catalog)
        usage.append({"step": "classify", **u})
        cls.doc_type = normalize_slug(cls.doc_type)
    confident = job.doc_type_forced or cls.confidence >= config.CLASSIFY_MIN_CONFIDENCE
    await _set(job_id, classification={**cls.model_dump(), "forced": job.doc_type_forced}, doc_type=cls.doc_type,
               doc_type_name=cls.display_name, doc_type_purpose=cls.purpose, llm_usage={"calls": usage})

    # 2a. built-in type: specialised schema
    if cls.doc_type in BUILTIN_TYPES:
        spec = BUILTIN_TYPES[cls.doc_type]
        await _set(job_id, status=JobStatus.EXTRACTING, doc_type_name=spec.label, schema_source=SchemaSource.BUILT_IN,
                   extraction_schema=builtin_field_specs(cls.doc_type))
        extracted, u = await llm.extract(block, spec)
        usage.append({"step": "extract", **u})
        data = extracted.model_dump()
        await _set(job_id, extracted_data=data, data=data, field_confidence=None, corrected=False,
                   llm_usage={"calls": usage}, status=JobStatus.VALIDATING)

    # 2b. any other type: reuse or design a schema, then extract with per-field confidence
    else:
        async with SessionLocal() as s:
            known = await type_registry.get(s, cls.doc_type)
        if known and not job.regenerate_schema:
            schema, source = known.schema, SchemaSource.REUSED
            name, purpose = known.display_name, known.purpose
        else:
            await _set(job_id, status=JobStatus.DESIGNING_SCHEMA)
            schema, u = await llm.generate_schema(block, cls)
            usage.append({"step": "design_schema", **u})
            source, name, purpose = SchemaSource.GENERATED, cls.display_name, cls.purpose
            if not schema:
                raise llm.LLMError("Could not design an extraction schema for this document.")
            if confident:
                async with SessionLocal() as s:
                    await type_registry.save_discovered(s, cls.doc_type, name, purpose, schema, job_id)
                    await s.commit()
        await _set(job_id, status=JobStatus.EXTRACTING, extraction_schema=schema, schema_source=source,
                   doc_type_name=name, doc_type_purpose=purpose, regenerate_schema=False)
        raw, u = await llm.extract_dynamic(block, schema, name)
        usage.append({"step": "extract", **u})
        data, confidence = split_extraction(schema, raw)
        await _set(job_id, extracted_data=raw, data=data, field_confidence=confidence, corrected=False,
                   llm_usage={"calls": usage}, status=JobStatus.VALIDATING)

    # 3. validate
    job = await revalidate(job_id)
    if job.status == JobStatus.VALIDATED and config.AUTO_EXPORT_VALIDATED:
        async with SessionLocal() as s:
            routed = await type_registry.erp_record_type(s, job.doc_type)
        if routed:
            await erp.approve(job_id)


async def revalidate(job_id: str) -> Job:
    """Run all deterministic checks on job.data. Also used after a reviewer edits fields."""
    async with SessionLocal() as s:
        job = await s.get(Job, job_id)
        data = job.data or {}
        schema = job.extraction_schema or []
        builtin = BUILTIN_TYPES.get(job.doc_type)

        if builtin:
            issues = validation.check_fields(data, builtin)
        else:
            issues = validation.check_generic(schema, data, job.field_confidence)

        keys = validation.role_keys(schema, data)
        job.vendor_key, job.doc_number = keys["party"], keys["document_number"]
        job.doc_date, job.total_amount = keys["document_date"], keys["total"]

        others = select(Job).where(
            Job.id != job.id, Job.status.not_in(list(JobStatus.EXCLUDED_FROM_HISTORY | JobStatus.IN_PROGRESS))
        )
        same_file = (await s.scalars(others.where(Job.file_sha256 == job.file_sha256))).all()
        same_number, same_amount_date, past = [], [], []
        if job.vendor_key:
            party = others.where(Job.doc_type == job.doc_type, Job.vendor_key == job.vendor_key)
            if job.doc_number:
                same_number = (await s.scalars(party.where(Job.doc_number == job.doc_number))).all()
            if job.doc_date and job.total_amount is not None:
                same_amount_date = (await s.scalars(
                    party.where(Job.doc_date == job.doc_date, Job.total_amount == job.total_amount))).all()
            past = [t for t in (await s.scalars(party.with_only_columns(Job.total_amount))).all() if t is not None]
        brief = lambda jobs: [{"id": j.id, "filename": j.filename} for j in jobs]  # noqa: E731
        label = job.doc_type_name or "Document"
        issues += validation.check_duplicates(label, keys, brief(same_file), brief(same_number), brief(same_amount_date))
        if job.vendor_key and keys["total_field"]:
            issues += validation.check_anomalies(keys, past)

        job.po_match = None
        if job.doc_type == "invoice":
            po = None
            if data.get("po_number"):
                row = await s.scalar(select(PurchaseOrder).where(PurchaseOrder.po_number == data["po_number"]))
                po = po_to_dict(row) if row else None
            job.po_match, po_issues = validation.match_purchase_order(data, po)
            issues += po_issues

        cls = job.classification or {}
        if not cls.get("forced") and cls.get("confidence") is not None \
                and cls["confidence"] < config.CLASSIFY_MIN_CONFIDENCE:
            issues.insert(0, validation.issue(
                "warning", "classification_uncertain",
                f"Document type '{job.doc_type_name}' identified with only {cls['confidence']:.0%} confidence. "
                "If it is wrong, reprocess it as the correct type.", None))

        job.issues = issues
        needs_review = any(i["severity"] in ("error", "warning") for i in issues)
        job.status = JobStatus.NEEDS_REVIEW if needs_review else JobStatus.VALIDATED
        job.error = None
        await s.commit()
        return job


def po_to_dict(po: PurchaseOrder) -> dict:
    return {
        "id": po.id, "po_number": po.po_number, "vendor_name": po.vendor_name, "currency": po.currency,
        "order_date": po.order_date, "total_amount": po.total_amount, "line_items": po.line_items or [],
        "source": po.source, "source_job_id": po.source_job_id,
    }


async def resume_pending() -> None:
    """Re-run jobs that were mid-flight when the server stopped (the file is still on disk)."""
    async with SessionLocal() as s:
        ids = (await s.scalars(select(Job.id).where(Job.status.in_(list(JobStatus.IN_PROGRESS))))).all()
    for job_id in ids:
        log.info("resuming interrupted job %s", job_id)
        asyncio.create_task(process_job(job_id))
