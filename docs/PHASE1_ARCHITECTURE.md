# Soneet Quotation Application — Architecture & Implementation Plan

Phase 1 deliverable. Companion document: `PHASE1_REFERENCE_ANALYSIS.md`.

Internal-only Streamlit application. Employees authenticate, build quotations, route them for
internal approval, generate branded PDFs, and record customer outcomes manually. No customer
login, no portal, no public links, no electronic signature — those are out of scope by design.

---

## 1. Layered architecture

```
┌─────────────────────────────────────────────────────────────┐
│  app.py            session bootstrap · login gate ·          │
│                    st.navigation(role-filtered page list)    │
├─────────────────────────────────────────────────────────────┤
│  pages/            presentation only — widgets, layout,      │
│                    formatting. No SQL. No money maths.       │
├─────────────────────────────────────────────────────────────┤
│  services/         quotation · pricing · approval · revision │
│                    excel_import · pdf · audit · auth         │
│                    ★ permission checks live HERE ★           │
├─────────────────────────────────────────────────────────────┤
│  calculation_engine.py   pure Decimal functions, no I/O      │
│  validation.py           Pydantic schemas, no I/O            │
├─────────────────────────────────────────────────────────────┤
│  repositories.py   all queries · all transactions            │
├─────────────────────────────────────────────────────────────┤
│  models.py         SQLAlchemy 2.0 ORM · database.py engine   │
├─────────────────────────────────────────────────────────────┤
│  PostgreSQL (prod)          SQLite (local dev / tests)       │
└─────────────────────────────────────────────────────────────┘
```

Three rules make the layering worth having:

1. **Pages never compute money and never open a session.** They call a service and render the
   result. This is what makes the calculation engine unit-testable without Streamlit.
2. **Permission checks run in the service layer, not the page.** Hiding a button is a UX courtesy;
   `require(user, Perm.QUOTE_APPROVE)` inside `approval_service.approve()` is the actual control.
   Streamlit re-runs scripts constantly and session state is client-driven — UI-only gating is not
   a security boundary.
3. **`calculation_engine` and `validation` import nothing from the project except each other.**
   No ORM, no Streamlit, no DB. Pure functions in, `Decimal` out.

### 1.1 Why these technology choices

| Decision | Choice | Reason |
|---|---|---|
| Navigation | `st.navigation` + `st.Page`, **not** magic `pages/` autodiscovery | Autodiscovery renders the sidebar before any auth code runs and cannot hide pages by role. Files keep the spec's `pages/01_Dashboard.py` names; `app.py` registers them explicitly. |
| Auth | Custom, DB-backed, `passlib[bcrypt]` | `streamlit-authenticator` keeps credentials in a YAML file, which cannot carry the `users`/`roles`/`user_roles` model, lockout counters, admin reset, or audit rows the brief requires. |
| Documents | **ReportLab** (PDF) + **python-docx** (Word), behind one shared document model | The employee chooses PDF or Word per download (§15). WeasyPrint was rejected: it needs GTK/Pango native libraries unavailable on the target Windows machine. Both chosen libraries are pure-Python wheels, verified on Python 3.14. |
| `templates/` | HTML+CSS used for the **on-screen preview** (`st.html`) only | Keeps a fast in-app preview without a second rendering engine in the print path. |
| ORM | SQLAlchemy 2.0 typed (`Mapped[...]`, `mapped_column`) | Static typing catches Decimal/None errors in a codebase this size. |
| Migrations | Alembic, `render_as_batch=True` | Batch mode keeps SQLite dev databases upgradable. |
| File storage | `modules/storage.py` adapter — `LocalStorage` \| `ObjectStorage` | Streamlit Community Cloud has an **ephemeral filesystem** (see §11.1). Uploaded price lists and generated PDFs are durable records, so they cannot live on local disk in production. |

---

## 2. Folder structure

```text
soneet_quotation_app/
├── app.py                          # entrypoint: bootstrap, login gate, st.navigation
├── pages/
│   ├── 01_Dashboard.py             06_Products_and_Pricing.py
│   ├── 02_Create_Quotation.py      07_Excel_Import.py
│   ├── 03_Quotation_History.py     08_Reports.py
│   ├── 04_Approval_Queue.py        09_Users_and_Permissions.py
│   ├── 05_Customers.py             10_Company_Settings.py
│   └── 11_Audit_Log.py
│       └── (Quotation Details is a mode of 02_Create_Quotation, read-only when locked —
│           see §9; keeping one editor avoids two divergent renderers of the same object)
├── modules/
│   ├── config.py                   # pydantic-settings, reads .env
│   ├── constants.py                # enums: statuses, permissions, tiers, charge types
│   ├── database.py                 # engine, session factory, @transactional
│   ├── models.py                   # SQLAlchemy ORM
│   ├── repositories.py
│   ├── authentication.py           # login, logout, session, lockout, password change
│   ├── authorization.py            # require(), has_perm(), role limits
│   ├── validation.py               # Pydantic v2 schemas
│   ├── calculation_engine.py       # pure Decimal maths
│   ├── pricing_service.py          # tier resolution, effective-price lookup, warnings
│   ├── quotation_service.py        # CRUD, numbering, totals recompute, status machine
│   ├── approval_service.py         # rule engine, submit/approve/reject
│   ├── revision_service.py         # snapshot, new revision, compare
│   ├── excel_importer.py           # detect → normalize → validate → diff → commit
│   ├── document_model.py           # backend-independent quotation document
│   ├── pdf_generator.py            # ReportLab renderer
│   ├── docx_generator.py           # python-docx renderer
│   ├── audit_service.py
│   ├── numbering.py                # document_sequences, format templates
│   ├── settings_service.py         # company_settings + app_settings accessors
│   └── utilities.py                # money formatting, dates, safe filenames, pandas guards
├── templates/
│   ├── quotation_template.html     # in-app preview
│   └── quotation_styles.css
├── seeds/
│   ├── seed_roles_permissions.py
│   ├── seed_term_templates.py
│   └── seed_catalogue_from_workbook.py   # runs the real importer against the reference xlsx
├── migrations/                     # alembic
├── tests/
├── docs/
├── uploads/            price_lists/ · logos/ · attachments/   (gitignored)
├── generated_quotes/                                          (gitignored)
├── requirements.txt · .env.example · alembic.ini · README.md · .gitignore
```

Deviations from the folder list in the brief, and why:

* `modules/config.py`, `constants.py`, `numbering.py`, `settings_service.py` added — the brief's
  module list has nowhere to put configuration loading, enum definitions, the quote-number
  sequence, or settings access, and burying them in `utilities.py` makes it a dumping ground.
* `seeds/` added — seed data is a listed deliverable (#22) with no home in the given structure.
* `09_Users_and_Permissions.py` … `11_Audit_Log.py` are numbered per the brief; note the brief's
  own page list has 13 entries but its folder listing has 11 files, because Login lives in `app.py`
  and Quotation Details is folded into the editor. That reconciliation is described in §9.

---

## 3. Database schema

31 tables. Conventions applied throughout: surrogate `id` PK; `created_at` / `updated_at`
(`server_default=now()`, `onupdate`); `created_by_id` / `updated_by_id` FK → `users`;
`deleted_at` nullable for soft delete on master data; explicit `Numeric` precision on every money
and quantity column — **never `Float`**.

### 3.1 Identity & access (6)

| Table | Key columns |
|---|---|
| `users` | `username` UQ, `email` UQ, `employee_name`, `password_hash`, `is_active`, `must_change_password`, `failed_login_count`, `locked_until`, `last_login_at` |
| `roles` | `code` UQ, `name`, `is_system`, **`max_discount_pct`**, **`max_quote_value`**, **`min_margin_pct`**, **`can_override_warnings`** |
| `permissions` | `code` UQ, `category`, `description` |
| `role_permissions` | (`role_id`, `permission_id`) composite PK |
| `user_roles` | (`user_id`, `role_id`) composite PK, `assigned_by_id`, `assigned_at` |
| `user_permissions` | (`user_id`, `permission_id`) composite PK, `granted_by_id`, `reason` |

Approval limits live on `roles` rather than a separate table because they are role attributes and
Finance configures them on the role screen. A user with several roles gets the **most permissive**
limit across their roles, resolved in `authorization.effective_limits(user)`.

`role_permissions` is not in the brief's table list but is required — `permissions` and `user_roles`
alone cannot express which role grants which permission.

`user_permissions` is the other addition. The brief says a Sales Employee cannot see internal costs
*"unless permission is granted"*, which is a grant to a **person**, not to their role — putting
`cost.view` on the Sales role would give it to every salesperson at once, and creating a
"Sales + costs" role would multiply roles for every future exception. A user's effective permissions
are the union of their role grants and their individual grants.

### 3.2 Configuration (4)

* `company_settings` — single row, typed columns: legal name, trading name, address lines, city,
  country, phone, email, website, tax number, `logo_path`, `signature_image_path`,
  `default_currency`, `default_tax_rate_id`, `default_quote_validity_days`,
  `quote_number_format`, `printing_plate_rate`, `pdf_page_size`, `pdf_footer_text`,
  `pdf_confidentiality_text`, `pdf_column_set` (JSON), `storage_root`, `backup_path`.
* `app_settings` — `key` UQ, `value_json`, `value_type`, `category`, `updated_by_id`. Holds the
  open-ended tunables (discount thresholds, margin thresholds, warning tolerances, feature flags)
  so adding one is a data change, not a migration.
* `tax_rates` — `code`, `name`, `rate_pct` `Numeric(9,6)`, `country`, `region`, `effective_from/to`.
* `exchange_rates` — `from_currency`, `to_currency`, `rate` `Numeric(18,8)`, `rate_date`, `source`.
  UQ (`from_currency`, `to_currency`, `rate_date`).

Nothing about the company is compiled into code. Seeds insert placeholders flagged
`is_placeholder=true`, and the Company Settings page shows a banner until they are replaced.

### 3.3 Customers (3)

`customers` (`customer_number` UQ, `company_name`, `default_currency`, `default_tax_rate_id`,
`payment_terms`, `assigned_sales_user_id`, `status`, `notes`) · `customer_contacts`
(`is_primary`, `is_active`) · `customer_addresses` (`address_type` = billing|shipping,
`is_default`, full address parts).

Addresses are normalized, but a quotation stores its own **flat text snapshot** of the addresses
used (§3.5) so that editing a customer never mutates an issued quotation.

### 3.4 Catalogue & pricing (5)

This is the part the workbook analysis drives.

```
products                 ← the physical shape
  item_number UQ, name, category, size_label ("12\" White"),
  length_in, width_in, depth_in, flute, material, finish,
  printing_method, is_perforated, lock_style, unit_of_measure,
  image_path, notes, is_active, deleted_at

product_variants         ← shape + board spec  (the thing you actually quote)
  product_id FK, variant_item_number UQ,
  board_quality  ("WT110 HPFL160 KM135"),
  case_pack, num_colours, moq_packs, moq_pieces,
  spec_text_override, is_active
  UQ (product_id, board_quality, case_pack)

price_tiers
  code UQ  STANDARD | THREE_CONTAINER | EIGHT_CONTAINER | CUSTOM
  name, min_containers (3, 8, NULL), requires_approval, sort_order

product_prices
  product_variant_id FK, price_tier_id FK,
  price_per_pack   Numeric(18,6),
  price_per_piece  Numeric(18,6),
  currency, effective_from DATE, effective_to DATE NULL,
  source_workbook_name, source_sheet_name, source_row_no,
  import_job_id FK, is_active, created_by_id, created_at
  UQ (product_variant_id, price_tier_id, currency, effective_from)
  IX (product_variant_id, price_tier_id, currency, effective_from DESC)
```

```
product_costs            ← internal cost, manually maintained (decision §15.4)
  product_variant_id FK,
  cost_per_pack  Numeric(18,6), cost_per_piece Numeric(18,6),
  currency, effective_from DATE, effective_to DATE NULL,
  source_note, created_by_id, created_at
  UQ (product_variant_id, currency, effective_from)
```

**Product vs variant split.** `product` = geometry (size, depth, flute, perforation, lock style).
`product_variant` = board quality + case pack. The brief allows either separate products or
variants; variants are correct here because `14" White` at `HPFL115` and at `HPFL160` share every
physical dimension and differ only in board spec. The workbook's natural import key is
`(size_label, depth, flute, case_pack, board_quality)` → resolves to exactly one variant.
Quotation lines reference `product_variant_id`, never `product_id`, so the two qualities can never
be conflated.

**Price history is append-only.** A new import never `UPDATE`s a price row. It sets
`effective_to = new.effective_from - 1 day` on the superseded row and inserts a new one. Lookup is
`effective_from <= :date AND (effective_to IS NULL OR effective_to >= :date)`, newest first.
Enforced by a `before_update` ORM guard on `product_prices` that rejects any change to a price
column, plus a test.

### 3.5 Quotations (7)

```
quotations
  root_quotation_id FK→quotations   (rev 0 points at itself; groups a revision family)
  quote_number, revision_no, is_current_revision, is_locked
  UQ (quote_number, revision_no)
  status, quote_date, valid_until
  customer_id, customer_contact_id
  contact_name / contact_email / contact_phone      ← snapshot
  billing_address_text / shipping_address_text      ← snapshot
  project_name, brand, distributor, sales_user_id
  currency, exchange_rate Numeric(18,8), customer_po_ref
  quote_discount_pct Numeric(9,4), quote_discount_amount Numeric(18,2)
  tax_rate_id, tax_rate_pct Numeric(9,6)
  subtotal / charges_total / tax_amount / grand_total   Numeric(18,2)
  total_cost / gross_profit  Numeric(18,2),  gross_margin_pct Numeric(9,4)
  requires_approval, internal_notes, customer_notes, issued_at

quotation_items
  quotation_id, line_no, sort_order,
  product_variant_id, product_price_id  ← FK to the exact price row used
  is_custom_product, custom_description,
  description_override, spec_text_override,
  -- snapshot of spec at quote time --
  size_label, depth_in, flute, board_quality, case_pack, printing_method,
  num_colours, moq_packs,
  price_tier_id, pricing_basis  ('pack' | 'piece'),
  quantity_packs Numeric(18,3), quantity_pieces Numeric(18,3),
  container_count Numeric(18,3),
  price_per_pack / price_per_piece Numeric(18,6),
  is_custom_price, custom_price_reason,
  line_discount_pct Numeric(9,4), line_discount_amount Numeric(18,2),
  gross_line_total / net_line_total Numeric(18,2),
  unit_cost_per_pack Numeric(18,6), line_cost_total Numeric(18,2),
  customer_remarks, internal_remarks

quotation_charges     charge_type, description, quantity, rate, amount,
                      currency, exchange_rate, is_taxable, is_customer_visible,
                      internal_note, source ('manual' | 'plate_calculator')
quotation_terms       quotation_id, term_template_id NULL, section, sort_order,
                      title, body_text, is_customer_visible
term_templates        code UQ, section, title, body_text, is_default, sort_order, version
quotation_revisions   root_quotation_id, revision_no, quotation_id,
                      snapshot_json, previous_snapshot_json,
                      previous_total, new_total, change_reason,
                      previous_pdf_attachment_id, new_pdf_attachment_id, changed_by_id
approvals             quotation_id, requested_by_id, requested_at,
                      approver_id, decided_at, decision, comments,
                      rejection_reason, override_reason, triggered_rules_json
```

`product_price_id` on the line is the immutability hinge: an issued quotation resolves back to the
exact historical price row, and the denormalized `price_per_pack` / `price_per_piece` on the line
mean it prints correctly even if that row were ever archived.

### 3.6 Operations (7)

`customer_response_logs` (date_sent, sent_by, send_method, contact, response, response_date,
loss_reason, competitor, follow_up_date, notes) · `attachments` (polymorphic `entity_type` /
`entity_id`, `stored_path`, `sha256`, `content_type`, `size_bytes`, `is_customer_visible`) ·
`import_jobs` · `import_rows` · `audit_logs` · `document_sequences` · (plus `tax_rates`,
`exchange_rates` counted in §3.2).

`document_sequences` (`scope_key` UQ e.g. `QUOTE:2026`, `last_value`) is the 28th table and is not
in the brief's list. It is required: deriving the next quote number from `MAX(quote_number)` races
under concurrent users. Allocation takes a row lock (`SELECT … FOR UPDATE` on PostgreSQL) inside
the same transaction that inserts the quotation.

`audit_logs` carries `username_snapshot` alongside `user_id` so the trail survives a user being
deleted, and `old_value_json` / `new_value_json` / `reason` / `session_id`.

---

## 4. Calculation rules

Every monetary value is `Decimal`. `float` is banned in the money path; a test asserts no
`Float` column exists in `models.py`.

**Rounding is `ROUND_HALF_UP`, set explicitly.** Python's default is `ROUND_HALF_EVEN`, which
rounds 0.125 → 0.12 and does not match invoice arithmetic. A module-level `Context` is applied and
tested.

**Precision bands**

| Class | Stored | Displayed |
|---|---|---|
| Unit prices, unit costs | `Numeric(18,6)` | pack 4 dp · piece 4 dp |
| Quantities (packs, pieces, containers) | `Numeric(18,3)` | as entered |
| Percentages | `Numeric(9,4)` | 2 dp |
| Exchange rate | `Numeric(18,8)` | 6 dp |
| **All line and quotation money** | **`Numeric(18,2)`** | 2 dp |

**Quantization points** — rounding happens at exactly these steps and nowhere else, which is what
makes the PDF columns foot:

```
qty_pieces  = qty_packs × case_pack                                → Q3
gross_line  = qty_packs  × price_per_pack     (basis = 'pack')     → Q2
            = qty_pieces × price_per_piece    (basis = 'piece')    → Q2
line_disc   = gross_line × line_discount_pct / 100                 → Q2
            (or the operator's explicit amount, already Q2)
net_line    = gross_line − line_disc                               exact
subtotal    = Σ net_line                                           exact
quote_disc  = subtotal × quote_discount_pct / 100                  → Q2
charges     = Σ charge.amount   (each Q2)                          exact
tax_base    = (subtotal − quote_disc) + Σ taxable charge amounts
tax         = tax_base × tax_rate_pct / 100                        → Q2
grand_total = subtotal − quote_disc + charges + tax                exact
```

Because every addend is already 2 dp, no total needs re-rounding and no displayed column can
disagree with its printed sum.

**Savings** (`savings_per_pack = standard_price_per_pack − selected_price_per_pack`,
`total_savings = savings_per_pack × qty_packs` → Q2) resolves "standard" from the *same variant,
same currency, same effective date*. If no standard price exists at that date, savings are `None`
and simply omitted — not shown as zero.

**Margin** — `gross_profit = net_line_total − line_cost_total`;
`gross_margin_pct = gross_profit / net_selling_price × 100`;
`markup_pct = gross_profit / total_cost × 100`. Both return `None` when the denominator is zero,
never `Decimal('Infinity')` and never a `ZeroDivisionError`.

**Plate charge** — `n_sizes × n_colours × n_designs × plate_rate`, rate from
`company_settings.printing_plate_rate` (seeded 200.00 USD). Zero when *existing plate available*
is ticked. Output is routed by the operator to one of: a `quotation_charges` row (customer-visible),
a `quotation_charges` row flagged internal-only, or an addition to a line's cost.

**Currency conversion** is applied only where a charge is entered in a currency other than the
quotation currency: `amount_in_quote_currency = amount × exchange_rate` → Q2. Prices themselves are
never auto-converted; a price record in a different currency is simply not offered for that
quotation.

---

## 5. Pricing-tier rules

`price_tiers.min_containers` (3 for THREE_CONTAINER, 8 for EIGHT_CONTAINER, NULL otherwise) drives
the warnings declaratively — no `if tier == ...` chains, and a future twelve-container tier is a
data row.

**The selected tier is authoritative. Quantity changes never re-select a tier** — they only raise
warnings. This is an explicit rule in the brief and is enforced by keeping tier selection out of
every quantity `on_change` handler.

`pricing_service.evaluate_warnings(quotation)` returns typed `PriceWarning(code, severity, message,
line_no, overridable)`:

| Code | Trigger | Severity |
|---|---|---|
| `TIER_CONTAINERS_SHORT` | tier `min_containers` = N, quotation container total < N | warning |
| `PRICE_EXPIRED` | chosen price row's `effective_to` < quote_date | blocking → approval |
| `PRICE_MISSING` | no active price for variant/tier/currency at quote_date | blocking |
| `PIECE_PACK_MISMATCH` | `abs(price_per_pack / case_pack − price_per_piece) > 0.0001` | info |
| `CUSTOM_PRICE_BELOW_FLOOR` | custom price < standard × `(1 − max_custom_discount_pct)` | blocking → approval |
| `BELOW_MOQ` | `quantity_pieces < variant.moq_pieces` | warning |
| `DUPLICATE_LINE` | same variant + tier appears twice | warning |
| `MIX_LIMIT` | distinct variants > `max_items_per_container` (seeded 3) | info |

The `PIECE_PACK_MISMATCH` tolerance of one rounding unit is the direct consequence of
§1.2 in the analysis — 25 of the 69 seeded price pairs deviate by exactly ±0.0001, and a
zero-tolerance check would fire on more than a third of the catalogue.

**Container-count scope — stated assumption.** The brief says "three-container pricing is selected
but fewer than three containers are entered" without saying whether that is per line or per
quotation. Commercially, a three-container price is earned by the *order*, so the check sums
`container_count` across all lines on the quotation. This is exposed as
`app_settings['tier_container_scope']` (`quotation` | `line`, default `quotation`) so it can be
flipped without a code change if the intended reading is per line.

Managers with `can_override_warnings` can clear any `overridable` warning by supplying a reason,
which is written to `approvals.override_reason` and to the audit log.

---

## 6. Authentication

**Login** — username or email + password → `passlib` bcrypt verify (cost 12, configurable). On
success: reset `failed_login_count`, set `last_login_at`, write `LOGIN` audit row, populate
`st.session_state['auth']` with `{user_id, username, roles, permissions, session_id, login_at,
last_seen_at}`.

**Failed logins** — increment counter; at `max_failed_logins` (default 5) set
`locked_until = now + lockout_minutes` (default 15). The failure message is identical for unknown
user, wrong password and locked account, so the form is not a username oracle. Every attempt is
audited. A constant-time dummy hash verify runs for unknown usernames so response timing does not
leak account existence.

**Session timeout** — `last_seen_at` refreshed on each script run; `app.py` compares against
`SESSION_TIMEOUT_MINUTES` (default 60) before rendering anything and logs out on expiry. Sessions
are server-side Streamlit state; nothing sensitive is placed in URL query params.

**Password rules** — minimum length, complexity and reuse-block are `app_settings`. Change-password
requires the current password. Admin reset sets a temporary password and forces
`must_change_password`, which short-circuits navigation to the change-password screen. Passwords
never appear in audit rows, logs, or exception traces.

**Disabled users** — `is_active = false` blocks login and invalidates existing sessions on the next
script run (the user record is re-checked, not trusted from session state).

**The page gate**, in `app.py`, before any page module is loaded:

```
bootstrap settings → is authenticated? → no  → render login only
                                       → yes → is_active? session fresh?
                                              must_change_password?
                                              → build page list filtered by permission
                                              → st.navigation(...).run()
```

---

## 7. Permission model

Permissions are string codes in `constants.Perm`, granted to roles, resolved to a flat set at
login. `authorization.require(perm)` raises `PermissionDenied` and is called **inside services**.

| Permission code | Sales | Manager | Finance | Pricing Admin | Sys Admin |
|---|:--:|:--:|:--:|:--:|:--:|
| `quote.create` | ● | ● | | | ● |
| `quote.edit_own_draft` | ● | ● | | | ● |
| `quote.edit_any_draft` | | ● | | | ● |
| `quote.view_own` | ● | ● | ● | | ● |
| `quote.view_team` | | ● | ● | | ● |
| `quote.view_all` | | | ● | | ● |
| `quote.submit_for_approval` | ● | ● | | | ● |
| `quote.approve` | | ● | | | ● |
| `quote.reject` | | ● | | | ● |
| `quote.return_for_revision` | | ● | | | ● |
| `quote.override_warning` | | ● | | | ● |
| `quote.approve_custom_price` | | ● | ● | | ● |
| `quote.generate_pdf` | ● | ● | ● | | ● |
| `quote.update_status` | ● | ● | | | ● |
| `quote.create_revision` | ● | ● | | | ● |
| `quote.cancel` | | ● | | | ● |
| `quote.export` | ● | ● | ● | | ● |
| `cost.view` | ○ | ● | ● | | ● |
| `margin.view` | ○ | ● | ● | | ● |
| `customer.view` / `.create` / `.edit` | ● | ● | ● | | ● |
| `customer.delete` | | ● | | | ● |
| `product.view` | ● | ● | ● | ● | ● |
| `product.create` / `.edit` | | | | ● | ● |
| `price.view` | ● | ● | ● | ● | ● |
| `price.manage` | | | | ● | ● |
| `price.import` | | | | ● | ● |
| `price.manage_tiers` | | | | ● | ● |
| `cost.manage` | | | ● | ● | ● |
| `plate_rate.manage` | | | ● | ● | ● |
| `tax.manage` | | | ● | | ● |
| `fx.manage` | | | ● | | ● |
| `approval_limits.manage` | | | ● | | ● |
| `terms.manage_templates` | | ● | ● | | ● |
| `report.view` | ● | ● | ● | ● | ● |
| `report.view_all` | | ● | ● | | ● |
| `user.manage` / `role.manage` | | | | | ● |
| `settings.manage` | | | | | ● |
| `audit.view_own` | ● | ● | ● | ● | ● |
| `audit.view_all` | | ● | ● | | ● |

● granted · ○ grantable per user (the brief's "unless permission is granted" for internal costs) ·
blank denied.

Two rules are enforced in code, not just by the matrix:

* **Self-approval is impossible.** `approval_service.approve()` rejects
  `approval.requested_by_id == approver.id` even for a System Administrator. A permission grant can
  never route around it.
* **Ownership narrows permission.** `quote.edit_own_draft` is checked *and* `quotation.sales_user_id
  == user.id` *and* `status == DRAFT`. Scope predicates live in `authorization.scope_filter()`, which
  returns the SQLAlchemy criterion applied to every quotation query — so "view own" is a WHERE
  clause, not a post-filter that could be bypassed by a stale ID in session state.

---

## 8. Status machine, approval and immutability

```
                    ┌──────────────── Revision Required ◄──┐
                    ▼                                      │
  Draft ──submit──► Pending Approval ──approve──► Approved ─┼──► Sent to Customer
    ▲                    │                          │      │         │
    │                    └──reject──► Rejected      │      │         ├──► Accepted
    └────────────────────────────────  Internally ──┘      │         ├──► Lost
                                                            │         └──► Revision Required
  any non-terminal ──► Cancelled            Approved/Sent ──► Expired (valid_until passed)
```

Transitions are a single table in `constants.py`; `quotation_service.change_status()` is the only
writer, and it rejects anything not in the table. A note is **mandatory** for
`Rejected Internally`, `Revision Required`, `Lost`, `Cancelled`.

**Approval triggers** (evaluated by `approval_service.evaluate(quotation, user)` and stored on the
approval row as `triggered_rules_json`): custom price used · manual price override · line or
quotation discount above the user's effective `max_discount_pct` · gross margin below
`min_margin_pct` · grand total above `max_quote_value` · payment terms beyond the customer's agreed
terms · an expired price used · a warning override requested. If none fire, `requires_approval` is
false and Draft → Approved is direct.

**PDF release gate** — `pdf_generator.generate(quotation, draft=False)` calls
`approval_service.assert_release_allowed()` first. If approval is required and not granted, a
final PDF cannot be produced; only a **DRAFT-watermarked** copy is available, and it is not stored
as a revision artefact.

**Immutability** — a quotation becomes `is_locked` when it is issued (first non-draft PDF). From
that moment:

1. An ORM `before_update` guard rejects writes to a locked `quotations` row or any of its children.
2. Editing routes to `revision_service.create_revision()`, which deep-copies the quotation into a
   new row with `revision_no + 1`, flips `is_current_revision`, and writes a `quotation_revisions`
   row holding **both** snapshots, both totals, both PDF attachment IDs, and the change reason.
3. Previous revisions and their PDFs are never modified or deleted. The comparison view diffs the
   two `snapshot_json` blobs field-by-field and line-by-line.

Snapshots are full JSON of the quotation with its items, charges and terms — deliberately
independent of the live tables, so a later schema change cannot alter what an issued quotation said.

---

## 9. Navigation

`app.py` builds the page list after login and filters it by permission, so a user never sees an
entry they cannot open — and the service layer still refuses if they get there anyway.

| Sidebar group | Page | Required permission |
|---|---|---|
| Quotations | Dashboard | *(any authenticated)* |
| | Create Quotation | `quote.create` |
| | Quotation History | `quote.view_own` |
| | Approval Queue | `quote.approve` |
| Master data | Customers | `customer.view` |
| | Products & Pricing | `product.view` |
| | Excel Import | `price.import` |
| Insight | Reports | `report.view` |
| Administration | Users & Permissions | `user.manage` |
| | Company Settings | `settings.manage` |
| | Audit Log | `audit.view_own` |

**Quotation Details** (the brief's page 5) is rendered by `02_Create_Quotation.py` in read-only
mode, reached from history via `?quote_id=`. One renderer, two modes: a separate detail page would
duplicate the entire line-item, charges and terms layout and the two copies would drift. Read-only
mode is forced whenever `is_locked` is true or the user lacks edit permission on that quotation.

**Login** (the brief's page 1) is not a registered page — it is the gate in `app.py`, since a
registered login page would appear in the sidebar and be navigable away from.

Two implementation notes discovered while building this, both of which cost a debugging cycle and
are easy to reintroduce:

* **`st.rerun()` is only ever called from the top level of `main()`.** Calling it from inside an
  `st.columns`, `st.form` or `st.sidebar` context unwinds the script without closing that
  container, and the following run then reports the login form as nested inside itself. The
  render functions therefore *return* a flag and `main()` performs the rerun.
* **`app.py` guards its entry with `if __name__ == "__main__":`.** Streamlit runs the entrypoint
  with `__name__ == "__main__"`, so the app behaves normally, but the module stays importable —
  otherwise importing it (from a test, or any tooling) executes the whole script into a bare
  Streamlit context and corrupts the global element stack.

The permission filtering itself lives in `visible_page_specs(user)`, a pure function over the page
table, with `build_navigation` mapping its result to `st.Page` objects. `st.Page` returns a
half-initialised object when constructed outside a script run, so keeping the decision separate is
what makes the role filtering testable at all.

---

## 10. Excel import design

Pipeline: **detect → normalize → validate → diff → preview → commit (one transaction)**.

1. **Detect sheets** — list sheet names, operator picks one.
2. **Detect header rows** — scan every row; a row is a header if ≥ 4 cells normalize to known
   tokens. On the reference workbook this finds rows **2 and 26**, confirming the header is not at
   row 1 and there is more than one.
3. **Detect blocks** — each header row starts a block that runs until the first row with an empty
   `Product` cell. Block label = nearest preceding merged single-cell text row (`alternative
   quality` for the second block), else `main`. Labels are recorded on `import_rows` for the audit
   summary only — **they never determine board quality**, which is read per row from the Quality
   column (see analysis §1.1).
4. **Normalize headers** — lowercase, strip, collapse embedded newlines and runs of whitespace,
   then match. Identity columns are a direct map; price columns go through a regex so future tiers
   need no code change:

   ```
   ^(?P<tier>standard|(?P<n>\d+)\s*containers?)\s*price\s*/\s*(?P<basis>pack|pcs|piece)s?$

   "Standard\nPrice/Pack"     → standard_price_per_pack
   "Standard\nPrice/Pcs"      → standard_price_per_piece
   "3 containers\nPrice/Pack" → three_container_price_per_pack
   "3 containers\nPrice/Pcs"  → three_container_price_per_piece
   "8 containers\nPrice/Pack" → eight_container_price_per_pack
   "8 containers\nPrice/Pcs"  → eight_container_price_per_piece
   ```

5. **Validate each row** through a Pydantic model — required fields present, `case_pack` a positive
   integer, prices parseable as `Decimal` and > 0, quality non-empty. Failures produce an
   `import_rows` record with `status='error'` and do not stop the run.
6. **Diff** on the natural key `(size_label, depth, flute, case_pack, board_quality)`:
   *new variant* → CREATE · *existing, price changed* → UPDATE (supersede + insert) ·
   *existing, price identical* → SKIP · *same key twice in one file* → duplicate error.
7. **Preview** — a table of every row with its proposed action, colour-coded, with per-row
   Create/Update/Skip override and a summary line. Nothing is written yet.
8. **Commit** — one transaction: create `import_jobs`, write all `import_rows`, upsert variants,
   supersede old prices (`effective_to = effective_from − 1 day`), insert new `product_prices`
   stamped with `import_job_id` and `source_row_no`. Any exception rolls the whole thing back and
   the job is marked `failed` with the error text.
9. **Store the workbook** under `uploads/price_lists/{job_id}_{safe_name}.xlsx` with its SHA-256, so
   any historical price is traceable to the exact file and row it came from.
10. **Terms footer** rows (Payment Terms, Printing, Delivery Terms, Loading Notes, Validity, Notes)
    are extracted and shown in the summary as *suggested* term-template updates. They are never
    applied automatically — the Validity line in the reference workbook is a stale date.

Effective date defaults to today, is operator-editable, and is validated as not earlier than the
newest existing `effective_from` for the affected variants.

**Seeding uses this exact importer** against the reference workbook rather than a hand-written
fixture, which means the import path is exercised on every fresh database and the seeded catalogue
is guaranteed to match what a real import produces: 12 products, 23 variants, 69 prices.

---

## 11. Configuration & deployment target

**Target confirmed: Streamlit Community Cloud.** That decision has consequences the local-server
plan did not, and they land in Phase 1 rather than Phase 5.

### 11.1 What Community Cloud changes

| Constraint | Consequence |
|---|---|
| **Ephemeral filesystem** — the container is rebuilt on every deploy, dependency change and wake-from-sleep | `uploads/` and `generated_quotes/` become *caches only*. Uploaded price-list workbooks, generated PDFs, logos and attachments must go to object storage. `attachments.stored_path` holds an object key, not a local path. |
| **No local database** | PostgreSQL must be hosted. `DATABASE_URL` points at a managed instance; connections are pooled and the engine is created with `pool_pre_ping=True` because idle connections get dropped. |
| **No shell access** | Alembic cannot be run on the host. Migrations are applied **from a developer machine against the hosted database** as an explicit deploy step. `app.py` performs a read-only version check on startup and refuses to run against a database whose Alembic revision does not match the code, rather than silently auto-migrating. |
| **Apps are public by default** | This is the single most important item — see §12.1. |
| **Container sleeps when idle** | First request after sleep is a cold start. Acceptable internally; no design change beyond keeping startup work minimal. |
| **Secrets come from `st.secrets`, not `.env`** | `config.py` reads `st.secrets` first, falls back to environment/`.env` for local development, so one code path serves both. |
| **Dependencies install from `requirements.txt` on a Linux image** | Every version is pinned. Note the local machine has pandas 3.0.3 and Python 3.14 — the pins make the deployed environment match rather than drift. |

Recommended hosting: **Supabase**, which provides managed PostgreSQL *and* S3-compatible object
storage as one dependency with one set of credentials. Neon (Postgres) + Cloudflare R2 (storage) is
an equally good split alternative. Because `storage.py` is an adapter over a four-method interface
(`put`, `get`, `url_for`, `delete`), the choice is not load-bearing and can change later without
touching any caller.

ReportLab is kept even though Community Cloud runs Linux (where WeasyPrint would install): local
development happens on Windows, and a PDF engine that only works in production is a PDF engine
nobody can test.

### 11.2 Settings layers

```
st.secrets / .env        infrastructure + secrets      never in the repo, never in audit rows
       ↓
company_settings         branding, defaults            one row, edited in the UI
       ↓
app_settings             tunable thresholds            key/value, edited in the UI
```

```
APP_ENV=development                 DATABASE_URL=sqlite:///./soneet.db
SECRET_KEY=                         SESSION_TIMEOUT_MINUTES=60
BCRYPT_ROUNDS=12                    MAX_FAILED_LOGINS=5
LOCKOUT_MINUTES=15                  LOG_LEVEL=INFO
STORAGE_BACKEND=local               # local | s3
STORAGE_BUCKET=                     STORAGE_ENDPOINT_URL=
STORAGE_ACCESS_KEY_ID=              STORAGE_SECRET_ACCESS_KEY=
LOCAL_STORAGE_ROOT=./uploads        GENERATED_QUOTES_DIR=./generated_quotes
MAX_UPLOAD_MB=10                    ALLOWED_UPLOAD_EXTENSIONS=.xlsx,.xls,.png,.jpg,.pdf
TZ=Europe/Istanbul
```

Local development runs `STORAGE_BACKEND=local` + SQLite and needs no cloud account at all.
Production sets `STORAGE_BACKEND=s3` + `postgresql+psycopg://…` in `st.secrets`. Only
`.env.example` is committed; `.env`, `.streamlit/secrets.toml`, `uploads/`, `generated_quotes/`
and `*.db` are gitignored.

---

## 12. Security

Password hashing (bcrypt, per-password salt) · role-based authorization enforced in services ·
ownership scope as SQL predicates · parameterized queries only (SQLAlchemy expressions throughout;
no f-string SQL) · upload extension **and** magic-byte validation with a size cap · uploads stored
under a generated `{id}_{slugified_name}` filename, never the client-supplied one, outside the
static path · generated PDFs served through `st.download_button` after a permission check, never by
URL · file paths validated against the storage root to block traversal · audit rows for every
mutating action · identical login failure messages and constant-time verification.

Known limitation, stated rather than glossed over: Streamlit has no CSRF token and no per-page HTTP
authorization layer. The mitigation is that every mutation goes through a service that re-checks
permission and ownership from the database, so a forged or replayed client state cannot authorize
an action.

### 12.1 Community Cloud is a public host — this needs a deliberate decision

**Streamlit Community Cloud apps are served on the public internet at a guessable
`*.streamlit.app` URL, and are public by default.** For an application that holds customer
contact details, cost data, margins and every quotation the company has ever priced, the
login form is then the only thing between the open internet and that data.

Three things follow, and the first is not optional:

1. **Set the app to private and maintain the viewer allowlist.** Community Cloud restricted apps
   authenticate viewers by email at the platform edge, before the Streamlit process is reached.
   That edge — not the in-app login form — is the real perimeter. The in-app roles then do what
   they are for: deciding what each authenticated employee may *do*. Offboarding an employee means
   removing them from **both** the allowlist and `users`.
2. **Everything in `st.secrets` is a production credential on a shared platform.** Use a database
   role scoped to this application's schema only, and storage keys scoped to one bucket. Rotate on
   any team change.
3. **Rate-limiting is not available.** The account lockout in §6 (5 attempts → 15 minutes) is
   therefore the only brute-force control in the application, which is why it is implemented in
   Phase 1 rather than deferred.

If the viewer allowlist is not acceptable operationally — for example if quotations must be
reachable from shared machines without individual platform accounts — then Community Cloud is the
wrong host for this data and a private-network deployment should be reconsidered. Flagging it now
because it is a five-minute decision today and a migration later.

---

## 13. Test plan

`pytest` against an in-memory SQLite database, factory fixtures, no Streamlit in the test path
(which is exactly why the calculation and pricing logic is not in the pages).

| Area | Representative assertions |
|---|---|
| Rounding | `ROUND_HALF_UP` is active; 0.125 → 0.13; no `Float` column exists in `models.py` |
| Quantity conversion | packs → pieces at case pack 50; fractional packs; zero |
| Line maths | gross, % discount, explicit-amount discount, net; both `pricing_basis` values |
| Totals | subtotal = Σ net lines; taxable vs non-taxable charges; grand total foots to the penny |
| Tier selection | each tier resolves the right price columns; changing quantity never changes tier |
| Warnings | each of the 8 codes fires and does not false-fire; ±0.0001 tolerance passes the 25 known-inconsistent workbook rows and still catches a real mismatch |
| Plate charge | sizes × colours × designs × rate; zero when an existing plate is flagged |
| Margin | profit, margin %, markup %; `None` (not an exception) on zero denominators |
| Approval rules | each trigger fires; **self-approval rejected even for System Administrator**; PDF release blocked while approval is pending |
| Numbering | format template honoured; sequence increments; concurrent allocation yields no duplicate |
| Revisions | Rev 0 → Rev 1; previous revision unchanged; snapshot survives a later master-data edit |
| Immutability | writing to a locked quotation raises; editing a `product_prices` row raises; a customer rename does not alter an issued quotation's addresses |
| Header normalization | all six price headers incl. embedded newlines; unknown header rejected |
| Import — main block | 11 rows → 11 variants at HPFL115, 33 prices |
| Import — alternative block | 12 rows → 7 at HPFL135 **and 5 at HPFL160**; quality read per row, never from the section label; `20"` created only here |
| Import — full workbook | 12 products, 23 variants, 69 prices; re-import is idempotent |
| Duplicate detection | same natural key twice in one file is an error, not two variants |
| Price history | superseding sets `effective_to`; historical lookup by date returns the old price |
| Expired pricing | expired price raises `PRICE_EXPIRED` and forces approval |
| Permissions | full matrix table-driven; every service entry point rejects an unpermitted caller |
| PDF | totals in the rendered PDF equal the stored totals; no cost or margin string appears anywhere in the output bytes; DRAFT watermark present only when unapproved |
| pandas 3.0 | import preview and every report render correctly with an empty result set |

---

## 14. Phase plan

Phase 1 (this document) closes with the architecture, schema, permission matrix, calculation rules
and configuration agreed. Implementation then proceeds:

| Phase | Delivers | Definition of done |
|---|---|---|
| **1 — Foundation** | project skeleton, venv, `requirements.txt`, config, `models.py` (31 tables), initial Alembic migration, `database.py`, storage adapter, auth, authorization, calculation engine, audit service, `app.py` with the login gate and role-filtered navigation, page stubs | login works; every page stub gates by permission; calculation-engine and permission tests green |
| **2 — Master data** | customers, product catalogue + variants, price tiers, price history, **per-variant cost entry**, the Excel importer, catalogue seeding from the reference workbook | a fresh DB seeds to 12 / 23 / 69 via the real importer; re-import idempotent; import tests green |
| **3 — Quotation creation** | quotation editor, line items, tier selection, warnings, charges, plate calculator, terms selection, draft save, validation, numbering | a full draft can be built and saved; totals foot; tier warnings behave |
| **4 — Internal controls** | approval rule engine and queue, margin controls, revisions and comparison, immutability guards, ReportLab PDF, manual customer-response tracking | approval blocks PDF release; Rev 1 leaves Rev 0 byte-identical; PDF contains no cost data |
| **5 — Management** | dashboard, reports + Excel export, company settings, user management, full test suite, deployment and backup documentation | all tests green; deployment and restore both rehearsed |

Each phase closes with: files created, database changes, run instructions, test instructions,
and an explicit list of what is still incomplete.

---

## 15. Quotation export — PDF and Word

Requirement added 2026-08-03: an employee chooses the format at download time. Both formats carry
the same content; neither is a second-class output.

**One document model, two renderers.** `document_model.py` turns a quotation into a
backend-independent structure — header block, customer block, line table, totals, selected terms,
signature block, footer. `pdf_generator.py` renders it with ReportLab; `docx_generator.py` renders
it with python-docx. Neither renderer reads the ORM, and neither computes money. Writing the two
outputs independently would guarantee they drift, and a Word file that disagrees with the PDF of
the same quotation number is worse than having only one format.

Renderer-specific handling, kept to the minimum:

| Concern | PDF (ReportLab) | Word (python-docx) |
|---|---|---|
| Repeating table header | `LongTable(repeatRows=1)` | `tblHeader` on the header row |
| Page breaks inside a row | `splitByRow`, `nosplit` on short rows | `cantSplit` on the row |
| DRAFT marking | diagonal canvas watermark | a full-width banner paragraph above the header — a true diagonal watermark in Word requires a VML shape in the header and is not worth the fragility |
| Page numbering | `onPage` callback | a `PAGE`/`NUMPAGES` field code in the footer |

**Two things worth being explicit about, since they affect what the business can rely on:**

1. **A Word file is editable by whoever receives it.** That is usually the point — a customer's
   buyer pastes lines into their own paperwork. It does mean a `.docx` is not evidence of what was
   sent. The **PDF is the record**: `quotation_revisions` stores the PDF as the revision artefact,
   and both files are stored with their SHA-256 so an issued document can be checked against what
   was generated.
2. **Cost and margin never reach either renderer.** The document model is built without them, so
   there is no code path in which an internal figure can appear in a customer file, in either
   format. A test asserts this against the produced bytes of both.

The approval gate applies identically: no format can be released before approval when approval is
required, and both carry the DRAFT marking until it is granted.

---

## 16. Open decisions

Assumptions made so work can proceed; each is cheap to change now and progressively less so later.

1. **Container-count scope** for tier warnings — assumed *quotation-level* (§5), switchable via
   `app_settings['tier_container_scope']`.
2. **Default currency** — assumed **USD**, from the workbook's plate note. The reference PDF is CAD,
   but it is a different company. Configurable in Company Settings.
3. ~~Deployment target~~ — **decided: Streamlit Community Cloud** (§11.1). Outstanding sub-decisions:
   which managed Postgres and object store (Supabase recommended as one dependency for both), and
   confirmation that the **private-app viewer allowlist** will be used (§12.1). The storage adapter
   means the first can be answered late; the second should be answered before go-live.
4. ~~Product cost data~~ — **decided: manual entry per variant.** Phase 2 adds a cost column to the
   Products & Pricing page, maintained by Pricing Administrator under a new `cost.manage`
   permission. Costs are effective-dated on the same append-only pattern as sell prices, so a
   historical quotation's margin stays reproducible. Margin reports read as empty until costs are
   populated — expected, not a defect.
5. **Company identity** — legal name, address, phone, email, website, tax number, logo and
   signature image are seeded as flagged placeholders and must be replaced in Company Settings
   before the first real quotation is issued.
