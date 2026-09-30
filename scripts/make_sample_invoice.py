"""Write sample PDFs into samples/ (no dependencies):

  sample_invoice.pdf         built-in type; deliberate problems (total doesn't add up, qty above the PO)
  sample_bill_of_lading.pdf  a type the platform doesn't know - exercises schema discovery
"""
from pathlib import Path

INVOICE = [
    "INVOICE                               INV-1001",
    "Acme Supplies Inc",
    "12 Industrial Rd, Pune",
    "",
    "Invoice date: 2026-09-01     Due: 2026-10-01",
    "PO number: PO-7              Terms: Net 30",
    "",
    "SKU      Description        Qty   Price   Amount",
    "BLT-M8   Steel bolts M8     500    0.12    60.00",
    "         Washers            120    0.50    50.00",
    "",
    "                              Subtotal    110.00",
    "                              Tax         11.00",
    "                              TOTAL USD  125.00",
]

BILL_OF_LADING = [
    "BILL OF LADING                    B/L No. MAEU-123",
    "Carrier: Maersk Line",
    "Issue date: 2026-09-05",
    "",
    "Shipper:    Acme Exports Pvt Ltd, Mumbai",
    "Consignee:  Nordic Machinery AB, Gothenburg",
    "Port of loading:   Nhava Sheva (INNSA)",
    "Port of discharge: Gothenburg (SEGOT)",
    "Vessel / voyage:   MAERSK KENSINGTON / 238W",
    "",
    "Marks     Description          Pkgs   Gross kg",
    "AE-01     Machine parts          12    3,400.0",
    "AE-02     Spare bearings          4      310.5",
    "",
    "Freight: PREPAID        Shipped on board 2026-09-05",
]


def build(lines: list[str]) -> bytes:
    esc = lambda s: s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")  # noqa: E731
    text = "BT /F1 12 Tf 60 740 Td 18 TL " + " ".join(f"({esc(l)}) '" for l in lines) + " ET"
    objs = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        "/Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(text)} >>\nstream\n{text}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Courier >>",
    ]
    out, offsets = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{o}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer << /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


if __name__ == "__main__":
    folder = Path(__file__).resolve().parent.parent / "samples"
    folder.mkdir(exist_ok=True)
    for name, lines in [("sample_invoice.pdf", INVOICE), ("sample_bill_of_lading.pdf", BILL_OF_LADING)]:
        (folder / name).write_bytes(build(lines))
        print(f"wrote {folder / name}")
