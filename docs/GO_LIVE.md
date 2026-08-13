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
| 1 | **A host for the worker** | Nothing is delivered without it. Streamlit Community Cloud sleeps when idle and cannot run it. See §5. |
| 2 | **SMTP credentials** | With `EMAIL_ENABLED=false` the outbox fills and waits; nothing reaches a customer. |
| 3 | **A domain for the portal** | `PORTAL_BASE_URL` must be an HTTPS origin you control. The link in every invitation points at it, and production refuses to start without it. |
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
| `WORKER_HEALTH_FILE` | Path the worker touches after each sweep. Powers the indicator in the app and any external liveness check. |

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

## 4. Hosting the customer portal

The portal is a separate FastAPI service sharing the same database. It must be
publicly reachable; the employee application must not be.

```bash
uvicorn portal.app:app --host 0.0.0.0 --port 8000 --proxy-headers
```

Requirements:

* **HTTPS, terminated in front of it.** The link *is* the credential; it must
  never travel in clear text. `PORTAL_BASE_URL` must match the public origin
  exactly, because the origin check for approvals compares against it.
* **`--proxy-headers`** when behind a reverse proxy, so client addresses are
  read correctly for rate limiting.
* **The same `SECRET_KEY`, `DATABASE_URL` and storage settings** as the
  employee app. Line references and submission nonces are derived from
  `SECRET_KEY`; a different value invalidates every issued link.
* `/health` returns `{"status":"ok"}` and nothing else. Point the load balancer
  at it.

Nginx, as an example:

```nginx
location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

Do not add caching in front of it. Every response already sets `no-store`, and a
cached quotation page would serve one customer's figures to the next visitor.

---

## 5. Hosting the worker

Three queues need sweeping without anybody pressing a button: storage cleanups,
accepted-PDF jobs, and the email outbox.

**It cannot run on Streamlit Community Cloud.** That container sleeps when idle,
which is the one thing a timer cannot tolerate. Put it on the host that runs the
portal, on a small always-on machine, or on any scheduler that can invoke it.

### As a service (preferred)

`/etc/systemd/system/soneet-worker.service`:

```ini
[Unit]
Description=Soneet quotation worker
After=network-online.target

[Service]
Type=simple
User=soneet
WorkingDirectory=/opt/soneet
EnvironmentFile=/etc/soneet/worker.env
ExecStart=/opt/soneet/.venv/bin/python -m modules.worker --loop
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
```

```bash
systemctl enable --now soneet-worker
journalctl -u soneet-worker -f
```

`Restart=always` matters: the worker exits **2** on a key mismatch, and you want
that visible in the logs rather than silently gone.

### From a scheduler

```
*/2 * * * * cd /opt/soneet && .venv/bin/python -m modules.worker --once
```

Exit status: `0` clean, `1` a subsystem failed (the others still ran), `2` the
key check refused to start. Alert on non-zero.

### Liveness

Set `WORKER_HEALTH_FILE` and check the file's modification time. It contains a
timestamp, a status word and counts — no recipients, no identifiers, nothing
that should not be read by monitoring.

```bash
find /var/run/soneet/worker-health -mmin -5 | grep -q . || echo "worker is stale"
```

Employees see the same signal in the application: **Running**, **Degraded**,
**Not running recently** or **Not configured**. They can still queue a message
while it is stale — the queue is durable — but they are told delivery may be
delayed.

---

## 6. Order of operations for the first deployment

1. Provision the database and object storage; set the shared variables from §2.
2. `alembic upgrade head`, then `python -m seeds.bootstrap`.
3. Deploy the employee application. Confirm it starts.
4. Enter company details, upload the logo, configure tax. Work through the
   readiness panel on the Customer Portal page until nothing is marked ⛔.
5. Deploy the portal behind HTTPS. Confirm `/health`.
6. Set `EMAIL_PAYLOAD_KEYS` **identically** on every host. Leave
   `EMAIL_ENABLED=false` for now.
7. Start the worker. Confirm "Email key agreement OK" in its log.
8. Send one quotation to an address you control. It queues; the worker delivers
   it; check the link opens and the figures are right.
9. Set `EMAIL_ENABLED=true` and send a real one.

Step 8 before step 9 is the point: with delivery off, a test send queues and
waits, and you can inspect the outbox row and the rendered message without a
customer receiving anything.

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
