from datetime import date

from app import validation as v
from app.doc_schemas import BUILTIN_TYPES as DOC_TYPES
from app.doc_schemas import builtin_field_specs

INV = DOC_TYPES["invoice"]


def good_invoice(**over):
    d = {
        "invoice_number": "INV-1001", "invoice_date": "2026-09-01", "due_date": "2026-10-01",
        "vendor_name": "Acme Supplies, Inc.", "currency": "USD", "po_number": "PO-7",
        "subtotal": 110.0, "tax_amount": 11.0, "total_amount": 121.0,
        "line_items": [
            {"description": "Steel bolts M8", "sku": "BLT-M8", "quantity": 500, "unit_price": 0.12, "amount": 60.0},
            {"description": "Washers", "sku": None, "quantity": 100, "unit_price": 0.5, "amount": 50.0},
        ],
        "low_confidence_fields": [],
    }
    d.update(over)
    return d


def codes(issues):
    return {i["code"] for i in issues}


def test_clean_invoice_has_no_issues():
    assert v.check_fields(good_invoice(), INV, today=date(2026, 9, 29)) == []


def test_missing_required_and_bad_dates():
    issues = v.check_fields(good_invoice(invoice_number=None, due_date="2026-08-01", invoice_date="2026-09-01"),
                            INV, today=date(2026, 9, 29))
    assert {"missing_field", "due_before_issue"} <= codes(issues)
    assert "bad_date" in codes(v.check_fields(good_invoice(invoice_date="09/01/2026"), INV))
    assert "future_date" in codes(v.check_fields(good_invoice(invoice_date="2027-01-01", due_date=None), INV,
                                                 today=date(2026, 9, 29)))


def test_arithmetic_checks():
    bad_line = good_invoice()
    bad_line["line_items"][0]["amount"] = 70.0
    assert {"line_math", "lines_vs_subtotal"} <= codes(v.check_arithmetic(bad_line, INV))
    assert "total_mismatch" in codes(v.check_arithmetic(good_invoice(total_amount=130.0), INV))
    # within rounding tolerance
    assert v.check_arithmetic(good_invoice(total_amount=121.01), INV) == []


def test_receipt_tip_counts_toward_total():
    r = {"merchant_name": "Cafe", "transaction_date": "2026-09-01", "subtotal": 10.0, "tax_amount": 1.0,
         "tip_amount": 2.0, "total_amount": 13.0, "line_items": []}
    assert v.check_arithmetic(r, DOC_TYPES["receipt"]) == []


def test_low_confidence_fields_become_warnings():
    issues = v.check_fields(good_invoice(low_confidence_fields=["due_date"]), INV, today=date(2026, 9, 29))
    assert [i["field"] for i in issues if i["code"] == "low_confidence"] == ["due_date"]


def test_normalize_party():
    assert v.normalize_party("ACME Supplies, Inc.") == v.normalize_party("Acme Supplies Inc") == "acme supplies"
    assert v.normalize_party("  ") is None


def inv_keys(**over):
    return v.role_keys(builtin_field_specs("invoice"), good_invoice(**over))


def test_role_keys_for_builtin_schema():
    k = inv_keys()
    assert (k["document_number"], k["document_date"], k["party"], k["total"]) == \
        ("INV-1001", "2026-09-01", "acme supplies", 121.0)
    assert (k["document_number_field"], k["party_field"], k["total_field"]) == \
        ("invoice_number", "vendor_name", "total_amount")


def test_duplicates():
    other = [{"id": "x", "filename": "a.pdf"}]
    assert codes(v.check_duplicates("Invoice", inv_keys(), other, [], [])) == {"duplicate_file"}
    assert codes(v.check_duplicates("Invoice", inv_keys(), [], other, other)) == {"duplicate_document"}
    assert codes(v.check_duplicates("Invoice", inv_keys(), [], [], other)) == {"possible_duplicate"}


def test_anomalies():
    assert codes(v.check_anomalies(inv_keys(), [])) == {"new_party"}
    assert v.check_anomalies(inv_keys(), [100, 120, 130]) == []
    assert codes(v.check_anomalies(inv_keys(total_amount=1000.0), [100, 120, 130])) == {"unusual_amount"}


PO = {"po_number": "PO-7", "vendor_name": "Acme Supplies LLC", "currency": "USD", "total_amount": 121.0,
      "line_items": [{"description": "Steel bolts M8", "sku": "BLT-M8", "quantity": 500, "unit_price": 0.12},
                     {"description": "Flat washers", "sku": None, "quantity": 100, "unit_price": 0.5}]}


def test_po_match_clean():
    match, issues = v.match_purchase_order(good_invoice(), PO)
    assert issues == [] and match["status"] == "matched"
    assert [l["status"] for l in match["lines"]] == ["matched", "matched"]


def test_po_match_problems():
    inv = good_invoice(vendor_name="Globex Corp", total_amount=500.0)
    inv["line_items"][0]["quantity"] = 600
    inv["line_items"][0]["unit_price"] = 0.2
    match, issues = v.match_purchase_order(inv, PO)
    assert {"po_vendor_mismatch", "po_total_exceeded", "po_qty_exceeded", "po_price_variance"} <= codes(issues)
    assert match["status"] == "mismatch"


def test_po_missing_or_unknown():
    assert v.match_purchase_order(good_invoice(po_number=None), None)[0]["status"] == "no_po"
    assert v.match_purchase_order(good_invoice(), None)[0]["status"] == "not_found"
