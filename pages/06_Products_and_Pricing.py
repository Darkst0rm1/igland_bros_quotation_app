"""Products & Pricing — catalogue, board qualities, price history and cost.

Two things this page is careful about:

* a product's **variants are its board qualities**, shown as separate rows
  everywhere, because merging them would misprice quotations;
* **price history is append-only** — the forms here supersede, never overwrite,
  and the full history stays visible.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import streamlit as st

from modules.authorization import PermissionDenied
from modules.catalogue_service import (
    PROTECTED_TIER_CODES,
    CatalogueError,
    create_price_tier,
    create_product,
    create_variant,
    set_cost,
    set_price,
    update_price_tier,
    update_product,
    update_variant,
)
from modules.constants import SUPPORTED_CURRENCIES, Perm
from modules.database import session_scope
from modules.repositories import (
    catalogue_counts,
    cost_history,
    current_prices_for_variant,
    get_effective_cost,
    get_price_tiers,
    price_history,
    search_products,
    variants_for_product,
)
from modules.session import page_header, require_page
from modules.utilities import (
    empty_frame,
    format_date,
    format_money,
    format_pack_price,
    format_quantity,
    format_piece_price,
)
from modules.validation import CostInput, PriceInput, ProductInput, VariantInput

user = require_page(Perm.PRODUCT_VIEW)
page_header("Products & Pricing", "Catalogue, board qualities, price history and cost")

can_manage_prices = user.has(Perm.PRICE_MANAGE)
can_manage_costs = user.has(Perm.COST_MANAGE)
can_view_costs = user.has(Perm.COST_VIEW)
can_edit_catalogue = user.has(Perm.PRODUCT_EDIT) and user.has(Perm.PRODUCT_CREATE)
can_manage_tiers = user.has(Perm.PRICE_MANAGE_TIERS)

with session_scope() as db:
    counts = catalogue_counts(db)
    tiers = [
        (t.code, t.name, t.min_containers, t.requires_approval, t.sort_order, t.is_active)
        for t in get_price_tiers(db, include_inactive=True)
    ]

metric_a, metric_b, metric_c = st.columns(3)
metric_a.metric("Products", counts["products"])
metric_b.metric("Variants", counts["variants"])
metric_c.metric("Price records", counts["prices"])

if counts["products"] == 0:
    st.info(
        "The catalogue is empty. Import a price list from the **Excel Import** page, "
        "run `python -m seeds.seed_catalogue_from_workbook <file.xlsx>`, or add a "
        "product by hand under **Catalogue** below."
    )

st.divider()
catalogue_tab, pricing_tab, tiers_tab = st.tabs(["Catalogue", "Prices & cost", "Price tiers"])


# --------------------------------------------------------------------------- #
# Catalogue
# --------------------------------------------------------------------------- #

with catalogue_tab:
    search_col, toggle_col = st.columns([3, 1])
    with search_col:
        term = st.text_input(
            "Search products",
            placeholder="Size, name or item number",
            label_visibility="collapsed",
        )
    with toggle_col:
        show_inactive = st.checkbox("Include inactive", value=False)

    with session_scope() as db:
        products = search_products(db, term or None, include_inactive=show_inactive)
        rows = [
            {
                "Size": product.size_label,
                "Board quality": variant.board_quality,
                "Case pack": variant.case_pack,
                "Depth": f'{format_quantity(product.depth_in)}"' if product.depth_in else "-",
                "Flute": product.flute or "-",
                "Item number": variant.variant_item_number,
                "Active": "Yes" if variant.is_active else "No",
            }
            for product in products
            for variant in product.variants
            if variant.deleted_at is None and (show_inactive or variant.is_active)
        ]
        product_count = len(products)

    st.caption(
        f"{len(rows)} variant{'s' if len(rows) != 1 else ''} across "
        f"{product_count} product{'s' if product_count != 1 else ''}"
    )

    catalogue_columns = [
        "Size", "Board quality", "Case pack", "Depth", "Flute", "Item number", "Active"
    ]
    if rows:
        st.dataframe(rows, width="stretch", hide_index=True)
    else:
        st.dataframe(empty_frame(catalogue_columns), width="stretch", hide_index=True)
        st.info("No products match that search.")

    st.caption(
        "A product is the shape; each board quality is a separate variant. The same size "
        "in two qualities is deliberately two rows — commercially they are different "
        "products and are never merged."
    )

    # ----------------------------------------------------------------- #
    # Maintenance
    # ----------------------------------------------------------------- #

    if can_edit_catalogue:
        st.divider()
        product_col, variant_col = st.columns(2)

        with session_scope() as db:
            all_products = [
                {
                    "id": p.id,
                    "item_number": p.item_number,
                    "name": p.name,
                    "size_label": p.size_label,
                    "category": p.category or "",
                    "depth_in": p.depth_in,
                    "flute": p.flute or "",
                    "material": p.material or "",
                    "finish": p.finish or "",
                    "printing_method": p.printing_method or "",
                    "is_perforated": p.is_perforated,
                    "lock_style": p.lock_style or "",
                    "units_per_bundle": p.units_per_bundle,
                    "is_active": p.is_active,
                }
                for p in search_products(db, include_inactive=True)
            ]

        # --- product -------------------------------------------------- #
        with product_col:
            st.markdown("##### Product")
            choices = ["Add a new product", *[p["size_label"] for p in all_products]]
            picked = st.selectbox("Product to edit", choices, key="product_editor_pick")
            target = next((p for p in all_products if p["size_label"] == picked), None)

            with st.form("product_editor"):
                item_number = st.text_input(
                    "Item number *", value=target["item_number"] if target else ""
                )
                size_label = st.text_input(
                    "Size label *",
                    value=target["size_label"] if target else "",
                    help='As it should read on a quotation, e.g. 12" White',
                )
                name = st.text_input("Name *", value=target["name"] if target else "")
                category = st.text_input(
                    "Category", value=target["category"] if target else "White Boxes"
                )
                dim_a, dim_b = st.columns(2)
                with dim_a:
                    depth = st.number_input(
                        "Depth (inches)",
                        min_value=0.0,
                        step=0.25,
                        value=float(target["depth_in"]) if target and target["depth_in"] else 0.0,
                    )
                    flute = st.text_input(
                        "Flute", value=target["flute"] if target else "B"
                    )
                    material = st.text_input(
                        "Material",
                        value=target["material"] if target else "",
                        placeholder="White/Kraft",
                    )
                with dim_b:
                    printing_method = st.text_input(
                        "Printing method",
                        value=target["printing_method"] if target else "",
                        placeholder="Flexographic",
                    )
                    lock_style = st.text_input(
                        "Lock style",
                        value=target["lock_style"] if target else "",
                        placeholder="No-Lock",
                    )
                    finish = st.text_input(
                        "Finish", value=target["finish"] if target else ""
                    )
                bundle_size = st.number_input(
                    "Boxes per bundle",
                    min_value=0,
                    step=1,
                    value=(
                        int(target["units_per_bundle"])
                        if target and target["units_per_bundle"]
                        else 0
                    ),
                    help=(
                        "How many boxes are strapped into one bundle of this size. "
                        "Leave at 0 if it is not settled — the bundle price and the "
                        "container estimate are then left off rather than guessed. "
                        "Neither the price list nor the capacity workbook states it."
                    ),
                )
                perf_choices = ["Not specified", "Perforated", "Non-perforated"]
                perf_index = (
                    0 if not target or target["is_perforated"] is None
                    else (1 if target["is_perforated"] else 2)
                )
                perforated = st.selectbox(
                    "Perforation", perf_choices, index=perf_index
                )
                product_active = st.checkbox(
                    "Active", value=target["is_active"] if target else True
                )
                product_saved = st.form_submit_button(
                    "Save product" if target else "Create product", type="primary"
                )

            if product_saved:
                try:
                    payload = ProductInput(
                        item_number=item_number,
                        name=name,
                        size_label=size_label,
                        category=category or None,
                        depth_in=Decimal(str(depth)) if depth else None,
                        units_per_bundle=bundle_size or None,
                        flute=flute or None,
                        material=material or None,
                        finish=finish or None,
                        printing_method=printing_method or None,
                        lock_style=lock_style or None,
                        is_perforated=(
                            None if perforated == "Not specified"
                            else perforated == "Perforated"
                        ),
                        is_active=product_active,
                    )
                    with session_scope() as db:
                        if target:
                            update_product(db, user, target["id"], payload)
                        else:
                            create_product(db, user, payload)
                except (ValueError, CatalogueError, PermissionDenied) as exc:
                    st.error(str(exc))
                else:
                    st.toast("Product saved", icon="✅")
                    st.rerun()

        # --- variant -------------------------------------------------- #
        with variant_col:
            st.markdown("##### Board-quality variant")
            if not all_products:
                st.info("Create a product first.")
            else:
                parent_label = st.selectbox(
                    "Belongs to product",
                    [p["size_label"] for p in all_products],
                    key="variant_parent_pick",
                )
                parent = next(p for p in all_products if p["size_label"] == parent_label)

                with session_scope() as db:
                    siblings = [
                        {
                            "id": v.id,
                            "variant_item_number": v.variant_item_number,
                            "board_quality": v.board_quality,
                            "case_pack": v.case_pack,
                            "num_colours": v.num_colours,
                            "moq_pieces": v.moq_pieces,
                            "spec_text_override": v.spec_text_override or "",
                            "is_active": v.is_active,
                        }
                        for v in variants_for_product(db, parent["id"])
                    ]

                variant_choices = [
                    "Add a new variant",
                    *[f"{v['board_quality']} · case {v['case_pack']}" for v in siblings],
                ]
                picked_v = st.selectbox(
                    "Variant to edit", variant_choices, key="variant_editor_pick"
                )
                v_index = variant_choices.index(picked_v) - 1
                v_target = siblings[v_index] if v_index >= 0 else None

                with st.form("variant_editor"):
                    variant_number = st.text_input(
                        "Variant item number *",
                        value=v_target["variant_item_number"] if v_target else "",
                    )
                    board_quality = st.text_input(
                        "Board quality *",
                        value=v_target["board_quality"] if v_target else "",
                        placeholder="WT110 HPFL115 KM135",
                        disabled=v_target is not None,
                        help=(
                            "Part of the variant's identity — it cannot be changed once "
                            "created, because every price and quotation line points at "
                            "this variant. Create a new variant instead."
                            if v_target else
                            "Read from the price list exactly as written."
                        ),
                    )
                    case_pack = st.number_input(
                        "Case pack *",
                        min_value=1,
                        value=v_target["case_pack"] if v_target else 50,
                        disabled=v_target is not None,
                        help="Also part of the variant's identity.",
                    )
                    colour_col, moq_col = st.columns(2)
                    with colour_col:
                        num_colours = st.number_input(
                            "Number of colours",
                            min_value=0, max_value=12,
                            value=int(v_target["num_colours"] or 0) if v_target else 0,
                        )
                    with moq_col:
                        moq_pieces = st.number_input(
                            "MOQ (pieces)",
                            min_value=0,
                            step=1000,
                            value=int(v_target["moq_pieces"] or 0) if v_target else 0,
                        )
                    spec_override = st.text_input(
                        "Specification text (optional)",
                        value=v_target["spec_text_override"] if v_target else "",
                        placeholder="White/Kraft 3-4C, Perforated / No-Lock",
                        help="Overrides the composed specification on the quotation document.",
                    )
                    variant_active = st.checkbox(
                        "Active", value=v_target["is_active"] if v_target else True,
                        key="variant_active",
                    )
                    variant_saved = st.form_submit_button(
                        "Save variant" if v_target else "Create variant", type="primary"
                    )

                if variant_saved:
                    try:
                        payload = VariantInput(
                            variant_item_number=variant_number,
                            board_quality=(
                                v_target["board_quality"] if v_target else board_quality
                            ),
                            case_pack=(
                                v_target["case_pack"] if v_target else int(case_pack)
                            ),
                            num_colours=int(num_colours) or None,
                            moq_pieces=Decimal(str(moq_pieces)) if moq_pieces else None,
                            spec_text_override=spec_override or None,
                            is_active=variant_active,
                        )
                        with session_scope() as db:
                            if v_target:
                                update_variant(db, user, v_target["id"], payload)
                            else:
                                create_variant(db, user, parent["id"], payload)
                    except (ValueError, CatalogueError, PermissionDenied) as exc:
                        st.error(str(exc))
                    else:
                        st.toast("Variant saved", icon="✅")
                        st.rerun()


# --------------------------------------------------------------------------- #
# Prices & cost
# --------------------------------------------------------------------------- #

with pricing_tab:
    with session_scope() as db:
        product_labels = {
            p.size_label: p.id for p in search_products(db, include_inactive=True)
        }

    # The page no longer stops on an empty catalogue — the maintenance forms on
    # the Catalogue tab are exactly what is needed at that point — so this tab
    # guards itself. Note this must NOT use st.stop(): tabs all execute in one
    # script run, so stopping here would leave the Price tiers tab blank.
    if not product_labels:
        st.info("Add or import a product before recording prices.")
        variants: list[dict] = []
    else:
        picked_product = st.selectbox("Product", sorted(product_labels))
        with session_scope() as db:
            variants = [
                {
                    "id": v.id,
                    "board_quality": v.board_quality,
                    "case_pack": v.case_pack,
                }
                for v in variants_for_product(db, product_labels[picked_product])
            ]

    if not variants:
        if product_labels:
            st.warning("This product has no variants.")
    else:
        variant_labels = {
            f"{v['board_quality']} · case {v['case_pack']}": v["id"] for v in variants
        }
        picked_variant = st.selectbox("Board quality", list(variant_labels))
        variant_id = variant_labels[picked_variant]

        pick_col, date_col = st.columns(2)
        with pick_col:
            currency = st.selectbox("Currency", SUPPORTED_CURRENCIES, index=0)
        with date_col:
            as_of = st.date_input("Prices as at", value=dt.date.today())

        with session_scope() as db:
            current = current_prices_for_variant(db, variant_id, as_of, currency)
            current_rows = [
                {
                    "Tier": next((row[1] for row in tiers if row[0] == code), code),
                    "Price / pack": format_pack_price(price.price_per_pack, currency),
                    "Price / piece": format_piece_price(price.price_per_piece, currency),
                    "Effective from": format_date(price.effective_from),
                    "Effective to": (
                        format_date(price.effective_to) if price.effective_to else "open"
                    ),
                }
                for code, price in current.items()
            ]
            history_rows = [
                {
                    "Tier": p.tier.name if p.tier else "-",
                    "Price / pack": format_pack_price(p.price_per_pack, p.currency),
                    "Price / piece": format_piece_price(p.price_per_piece, p.currency),
                    "Currency": p.currency,
                    "From": format_date(p.effective_from),
                    "To": format_date(p.effective_to) if p.effective_to else "open",
                    "Source": p.source_workbook_name or "manual entry",
                    "Row": str(p.source_row_no) if p.source_row_no else "-",
                    "Withdrawn": "" if p.is_active else "Yes",
                }
                for p in price_history(db, variant_id)
            ]
            standard_pack = (
                current["STANDARD"].price_per_pack if "STANDARD" in current else None
            )
            cost_now = (
                get_effective_cost(db, variant_id, as_of, currency)
                if can_view_costs else None
            )
            cost_pack_now = cost_now.cost_per_pack if cost_now else None
            cost_rows = (
                [
                    {
                        "Cost / pack": format_money(c.cost_per_pack, c.currency, decimals=4),
                        "Currency": c.currency,
                        "From": format_date(c.effective_from),
                        "To": format_date(c.effective_to) if c.effective_to else "open",
                        "Note": c.source_note or "-",
                    }
                    for c in cost_history(db, variant_id)
                ]
                if can_view_costs else []
            )

        st.markdown(f"##### In force on {format_date(as_of)}")
        if current_rows:
            st.dataframe(current_rows, width="stretch", hide_index=True)
        else:
            st.warning(
                f"No {currency} price is in force for this variant on "
                f"{format_date(as_of)}. A quotation using it would be blocked."
            )

        if can_view_costs:
            st.markdown("##### Internal cost")
            st.caption("Never appears on a customer document, in either format.")
            if cost_pack_now is not None:
                cost_col, margin_col = st.columns(2)
                cost_col.metric(
                    "Cost / pack", format_money(cost_pack_now, currency, decimals=4)
                )
                if standard_pack:
                    profit = standard_pack - cost_pack_now
                    pct = (profit / standard_pack * Decimal(100)).quantize(Decimal("0.01"))
                    margin_col.metric(
                        "Margin at standard price",
                        f"{pct}%",
                        delta=format_money(profit, currency),
                    )
                else:
                    margin_col.metric("Margin at standard price", "-")
            else:
                st.caption(
                    "No internal cost recorded, so margins are unavailable rather than "
                    "shown as zero."
                )

        with st.expander("Full price history", expanded=False):
            if history_rows:
                st.dataframe(history_rows, width="stretch", hide_index=True)
                st.caption(
                    "History is append-only. A new price closes the previous one the day "
                    "before it starts; nothing is overwritten, so a quotation always "
                    "resolves to the price that applied on its own date."
                )
            else:
                st.info("No prices recorded for this variant yet.")

        if cost_rows:
            with st.expander("Cost history", expanded=False):
                st.dataframe(cost_rows, width="stretch", hide_index=True)

        if can_manage_prices:
            st.divider()
            st.markdown("##### Record a new price")
            with st.form("new_price"):
                col_a, col_b, col_c = st.columns(3)
                with col_a:
                    tier_code = st.selectbox(
                        "Tier",
                        [row[0] for row in tiers if row[5]],
                        format_func=lambda code: next(
                            row[1] for row in tiers if row[0] == code
                        ),
                    )
                    price_currency = st.selectbox(
                        "Currency", SUPPORTED_CURRENCIES, index=0, key="price_currency"
                    )
                with col_b:
                    pack_price = st.number_input(
                        "Price per pack", min_value=0.0, step=0.01, format="%.4f"
                    )
                    piece_price = st.number_input(
                        "Price per piece (0 to derive)",
                        min_value=0.0,
                        step=0.0001,
                        format="%.4f",
                        help=(
                            "Leave at 0 to derive it from the pack price. Enter a value "
                            "when the price list states one — the two columns are "
                            "independent and can legitimately differ by a rounding unit."
                        ),
                    )
                with col_c:
                    effective_from = st.date_input(
                        "Effective from", value=dt.date.today(), key="price_effective_from"
                    )
                price_submitted = st.form_submit_button("Save price", type="primary")

            if price_submitted:
                try:
                    with session_scope() as db:
                        set_price(
                            db,
                            user,
                            PriceInput(
                                product_variant_id=variant_id,
                                price_tier_code=tier_code,
                                price_per_pack=Decimal(str(pack_price)),
                                price_per_piece=(
                                    Decimal(str(piece_price)) if piece_price > 0 else None
                                ),
                                currency=price_currency,
                                effective_from=effective_from,
                            ),
                        )
                except (ValueError, CatalogueError, PermissionDenied) as exc:
                    st.error(str(exc))
                else:
                    st.toast("Price recorded", icon="✅")
                    st.rerun()

        if can_manage_costs:
            st.markdown("##### Record an internal cost")
            st.caption(
                "Costs are entered manually and effective-dated like prices, so the "
                "margin on a historical quotation stays reproducible."
            )
            with st.form("new_cost"):
                cost_a, cost_b, cost_c = st.columns(3)
                with cost_a:
                    cost_pack = st.number_input(
                        "Cost per pack", min_value=0.0, step=0.01, format="%.4f"
                    )
                with cost_b:
                    cost_currency = st.selectbox(
                        "Currency", SUPPORTED_CURRENCIES, index=0, key="cost_currency"
                    )
                with cost_c:
                    cost_from = st.date_input(
                        "Effective from", value=dt.date.today(), key="cost_effective_from"
                    )
                note = st.text_input(
                    "Source note", placeholder="Supplier quote, works order, …"
                )
                cost_submitted = st.form_submit_button("Save cost", type="primary")

            if cost_submitted:
                try:
                    with session_scope() as db:
                        set_cost(
                            db,
                            user,
                            CostInput(
                                product_variant_id=variant_id,
                                cost_per_pack=Decimal(str(cost_pack)),
                                currency=cost_currency,
                                effective_from=cost_from,
                                source_note=note or None,
                            ),
                        )
                except (ValueError, CatalogueError, PermissionDenied) as exc:
                    st.error(str(exc))
                else:
                    st.toast("Cost recorded", icon="✅")
                    st.rerun()


# --------------------------------------------------------------------------- #
# Tiers
# --------------------------------------------------------------------------- #

with tiers_tab:
    st.dataframe(
        [
            {
                "Code": code,
                "Name": name,
                # Kept as a string: a column mixing int and "-" cannot be
                # serialised to Arrow and makes Streamlit log a conversion error.
                "Minimum containers": str(min_containers) if min_containers else "-",
                "Requires approval": "Yes" if requires_approval else "",
                "Order": str(sort_order),
                "Active": "Yes" if is_active else "No",
            }
            for code, name, min_containers, requires_approval, sort_order, is_active in tiers
        ],
        width="stretch",
        hide_index=True,
    )
    st.caption(
        "The minimum-container figure drives the quantity warnings. The selected tier "
        "always determines the price — entering fewer containers raises a warning but "
        "never silently changes the tier."
    )

    if not can_manage_tiers:
        st.info("Editing tiers requires the Pricing Administrator role.")
    else:
        st.divider()
        edit_col, add_col = st.columns(2)

        with edit_col:
            st.markdown("##### Edit a tier")
            tier_codes = [row[0] for row in tiers]
            picked_tier = st.selectbox(
                "Tier", tier_codes,
                format_func=lambda code: next(r[1] for r in tiers if r[0] == code),
            )
            row = next(r for r in tiers if r[0] == picked_tier)
            protected = row[0] in PROTECTED_TIER_CODES

            with st.form("tier_editor"):
                st.text_input(
                    "Code", value=row[0], disabled=True,
                    help=(
                        "The code is fixed. The price-list importer maps "
                        "'3 containers' columns onto it, and every historical price "
                        "points at this tier — renaming it would break both."
                    ),
                )
                tier_name = st.text_input("Name *", value=row[1])
                tier_min = st.number_input(
                    "Minimum containers (0 for none)",
                    min_value=0, max_value=999,
                    value=int(row[2] or 0),
                    help=(
                        "Quoting fewer containers than this raises a warning. It never "
                        "changes the selected tier."
                    ),
                )
                tier_requires_approval = st.checkbox(
                    "Using this tier requires approval", value=bool(row[3])
                )
                tier_order = st.number_input(
                    "Display order", min_value=0, max_value=999, value=int(row[4])
                )
                tier_active = st.checkbox(
                    "Active", value=bool(row[5]), disabled=protected,
                    help=(
                        "This tier is referenced by the importer and by existing prices, "
                        "so it cannot be deactivated." if protected else None
                    ),
                )
                tier_saved = st.form_submit_button("Save tier", type="primary")

            if tier_saved:
                try:
                    with session_scope() as db:
                        update_price_tier(
                            db, user, picked_tier,
                            name=tier_name,
                            min_containers=int(tier_min) or None,
                            requires_approval=tier_requires_approval,
                            sort_order=int(tier_order),
                            is_active=True if protected else tier_active,
                        )
                except (CatalogueError, PermissionDenied) as exc:
                    st.error(str(exc))
                else:
                    st.toast("Tier saved", icon="✅")
                    st.rerun()

        with add_col:
            st.markdown("##### Add a tier")
            st.caption(
                "A new container band is picked up by the importer automatically — its "
                "header matching is generic, so a `12 containers Price/Pack` column "
                "needs no code change."
            )
            with st.form("tier_creator"):
                new_code = st.text_input(
                    "Code *", placeholder="TWELVE_CONTAINER",
                    help="Uppercase, no spaces. Cannot be changed later.",
                )
                new_name = st.text_input("Name *", placeholder="Twelve Containers")
                new_min = st.number_input(
                    "Minimum containers (0 for none)", min_value=0, max_value=999, value=0
                )
                new_requires_approval = st.checkbox(
                    "Using this tier requires approval", value=False, key="new_tier_approval"
                )
                new_order = st.number_input(
                    "Display order", min_value=0, max_value=999, value=100,
                    key="new_tier_order",
                )
                tier_created = st.form_submit_button("Create tier", type="primary")

            if tier_created:
                try:
                    with session_scope() as db:
                        create_price_tier(
                            db, user,
                            code=new_code,
                            name=new_name,
                            min_containers=int(new_min) or None,
                            requires_approval=new_requires_approval,
                            sort_order=int(new_order),
                        )
                except (CatalogueError, PermissionDenied) as exc:
                    st.error(str(exc))
                else:
                    st.toast("Tier created", icon="✅")
                    st.rerun()
