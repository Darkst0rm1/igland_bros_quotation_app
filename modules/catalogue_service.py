"""Product, variant, price and cost operations.

The two rules this module exists to hold:

* **Prices and costs are append-only.** Nothing here updates an amount in
  place. A change supersedes the old row and inserts a new one, so a quotation
  raised last June still resolves to last June's price.
* **A variant is identified by board quality and case pack**, not by size
  alone. Two qualities of the same box are two variants and must never merge.
"""

from __future__ import annotations

import datetime as dt
import logging
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from modules.audit_service import record_audit, record_field_changes
from modules.authorization import AuthUser, require
from modules.constants import AuditAction, EntityType, Perm
from modules.models import PriceTier, Product, ProductCost, ProductPrice, ProductVariant
from modules.repositories import (
    find_variant_by_natural_key,
    get_effective_cost,
    get_latest_price,
    get_price_tier,
    supersede_cost,
    supersede_price,
)
from modules.validation import CostInput, PriceInput, ProductInput, VariantInput

log = logging.getLogger(__name__)


class CatalogueError(ValueError):
    """A catalogue operation that failed a business rule. Safe to show the user."""


# --------------------------------------------------------------------------- #
# Products
# --------------------------------------------------------------------------- #

def _product_snapshot(product: Product) -> dict[str, object]:
    return {
        "item_number": product.item_number,
        "name": product.name,
        "size_label": product.size_label,
        "category": product.category,
        "depth_in": product.depth_in,
        "flute": product.flute,
        "units_per_bundle": product.units_per_bundle,
        "material": product.material,
        "finish": product.finish,
        "is_perforated": product.is_perforated,
        "lock_style": product.lock_style,
        "printing_method": product.printing_method,
        "is_active": product.is_active,
    }


def create_product(session: Session, user: AuthUser, data: ProductInput) -> Product:
    require(user, Perm.PRODUCT_CREATE)

    clash = session.execute(
        select(Product.id).where(Product.item_number == data.item_number)
    ).first()
    if clash:
        raise CatalogueError(f"Item number {data.item_number!r} is already in use.")

    product = Product(
        **data.model_dump(exclude={"is_active"}),
        is_active=data.is_active,
        created_by_id=user.id,
        updated_by_id=user.id,
    )
    session.add(product)
    session.flush()

    record_audit(
        session, user, AuditAction.PRODUCT_CREATED, EntityType.PRODUCT, product.id,
        new_value=_product_snapshot(product),
    )
    return product


def update_product(
    session: Session, user: AuthUser, product_id: int, data: ProductInput
) -> Product:
    require(user, Perm.PRODUCT_EDIT)

    product = session.get(Product, product_id)
    if product is None or product.deleted_at is not None:
        raise CatalogueError("That product no longer exists.")

    clash = session.execute(
        select(Product.id).where(
            Product.item_number == data.item_number, Product.id != product_id
        )
    ).first()
    if clash:
        raise CatalogueError(f"Item number {data.item_number!r} is already in use.")

    before = _product_snapshot(product)
    for field, value in data.model_dump().items():
        setattr(product, field, value)
    product.updated_by_id = user.id
    session.flush()

    record_field_changes(
        session, user, AuditAction.PRODUCT_EDITED, EntityType.PRODUCT, product.id,
        before, _product_snapshot(product),
    )
    return product


# --------------------------------------------------------------------------- #
# Variants
# --------------------------------------------------------------------------- #

def create_variant(
    session: Session, user: AuthUser, product_id: int, data: VariantInput
) -> ProductVariant:
    require(user, Perm.PRODUCT_CREATE)

    product = session.get(Product, product_id)
    if product is None or product.deleted_at is not None:
        raise CatalogueError("That product no longer exists.")

    existing = find_variant_by_natural_key(
        session,
        size_label=product.size_label,
        depth=None,
        flute=product.flute,
        case_pack=data.case_pack,
        board_quality=data.board_quality,
    )
    if existing is not None:
        raise CatalogueError(
            f"{product.size_label} already has a variant in {data.board_quality} "
            f"with a case pack of {data.case_pack}."
        )

    clash = session.execute(
        select(ProductVariant.id).where(
            ProductVariant.variant_item_number == data.variant_item_number
        )
    ).first()
    if clash:
        raise CatalogueError(
            f"Variant item number {data.variant_item_number!r} is already in use."
        )

    variant = ProductVariant(
        product_id=product_id,
        **data.model_dump(),
        created_by_id=user.id,
        updated_by_id=user.id,
    )
    session.add(variant)
    session.flush()

    record_audit(
        session, user, AuditAction.PRODUCT_CREATED, EntityType.PRODUCT_VARIANT, variant.id,
        new_value={
            "product_id": product_id,
            "board_quality": variant.board_quality,
            "case_pack": variant.case_pack,
        },
    )
    return variant


def update_variant(
    session: Session, user: AuthUser, variant_id: int, data: VariantInput
) -> ProductVariant:
    """Update a variant's descriptive fields.

    Board quality and case pack are part of the variant's identity and are
    rejected here: changing them would silently re-point every historical
    quotation line and price to a different product. Create a new variant
    instead and deactivate this one.
    """
    require(user, Perm.PRODUCT_EDIT)

    variant = session.get(ProductVariant, variant_id)
    if variant is None or variant.deleted_at is not None:
        raise CatalogueError("That variant no longer exists.")

    if data.board_quality != variant.board_quality or data.case_pack != variant.case_pack:
        raise CatalogueError(
            "Board quality and case pack identify the variant and cannot be changed. "
            "Create a new variant and deactivate this one."
        )

    before = {
        "num_colours": variant.num_colours,
        "moq_packs": variant.moq_packs,
        "moq_pieces": variant.moq_pieces,
        "spec_text_override": variant.spec_text_override,
        "is_active": variant.is_active,
    }
    variant.variant_item_number = data.variant_item_number
    variant.num_colours = data.num_colours
    variant.moq_packs = data.moq_packs
    variant.moq_pieces = data.moq_pieces
    variant.spec_text_override = data.spec_text_override
    variant.notes = data.notes
    variant.is_active = data.is_active
    variant.updated_by_id = user.id
    session.flush()

    record_field_changes(
        session, user, AuditAction.PRODUCT_EDITED, EntityType.PRODUCT_VARIANT, variant.id,
        before,
        {
            "num_colours": variant.num_colours,
            "moq_packs": variant.moq_packs,
            "moq_pieces": variant.moq_pieces,
            "spec_text_override": variant.spec_text_override,
            "is_active": variant.is_active,
        },
    )
    return variant


# --------------------------------------------------------------------------- #
# Price tiers
# --------------------------------------------------------------------------- #

#: Tier codes the rest of the application resolves by name — the Excel importer
#: maps ``3 containers``/``8 containers`` columns onto them, and the pricing
#: rules treat CUSTOM as approval-triggering. They may be renamed and retuned,
#: but not renumbered or deleted.
PROTECTED_TIER_CODES = frozenset({
    "STANDARD", "THREE_CONTAINER", "EIGHT_CONTAINER", "CUSTOM",
})


def update_price_tier(
    session: Session,
    user: AuthUser,
    tier_code: str,
    *,
    name: str,
    min_containers: int | None,
    requires_approval: bool,
    sort_order: int,
    is_active: bool,
) -> PriceTier:
    """Retune a price tier.

    ``code`` is deliberately not editable. The importer resolves
    ``3 containers`` columns to ``THREE_CONTAINER`` by code, and every
    historical price row points at the tier by id — renaming the code would
    break the first and silently relabel the second.
    """
    require(user, Perm.PRICE_MANAGE_TIERS)

    tier = get_price_tier(session, tier_code)
    if tier is None:
        raise CatalogueError(f"Unknown price tier {tier_code!r}.")

    if not name or not name.strip():
        raise CatalogueError("A tier needs a name.")

    if min_containers is not None and min_containers < 0:
        raise CatalogueError("Minimum containers cannot be negative.")

    if tier.code in PROTECTED_TIER_CODES and not is_active:
        raise CatalogueError(
            f"{tier.name} is referenced by the price-list importer and by existing "
            "prices, so it cannot be deactivated."
        )

    before = {
        "name": tier.name,
        "min_containers": tier.min_containers,
        "requires_approval": tier.requires_approval,
        "sort_order": tier.sort_order,
        "is_active": tier.is_active,
    }
    tier.name = name.strip()
    tier.min_containers = min_containers or None
    tier.requires_approval = requires_approval
    tier.sort_order = sort_order
    tier.is_active = is_active
    session.flush()

    record_field_changes(
        session, user, AuditAction.SETTINGS_CHANGED, EntityType.PRODUCT_PRICE, tier.id,
        before,
        {
            "name": tier.name,
            "min_containers": tier.min_containers,
            "requires_approval": tier.requires_approval,
            "sort_order": tier.sort_order,
            "is_active": tier.is_active,
        },
        reason=f"price tier {tier.code}",
    )
    return tier


def create_price_tier(
    session: Session,
    user: AuthUser,
    *,
    code: str,
    name: str,
    min_containers: int | None = None,
    requires_approval: bool = False,
    sort_order: int = 100,
) -> PriceTier:
    """Add a tier beyond the seeded four, e.g. a twelve-container band.

    The importer picks up a matching ``<n> containers`` column automatically —
    its header regex is generic — so a new tier needs no code change to be
    importable.
    """
    require(user, Perm.PRICE_MANAGE_TIERS)

    normalised = (code or "").strip().upper().replace(" ", "_")
    if not normalised:
        raise CatalogueError("A tier needs a code.")
    if get_price_tier(session, normalised) is not None:
        raise CatalogueError(f"Price tier {normalised!r} already exists.")
    if not name or not name.strip():
        raise CatalogueError("A tier needs a name.")

    tier = PriceTier(
        code=normalised,
        name=name.strip(),
        min_containers=min_containers or None,
        requires_approval=requires_approval,
        sort_order=sort_order,
        is_active=True,
    )
    session.add(tier)
    session.flush()

    record_audit(
        session, user, AuditAction.SETTINGS_CHANGED, EntityType.PRODUCT_PRICE, tier.id,
        new_value={
            "code": tier.code, "name": tier.name,
            "min_containers": tier.min_containers,
            "requires_approval": tier.requires_approval,
        },
        reason="price tier created",
    )
    return tier


# --------------------------------------------------------------------------- #
# Prices
# --------------------------------------------------------------------------- #

def set_price(session: Session, user: AuthUser, data: PriceInput) -> ProductPrice:
    """Record a new price, superseding whatever it replaces.

    Never updates an amount in place. If a price already starts on or after the
    requested date, the operation is refused rather than rewriting it — an
    issued price is evidence of what a customer was told.
    """
    require(user, Perm.PRICE_MANAGE)

    variant = session.get(ProductVariant, data.product_variant_id)
    if variant is None or variant.deleted_at is not None:
        raise CatalogueError("That product variant no longer exists.")

    tier = get_price_tier(session, data.price_tier_code)
    if tier is None:
        raise CatalogueError(f"Unknown price tier {data.price_tier_code!r}.")

    latest = get_latest_price(
        session, variant.id, data.price_tier_code, data.currency
    )
    if latest is not None and latest.effective_from >= data.effective_from:
        raise CatalogueError(
            f"A {tier.name} price is already effective from {latest.effective_from}. "
            f"Choose a later effective date — an existing price cannot be rewritten."
        )

    if latest is not None and (
        latest.effective_to is None or latest.effective_to >= data.effective_from
    ):
        supersede_price(latest, data.effective_from)

    piece = data.price_per_piece
    if piece is None:
        piece = (data.price_per_pack / Decimal(variant.case_pack)).quantize(
            Decimal("0.000001")
        )

    price = ProductPrice(
        product_variant_id=variant.id,
        price_tier_id=tier.id,
        price_per_pack=data.price_per_pack,
        price_per_piece=piece,
        currency=data.currency,
        effective_from=data.effective_from,
        effective_to=data.effective_to,
        source_workbook_name=None,
        created_by_id=user.id,
        is_active=True,
    )
    session.add(price)
    session.flush()

    record_audit(
        session, user, AuditAction.PRICE_CHANGED, EntityType.PRODUCT_PRICE, price.id,
        old_value=(
            {"price_per_pack": latest.price_per_pack, "effective_from": latest.effective_from}
            if latest else None
        ),
        new_value={
            "variant": variant.variant_item_number,
            "tier": tier.code,
            "price_per_pack": price.price_per_pack,
            "price_per_piece": price.price_per_piece,
            "currency": price.currency,
            "effective_from": price.effective_from,
        },
    )
    return price


def withdraw_price(
    session: Session, user: AuthUser, price_id: int, reason: str
) -> ProductPrice:
    """Mark a price as entered in error.

    Distinct from superseding: a withdrawn price never resolves for any date,
    whereas a superseded one remains correct for the dates it covered. Use this
    only for a genuine data-entry mistake.
    """
    require(user, Perm.PRICE_MANAGE)
    if not reason or not reason.strip():
        raise CatalogueError("A reason is required to withdraw a price.")

    price = session.get(ProductPrice, price_id)
    if price is None:
        raise CatalogueError("That price no longer exists.")

    price.is_active = False
    session.flush()

    record_audit(
        session, user, AuditAction.PRICE_CHANGED, EntityType.PRODUCT_PRICE, price.id,
        old_value={"is_active": True}, new_value={"is_active": False}, reason=reason,
    )
    return price


# --------------------------------------------------------------------------- #
# Costs
# --------------------------------------------------------------------------- #

def set_cost(session: Session, user: AuthUser, data: CostInput) -> ProductCost:
    """Record an internal cost, superseding the previous one.

    Effective-dated on the same append-only pattern as prices, so the margin on
    a historical quotation stays reproducible. Never appears on a customer PDF.
    """
    require(user, Perm.COST_MANAGE)

    variant = session.get(ProductVariant, data.product_variant_id)
    if variant is None or variant.deleted_at is not None:
        raise CatalogueError("That product variant no longer exists.")

    latest = session.execute(
        select(ProductCost)
        .where(
            ProductCost.product_variant_id == variant.id,
            ProductCost.currency == data.currency.upper(),
        )
        .order_by(ProductCost.effective_from.desc(), ProductCost.id.desc())
    ).scalars().first()

    if latest is not None and latest.effective_from >= data.effective_from:
        raise CatalogueError(
            f"A cost is already effective from {latest.effective_from}. "
            f"Choose a later effective date."
        )

    if latest is not None and (
        latest.effective_to is None or latest.effective_to >= data.effective_from
    ):
        supersede_cost(latest, data.effective_from)

    piece = data.cost_per_piece
    if piece is None and variant.case_pack:
        piece = (data.cost_per_pack / Decimal(variant.case_pack)).quantize(
            Decimal("0.000001")
        )

    cost = ProductCost(
        product_variant_id=variant.id,
        cost_per_pack=data.cost_per_pack,
        cost_per_piece=piece,
        currency=data.currency.upper(),
        effective_from=data.effective_from,
        source_note=data.source_note,
        created_by_id=user.id,
    )
    session.add(cost)
    session.flush()

    record_audit(
        session, user, AuditAction.COST_CHANGED, EntityType.PRODUCT_COST, cost.id,
        old_value={"cost_per_pack": latest.cost_per_pack} if latest else None,
        new_value={
            "variant": variant.variant_item_number,
            "cost_per_pack": cost.cost_per_pack,
            "currency": cost.currency,
            "effective_from": cost.effective_from,
        },
    )
    return cost


def current_cost(
    session: Session,
    user: AuthUser,
    variant_id: int,
    on_date: dt.date | None = None,
    currency: str = "USD",
) -> ProductCost | None:
    """Read a cost, gated by ``cost.view``.

    The permission check is here rather than only in the page: cost data must
    not be reachable by any caller that has not been granted it.
    """
    require(user, Perm.COST_VIEW)
    return get_effective_cost(session, variant_id, on_date, currency)
