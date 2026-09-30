"""One place to ask "what is document type X?", whether it is built in or was discovered at runtime."""
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.doc_schemas import BUILTIN_TYPES, builtin_field_specs
from app.models import DocumentType, Job, Record, RecordErpStatus

# Built-in ERP mappings. Discovered types get one only when an operator configures it.
BUILTIN_ERP_RECORD_TYPES = {"invoice": "ap_bill", "receipt": "expense", "purchase_order": "purchase_order"}


@dataclass
class TypeInfo:
    slug: str
    display_name: str
    purpose: str | None
    kind: str  # "built_in" | "discovered"
    schema: list[dict]
    erp_record_type: str | None


def builtin_info(slug: str) -> TypeInfo:
    spec = BUILTIN_TYPES[slug]
    return TypeInfo(slug, spec.label, spec.description, "built_in", builtin_field_specs(slug),
                    BUILTIN_ERP_RECORD_TYPES.get(slug))


def _discovered_info(row: DocumentType) -> TypeInfo:
    return TypeInfo(row.slug, row.display_name, row.purpose, "discovered", row.schema, row.erp_record_type)


async def get(s: AsyncSession, slug: str | None) -> TypeInfo | None:
    if not slug:
        return None
    if slug in BUILTIN_TYPES:
        return builtin_info(slug)
    row = await s.get(DocumentType, slug)
    return _discovered_info(row) if row else None


async def catalog(s: AsyncSession, limit: int = 60) -> list[TypeInfo]:
    """Built-ins first, then the most recently used discovered types (shown to the classifier)."""
    rows = (await s.scalars(select(DocumentType).order_by(DocumentType.updated_at.desc()).limit(limit))).all()
    return [builtin_info(k) for k in BUILTIN_TYPES] + [_discovered_info(r) for r in rows]


async def save_discovered(s: AsyncSession, slug: str, display_name: str, purpose: str | None,
                          schema: list[dict], job_id: str) -> DocumentType:
    row = await s.get(DocumentType, slug)
    if row is None:
        row = DocumentType(slug=slug, display_name=display_name, purpose=purpose, schema=schema,
                           discovered_from_job_id=job_id)
        s.add(row)
    else:  # schema regenerated on request; earlier jobs keep their own snapshot
        row.display_name, row.purpose, row.schema = display_name, purpose, schema
        row.schema_version += 1
        row.discovered_from_job_id = job_id
    return row


async def erp_record_type(s: AsyncSession, slug: str | None) -> str | None:
    info = await get(s, slug)
    return info.erp_record_type if info else None


async def summary(s: AsyncSession) -> list[dict]:
    """All types with usage counts, for the Document types screen."""
    job_counts = dict((await s.execute(select(Job.doc_type, func.count()).group_by(Job.doc_type))).all())
    held = dict((await s.execute(
        select(Record.doc_type, func.count()).where(Record.erp_status == RecordErpStatus.AWAITING_CONFIGURATION)
        .group_by(Record.doc_type))).all())
    out = []
    for t in await catalog(s, limit=500):
        out.append({
            "id": t.slug, "label": t.display_name, "purpose": t.purpose, "kind": t.kind,
            "schema": t.schema, "field_count": len(t.schema), "erp_record_type": t.erp_record_type,
            "job_count": job_counts.get(t.slug, 0), "records_awaiting_configuration": held.get(t.slug, 0),
        })
    return out
