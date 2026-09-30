"""Discovered (runtime) document types: schema handling and generic validation. No DB, no LLM."""
from datetime import date

import pytest
from pydantic import ValidationError

from app import dynamic_schema as ds
from app import validation as v
from app.doc_schemas import BUILTIN_TYPES, GeneratedSchema, builtin_field_specs

# A schema as Claude might design it for a utility bill - including some mess to clean up.
RAW_UTILITY_SCHEMA = [
    {"key": "Account Number", "label": "Account number", "type": "string", "description": "", "required": True,
     "role": "document_number", "columns": []},
    {"key": "provider", "label": "Provider", "type": "string", "required": True, "role": "party", "columns": []},
    {"key": "bill_date", "label": "Bill date", "type": "date", "required": True, "role": "document_date",
     "columns": []},
    {"key": "due_date", "label": "Due date", "type": "date", "required": False, "role": "due_date", "columns": []},
    {"key": "period_start", "label": "Period start", "type": "date", "role": "period_start", "columns": []},
    {"key": "period_end", "label": "Period end", "type": "date", "role": "period_end", "columns": []},
    {"key": "currency", "label": "Currency", "type": "string", "role": "currency", "columns": []},
    {"key": "charges", "label": "Charges", "type": "table", "required": True, "role": "none", "columns": [
        {"key": "description", "label": "Description", "type": "string", "role": "description"},
        {"key": "usage_kwh", "label": "Usage (kWh)", "type": "number", "role": "quantity"},
        {"key": "rate", "label": "Rate", "type": "number", "role": "unit_price"},
        {"key": "amount", "label": "Amount", "type": "number", "role": "line_amount"},
    ]},
    {"key": "subtotal", "label": "Subtotal", "type": "number", "role": "subtotal", "columns": []},
    {"key": "tax", "label": "Tax", "type": "number", "role": "tax", "columns": []},
    {"key": "total_due", "label": "Total due", "type": "number", "required": True, "role": "total", "columns": []},
    {"key": "total_due", "label": "Duplicate key", "type": "number", "role": "total", "columns": []},
    {"key": "schema", "label": "Name clashing with BaseModel", "type": "string", "role": "none", "columns": []},
    {"key": "2nd meter", "label": "Starts with digit", "type": "weird", "role": "none", "columns": []},
    {"key": "empty_table", "label": "Table without columns", "type": "table", "role": "none", "columns": []},
]
SCHEMA = ds.sanitize_schema(RAW_UTILITY_SCHEMA)


def good_bill(**over):
    d = {
        "account_number": "ACC-778", "provider": "City Power Ltd", "bill_date": "2026-09-01",
        "due_date": "2026-09-21", "period_start": "2026-08-01", "period_end": "2026-08-31", "currency": "EUR",
        "charges": [
            {"description": "Energy", "usage_kwh": 300, "rate": 0.25, "amount": 75.0},
            {"description": "Standing charge", "usage_kwh": None, "rate": None, "amount": 10.0},
        ],
        "subtotal": 85.0, "tax": 17.0, "total_due": 102.0, "schema": None, "f_2nd_meter": None, "total_due_2": None,
    }
    d.update(over)
    return d


def codes(issues):
    return {i["code"] for i in issues}


# --------------------------------------------------------------------------- schema sanitising


def test_sanitize_cleans_keys_types_and_roles():
    keys = [s["key"] for s in SCHEMA]
    assert keys[0] == "account_number"
    assert "total_due_2" in keys  # duplicate key renamed
    assert "schema_field" in keys and "schema" not in keys  # would shadow BaseModel.schema
    assert "f_2nd_meter" in keys  # can't start with a digit
    assert "empty_table" not in keys  # tables need columns
    by_key = {s["key"]: s for s in SCHEMA}
    assert by_key["f_2nd_meter"]["type"] == "string"  # unknown type -> string
    assert by_key["total_due"]["role"] == "total" and by_key["total_due_2"]["role"] == "none"  # role used once


def test_sanitize_caps_field_count():
    many = [{"key": f"f{i}", "label": f"F{i}", "type": "string", "role": "none", "columns": []} for i in range(100)]
    assert len(ds.sanitize_schema(many)) == ds.MAX_FIELDS


def test_normalize_slug():
    assert ds.normalize_slug("Bill of Lading") == "bill_of_lading"
    assert ds.normalize_slug("unknown") == "unclassified_document"
    assert ds.normalize_slug("") == "unclassified_document"


def test_generated_schema_model_accepts_sanitized_output():
    GeneratedSchema.model_validate({"fields": SCHEMA})


# --------------------------------------------------------------------------- compiled models


def test_extraction_model_compiles_to_strict_json_schema():
    model = ds.extraction_model(SCHEMA)
    js = model.model_json_schema()
    assert set(js["required"]) == {s["key"] for s in SCHEMA}  # every field must be answered
    assert js["additionalProperties"] is False
    raw = {s["key"]: ({"rows": [], "confidence": 0.9} if s["type"] == "table" else {"value": None, "confidence": 0.9})
           for s in SCHEMA}
    raw["total_due"] = {"value": 102.0, "confidence": 0.95}
    raw["charges"] = {"rows": [{"description": "Energy", "usage_kwh": 300, "rate": 0.25, "amount": 75}],
                      "confidence": 0.8}
    parsed = model.model_validate(raw).model_dump()
    data, conf = ds.split_extraction(SCHEMA, parsed)
    assert data["total_due"] == 102.0 and data["charges"][0]["amount"] == 75
    assert conf["total_due"] == 0.95 and conf["charges"] == 0.8


def test_values_model_validates_reviewer_corrections():
    model = ds.values_model(SCHEMA)
    clean = model.model_validate({**good_bill(), "total_due": "102.50", "not_in_schema": 1}).model_dump()
    assert clean["total_due"] == 102.5 and "not_in_schema" not in clean
    with pytest.raises(ValidationError):
        model.model_validate({**good_bill(), "total_due": "about a hundred"})


# --------------------------------------------------------------------------- fallback format


def test_coerce():
    assert ds.coerce("number", "EUR 1,234.50") == 1234.5
    assert ds.coerce("number", "(12.00)") == -12.0
    assert ds.coerce("integer", "42") == 42
    assert ds.coerce("boolean", "Yes") is True
    assert ds.coerce("number", "n/a") == "n/a"  # left for validation to flag
    assert ds.coerce("date", " 2026-09-01 ") == "2026-09-01"


def test_envelope_to_raw():
    env = {"fields": [{"key": "total_due", "value": "102.00", "confidence": 0.9},
                      {"key": "not_a_field", "value": "x", "confidence": 1}],
           "tables": [{"key": "charges", "confidence": 0.7,
                       "rows": [{"cells": [{"column": "description", "value": "Energy"},
                                           {"column": "amount", "value": "75"}]}]}]}
    raw = ds.envelope_to_raw(SCHEMA, env)
    assert raw["total_due"] == {"value": 102.0, "confidence": 0.9}
    assert raw["charges"]["rows"][0] == {"description": "Energy", "usage_kwh": None, "rate": None, "amount": 75.0}
    assert "not_a_field" not in raw
    data, conf = ds.split_extraction(SCHEMA, raw)
    assert data["account_number"] is None and conf["account_number"] is None  # absent -> null


# --------------------------------------------------------------------------- generic validation

TODAY = date(2026, 9, 29)


def test_generic_clean_document_has_no_issues():
    assert v.check_generic(SCHEMA, good_bill(), {"total_due": 0.99}, today=TODAY) == []


def test_generic_missing_and_type_errors():
    issues = v.check_generic(SCHEMA, good_bill(account_number=None, bill_date="01/09/2026", tax="seventeen",
                                               charges=[]), today=TODAY)
    assert {"missing_field", "bad_date", "bad_type"} <= codes(issues)
    assert [i["field"] for i in issues if i["code"] == "missing_field"] == ["account_number", "charges"]


def test_generic_date_logic():
    issues = v.check_generic(SCHEMA, good_bill(due_date="2026-08-15", period_end="2026-07-01",
                                               bill_date="2026-12-01"), today=TODAY)
    assert codes(issues) >= {"date_order", "future_date"}
    assert {i["field"] for i in issues if i["code"] == "date_order"} == {"due_date", "period_end"}


def test_generic_arithmetic():
    bill = good_bill()
    bill["charges"][0]["amount"] = 80.0  # 300 x 0.25 = 75
    issues = v.check_generic(SCHEMA, bill, today=TODAY)
    assert {"line_math", "rows_vs_total"} <= codes(issues)
    assert "total_mismatch" in codes(v.check_generic(SCHEMA, good_bill(total_due=150.0), today=TODAY))


def test_generic_rows_compared_to_total_when_no_subtotal_or_tax():
    bill = good_bill(subtotal=None, tax=None, total_due=90.0)
    assert "rows_vs_total" in codes(v.check_generic(SCHEMA, bill, today=TODAY))
    assert v.check_generic(SCHEMA, good_bill(subtotal=None, tax=None, total_due=85.0), today=TODAY) == []


def test_generic_low_confidence_and_currency():
    issues = v.check_generic(SCHEMA, good_bill(currency="euros"), {"provider": 0.4, "total_due": 0.9}, today=TODAY)
    assert "bad_currency" in codes(issues)
    assert [i["field"] for i in issues if i["code"] == "low_confidence"] == ["provider"]


def test_generic_nothing_extracted():
    empty = {s["key"]: ([] if s["type"] == "table" else None) for s in SCHEMA}
    assert codes(v.check_generic(SCHEMA, empty, today=TODAY)) == {"nothing_extracted"}


def test_role_keys_for_discovered_schema():
    k = v.role_keys(SCHEMA, good_bill())
    assert (k["document_number"], k["party"], k["total"], k["document_date"]) == \
        ("ACC-778", "city power", 102.0, "2026-09-01")


def test_schema_without_roles_still_validates():
    schema = ds.sanitize_schema([{"key": "note", "label": "Note", "type": "string", "role": "none", "columns": []}])
    assert v.check_generic(schema, {"note": "hello"}, today=TODAY) == []
    assert v.role_keys(schema, {"note": "hello"})["party"] is None


# --------------------------------------------------------------------------- built-ins expressed as field specs


@pytest.mark.parametrize("slug", list(BUILTIN_TYPES))
def test_builtin_field_specs(slug):
    specs = builtin_field_specs(slug)
    roles = {s["role"]: s["key"] for s in specs if s["role"] != "none"}
    spec = BUILTIN_TYPES[slug]
    assert roles["document_number"] == spec.number_field and roles["party"] == spec.party_field
    assert roles["total"] == "total_amount"
    table = next(s for s in specs if s["type"] == "table")
    assert {c["role"] for c in table["columns"]} >= {"quantity", "unit_price", "line_amount"}
    assert all(s["key"] != "low_confidence_fields" for s in specs)
    assert {s["key"] for s in specs if s["required"]} == set(spec.required_fields)
