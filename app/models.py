"""Database tables: jobs, discovered document types, reviewed records, purchase orders, ERP export log."""
import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

JSONType = JSON().with_variant(JSONB(), "postgresql")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class JobStatus:
    QUEUED = "queued"
    CLASSIFYING = "classifying"
    DESIGNING_SCHEMA = "designing_schema"
    EXTRACTING = "extracting"
    VALIDATING = "validating"
    NEEDS_REVIEW = "needs_review"
    VALIDATED = "validated"
    APPROVED = "approved"  # reviewed record stored; not (yet) exported to an ERP
    EXPORTED = "exported"
    REJECTED = "rejected"
    FAILED = "failed"

    IN_PROGRESS = {QUEUED, CLASSIFYING, DESIGNING_SCHEMA, EXTRACTING, VALIDATING}
    REVIEWABLE = {NEEDS_REVIEW, VALIDATED}
    FINAL = {APPROVED, EXPORTED}
    # Jobs in these states are ignored by duplicate / anomaly checks.
    EXCLUDED_FROM_HISTORY = {REJECTED, FAILED}


class SchemaSource:
    BUILT_IN = "built_in"  # hand-written schema in doc_schemas.BUILTIN_TYPES
    GENERATED = "generated"  # designed by Claude for this job
    REUSED = "reused"  # taken from a previously discovered type


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    filename: Mapped[str] = mapped_column(String(255))
    stored_path: Mapped[str] = mapped_column(String(512))
    media_type: Mapped[str] = mapped_column(String(64))
    file_size: Mapped[int | None] = mapped_column(Integer)
    file_sha256: Mapped[str] = mapped_column(String(64), index=True)

    status: Mapped[str] = mapped_column(String(32), default=JobStatus.QUEUED, index=True)
    error: Mapped[str | None] = mapped_column(Text)

    # Document type as determined for this job (built-in id or discovered slug).
    doc_type: Mapped[str | None] = mapped_column(String(64), index=True)
    doc_type_name: Mapped[str | None] = mapped_column(String(128))
    doc_type_purpose: Mapped[str | None] = mapped_column(Text)
    doc_type_forced: Mapped[bool] = mapped_column(Boolean, default=False)
    regenerate_schema: Mapped[bool] = mapped_column(Boolean, default=False)
    classification: Mapped[dict | None] = mapped_column(JSONType)

    # The schema used for this job, preserved even if the type's registry entry changes later.
    extraction_schema: Mapped[list | None] = mapped_column(JSONType)
    schema_source: Mapped[str | None] = mapped_column(String(16))

    extracted_data: Mapped[dict | None] = mapped_column(JSONType)  # raw model output, kept for audit
    data: Mapped[dict | None] = mapped_column(JSONType)  # current values, including reviewer corrections
    field_confidence: Mapped[dict | None] = mapped_column(JSONType)  # key -> 0..1 (discovered types)
    corrected: Mapped[bool] = mapped_column(Boolean, default=False)
    issues: Mapped[list | None] = mapped_column(JSONType)
    po_match: Mapped[dict | None] = mapped_column(JSONType)
    llm_usage: Mapped[dict | None] = mapped_column(JSONType)

    # Denormalised keys (from field roles) used for duplicate and anomaly lookups.
    vendor_key: Mapped[str | None] = mapped_column(String(255), index=True)
    doc_number: Mapped[str | None] = mapped_column(String(128), index=True)
    doc_date: Mapped[str | None] = mapped_column(String(10))
    total_amount: Mapped[float | None] = mapped_column(Float)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DocumentType(Base):
    """A document type discovered at runtime. Built-in types live in code, not here."""
    __tablename__ = "document_types"

    slug: Mapped[str] = mapped_column(String(64), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(128))
    purpose: Mapped[str | None] = mapped_column(Text)
    schema: Mapped[list] = mapped_column(JSONType)
    schema_version: Mapped[int] = mapped_column(Integer, default=1)
    # None = no ERP mapping; approved records are held until one is configured.
    erp_record_type: Mapped[str | None] = mapped_column(String(64))
    discovered_from_job_id: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class RecordErpStatus:
    EXPORTED = "exported"
    AWAITING_CONFIGURATION = "awaiting_configuration"
    EXPORT_FAILED = "export_failed"


class Record(Base):
    """Immutable snapshot of an approved document: the reviewed structured data, whatever its type."""
    __tablename__ = "records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), unique=True, index=True)
    doc_type: Mapped[str] = mapped_column(String(64), index=True)
    doc_type_name: Mapped[str | None] = mapped_column(String(128))
    schema: Mapped[list] = mapped_column(JSONType)
    data: Mapped[dict] = mapped_column(JSONType)
    field_confidence: Mapped[dict | None] = mapped_column(JSONType)
    human_corrected: Mapped[bool] = mapped_column(Boolean, default=False)
    errors_overridden: Mapped[bool] = mapped_column(Boolean, default=False)
    erp_status: Mapped[str] = mapped_column(String(32), index=True)
    erp_export_id: Mapped[int | None] = mapped_column(Integer)
    approved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PurchaseOrder(Base):
    __tablename__ = "purchase_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    po_number: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    vendor_name: Mapped[str] = mapped_column(String(255))
    currency: Mapped[str | None] = mapped_column(String(8))
    order_date: Mapped[str | None] = mapped_column(String(10))
    total_amount: Mapped[float | None] = mapped_column(Float)
    line_items: Mapped[list] = mapped_column(JSONType, default=list)
    source: Mapped[str] = mapped_column(String(32), default="manual")  # manual | document
    source_job_id: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ErpExport(Base):
    __tablename__ = "erp_exports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    target: Mapped[str] = mapped_column(String(512))
    payload: Mapped[dict] = mapped_column(JSONType)
    success: Mapped[bool] = mapped_column(Boolean)
    response_status: Mapped[int | None] = mapped_column(Integer)
    response_body: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
