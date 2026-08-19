# Credential rotation — runbook

Every credential this application holds, in the order that keeps the lights on.

**You perform every change on this page yourself.** Nothing here is automated,
and nothing here should be pasted into Claude, ChatGPT or any other assistant —
not to "check the format", not to have a URL assembled. A secret read by a
third party is a secret that needs rotating again. Copy from the provider
straight into the Render or Streamlit panel and nowhere else.

Two consoles receive the new values:

| Console | Path | Effect of saving |
|---|---|---|
| **Render** | Dashboard → Environment Groups → `soneet-shared` → Environment Variables → **Edit** | Redeploys `soneet-portal` and `soneet-worker` automatically |
| **Streamlit** | share.streamlit.io → the employee app → **⋮** → Settings → **Secrets** | Reboots the app automatically |

The employee app and the two Render services must agree. They share one
database, one object store and — critically — one set of email payload keys.

**Which console needs which secret:**

| Secret | Render | Streamlit |
|---|:--:|:--:|
| `DATABASE_URL` | yes | yes |
| `STORAGE_ACCESS_KEY_ID` / `STORAGE_SECRET_ACCESS_KEY` | yes | yes |
| `SECRET_KEY` | yes | yes |
| `EMAIL_PAYLOAD_KEYS` / `EMAIL_PAYLOAD_KEY_VERSION` | yes | yes |
| `SMTP_PASSWORD` | yes | no — the worker sends; the app only queues |

> **Write booleans as quoted strings in Streamlit.** `EMAIL_ENABLED = "true"`,
> not `true`. Streamlit does not promote bare booleans to the environment, and
> that is what made delivery appear switched off while the panel said otherwise.

---

## The order, and why

Each step below is **additive first, revoke last**: create the new credential,
put it in both consoles, prove it works, and only then withdraw the old one.
That is what keeps downtime near zero — at no point is the only valid
credential one that the running services do not yet have.

`SECRET_KEY` is the exception and goes last, because it cannot be rotated
additively.

---

## 1. Neon database credential

Zero-downtime path: add a **second role** rather than resetting the existing
one. Resetting in place invalidates the old password the instant you click, and
both services are down until you finish pasting.

1. **Neon Console** → your project → **Roles** → **New Role**.
   Name it something you can tell apart, e.g. `soneet_app_2026_08`.
2. Grant it the same rights as the current role. In the **SQL Editor**:
   ```sql
   GRANT ALL PRIVILEGES ON DATABASE neondb TO soneet_app_2026_08;
   GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO soneet_app_2026_08;
   GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO soneet_app_2026_08;
   ALTER DEFAULT PRIVILEGES IN SCHEMA public
     GRANT ALL ON TABLES TO soneet_app_2026_08;
   ```
3. **Dashboard → Connection Details** → select the new role → copy the
   connection string. Use the **pooled** endpoint, as now.
4. Paste into **Render** `DATABASE_URL`, save. Wait for both services to go
   green.
5. Paste the same value into **Streamlit** `DATABASE_URL`, save, let it reboot.
6. **Verify** before revoking — see §6. Both services must be serving.
7. **Neon → Roles → delete the old role.** Only now.

Rollback: the old role still exists until step 7. Put the old string back.

---

## 2. Cloudflare R2

1. **Cloudflare Dashboard** → **R2** → **Manage R2 API Tokens** → **Create API
   Token**. Permission **Object Read & Write**, scoped to the
   `soneet-quotations` bucket.
2. Copy the **Access Key ID** and **Secret Access Key**. The secret is shown
   once.
3. **Render** → `STORAGE_ACCESS_KEY_ID` and `STORAGE_SECRET_ACCESS_KEY` → save.
4. **Streamlit** → the same two keys → save.
5. **Verify** — see §6. A quotation PDF must render.
6. **Cloudflare → R2 API Tokens → revoke the old token.** Only now.

`STORAGE_ENDPOINT_URL` does not change; it carries the account id, not a
credential.

---

## 3. SMTP password (GoDaddy)

Render only. The employee app queues messages; the worker is what opens a
socket.

1. **GoDaddy Email & Office Dashboard** → the `Sales@noorgrup.com` mailbox →
   **Manage** → change the password.
2. **Render** → `SMTP_PASSWORD` → save. Both services redeploy.
3. **Verify** — send one test message and watch it reach `SENT` (§6).

GoDaddy has no additive path: the old password dies when you set the new one.
Expect a gap of a few minutes. Anything queued in that window is retried
automatically — `EMAIL_MAX_ATTEMPTS` is 6 — so nothing is lost, it just waits.

Do this when nothing urgent is in the outbox.

---

## 4. `EMAIL_PAYLOAD_KEYS` — additive

An invitation waiting in the outbox carries the customer's real link, sealed
under this key. Replace the key outright and every queued message becomes
undeliverable. So **add** a version and keep the old one.

1. Generate a key **on your own machine**:
   ```
   python -c "from modules.secret_box import generate_key; print(generate_key())"
   ```
2. The current value looks like `v1:<key>`. The new value is **both**, comma
   separated, old first:
   ```
   v1:<existing key>,v2:<new key>
   ```
3. Set `EMAIL_PAYLOAD_KEYS` to that combined value in **Render** and
   **Streamlit**. Leave `EMAIL_PAYLOAD_KEY_VERSION` at `v1` for now, and save
   both.
4. Once both are green, set `EMAIL_PAYLOAD_KEY_VERSION` to `v2` in **both**.
   New messages now seal under v2; anything queued under v1 still opens.
5. Wait out `EMAIL_PAYLOAD_TTL_HOURS` (72h) **and** confirm the outbox holds no
   row older than that, then drop `v1:` from the list in both consoles.

Both processes compare a fingerprint of this key at startup and refuse to run
if they disagree — the worker exits 2 and crash-loops rather than silently
delivering nothing. If that happens, the two consoles are out of step; make
them match.

---

## 5. `SECRET_KEY` — planned maintenance window

**This invalidates every issued customer link.** It derives the opaque line
references customers submit, so every link already in a customer's inbox stops
working the moment it changes. You have accepted this.

Before you start, list what will need replacing:

```sql
SELECT q.quote_number, q.revision_no, q.status
FROM quotations q
WHERE q.deleted_at IS NULL
  AND q.is_current_revision
  AND q.status IN ('SENT', 'APPROVED');
```

Then:

1. Announce the window. Nothing customer-facing works reliably during it.
2. Generate the key **on your own machine**:
   ```
   python -c "import secrets; print(secrets.token_urlsafe(48))"
   ```
3. Set `SECRET_KEY` in **Render**, save, wait for green.
4. Set the same value in **Streamlit**, save, let it reboot.
5. **Verify** — see §6.
6. For each quotation on the list above that still needs customer access:
   Customer Portal → pick the quotation → **Resend**. That revokes the dead link
   and mints a replacement. Do **not** use Retry — it pushes the same dead link
   again.

Only reissue links for quotations a customer actually still needs to open.
Accepted and closed quotations do not need one.

---

## 6. Verification, after every step

1. **Render** → both services **Deployed**, no crash loop.
2. **Render → soneet-worker → Logs** → `sweep complete: …` still arriving every
   ~30s. A worker whose key disagrees exits 2 instead.
3. **Streamlit app** loads past the startup screen. If configuration did not
   arrive, that screen names what it found — key names only, never values.
4. **Customer Portal → Why is delivery switched off?** (administrators only)
   shows `EMAIL_ENABLED on` and the database you expect.
5. **Both point at the same database.** The diagnostic's `Database:` line and
   Render's `DATABASE_URL` host must match.
6. **End to end**, on a throwaway quotation and a test recipient only: send,
   then watch the Outbox row go `QUEUED → PROCESSING → SENT`.

If a step fails, put the old value back — it is still valid until the explicit
revoke step — and work out why before continuing.

---

## What is not rotated here

`STORAGE_ENDPOINT_URL`, `PORTAL_BASE_URL`, `SMTP_HOST`, `SMTP_PORT`,
`SMTP_USERNAME`, `EMAIL_FROM_ADDRESS` — configuration, not credentials. They
identify where to connect and as whom, and are not secret. Leave them alone.
