# DocParse — intelligent document processing

Upload any business document (PDF or image). DocParse identifies what kind of document it is, picks or designs a schema for it, extracts the data with Claude along with confidence scores, validates the result, and sends it to a human for review. Approved documents become structured records; the ones whose document type has an ERP mapping are also exported.

Invoices, receipts and purchase orders have **built-in** specialised schemas, validation rules (including matching invoices against POs) and ERP mappings. **Any other document type** is *discovered*: the first time one arrives, Claude designs an extraction schema for it. That schema is saved and reused for later documents of the same type.

```
upload ─► FastAPI ─► 202 + job_id ─► BackgroundTasks:
    classify (catalogue = built-in + discovered types)
       ├─ built-in ─► specialised Pydantic schema ─► extract ─► specialised checks (+ PO match)
       └─ other    ─► discovered before? reuse schema : Claude designs one (saved to registry)
                      ─► extract {value, confidence} per field ─► generic role-based checks
    ─► review UI (drawn from the job's schema) ─► correct ─► re-validate ─► approve
         ─► Record (always stored)
              ├─ type has an ERP mapping ─► export                    job: exported
              └─ no mapping              ─► held for configuration    job: approved
```

## How document types work

| | Built-in (`invoice`, `receipt`, `purchase_order`) | Discovered (any other type) |
|---|---|---|
| Where the schema lives | Pydantic models in `app/doc_schemas.py` | `document_types` table, and a copy on every job |
| Schema source | hand-written | designed by Claude on the first document, reused afterwards (`generated` / `reused`) |
| Confidence | the model lists fields it was unsure of | a 0–1 score for every field |
| Validation | specialised rules plus invoice-to-PO matching | generic rules driven by the schema's field types and roles |
| ERP | fixed mapping (`ap_bill`, `expense`, `purchase_order`) | none until an operator configures one, so approved records are held |

**Schemas are data.** A schema is a list of field specs, each with a key, label, type (`string`, `number`, `integer`, `date`, `boolean` or `table` with columns), whether it's required, and a **role** (`document_number`, `document_date`, `due_date`, `period_start`, `period_end`, `party`, `counterparty`, `currency`, `subtotal`, `tax`, `total`; table columns can be `description`, `quantity`, `unit_price` or `line_amount`). Built-in types are expressed in the same format, so a single review UI and a single set of duplicate and arithmetic checks work for every type.

**Generic validation** (discovered types) checks:
- required fields that are missing;
- values that don't match their declared type;
- invalid dates, a document date in the future, a due date before the document date, and a period end before its start;
- currency codes;
- row arithmetic (quantity × unit price = line amount), rows adding up to the subtotal (or to the total when there's no subtotal or tax), and subtotal + tax = total;
- negative totals and fields with low confidence;
- documents where nothing could be extracted.

Duplicate detection (same file, or same party and document number) and unusual-amount checks use the roles, so they work for any type that has those roles.

**Safeguards**
- A low classification confidence never blocks a document. It's processed and flagged for review, but a type discovered with low confidence isn't saved to the registry, so one uncertain document can't set the schema for later ones.
- Reviewers can reprocess a document as any built-in, discovered or brand-new type, or have Claude design a fresh schema (`regenerate_schema`). Jobs processed earlier keep their own copy of the schema.
- Claude's structured outputs enforce generated schemas. If the API rejects a generated schema, extraction switches to a fixed fallback format and the values are converted to the right types in code.
- A record is exported to the ERP only when its type has a mapping. Otherwise it stays in the `records` table as `awaiting_configuration` and can be exported (`POST /api/jobs/{id}/export`) once a mapping is set on the Document types screen.

## Stack

Python · FastAPI (async plus `BackgroundTasks`) · an LLM with vision and structured outputs: Claude (`claude-opus-5`, the default) or Gemini (`gemini-2.5-flash-lite`), selected with `LLM_PROVIDER` · SQLAlchemy async on PostgreSQL, or SQLite when no database is configured · a single-file vanilla JS front end. There's no Redis or Celery: job state is kept in the database, and jobs interrupted by a restart run again on startup.

## Frontend

`app/static/index.html` is the whole UI: HTML, CSS and JS in one file, with no build step. FastAPI serves it at `/`. It uses the **Nocturne** design system from the *Sift document parser mockups*: the tokens and component classes are copied into the file, plus three low-saturation colours for error, warning and OK states, which Nocturne doesn't define.

| Page | What it does |
|---|---|
| **Parse** (`#parse`) | The Nocturne upload screen. You can drop files, use *Select files*, or use *Try a sample* (`POST /api/samples` queues the files in `samples/`). Recent runs shows real jobs with a progress bar for each pipeline stage, a filled-field count, *Open*, and a download in the chosen output format (Markdown, JSON or CSV, generated in the browser from the reviewed data). |
| **Runs** (`#runs/<id>`) | The review screen: document preview, checks, fields built from the job's schema with confidence badges, editable tables, PO match, and save / approve / reprocess / reject / export. |
| **Schemas** (`#schemas`) | Built-in and discovered document types, their schemas, and ERP-mapping setup for discovered types. |
| **Purchase orders**, **ERP exports** | The PO list and form; the export log. |

The accepted file types and size limit shown on the Parse page come from `GET /api/config`. Opening the file directly from disk (`file://`) displays the page with a notice to start the server, because every feature needs the API.

## Run it

```bash
python -m venv .venv
.venv\Scripts\activate          # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
copy .env.example .env          # then choose the provider and set its key (see below)
uvicorn app.main:app --reload
```

Open http://localhost:8000. API docs are at http://localhost:8000/docs.

**Choosing the LLM** (in `.env`):

| | Claude (default) | Gemini |
|---|---|---|
| Settings | `LLM_PROVIDER=anthropic`, `ANTHROPIC_API_KEY=…` | `LLM_PROVIDER=gemini`, `GEMINI_API_KEY=…` (or `GOOGLE_API_KEY`) |
| Default model (`LLM_MODEL` overrides it) | `claude-opus-5` | `gemini-2.5-flash-lite` |
| Structured output | `messages.parse` with the Pydantic schema | `response_json_schema` from the same Pydantic schema |
| Provider-specific options | `CLASSIFY_EFFORT`, `EXTRACT_EFFORT`, `LLM_FALLBACKS` | retries on 429 and 5xx errors |

Prompts, schemas (including the ones generated for discovered types), validation and the fallback format are the same for both providers; only the API call in `llm._parse` differs. Restart the server after changing `.env`.

**Postgres (optional):** run `docker compose up -d`, then set `DATABASE_URL=postgresql+asyncpg://docparse:docparse@localhost:5432/docparse`. Tables are created on startup. There are no migrations, so after pulling schema changes, delete `docparse.db` (or drop the tables).

**Tests** (no API key needed, since the Claude calls are replaced by stand-ins): `pytest`

**Demo without an API key:** run `python scripts/make_sample_invoice.py`, then `python scripts/demo_server.py`, and open http://localhost:8765. The demo uses canned Claude responses:
- `samples/sample_invoice.pdf` goes through the built-in path. The checks catch a total that doesn't add up, a quantity above the PO, and a low-confidence date.
- `samples/sample_bill_of_lading.pdf` is a type the app doesn't know. It gets a newly designed schema with confidence scores; approving it holds the record, and once you configure an ERP mapping it can be exported.

## API

| Method | Path | |
|---|---|---|
| POST | `/api/documents` | multipart `file` → `202 {job_id}` |
| POST | `/api/samples` | queue the bundled sample documents (the UI's *Try a sample* button) |
| GET | `/api/config` | accepted file types and the upload size limit |
| GET | `/api/jobs`, `/api/jobs/{id}` | list (filter by `status` or `doc_type`) / poll one job: schema, data, field confidences, checks, record |
| GET | `/api/jobs/{id}/file` | the original document |
| PUT | `/api/jobs/{id}/data` | reviewer corrections, validated against the job's schema → checks run again |
| POST | `/api/jobs/{id}/reprocess` | `{doc_type?, display_name?, regenerate_schema?}` |
| POST | `/api/jobs/{id}/approve` | `{override_errors?}` → stores the record, and exports it if the type has an ERP mapping |
| POST | `/api/jobs/{id}/export` | export a held record once its type has a mapping |
| POST | `/api/jobs/{id}/reject` | |
| GET | `/api/doc-types`, `/api/doc-types/{id}` | built-in and discovered types, with schema, usage and ERP routing |
| PUT | `/api/doc-types/{id}` | `{erp_record_type, display_name?}`, for discovered types only |
| GET | `/api/records` | reviewed records of any type (filter by `doc_type` or `erp_status`) |
| GET/POST/DELETE | `/api/purchase-orders` | |
| GET | `/api/erp/exports` | delivery log |

## Project layout

```
app/
  main.py           HTTP API + static UI
  pipeline.py       classify → built-in or discovered schema → extract → validate; resume on startup
  llm.py            Claude calls: classify, generate_schema, extract (built-in), extract_dynamic (+ fallback)
  doc_schemas.py    built-in Pydantic schemas, field-spec language, Classification
  dynamic_schema.py tidy generated schemas, build runtime models, fallback format, type conversion
  type_registry.py  lookups across built-in and discovered types, ERP routes
  validation.py     specialised + generic role-based checks, duplicates, anomalies, PO match
  erp.py            approve → record → export or hold; payloads for built-in and configured types
  models.py         jobs, document_types, records, purchase_orders, erp_exports
  static/           review UI
tests/              unit tests (validation, dynamic schemas) + end-to-end API tests for every path
scripts/            demo server, sample documents
```

## Adding a specialised (built-in) type

A discovered type already works without writing any code. To give one specialised validation or a fixed ERP payload:
1. Add a Pydantic model and a `DocTypeSpec` to `BUILTIN_TYPES` in `doc_schemas.py`.
2. Add its ERP record type to `BUILTIN_ERP_RECORD_TYPES` in `type_registry.py`.
3. Optionally add a specialised payload in `erp.build_payload`.
