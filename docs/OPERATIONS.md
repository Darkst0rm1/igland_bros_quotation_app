# Operations — deployment, backup and recovery

Companion to `PHASE1_ARCHITECTURE.md` §11 (deployment) and §12 (security).

This document is the runbook. It assumes the target decided in Phase 1:
**Streamlit Community Cloud**, managed PostgreSQL, S3-compatible object storage.

---

## 1. Before the first deployment

Four things must be settled. The first is not optional.

### 1.1 Make the app private — do this before any real data goes in

Community Cloud serves apps on the public internet at a guessable
`*.streamlit.app` URL, and they are **public by default**. This application
holds customer contacts, cost and margin data, and every quotation the business
has ever priced.

* Set the app to **private** and maintain the viewer allowlist.
* That platform-edge check is the real perimeter. The in-app login and roles
  decide what each authenticated employee may *do* — they are not a substitute
  for it.
* Offboarding someone means removing them from **both** the viewer allowlist and
  the `users` table. Doing only one leaves a hole.

There is no request-level rate limiting available on the platform, which is why
the account lockout (5 failed attempts → 15 minutes) is implemented in the
application rather than deferred to infrastructure.

### 1.2 Provision the database and storage

Supabase provides both with one set of credentials; Neon (PostgreSQL) plus
Cloudflare R2 (storage) is an equivalent split. `modules/storage.py` is an
adapter over four methods, so the choice is not load-bearing.

Create a database role scoped to this application's schema only, and storage
keys scoped to one bucket. Everything in `st.secrets` is a live credential on a
shared platform.

### 1.3 Fill in the company identity

Until someone saves **Company Settings**, the seeded row is flagged
`is_placeholder` and the page shows a banner. Blank fields are *omitted* from
the quotation document rather than printed, so an unconfigured install produces
a plain header rather than showing "Address not set" to a customer — but the
legal name, contact details and signatory should still be set before the first
real quotation goes out.

### 1.4 Set the approval limits

The seeded limits are starting points, not policy:

| Role | Max discount | Max quotation value | Min margin |
|---|---|---|---|
| Sales Employee | 5% | 25,000 | 15% |
| Sales Manager | 15% | 250,000 | 10% |
| Finance | 20% | unlimited | 8% |
| System Administrator | unlimited | unlimited | none |

Review them on **Users & Permissions → Approval limits**. Note that where
someone holds several roles the **most permissive** value applies.

---

## 2. Deploying

Migrations cannot run on Community Cloud — there is no shell. Apply them from a
developer machine, then push the code.

```bash
# 1. Point at production
export DATABASE_URL="postgresql+psycopg://USER:PASSWORD@HOST:5432/DB?sslmode=require"

# 2. Apply migrations
alembic upgrade head

# 3. Seed roles, permissions, tiers, terms and settings (idempotent)
python -m seeds.bootstrap

# 4. Optionally load the current price list
python -m seeds.seed_catalogue_from_workbook "White Boxes B Flute Quotation.xlsx" \
       --effective-from 2026-01-01

# 5. Push the code; Community Cloud redeploys automatically
git push
```

**Order matters.** The app checks the applied Alembic revision at startup and
**refuses to run** against a schema that does not match the code, rather than
auto-migrating. That is deliberate: several containers can start at once, and a
schema change should not be a side effect of a page load. If you push code
before migrating, the app shows a clear message and stops.

### Secrets

Set in the app's Secrets panel (TOML):

```toml
APP_ENV = "production"
DATABASE_URL = "postgresql+psycopg://USER:PASSWORD@HOST:5432/DB?sslmode=require"
SECRET_KEY = "..."            # python -c "import secrets; print(secrets.token_urlsafe(48))"
SESSION_TIMEOUT_MINUTES = 60

STORAGE_BACKEND = "s3"
STORAGE_BUCKET = "soneet-quotations"
STORAGE_ENDPOINT_URL = "https://<project>.supabase.co/storage/v1/s3"
STORAGE_REGION = "auto"
STORAGE_ACCESS_KEY_ID = "..."
STORAGE_SECRET_ACCESS_KEY = "..."
```

`modules/config.py` refuses to start in production on the sample `SECRET_KEY`,
on SQLite, or with `STORAGE_BACKEND=local` — each of those would silently lose
data on the next redeploy.

### Post-deployment checks

1. The app loads and shows the login form.
2. Sign in as the bootstrap administrator; you are forced to change the password.
3. **Company Settings** — the placeholder banner is gone.
4. **Products & Pricing** — the expected counts appear (12 / 23 / 69 for the
   reference workbook).
5. Raise a throwaway quotation, generate both a PDF and a Word document, then
   cancel it. This exercises the whole chain: numbering, pricing, warnings, the
   release gate, both renderers and object storage.

---

## 3. Backup

Two things must be backed up, and **a restore of one without the other is
useless**: the database holds quotations that reference stored documents by
key, and the storage holds documents that mean nothing without the quotations.

| What | How | Frequency |
|---|---|---|
| **Database** | The provider's automated backups plus point-in-time recovery | Continuous |
| **Database** | `pg_dump` to storage *you* control | Weekly |
| **Object storage** | Bucket versioning enabled, with a lifecycle rule | Continuous |
| **Object storage** | Sync to a second location | Monthly |

The second row is not redundant with the first. A provider account you lose
access to — billing lapse, ownership dispute, suspension — takes its backups
with it. Keep one copy somewhere the provider does not control.

```bash
# Weekly database dump
pg_dump --format=custom --no-owner \
        --file="soneet-$(date +%Y%m%d).dump" "$DATABASE_URL"

# Monthly storage sync
aws s3 sync s3://soneet-quotations ./storage-backup \
    --endpoint-url "$STORAGE_ENDPOINT_URL"
```

Retain weekly dumps for three months and monthly ones for seven years —
quotations are commercial records and the retention period should match whatever
the business applies to its invoices.

### What backup is actually protecting against

Records here are **append-only by design**. Issued quotations, price history,
cost history and audit rows are never updated or deleted; the ORM guards refuse
it. So recovery is about media failure, provider loss and operator error — not
about undoing application writes, which the application does not do.

---

## 4. Restore drill

**Rehearse this before go-live, then annually.** A backup nobody has restored is
a hypothesis, not a backup.

```bash
# 1. Restore into a scratch database, never over the live one
createdb soneet_restore_test
pg_restore --clean --if-exists --no-owner \
           -d "postgresql://.../soneet_restore_test" soneet-20260803.dump

# 2. Confirm the schema matches the code
DATABASE_URL="postgresql+psycopg://.../soneet_restore_test" alembic current
# must report the same revision as: alembic heads

# 3. Confirm the schema still satisfies the application
DATABASE_URL="postgresql+psycopg://.../soneet_restore_test" pytest

# 4. Spot-check the data
DATABASE_URL="postgresql+psycopg://.../soneet_restore_test" python - <<'EOF'
from sqlalchemy import func, select
from modules.database import session_scope
from modules.models import Attachment, Quotation, QuotationRevision
from modules.storage import get_storage

with session_scope() as db:
    quotations = db.execute(select(func.count(Quotation.id))).scalar_one()
    revisions = db.execute(select(func.count(QuotationRevision.id))).scalar_one()
    attachments = db.execute(select(Attachment)).scalars().all()
    print(f"{quotations} quotations, {revisions} issued revisions, "
          f"{len(attachments)} stored documents")

    # The join that matters: every recorded document is still retrievable.
    missing = [a.storage_key for a in attachments if not get_storage().exists(a.storage_key)]
    print("missing from storage:", missing or "none")
EOF
```

Step 4 is the one that catches the failure mode people miss: a database restored
from Monday alongside storage restored from Friday leaves issued quotations
pointing at documents that do not exist. If `missing` is non-empty, the two
backups are not from a consistent point and one of them needs re-restoring.

Record the date and outcome of each drill.

---

## 5. Routine operations

| Task | Where | Who |
|---|---|---|
| New price list | Excel Import page | Pricing Administrator |
| New employee | Users & Permissions | System Administrator |
| Password reset | Users & Permissions | System Administrator |
| Adjust approval limits | Users & Permissions → Approval limits | Finance |
| Adjust pricing thresholds | Company Settings → Thresholds | System Administrator |
| Expire overdue quotations | Dashboard → "Mark them expired" | Any sales user |

**Expiring quotations is manual on purpose.** Community Cloud sleeps when idle,
so there is no reliable background timer to hang a scheduled job on. The
dashboard surfaces overdue quotations and offers a one-click sweep, which is
honest about how it works rather than pretending a cron job exists.

---

## 6. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| "The database schema is at revision X but this code expects Y" | Code pushed before migrating | `alembic upgrade head` against production |
| "The application cannot start — the database has no schema yet" | Migrations never applied | `alembic upgrade head`, then `python -m seeds.bootstrap` |
| "Invalid production configuration" on startup | Sample `SECRET_KEY`, SQLite, or `STORAGE_BACKEND=local` in production | Correct the Secrets panel; each of these would lose data on redeploy |
| A user cannot sign in and the message gives no detail | By design — one message for unknown user, wrong password and lockout | Check the Audit Log, which records which it actually was |
| An account is locked out | 5 failed attempts | Wait 15 minutes, or reset the password (which clears the lock) |
| A final document is refused | The quotation is not approved, or has blocking warnings | The Review tab lists the exact blockers |
| An issued quotation cannot be edited | Working as intended | Create a revision |
| Uploaded files vanish after a redeploy | `STORAGE_BACKEND=local` in production | Switch to `s3`; the local filesystem is ephemeral |

---

## 7. What is deliberately absent

Worth stating so nobody goes looking:

* **No customer login, portal, public link or electronic signature.** Employees
  send documents externally and record responses manually. This is a scope
  decision, not a gap.
* **No automatic emailing.** The application produces a file; sending it is done
  from the employee's own mail client.
* **No scheduled jobs.** See §5 on expiry.
* **No hard delete.** Master data is soft-deleted; transactional records are
  never deleted at all.
