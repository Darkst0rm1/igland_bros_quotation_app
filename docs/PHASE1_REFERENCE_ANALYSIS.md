# Phase 1 — Reference File Analysis

Both reference files were opened and parsed cell-by-cell / text-extracted. This document records
what is actually in them, because several details differ from the assumptions in the brief and
they change the data model.

---

## 1. `White Boxes B Flute Quotation.xlsx`

**Workbook shape**

| Property | Value |
|---|---|
| Sheets | exactly one: `White Boxes B Flute` |
| Used range | `A1:K46` (46 rows × 11 columns) |
| Merged ranges | 19 (title bars, section label, terms label spans) |

**Block layout** — the sheet contains **two independent price blocks**, each with its own title,
header row, data rows and terms footer:

| | Block 1 ("main") | Block 2 ("alternative quality") |
|---|---|---|
| Section marker | — | row 24, merged `A24:D24`, text `alternative quality` |
| Title bar | row 1 (`A1:D1` = "WHITE BOXES B FLUTE", `F1:K1` = "BULK") | row 25 (`A25:D25`, `F25:K25`) |
| **Header row** | **row 2** | **row 26** |
| Data rows | 3–13 (11 rows) | 27–38 (12 rows) |
| Terms footer | 15–21 | 40–46 (identical text) |

> **The header row is not row 1 and there are two of them.** The importer must scan for header
> rows rather than assume a position, and must handle N blocks per sheet. Confirmed by inspection.

**Header cells contain embedded newlines**, e.g. the literal cell value is `"Standard\nPrice/Pack"`,
`"3 containers\nPrice/Pcs"`, `"8 containers\nPrice/Pack"`. Normalization must collapse newlines
before matching.

### 1.1 Finding: "alternative quality" is a *section*, not a quality

The brief implies the alternative block is one alternative. It is not — the block silently changes
board quality partway down:

| Rows | Sizes | Board quality |
|---|---|---|
| 3–13 | 7"–18" (11 sizes, no 20") | `WT110 HPFL115 KM135` |
| 27–33 | 7"–13" (7 sizes) | `WT110 HPFL135 KM135` |
| 34–38 | 14", 15", 16", 18", 20" | `WT110 HPFL160 KM135` |

Consequences:

* The **quality column is per-row**, never per-block. The importer must read quality from column E
  on every row and must never infer it from the section label.
* `20" White` exists **only** in the alternative block, and only at `HPFL160`.
* `14"–18"` exist in both blocks but at `HPFL115` vs `HPFL160` — the alternative is *not* a uniform
  step up from the main list.

**Resulting catalogue size:** 12 distinct sizes → **12 products**, **23 product variants**
(11 × HPFL115 + 7 × HPFL135 + 5 × HPFL160), **69 price records** (23 variants × 3 tiers).

### 1.2 Finding: pack and piece prices are mutually inconsistent by ±0.0001

Every row has both a `Price/Pack` and a `Price/Pcs`. `Price/Pcs` should equal `Price/Pack ÷ 50`.
It does not, in **25 of the 69 price pairs**. Examples:

```
 8" White  HPFL115  standard : 4.49 / 50 = 0.0898   sheet says 0.0899   (-0.0001)
11" White  HPFL115  8-ctnr   : 6.32 / 50 = 0.1264   sheet says 0.1263   (+0.0001)
13" White  HPFL115  8-ctnr   : 7.79 / 50 = 0.1558   sheet says 0.1559   (-0.0001)
10" White  HPFL135  standard : 6.29 / 50 = 0.1258   sheet says 0.1259   (-0.0001)
```

The deviation goes in **both directions**, and reversing the derivation (`pcs × 50`) does not
reproduce the pack price either. Both columns are independently rounded displays of a more precise
underlying value that the workbook does not expose.

This is not a data-entry error to clean up — it is a permanent property of the source. Design impact:

1. Import **both columns verbatim**. Never derive one from the other, never "correct" one to match.
2. Store unit prices at 6 decimal places so the imported 4-dp values survive intact.
3. The brief's warning *"the entered piece price does not match pack price ÷ case pack"* must use a
   **tolerance of ± 1 rounding unit** (0.0001 on the piece price). Enforced strictly, it would fire
   on 36% of the seeded catalogue and be ignored by users within a week.
4. Quoting the same line by packs vs by pieces yields **different totals**. Each quotation line
   therefore needs an explicit `pricing_basis` (`pack` | `piece`) recording which column drove the
   money, so the figure on an issued PDF is always reproducible.

### 1.3 Commercial terms in the workbook (identical in both blocks)

| Label (col A) | Value (col B, merged B:E) |
|---|---|
| Payment Terms | Payment upon receipt. |
| Printing | Flexo Printing to be Applied. |
| Delivery Terms | FOB Çerkezköy (Türkiye) (INCOTERMS 2020) |
| Loading Notes | Shipment with 40' HC containers. Floor Loaded. |
| *(continuation)* | Containers to be filled with only three items. |
| Validity | Valid through July '26 |
| Notes | Printing plate charge is 200 USD per size per color. |

Notes:
* Çerkezköy renders as mojibake (`�erkezk�y`) through the default extraction path — the importer
  and all file reads must be explicitly UTF-8, and seed data must carry the correct Turkish glyphs.
* "Valid through July '26" is historical. It seeds a **term template**, never the default
  `valid_until` on a new quotation (which comes from `default_quote_validity_days`).
* `200 USD per size per colour` seeds the configurable `printing_plate_rate`.
* No currency is stated anywhere in the price table. Only the plate note says USD. Prices are
  treated as **USD** and the currency is an explicit column on every price record.
* Case pack is `50` on all 23 variants; depth `2"` and flute `B` on all. These are still stored
  per-variant, not assumed.

---

## 2. `ECOPAC_Quotation_QT-2026-0728_BunzlPizzaBox_1.pdf`

Single page, US Letter portrait (612 × 792 pt). Structure extracted:

**Header block**
```
E C O P A C   P R O D U C T S   I N C .        (letter-spaced wordmark)
QUOTATION                                       (document title)
Food Service Packaging Solutions                (tagline)
Markham, Ontario, Canada                        (location)
```

**Metadata block** (right-aligned label/value pairs)
`Quotation No.` · `Quote Date` · `Project` · `Distributor` · `Prepared for`

**Product table** — 6 columns, 11 rows:

| ITEM | DESCRIPTION | PACK SIZE | MOQ (UNITS) | PRICE / CASE (CAD) | SPEC |
|---|---|---|---|---|---|
| Pizza Box | 8 x 8 x 2 | 50 / case | 10,000 | $13.31 | White/Kraft 3-4C, Perforated / No-Lock |

**Terms** — 7 free-text lines: MOQ basis, 14–16 week lead time from artwork + structural approval,
30-day validity, freight/releases TBD, CDN funds subject to raw-material increases, pricing subject
to final art/structural approval and POs for boxes *and tooling*, ±10% underrun/overrun.

**Signature row** — three inline labels: `Signature` · `Name` · `Date` (printed, not electronic).

**Footer** — `Thank you for considering ECOPAC.` + a tagline line.

### 2.1 What this changes in the PDF generator

* The reference has **no subtotal / tax / grand-total block and no line quantities** — it is a rate
  card, not a priced order. Our PDF needs both, so the totals block is original design,
  not a copy.
* Its money column is *price per case in CAD*; ours is *price per pack and per piece in USD,
  FOB Çerkezköy*. The product table must therefore be **column-configurable** rather than fixed —
  the column set is a company setting, defaulting to
  `Item · Description · Size · Pack size · Qty (packs) · Price/pack · Price/pcs · Line total`.
* `SPEC` is a single dense string (`White/Kraft 3-4C, Perforated / No-Lock`). That maps to a
  **composed** value from `material · num_colours · perforated · lock_style` on the variant, with a
  per-line editable override.
* Only 11 rows, so pagination is untested by the reference. Our generator must still do
  repeating table headers and controlled row splitting.
* No page numbering, no confidentiality wording, no revision marker. All three are additions the
  brief requires.
* Branding, colour and the wordmark treatment are ECOPAC's and are **not** reproduced. The document
  gets an original layout driven by `company_settings` (logo, colours, footer text, signature block).

---

## 3. Environment facts checked on this machine

| Item | Found |
|---|---|
| Python | 3.14.5 (system) |
| streamlit | 1.58.0 — `st.navigation` / `st.Page`, `st.dialog`, `st.data_editor`, `st.toast` all available |
| pandas | 3.0.3 |
| sqlalchemy, pydantic, alembic, reportlab, passlib | **not installed** → project venv required |
| Project home | `C:\Users\melgh\Documents\GitHub\` (alongside WMS, delivery_risk_dashboard, …) |

Two consequences carried into the architecture:

* **pandas 3.0** — empty-frame behaviour differs from 2.x (`Series.map` on an empty datetime series
  raises on the float64 cast; `DataFrame.apply(axis=1)` on an empty frame returns an empty
  *DataFrame*, not Series). Import previews and report filters routinely produce empty frames, so
  every such call site is guarded.
* **WeasyPrint is rejected** in favour of **ReportLab** — WeasyPrint needs GTK/Pango/Cairo native
  DLLs on Windows, which this machine has no admin rights to install. ReportLab is a pure-Python
  wheel and renders the required layout (repeating headers, split control, watermark) natively.
