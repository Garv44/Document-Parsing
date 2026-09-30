"""Document-type schemas.

Two kinds of document type exist:
  * built-in  - hand-written Pydantic schemas with specialised validation and ERP mappings (below).
  * discovered - any other business document. Claude designs a schema (a list of FieldSpec) the first
                 time the type is seen; it is stored in the `document_types` table and reused.

Every job stores its schema as a list of FieldSpec dicts, so the review UI and the generic,
role-based checks work the same way for both kinds. Built-in schemas are converted to that form
by builtin_field_specs().
"""
import types
import typing
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field

# --------------------------------------------------------------------------- built-in schemas


class LineItem(BaseModel):
    description: str = Field(description="Item or service description as written")
    sku: str | None = Field(None, description="Item code / SKU / part number if present")
    quantity: float | None = None
    unit_price: float | None = None
    amount: float | None = Field(None, description="Line total as printed")


class _Base(BaseModel):
    currency: str | None = Field(None, description="ISO 4217 code, e.g. USD, EUR, INR")
    subtotal: float | None = None
    tax_amount: float | None = None
    total_amount: float | None = Field(None, description="Grand total / amount due")
    line_items: list[LineItem] = Field(default_factory=list)
    low_confidence_fields: list[str] = Field(
        default_factory=list,
        description="Field paths you are unsure about (blurry, ambiguous, inferred), e.g. 'due_date' or 'line_items[2].amount'",
    )


class InvoiceData(_Base):
    invoice_number: str | None = None
    invoice_date: str | None = Field(None, description="YYYY-MM-DD")
    due_date: str | None = Field(None, description="YYYY-MM-DD")
    vendor_name: str | None = None
    vendor_address: str | None = None
    vendor_tax_id: str | None = Field(None, description="VAT / GSTIN / EIN etc.")
    bill_to_name: str | None = None
    po_number: str | None = Field(None, description="Referenced purchase order number, if any")
    payment_terms: str | None = None


class ReceiptData(_Base):
    merchant_name: str | None = None
    receipt_number: str | None = None
    transaction_date: str | None = Field(None, description="YYYY-MM-DD")
    payment_method: str | None = None
    tip_amount: float | None = None


class PurchaseOrderData(_Base):
    po_number: str | None = None
    order_date: str | None = Field(None, description="YYYY-MM-DD")
    delivery_date: str | None = Field(None, description="YYYY-MM-DD")
    buyer_name: str | None = None
    vendor_name: str | None = None


# Roles shared by every built-in type (see FieldRole below).
_COMMON_ROLES = {"currency": "currency", "subtotal": "subtotal", "tax_amount": "tax", "total_amount": "total"}
_LINE_ROLES = {"description": "description", "quantity": "quantity", "unit_price": "unit_price", "amount": "line_amount"}


@dataclass(frozen=True)
class DocTypeSpec:
    label: str
    description: str  # shown to the classifier
    model: type[BaseModel]
    number_field: str  # the document's own identifier
    date_field: str
    party_field: str  # vendor / merchant used for duplicate + anomaly checks
    required_fields: list[str] = field(default_factory=list)
    extra_total_fields: list[str] = field(default_factory=list)  # added to subtotal+tax to reach total
    extra_roles: dict[str, str] = field(default_factory=dict)

    @property
    def roles(self) -> dict[str, str]:
        return {**_COMMON_ROLES, self.number_field: "document_number", self.date_field: "document_date",
                self.party_field: "party", **self.extra_roles}


BUILTIN_TYPES: dict[str, DocTypeSpec] = {
    "invoice": DocTypeSpec(
        label="Invoice",
        description="a bill from a supplier requesting payment",
        model=InvoiceData,
        number_field="invoice_number",
        date_field="invoice_date",
        party_field="vendor_name",
        required_fields=["invoice_number", "invoice_date", "vendor_name", "total_amount", "currency"],
        extra_roles={"due_date": "due_date", "bill_to_name": "counterparty"},
    ),
    "receipt": DocTypeSpec(
        label="Receipt",
        description="proof of a payment already made, e.g. store, restaurant or card receipt",
        model=ReceiptData,
        number_field="receipt_number",
        date_field="transaction_date",
        party_field="merchant_name",
        required_fields=["merchant_name", "transaction_date", "total_amount"],
        extra_total_fields=["tip_amount"],
    ),
    "purchase_order": DocTypeSpec(
        label="Purchase order",
        description="a buyer's order to a supplier",
        model=PurchaseOrderData,
        number_field="po_number",
        date_field="order_date",
        party_field="vendor_name",
        required_fields=["po_number", "vendor_name", "total_amount"],
        extra_roles={"buyer_name": "counterparty"},
    ),
}

# --------------------------------------------------------------------------- field-spec schema language

FieldType = Literal["string", "number", "integer", "date", "boolean", "table"]
ColumnType = Literal["string", "number", "integer", "date", "boolean"]
FieldRole = Literal[
    "none", "document_number", "document_date", "due_date", "period_start", "period_end",
    "party", "counterparty", "currency", "subtotal", "tax", "total",
]
ColumnRole = Literal["none", "description", "quantity", "unit_price", "line_amount"]


class ColumnSpec(BaseModel):
    key: str = Field(description="snake_case column key")
    label: str
    type: ColumnType
    role: ColumnRole = Field(description="Semantic role used for row arithmetic checks; 'none' if not applicable")


class FieldSpec(BaseModel):
    key: str = Field(description="snake_case field key, unique within the schema")
    label: str = Field(description="Human-readable label")
    type: FieldType = Field(description="'table' for repeating rows such as line items, schedules, entries")
    description: str = Field(description="What this field holds and how to read it from the document")
    required: bool = Field(description="True only if every document of this type must contain it")
    role: FieldRole = Field(description="Semantic role used by generic validation and duplicate detection; 'none' if not applicable")
    columns: list[ColumnSpec] = Field(description="Columns for type 'table'; empty list for other types")


class GeneratedSchema(BaseModel):
    fields: list[FieldSpec]


class Classification(BaseModel):
    doc_type: str = Field(description="Type id: a built-in or previously discovered id if it fits, otherwise a new snake_case id")
    display_name: str = Field(description="Human-readable type name, e.g. 'Bill of lading'")
    purpose: str = Field(description="One sentence: what this kind of document is used for")
    confidence: float = Field(description="0.0 to 1.0 confidence in the doc_type")
    reasoning: str = Field(description="One short sentence")


def _field_type(annotation) -> str:
    args = [a for a in typing.get_args(annotation) if a is not type(None)]
    base = args[0] if isinstance(annotation, types.UnionType) or typing.get_origin(annotation) is typing.Union else annotation
    return {str: "string", float: "number", int: "integer", bool: "boolean"}.get(base, "string")


def builtin_field_specs(slug: str) -> list[dict]:
    """Express a built-in Pydantic schema in FieldSpec form (for the UI and role-based checks)."""
    spec = BUILTIN_TYPES[slug]
    roles = spec.roles
    out = []
    for name, f in spec.model.model_fields.items():
        if name == "low_confidence_fields":
            continue
        if name == "line_items":
            columns = [{"key": k, "label": k.replace("_", " ").capitalize(), "type": _field_type(cf.annotation),
                        "role": _LINE_ROLES.get(k, "none")} for k, cf in LineItem.model_fields.items()]
            out.append({"key": name, "label": "Line items", "type": "table", "description": "Line items",
                        "required": False, "role": "none", "columns": columns})
            continue
        ftype = "date" if name.endswith("_date") else _field_type(f.annotation)
        out.append({"key": name, "label": name.replace("_", " ").capitalize(), "type": ftype,
                    "description": f.description or "", "required": name in spec.required_fields,
                    "role": roles.get(name, "none"), "columns": []})
    return out
