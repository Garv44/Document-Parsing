"""Deterministic checks on extracted data. Pure functions - no DB or LLM - so they are easy to test.

Every check returns Issue dicts: {severity, code, field, message}
  error   -> blocks export until fixed or overridden by a reviewer
  warning -> needs a human look
  info    -> context only
"""
import re
from datetime import date, datetime
from difflib import SequenceMatcher
from statistics import median

from app import config
from app.doc_schemas import DocTypeSpec


def issue(severity: str, code: str, message: str, field: str | None = None) -> dict:
    return {"severity": severity, "code": code, "field": field, "message": message}


def _num(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _close(a: float, b: float, tol: float | None = None) -> bool:
    tol = config.AMOUNT_TOLERANCE if tol is None else tol
    return abs(a - b) <= max(tol, abs(b) * 0.0005)


def _parse_date(v) -> date | None:
    if not isinstance(v, str):
        return None
    try:
        return datetime.strptime(v, "%Y-%m-%d").date()
    except ValueError:
        return None


_SUFFIXES = r"\b(inc|incorporated|llc|ltd|limited|pvt|private|co|corp|corporation|company|gmbh|plc|sa|ag|bv)\b"


def normalize_party(name: str | None) -> str | None:
    """'ACME Supplies, Inc.' and 'Acme Supplies Inc' -> 'acme supplies'."""
    if not name:
        return None
    s = re.sub(r"[^a-z0-9 ]+", " ", name.lower())
    s = re.sub(_SUFFIXES, " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s or None


def similarity(a: str | None, b: str | None) -> float:
    na, nb = normalize_party(a), normalize_party(b)
    if not na or not nb:
        return 0.0
    return SequenceMatcher(None, na, nb).ratio()


# --------------------------------------------------------------------------- field + arithmetic checks


def check_fields(data: dict, spec: DocTypeSpec, today: date | None = None) -> list[dict]:
    today = today or date.today()
    issues: list[dict] = []

    for f in spec.required_fields:
        if data.get(f) in (None, "", []):
            issues.append(issue("error", "missing_field", f"Required field '{f}' is missing.", f))

    for f, v in data.items():
        if f.endswith("_date") and v not in (None, ""):
            d = _parse_date(v)
            if d is None:
                issues.append(issue("error", "bad_date", f"'{v}' is not a valid YYYY-MM-DD date.", f))
            elif d > today and f not in ("due_date", "delivery_date"):
                issues.append(issue("warning", "future_date", f"Date {v} is in the future.", f))

    doc_date, due = _parse_date(data.get(spec.date_field)), _parse_date(data.get("due_date"))
    if doc_date and due and due < doc_date:
        issues.append(issue("error", "due_before_issue", "Due date is before the document date.", "due_date"))

    cur = data.get("currency")
    if cur and not re.fullmatch(r"[A-Z]{3}", str(cur)):
        issues.append(issue("warning", "bad_currency", f"Currency '{cur}' is not an ISO 4217 code.", "currency"))

    issues += check_arithmetic(data, spec)

    for f in data.get("low_confidence_fields") or []:
        issues.append(issue("warning", "low_confidence", "The model was unsure about this value - please verify.", f))
    return issues


def check_arithmetic(data: dict, spec: DocTypeSpec) -> list[dict]:
    issues: list[dict] = []
    items = data.get("line_items") or []

    for i, li in enumerate(items):
        q, p, a = _num(li.get("quantity")), _num(li.get("unit_price")), _num(li.get("amount"))
        if q is not None and p is not None and a is not None and not _close(q * p, a):
            issues.append(issue(
                "warning", "line_math",
                f"Line {i + 1}: {q:g} x {p:,.2f} = {q * p:,.2f}, but line amount is {a:,.2f}.",
                f"line_items[{i}].amount",
            ))

    subtotal, tax, total = _num(data.get("subtotal")), _num(data.get("tax_amount")), _num(data.get("total_amount"))
    amounts = [_num(li.get("amount")) for li in items]
    if items and subtotal is not None and all(a is not None for a in amounts):
        s = sum(amounts)
        if not _close(s, subtotal):
            issues.append(issue(
                "warning", "lines_vs_subtotal",
                f"Line items add up to {s:,.2f} but subtotal is {subtotal:,.2f}.", "subtotal",
            ))

    if subtotal is not None and total is not None:
        expected = subtotal + (tax or 0) + sum(_num(data.get(f)) or 0 for f in spec.extra_total_fields)
        if not _close(expected, total):
            issues.append(issue(
                "error", "total_mismatch",
                f"Subtotal + tax = {expected:,.2f} but total is {total:,.2f} "
                "(could be an unextracted discount/shipping line).", "total_amount",
            ))

    if total is not None and total < 0:
        issues.append(issue("info", "credit_note", "Negative total - this looks like a credit note.", "total_amount"))
    return issues


# --------------------------------------------------------------------------- history-based checks


def check_duplicates(label: str, keys: dict, same_file: list[dict], same_number: list[dict],
                     same_amount_date: list[dict]) -> list[dict]:
    """`keys` comes from role_keys(). Callers pass other jobs (as {id, filename}) found by the three lookups."""
    issues = []
    if same_file:
        issues.append(issue("error", "duplicate_file",
                            f"This exact file was already uploaded ({same_file[0]['filename']}).", None))
    if same_number:
        issues.append(issue("error", "duplicate_document",
                            f"{label} {keys['document_number']} from this party already exists "
                            f"({same_number[0]['filename']}).", keys["document_number_field"]))
    elif same_amount_date:
        issues.append(issue("warning", "possible_duplicate",
                            f"Another {label.lower()} from this party has the same date and total "
                            f"({same_amount_date[0]['filename']}).", keys["document_number_field"]))
    return issues


def check_anomalies(keys: dict, past_totals: list[float]) -> list[dict]:
    total = keys["total"]
    if not past_totals:
        return [issue("info", "new_party", "First document of this type from this party.", keys["party_field"])]
    if total is None or len(past_totals) < 3:
        return []
    typical = median(past_totals)
    if typical > 0 and total > typical * config.ANOMALY_MULTIPLIER:
        return [issue("warning", "unusual_amount",
                      f"Total {total:,.2f} is {total / typical:.1f}x this party's typical amount ({typical:,.2f}).",
                      keys["total_field"])]
    return []


# --------------------------------------------------------------------------- generic, role-based checks


def role_keys(schema: list[dict], data: dict) -> dict:
    """Pull the identifying values out of any document via field roles."""
    by_role = {s["role"]: s["key"] for s in schema if s.get("role", "none") != "none" and s["type"] != "table"}
    number = data.get(by_role.get("document_number", ""))
    doc_date = data.get(by_role.get("document_date", ""))
    return {
        "document_number": str(number).strip() or None if number not in (None, "") else None,
        "document_number_field": by_role.get("document_number"),
        "document_date": doc_date if isinstance(doc_date, str) and _parse_date(doc_date) else None,
        "party": normalize_party(data.get(by_role.get("party", "")) if by_role.get("party") else None),
        "party_field": by_role.get("party"),
        "total": _num(data.get(by_role.get("total", ""))),
        "total_field": by_role.get("total"),
    }


def _type_ok(ftype: str, v) -> bool:
    if ftype in ("number",):
        return _num(v) is not None
    if ftype == "integer":
        return _num(v) is not None and float(v).is_integer()
    if ftype == "boolean":
        return isinstance(v, bool)
    return isinstance(v, str)


def _type_issue(ftype: str, v, path: str) -> dict | None:
    if v in (None, ""):
        return None
    if not _type_ok(ftype, v):
        return issue("error", "bad_type", f"'{v}' is not a valid {ftype}.", path)
    if ftype == "date" and _parse_date(v) is None:
        return issue("error", "bad_date", f"'{v}' is not a valid YYYY-MM-DD date.", path)
    return None


def check_generic(schema: list[dict], data: dict, confidence: dict | None = None,
                  today: date | None = None) -> list[dict]:
    """Validation for discovered document types, driven entirely by the schema's types and roles."""
    today = today or date.today()
    issues: list[dict] = []
    scalars = [s for s in schema if s["type"] != "table"]
    tables = [s for s in schema if s["type"] == "table"]

    if all(data.get(s["key"]) in (None, "") for s in scalars) and all(not data.get(t["key"]) for t in tables):
        return [issue("error", "nothing_extracted", "No fields could be extracted from this document.")]

    for s in scalars:
        v = data.get(s["key"])
        if s.get("required") and v in (None, ""):
            issues.append(issue("warning", "missing_field",
                                f"'{s['label']}' is expected on this type of document but was not found.", s["key"]))
        elif (t := _type_issue(s["type"], v, s["key"])) is not None:
            issues.append(t)
    for t in tables:
        if t.get("required") and not data.get(t["key"]):
            issues.append(issue("warning", "missing_field", f"'{t['label']}' has no rows.", t["key"]))
        for i, row in enumerate(data.get(t["key"]) or []):
            for c in t["columns"]:
                if (x := _type_issue(c["type"], row.get(c["key"]), f"{t['key']}[{i}].{c['key']}")) is not None:
                    issues.append(x)

    role = {s["role"]: s["key"] for s in scalars if s.get("role", "none") != "none"}
    val = lambda r: data.get(role[r]) if r in role else None  # noqa: E731

    doc_date = _parse_date(val("document_date"))
    if doc_date and doc_date > today:
        issues.append(issue("warning", "future_date", f"Document date {val('document_date')} is in the future.",
                            role["document_date"]))
    for later, earlier, msg in (("due_date", "document_date", "Due date is before the document date."),
                                ("period_end", "period_start", "Period end is before period start.")):
        a, b = _parse_date(val(later)), _parse_date(val(earlier))
        if a and b and a < b:
            issues.append(issue("error", "date_order", msg, role[later]))

    cur = val("currency")
    if cur and not re.fullmatch(r"[A-Z]{3}", str(cur)):
        issues.append(issue("warning", "bad_currency", f"Currency '{cur}' is not an ISO 4217 code.", role["currency"]))

    subtotal, tax, total = _num(val("subtotal")), _num(val("tax")), _num(val("total"))
    for t in tables:
        cols = {c["role"]: c["key"] for c in t["columns"] if c["role"] != "none"}
        rows = data.get(t["key"]) or []
        q_k, p_k, a_k = cols.get("quantity"), cols.get("unit_price"), cols.get("line_amount")
        if q_k and p_k and a_k:
            for i, r in enumerate(rows):
                q, p, a = _num(r.get(q_k)), _num(r.get(p_k)), _num(r.get(a_k))
                if q is not None and p is not None and a is not None and not _close(q * p, a):
                    issues.append(issue("warning", "line_math",
                                        f"{t['label']} row {i + 1}: {q:g} x {p:,.2f} = {q * p:,.2f}, "
                                        f"but amount is {a:,.2f}.", f"{t['key']}[{i}].{a_k}"))
        amounts = [_num(r.get(a_k)) for r in rows] if a_k else []
        if rows and amounts and all(x is not None for x in amounts):
            s_ = sum(amounts)
            target, name = (subtotal, "subtotal") if subtotal is not None else (
                (total, "total") if tax is None and total is not None else (None, None))
            if target is not None and not _close(s_, target):
                issues.append(issue("warning", "rows_vs_total",
                                    f"{t['label']} rows add up to {s_:,.2f} but {name} is {target:,.2f}.",
                                    role.get(name if name == "subtotal" else "total")))
    if subtotal is not None and total is not None and not _close(subtotal + (tax or 0), total):
        issues.append(issue("error", "total_mismatch",
                            f"Subtotal + tax = {subtotal + (tax or 0):,.2f} but total is {total:,.2f}.", role["total"]))
    if total is not None and total < 0:
        issues.append(issue("info", "negative_total", "Total is negative (credit / refund?).", role["total"]))

    for key, c in (confidence or {}).items():
        if c is not None and c < config.FIELD_MIN_CONFIDENCE:
            issues.append(issue("warning", "low_confidence",
                                f"Model confidence {c:.0%} - please verify this value.", key))
    return issues


# --------------------------------------------------------------------------- purchase-order matching


def _match_line(inv_line: dict, po_lines: list[dict]) -> dict | None:
    sku = (inv_line.get("sku") or "").strip().lower()
    if sku:
        for pl in po_lines:
            if (pl.get("sku") or "").strip().lower() == sku:
                return pl
    best, best_score = None, 0.0
    for pl in po_lines:
        score = SequenceMatcher(None, (inv_line.get("description") or "").lower(),
                                (pl.get("description") or "").lower()).ratio()
        if score > best_score:
            best, best_score = pl, score
    return best if best_score >= 0.6 else None


def match_purchase_order(invoice: dict, po: dict | None) -> tuple[dict, list[dict]]:
    """Two-way match (invoice vs PO). Returns (match summary, issues)."""
    po_number = invoice.get("po_number")
    if not po_number:
        return {"status": "no_po", "po_number": None, "lines": []}, [
            issue("warning", "no_po_reference", "Invoice does not reference a purchase order.", "po_number")]
    if po is None:
        return {"status": "not_found", "po_number": po_number, "lines": []}, [
            issue("warning", "po_not_found", f"PO {po_number} is not in the system.", "po_number")]

    issues: list[dict] = []
    vendor_score = similarity(invoice.get("vendor_name"), po.get("vendor_name"))
    if vendor_score < 0.8:
        issues.append(issue("error", "po_vendor_mismatch",
                            f"Invoice vendor '{invoice.get('vendor_name')}' does not match PO vendor "
                            f"'{po.get('vendor_name')}'.", "vendor_name"))

    if invoice.get("currency") and po.get("currency") and invoice["currency"] != po["currency"]:
        issues.append(issue("error", "po_currency_mismatch",
                            f"Currency {invoice['currency']} differs from PO currency {po['currency']}.", "currency"))

    inv_total, po_total = _num(invoice.get("total_amount")), _num(po.get("total_amount"))
    if inv_total is not None and po_total is not None and inv_total > po_total + config.AMOUNT_TOLERANCE:
        issues.append(issue("error", "po_total_exceeded",
                            f"Invoice total {inv_total:,.2f} exceeds PO total {po_total:,.2f}.", "total_amount"))

    lines = []
    po_lines = po.get("line_items") or []
    for i, li in enumerate(invoice.get("line_items") or []):
        pl = _match_line(li, po_lines)
        row = {"invoice_line": i, "description": li.get("description"), "po_description": None, "status": "unmatched"}
        if pl is None:
            if po_lines:
                issues.append(issue("warning", "po_line_unmatched",
                                    f"Line {i + 1} ('{li.get('description')}') is not on the PO.",
                                    f"line_items[{i}]"))
            lines.append(row)
            continue
        row.update(po_description=pl.get("description"), status="matched")
        q, pq = _num(li.get("quantity")), _num(pl.get("quantity"))
        if q is not None and pq is not None and q > pq:
            row["status"] = "mismatch"
            issues.append(issue("error", "po_qty_exceeded",
                                f"Line {i + 1}: invoiced qty {q:g} exceeds ordered qty {pq:g}.",
                                f"line_items[{i}].quantity"))
        p, pp = _num(li.get("unit_price")), _num(pl.get("unit_price"))
        if p is not None and pp and abs(p - pp) / pp * 100 > config.PO_PRICE_TOLERANCE_PCT:
            row["status"] = "mismatch"
            issues.append(issue("warning", "po_price_variance",
                                f"Line {i + 1}: unit price {p:,.2f} vs PO price {pp:,.2f}.",
                                f"line_items[{i}].unit_price"))
        lines.append(row)

    status = "matched" if not issues else ("mismatch" if any(x["severity"] == "error" for x in issues) else "partial")
    return {"status": status, "po_number": po_number, "po_total": po_total,
            "vendor_similarity": round(vendor_score, 2), "lines": lines}, issues
