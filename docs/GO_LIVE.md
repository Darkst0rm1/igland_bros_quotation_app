# Going live — the customer portal, email delivery and the worker

Companion to `OPERATIONS.md`. That document covers running the employee
application; this one covers the three pieces added by Phase 6 that have to be
stood up before a customer ever sees a quotation.

**Nothing here has been deployed.** The application is complete and tested, and
six things are outstanding — listed in §1. Do not work through the rest of this
document until they are.

---

## 1. Blockers — do not deploy until every one is resolved

| # | Blocker | Why it blocks |
|---|---|---|
| 1 | **A Render account with a paid plan** | The worker has no free tier and a free web service sleeps for 15 minutes. See §4. |
| 2 | **SMTP credentials** | With `EMAIL_ENABLED=false` the outbox fills and waits; nothing reaches a customer. |
| 3 | **The portal's public URL** | `PORTAL_BASE_URL` must be the exact HTTPS origin Render assigns. Every invitation points at it, and production refuses to start without it. See §4. |
| 4 | **Company information** | Legal name, trading name, address, phone and email. The readiness check blocks link issuing in production until these are set, and they appear on every customer page, email and PDF. |
| 5 | **A company logo** | Not strictly blocking, but its absence is visible: the customer page and PDF show the brand name as plain text. |
| 6 | **Tax settings** | At least one active tax rate, and the correct rate on each quotation. A quotation sent with the wrong rate is a wrong price to a customer. |

Items 4–6 are entered in the application (Company Settings, and the tax
configuration under Finance). Items 1–3 are infrastructure.

---

## 2. Production environment variables

Set these wherever the process runs — Streamlit's Secrets panel for the
employee app, real environment variables for the portal and worker.

### Required everywhere

| Variable | Notes |
|---|---|
| `APP_ENV` | `production`. Turns on the fail-closed configuration checks. |
| `DATABASE_URL` | PostgreSQL. Production refuses to start on SQLite. |
| `SECRET_KEY` | 32+ characters, unique. Signs portal nonces and derives line references. |
| `STORAGE_BACKEND` | `s3`. Required in production — a local disk is discarded on redeploy. |
| `STORAGE_BUCKET` | |
| `STORAGE_ACCESS_KEY_ID` | |
| `STORAGE_SECRET_ACCESS_KEY` | |
| `STORAGE_ENDPOINT_URL` | For Supabase Storage or Cloudflare R2. Omit for AWS S3. |

### The customer portal

| Variable | Notes |
|---|---|
| `PORTAL_BASE_URL` | e.g. `https://quotes.example.com`. **Must be HTTPS** — startup fails otherwise. |
| `PORTAL_SUPPORT_EMAIL` | Shown on the "link not available" page. Optional but kind. |
| `PORTAL_LINK_DAYS` | Fallback link lifetime when a quotation has no validity date. Default 30. |
| `PORTAL_BRAND_*` | Presentation overrides only. Identity always comes from Company Settings. |

### Email delivery

| Variable | Notes |
|---|---|
| `EMAIL_ENABLED` | `true` to deliver. While `false`, messages queue and wait — nothing is lost. |
| `EMAIL_BACKEND` | `smtp`. Production refuses to start with `memory` or `console` while enabled. |
| `EMAIL_FROM_ADDRESS` | Required when enabled. |
| `EMAIL_FROM_NAME` | The display name customers see. |
| `EMAIL_REPLY_TO` | Where replies go, if not the From address. |
| `EMAIL_INTERNAL_RECIPIENTS` | Comma-separated. Blank means no internal notifications. |
| `SMTP_HOST` | Required when enabled. |
| `SMTP_PORT` | 587 with `starttls`, or 465 with `tls`. |
| `SMTP_SECURITY` | `starttls` or `tls`. There is no unencrypted mode. |
| `SMTP_USERNAME` / `SMTP_PASSWORD` | Omit if the relay authorises by IP. |
| `EMAIL_MAX_ATTEMPTS` | Delivery attempts before a message is shown as failed. Default 6. |

### Encryption keys — **identical on every host**

| Variable | Notes |
|---|---|
| `EMAIL_PAYLOAD_KEYS` | `v1:<base64>`. Required when email is enabled. |
| `EMAIL_PAYLOAD_KEY_VERSION` | Which key new messages are sealed under. |
| `EMAIL_PAYLOAD_TTL_HOURS` | How long a queued invitation stays resendable. Default 72. |

Generate a key with:

```bash
python -c "from modules.secret_box import generate_key; print(generate_key())"
```

### The worker

| Variable | Notes |
|---|---|
| `WORKER_POLL_SECONDS` | Seconds between sweeps in continuous mode. Default 30. |
| `WORKER_BATCH_SIZE` | Rows per subsystem per sweep. Default 20. |
| `WORKER_LEASE_SECONDS` | How long a worker's claim on a row is honoured. Default 300. |
| `WORKER_STALE_AFTER_SECONDS` | When employees are told the worker looks stale. Default 900. |
| `WORKER_HEALTH_FILE` | **Local development only.** Production uses the database heartbeat, which is the only signal three separate machines can share. Leave unset on Render. |

---

## 3. The encryption-key agreement check

The employee app seals each invitation's link; the worker opens it to send.
**If they hold different key material, nothing is delivered** — every invitation
fails with `link_unsealable`, on a machine nobody is watching, and the only
visible symptom is customers not receiving quotations.

Matching *version labels* does not prove matching keys. `v1` on one host and
`v1` on another are the same label whatever is behind them, and a rotation
applied to one host and not the other leaves exactly that.

So both processes compute a fingerprint — an HMAC of a fixed string under the
key, which identifies it without revealing anything — and compare it against the
value recorded in `app_settings`. The first process to start records it; every
process afterwards checks.

* **The employee app** shows a startup error naming the mismatch.
* **The worker** refuses to start and exits with status **2**.

Verify after deployment:

```bash
python -m modules.worker --once
# Look for: "Email key agreement OK (v1:…)"
```

### Rotating a key

1. Add the new key alongside the old one **on every host**:
   `EMAIL_PAYLOAD_KEYS=v1:<old>,v2:<new>`
2. Point `EMAIL_PAYLOAD_KEY_VERSION=v2` **on every host**.
3. Clear the recorded fingerprint so the deployment settles on the new key:

   ```bash
   python -c "
   from modules.database import session_scope
   from modules.secret_box import clear_key_agreement
   with session_scope() as s: clear_key_agreement(s)"
   ```
4. Restart. Confirm the agreement line appears with the new fingerprint.
5. Remove `v1` only once no queued invitation still references it — roughly
   `EMAIL_PAYLOAD_TTL_HOURS` after the rotation. Removing it earlier makes those
   invitations unsendable; they fail safely and a new link can be issued.

---

## 4. Hosting on Render

Two services, one Blueprint (`render.yaml` at the repository root), both from
this repository:

| Service | Type | Plan | Public | Runs |
|---|---|---|---|---|
| `soneet-portal` | Web service | Starter | Yes, HTTPS | `uvicorn portal.app:app --host 0.0.0.0 --port $PORT --proxy-headers` |
| `soneet-worker` | Background worker | Starter | No | `python -m modules.worker --loop` |

The private employee application stays on Streamlit Community Cloud. Only these
two move — the portal because it must be reachable, the worker because it must
run continuously.

**Neither is on the free plan, deliberately.** A free web service sleeps after
15 minutes without traffic and takes about a minute to wake; a customer opening
a quotation link would sit on a loading page before seeing a document asking
them to commit money. Render has no free tier for background workers at all, so
Starter is the floor. Check the current rate on Render's pricing page — the
plans and specifications are in their compute-plans documentation.

### Creating them

1. **Render Dashboard → New → Blueprint**, point it at this repository.
2. Render reads `render.yaml` and proposes both services plus the
   `soneet-shared` environment group.
3. It prompts for every `sync: false` value — this is the only time it asks, so
   have the rotated credentials ready. Anything you skip can be added later
   under **Environment → soneet-shared**.
4. Approve. The portal builds, runs `alembic upgrade head` as its pre-deploy
   step, then starts.

### Why the portal owns migrations

`preDeployCommand: alembic upgrade head` is on the portal **only**. Two services
running Alembic concurrently would race for the same lock, and the loser's
deploy fails for a reason that looks like a database fault.

Render runs a pre-deploy after the build and before the start command. A failed
migration fails the deploy, and the previous version keeps serving — so a broken
migration cannot leave a half-migrated schema with new code on top of it.

The worker never migrates. That is what stops it consuming jobs against a schema
it does not understand: if the portal's migration has not run, the worker's own
code is simply older or newer, and the key-agreement and startup checks catch
the mismatch.

**Rollback.** Alembic downgrades exist for every migration in this project, but
the safe rollback for a *deployment* is Render's own: roll the service back to
the previous deploy, which leaves the schema forward-compatible because every
portal migration is additive. Take a `pg_dump` before the first production
migration regardless — §6 step 6.

### The portal URL

`PORTAL_BASE_URL` is what every customer's link points at. Production refuses to
start without an HTTPS value, and it cannot be known before the service exists.

1. Create the services and let the first deploy finish.
2. Copy the assigned address from the portal service's page — it looks like
   `https://soneet-portal.onrender.com`.
3. Set `PORTAL_BASE_URL` to **exactly** that origin: `https`, no trailing slash,
   no path. The approval origin check compares against it, so a mismatch makes
   every customer approval fail with a 403.
4. Redeploy both services so the worker picks it up too.

Do not start the portal with a blank or guessed value, and do not paste the
Cloudflare R2 endpoint here — they are handed to you at the same time and are
easy to confuse. The preflight refuses both.

**Moving to a custom domain later** means adding the domain in Render, then
updating `PORTAL_BASE_URL` in the shared group and redeploying. Links already
issued keep working — they are absolute URLs baked into emails already sent —
but every *new* link uses the new origin, and the origin check follows the
setting. Change it during a quiet period.

### Object storage

The R2 bucket stays **private**. The application never derives a public object
URL: price lists, logos and quotation PDFs are served through routes that check
a permission or validate a capability token first, and the accepted-quotation
PDF is fetched, hash-verified and streamed by the portal.

You therefore do **not** need R2's public development URL, a custom R2 domain,
or any CORS policy. Leave all three off — enabling them would route around every
access check the application makes.

```
STORAGE_BACKEND=s3
STORAGE_BUCKET=soneet-quotations
STORAGE_ENDPOINT_URL=https://ACCOUNT_ID.r2.cloudflarestorage.com
STORAGE_REGION=auto
STORAGE_ACCESS_KEY_ID=[rotated R2 S3 key]
STORAGE_SECRET_ACCESS_KEY=[rotated R2 S3 secret]
```

### Python version

Pinned to 3.13 in `.python-version`. Render's current default is newer, but
`requirements.txt` is verified against cp313 wheels and that is what Community
Cloud runs — matching the two removes a class of "works on the employee app,
fails on the portal" surprise. Local development on 3.14 is unaffected.

---

## 5. The worker and its heartbeat

`python -m modules.worker --loop` sweeps three queues: storage cleanups,
accepted-PDF jobs and the email outbox. Each is swept in isolation, so an
unreachable mail server does not stop PDFs being produced.

Exit codes matter here because Render restarts an exited worker:

| Code | Meaning |
|---|---|
| 0 | Clean shutdown (SIGTERM — it finishes the sweep it is in) |
| 1 | A subsystem failed; the others still ran |
| 2 | **Encryption keys disagree with the rest of the deployment.** It refuses to start, which shows up as a crash-looping service rather than silently undelivered mail |

### How the employee app knows the worker is alive

Through the **database**, not a file. The employee app, the portal and the
worker are on three different machines with no shared filesystem, so the
original `WORKER_HEALTH_FILE` signal is invisible to the page that needs it —
it would report "Not configured" forever while delivery ran perfectly, or look
healthy while the worker was dead.

The worker writes a row to `worker_heartbeats` after every sweep. The employee
app reads it. Staleness is measured against the last *successful* sweep, so a
worker looping on errors reads as stale rather than healthy, which is the
honest answer because nothing is being delivered.

| State | Meaning |
|---|---|
| **Running** | A clean sweep within `WORKER_STALE_AFTER_SECONDS` (900 by default) |
| **Degraded** | Running, but the last pass reported a subsystem failure |
| **Not running recently** | Last success is older than the threshold |
| **Not configured** | The worker has never reported against this database |

The row holds status, timestamps, a counts summary and the environment name.
No host, no path, no process identity, no error text — it is rendered on a page
a salesperson looks at.

`WORKER_HEALTH_FILE` still works and is still read as a fallback when no
database row exists. That is the local-development case, where one machine runs
everything. Do not set it on Render.

---

## 6. The initial deployment sequence

Follow this order. Each step is cheap to undo; the ones that are not come last.

1. **Rotate the exposed credentials.** The Neon role password and the
   Cloudflare R2 S3 key used during testing were exposed and must be treated as
   compromised. Reset the Neon password; delete and recreate the R2 API token.
   Do not reuse either anywhere.
2. **Create the Render project**, connected to this repository.
3. **Add the rotated secrets manually** in Render, in the `soneet-shared`
   environment group. Never in `render.yaml`, never in a file in the repository.
4. **Create both services from the Blueprint** (New → Blueprint).
5. **Confirm `EMAIL_ENABLED=false`.** Messages will queue and wait; nothing is
   lost, and nothing reaches a customer while you are still checking.
6. **Take a database backup, then run migrations once.** The portal's
   pre-deploy does this automatically on first deploy; to run it by hand:
   `pg_dump --format=custom --no-owner --file=pre-render.dump "$DATABASE_URL"`
   then `alembic upgrade head`.
7. **Run the preflight** from a Render shell on the portal service, or locally
   against production:
   `python -m scripts.preflight`
   It checks variables, the HTTPS origin, database reachability, migration head,
   the heartbeat table, an R2 write/read/delete round trip, encryption-key
   agreement, SMTP configuration shape and company readiness. It redacts
   everything and **sends no email**.
8. **Start the portal and worker.**
9. **Confirm `/health`** returns `{"status":"ok"}` on the portal's public URL.
10. **Confirm the worker heartbeat** shows **Running** on the Customer Portal
    page in the employee application. This proves both machines are talking to
    the same database.
11. **Upload a logo** in Company Settings and confirm it persists — that is the
    private R2 round trip, through the real application.
12. **Create a synthetic quotation** for a test customer.
13. **Preview and queue a test invitation** while email is still disabled.
    Preview creates no token and no outbox row; sending queues one.
14. **Inspect the outbox** in Delivery history: status Queued, correct
    recipient, correct revision.
15. **Configure SMTP** in the shared environment group.
16. **Send one test message to your own address** — set
    `EMAIL_INTERNAL_RECIPIENTS` to yourself first, and use a quotation for a
    test customer whose contact address is also yours.
17. **Confirm the whole loop**: the link opens, the figures are right, approval
    records, the confirmation and internal notice arrive, and the accepted PDF
    downloads and verifies.
18. **Complete company identity, logo and tax configuration** until the
    readiness panel shows nothing marked ⛔.
19. **Set the real internal notification recipients.**
20. **Set `EMAIL_ENABLED=true`** — only after every check above has passed.

Steps 13 and 14 before step 15 are the point: with delivery off you can inspect
exactly what would be sent, to whom, with no possibility of a customer
receiving it.

---

## 7. What to watch in the first week

| Signal | Where | What it means |
|---|---|---|
| Worker health | Customer Portal page | Anything but "Running" means delivery is stalled |
| Failed deliveries | Delivery history | A permanent failure needs a new address; a temporary one retries itself |
| "Link expired" rows | Delivery history | The resend window closed; issue a new link |
| Accepted PDFs marked "Needs attention" | Customer Portal page | The document failed to generate; the acceptance is still valid |
| Storage cleanup rows accumulating | `storage_cleanups` table | Object storage may be rejecting deletes |

Duplicate emails are possible but rare. If the mail server accepts a message and
the worker dies before recording that it did, a later sweep sends it again.
Nothing in SMTP closes that window. Queueing the same notification twice — the
far more common cause — is prevented outright.
