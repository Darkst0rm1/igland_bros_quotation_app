# Igland Bros — Quotation Application

Internal Streamlit application for creating, approving and tracking customer
quotations. **Employees only** — customers never log in. Quotations are sent to
customers as PDFs by email or another external process, and their responses are
recorded manually.

> **Status: all five phases complete.**
>
> *Phase 1 — Foundation:* authentication, authorization, the 31-table schema,
> the calculation engine, the audit trail and the application shell.
>
> *Phase 2 — Master data:* customers with contacts and addresses, the product
> catalogue with board-quality variants, append-only price and cost history,
> per-variant cost entry, price-tier management, and the Excel price-list
> importer. The reference workbook imports to **12 products, 23 variants,
> 69 prices**, and re-importing is a no-op.
>
> *Phase 3 — Quotations:* the quotation editor — header with customer snapshot,
> line items with tier selection, charges and the printing-plate calculator,
> per-quotation term editing, live totals, the eight pricing warnings, the
> status machine, and a searchable, scope-filtered history with Excel export.
>
> *Phase 4 — Documents and controls:* the quotation document in **PDF and
> Word** from one shared model, the approval workflow with its release gate,
> revisions with field-by-field comparison, and manual customer-response
> logging.
>
> *Phase 5 — Management:* dashboard with seven charts and the full filter set,
> fifteen reports with Excel export, user and role administration with approval
> limits, company settings, and the audit log. Operational runbook in
> [`docs/OPERATIONS.md`](docs/OPERATIONS.md).
>
> All eleven pages are built. See
> [`docs/PHASE1_ARCHITECTURE.md`](docs/PHASE1_ARCHITECTURE.md) §14 for the plan
> this was built to.

---

## Documentation

| Document | Contents |
|---|---|
| [`docs/PHASE1_REFERENCE_ANALYSIS.md`](docs/PHASE1_REFERENCE_ANALYSIS.md) | What is actually in the two reference files, and the three findings that shaped the data model |
| [`docs/PHASE1_ARCHITECTURE.md`](docs/PHASE1_ARCHITECTURE.md) | Architecture, schema, permission matrix, calculation rules, import design, deployment |
| [`docs/OPERATIONS.md`](docs/OPERATIONS.md) | Deployment sequence, backup schedule, restore drill, routine operations, troubleshooting |

---

## Local setup

Requires Python 3.13 or 3.14.

```bash
git clone <repository-url>
cd igland_bros_quotation_app

python -m venv .venv
.venv/Scripts/activate          # Windows
# source .venv/bin/activate     # macOS / Linux

pip install -r requirements.txt

cp .env.example .env            # defaults to SQLite + local file storage

alembic upgrade head            # create the schema
python -m seeds.bootstrap       # roles, permissions, tiers, terms, settings
```

`seeds.bootstrap` prints a temporary administrator password **once**. It is
stored only as a bcrypt hash and must be changed at first login.

Optionally load a price list, which also populates the catalogue:

```bash
python -m seeds.seed_catalogue_from_workbook "White Boxes B Flute Quotation.xlsx" \
       --effective-from 2026-01-01
```

This runs the **real importer**, not a fixture, so the import path is exercised
on every fresh database and the seeded catalogue is identical to what the Excel
Import page produces. The workbook itself is not committed — it is commercial
pricing data, so pass the path.

```bash
streamlit run app.py            # http://localhost:8501
```

Local development needs no cloud account: SQLite and the local filesystem are
the defaults.

## Tests

```bash
pytest                          # 495 tests
pytest tests/test_calculation_engine.py -v
pytest tests/test_excel_importer.py -v
```

The importer suite runs mostly against a **synthetic workbook built in code**
that reproduces the reference file's structural quirks — header not on row 1,
two blocks, board quality changing partway down the second block, embedded
newlines in the price headers — so it is deterministic and runs on a machine
that does not have the real file. A final class runs against the real workbook
when it is present.

The suite runs against a throwaway SQLite database in a temp directory and needs
no configuration. It covers rounding and money arithmetic, the permission
matrix, quotation numbering, authentication and lockout, record immutability,
and the Streamlit shell itself via `AppTest`.

---

## Deployment — Streamlit Community Cloud

Three things differ from local development, all of them consequences of the
platform. Full detail in `docs/PHASE1_ARCHITECTURE.md` §11.1 and §12.1.

**1. Make the app private.** Community Cloud apps are served on the public
internet at a guessable URL and are **public by default**. This application
holds customer contacts, costs, margins and every quotation ever priced. Set the
app to private and maintain the viewer allowlist — that platform-edge check is
the real perimeter; the in-app roles decide what each authenticated employee may
*do*. Offboarding means removing someone from both the allowlist and `users`.

**2. Use hosted Postgres and object storage.** The container filesystem is
rebuilt on every redeploy, so a SQLite file and any uploaded workbook or
generated PDF written to disk would be discarded. Supabase provides both in one
dependency; Neon + Cloudflare R2 is an equivalent split. Set in the app's
**Secrets** panel:

```toml
APP_ENV = "production"
DATABASE_URL = "postgresql+psycopg://USER:PASSWORD@HOST:5432/DB?sslmode=require"
SECRET_KEY = "<python -c 'import secrets; print(secrets.token_urlsafe(48))'>"

STORAGE_BACKEND = "s3"
STORAGE_BUCKET = "igland-quotations"
STORAGE_ENDPOINT_URL = "https://<project>.supabase.co/storage/v1/s3"
STORAGE_ACCESS_KEY_ID = "..."
STORAGE_SECRET_ACCESS_KEY = "..."
```

`modules/config.py` refuses to start in production on the sample `SECRET_KEY`,
on SQLite, or with `STORAGE_BACKEND=local`.

**3. Run migrations from your machine.** There is no shell on the platform.

```bash
DATABASE_URL="postgresql+psycopg://..." alembic upgrade head
DATABASE_URL="postgresql+psycopg://..." python -m seeds.bootstrap
```

The app checks the applied Alembic revision at startup and refuses to run
against a mismatched schema rather than auto-migrating — several containers can
start at once, and a schema change should not be a side effect of a page load.

**Deploy order:** apply migrations → seed → push code → verify the app starts.

---

## Backup and recovery

*Summary — the full runbook, including the restore drill, is in
[`docs/OPERATIONS.md`](docs/OPERATIONS.md).*

Two independent things must be backed up, and a restore of one without the other
leaves quotations that reference PDFs that no longer exist:

| What | How |
|---|---|
| **Database** | Managed Postgres daily automated backups plus point-in-time recovery. Additionally `pg_dump` weekly to storage you control — a provider account you lose access to takes its backups with it. |
| **Object storage** | Enable bucket versioning and a lifecycle rule. Uploaded price-list workbooks and generated PDFs are the evidence behind issued quotations. |

Restore drill, to be rehearsed before go-live and then annually:

```bash
pg_restore --clean --if-exists -d "$DATABASE_URL" backup.dump
alembic current                  # confirm the revision matches the code
pytest                           # confirm the schema still satisfies the suite
```

Records are append-only by design — issued quotations, price history and audit
rows are never updated or deleted — so recovery is about media failure and
operator error, not about undoing application writes.

---

## Project layout

```
app.py                  Entry point: login gate, role-filtered navigation
pages/                  Eleven pages, each gated by its permission
modules/
  config.py             Settings (st.secrets → env → defaults)
  constants.py          Enums, permission matrix, status transitions
  database.py           Engine, sessions, ExactNumeric column type
  models.py             31 tables + immutability guards
  calculation_engine.py Pure Decimal arithmetic — no I/O
  authentication.py     bcrypt, lockout, session timeout
  authorization.py      AuthUser, require(), SQL scope predicates
  audit_service.py      Audit trail
  numbering.py          Quotation numbers via a locked sequence
  storage.py            Local ⇄ object storage adapter
  session.py            The only module that touches st.session_state
  utilities.py          Formatting, pandas 3.0 guards
seeds/                  Roles, permissions, tiers, terms, settings
migrations/             Alembic
tests/                  pytest
docs/                   Architecture and reference analysis
```

## Conventions worth knowing before contributing

- **Money is `Decimal`, never `float`.** A test asserts no `Float` column
  exists. Rounding is `ROUND_HALF_UP`, set explicitly — Python's default is
  banker's rounding and gets 0.125 wrong for invoicing.
- **Pages do not compute money and do not open database sessions.** They call a
  service. That is what keeps the arithmetic testable without Streamlit.
- **Permission checks live in the service layer.** Hiding a button is a
  courtesy; `require(user, Perm.X)` inside the service is the control.
- **Issued quotations and price history are immutable.** Session-level guards in
  `models.py` reject the write; edits go through `revision_service`.
- **`effective_to` and `is_active` are not the same thing.** `effective_to`
  records *when* a price applied — a superseded price is still the right answer
  for a date inside its range, which is what lets an old quotation reprint
  correctly. `is_active=False` means the row was entered in error and must never
  resolve for any date. Conflating them silently reprices history.
- **Quotations export as PDF or Word**, chosen by the employee. Both render from
  one document model so they cannot drift; the PDF is the record of what was
  sent, since a `.docx` is editable by whoever receives it.
- **The selected price tier is authoritative.** Quantity never re-selects a
  tier — entering fewer containers than a tier expects raises a warning that
  says so explicitly. Only `change_line_tier` re-prices a line.
- **`st.rerun()` is only called from the top level of `main()`** — calling it
  inside a column, form or sidebar leaves that container's context open and the
  next run reports the form as nested inside itself.
