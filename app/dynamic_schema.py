"""Runtime schemas for discovered document types.

A schema is a list of FieldSpec dicts (see doc_schemas). This module:
  * sanitize_schema     - makes a model-generated schema safe to use (keys, duplicates, caps, roles)
  * extraction_model    - compiles it to a Pydantic model where every field is {value, confidence};
                          used as the structured-output format so the API enforces the schema
  * values_model        - the same schema without confidences; validates reviewer corrections
  * envelope fallback   - a fixed generic format used if the API rejects a compiled schema
  * split_extraction    - turns model output into (data, field_confidence)
"""
import keyword
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, create_model

MAX_FIELDS = 40
MAX_COLUMNS = 12
SCALAR_ROLES_UNIQUE = {"document_number", "document_date", "due_date", "period_start", "period_end", "party",
                       "counterparty", "currency", "subtotal", "tax", "total"}
_PY_TYPES = {"string": str, "number": float, "integer": int, "boolean": bool, "date": str}
_CFG = ConfigDict(protected_namespaces=(), extra="forbid")


def normalize_slug(text: str | None, fallback: str = "unclassified_document") -> str:
    s = re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")[:60]
    return s if s and s != "unknown" else fallback


def _clean_key(raw: str, fallback: str, taken: set[str]) -> str:
    key = re.sub(r"[^a-z0-9]+", "_", (raw or "").lower()).strip("_")[:50] or fallback
    if key[0].isdigit():
        key = "f_" + key
    if keyword.iskeyword(key) or hasattr(BaseModel, key):  # e.g. 'json', 'copy', 'schema' shadow BaseModel
        key += "_field"
    base, n = key, 2
    while key in taken:
        key, n = f"{base}_{n}", n + 1
    taken.add(key)
    return key


def sanitize_schema(fields: list[dict]) -> list[dict]:
    out, keys, used_roles = [], set(), set()
    for i, f in enumerate(fields[:MAX_FIELDS]):
        ftype = f.get("type") if f.get("type") in (*_PY_TYPES, "table") else "string"
        role = f.get("role") or "none"
        if ftype == "table" or role in used_roles:
            role = "none"
        if role in SCALAR_ROLES_UNIQUE:
            used_roles.add(role)
        spec = {
            "key": _clean_key(f.get("key") or f.get("label"), f"field_{i + 1}", keys),
            "label": (f.get("label") or f.get("key") or f"Field {i + 1}").strip()[:80],
            "type": ftype,
            "description": (f.get("description") or "").strip()[:300],
            "required": bool(f.get("required")),
            "role": role,
            "columns": [],
        }
        if ftype == "table":
            col_keys, col_roles = set(), set()
            for j, c in enumerate((f.get("columns") or [])[:MAX_COLUMNS]):
                crole = c.get("role") or "none"
                if crole in col_roles:
                    crole = "none"
                if crole != "none":
                    col_roles.add(crole)
                spec["columns"].append({
                    "key": _clean_key(c.get("key") or c.get("label"), f"column_{j + 1}", col_keys),
                    "label": (c.get("label") or c.get("key") or f"Column {j + 1}").strip()[:80],
                    "type": c.get("type") if c.get("type") in _PY_TYPES else "string",
                    "role": crole,
                })
            if not spec["columns"]:
                continue  # a table with no columns can't hold anything
        out.append(spec)
    return out


def _desc(spec: dict) -> str:
    d = f"{spec['label']}. {spec.get('description') or ''}".strip()
    return d + (" Format YYYY-MM-DD." if spec["type"] == "date" else "")


def _row_model(name: str, columns: list[dict]) -> type[BaseModel]:
    return create_model(name, __config__=_CFG, **{
        c["key"]: (_PY_TYPES[c["type"]] | None, Field(description=_desc(c))) for c in columns})


def extraction_model(schema: list[dict]) -> type[BaseModel]:
    """Every field becomes {value, confidence} (tables: {rows, confidence})."""
    conf = (float, Field(description="Your confidence in this value, 0.0 to 1.0. Use 1.0 for a clear printed "
                                     "value and a low number for blurry, handwritten, ambiguous or inferred ones."))
    fields: dict[str, Any] = {}
    for i, spec in enumerate(schema):
        if spec["type"] == "table":
            row = _row_model(f"Row{i}", spec["columns"])
            inner = create_model(f"Table{i}", __config__=_CFG,
                                 rows=(list[row], Field(description="One entry per row, in document order")),
                                 confidence=conf)
        else:
            inner = create_model(f"Field{i}", __config__=_CFG,
                                 value=(_PY_TYPES[spec["type"]] | None,
                                        Field(description="null if not present on the document")),
                                 confidence=conf)
        fields[spec["key"]] = (inner, Field(description=_desc(spec)))
    return create_model("DynamicExtraction", __config__=_CFG, **fields)


def values_model(schema: list[dict]) -> type[BaseModel]:
    """Plain values (no confidences) - validates reviewer corrections."""
    fields: dict[str, Any] = {}
    for i, spec in enumerate(schema):
        if spec["type"] == "table":
            fields[spec["key"]] = (list[_row_model(f"ValRow{i}", spec["columns"])], Field(default_factory=list))
        else:
            fields[spec["key"]] = (_PY_TYPES[spec["type"]] | None, None)
    return create_model("DynamicValues", __config__=ConfigDict(protected_namespaces=(), extra="ignore"), **fields)


def split_extraction(schema: list[dict], raw: dict) -> tuple[dict, dict]:
    data, confidence = {}, {}
    for spec in schema:
        item = raw.get(spec["key"]) or {}
        if spec["type"] == "table":
            data[spec["key"]] = item.get("rows") or []
        else:
            data[spec["key"]] = item.get("value")
        c = item.get("confidence")
        confidence[spec["key"]] = max(0.0, min(1.0, float(c))) if isinstance(c, (int, float)) else None
    return data, confidence


# --------------------------------------------------------------------------- generic fallback format


class EnvelopeCell(BaseModel):
    column: str
    value: str | None


class EnvelopeRow(BaseModel):
    cells: list[EnvelopeCell]


class EnvelopeField(BaseModel):
    key: str
    value: str | None = Field(description="Value as text; null if absent")
    confidence: float


class EnvelopeTable(BaseModel):
    key: str
    rows: list[EnvelopeRow]
    confidence: float


class Envelope(BaseModel):
    fields: list[EnvelopeField]
    tables: list[EnvelopeTable]


def coerce(ftype: str, value):
    """Best-effort conversion of text to the declared type. Unconvertible values are returned unchanged
    so that validation flags them for the reviewer instead of silently dropping them."""
    if value is None or ftype in ("string", "date"):
        return value.strip() if isinstance(value, str) else value
    if isinstance(value, str):
        s = value.strip()
        if s == "":
            return None
        if ftype == "boolean":
            low = s.lower()
            return True if low in {"true", "yes", "y", "1"} else False if low in {"false", "no", "n", "0"} else value
        neg = s.startswith("(") and s.endswith(")")
        num = re.sub(r"[^0-9.\-]", "", s)
        try:
            n = float(num)
        except ValueError:
            return value
        n = -abs(n) if neg else n
        return int(n) if ftype == "integer" and n.is_integer() else (n if ftype == "number" else value)
    return value


def envelope_to_raw(schema: list[dict], env: dict) -> dict:
    """Convert fallback output into the same {key: {value|rows, confidence}} shape as extraction_model."""
    by_key = {s["key"]: s for s in schema}
    raw: dict[str, dict] = {}
    for f in env.get("fields", []):
        spec = by_key.get(f.get("key"))
        if spec and spec["type"] != "table":
            raw[spec["key"]] = {"value": coerce(spec["type"], f.get("value")), "confidence": f.get("confidence")}
    for t in env.get("tables", []):
        spec = by_key.get(t.get("key"))
        if spec and spec["type"] == "table":
            cols = {c["key"]: c for c in spec["columns"]}
            rows = []
            for r in t.get("rows", []):
                row = {k: None for k in cols}
                for cell in r.get("cells", []):
                    if cell.get("column") in cols:
                        row[cell["column"]] = coerce(cols[cell["column"]]["type"], cell.get("value"))
                rows.append(row)
            raw[spec["key"]] = {"rows": rows, "confidence": t.get("confidence")}
    return raw


def describe_for_envelope(schema: list[dict]) -> str:
    lines = []
    for s in schema:
        if s["type"] == "table":
            cols = ", ".join(f"{c['key']} ({c['type']})" for c in s["columns"])
            lines.append(f"- table `{s['key']}`: {s['label']} - columns: {cols}")
        else:
            lines.append(f"- `{s['key']}` ({s['type']}): {s['label']}. {s.get('description', '')}")
    return "\n".join(lines)
