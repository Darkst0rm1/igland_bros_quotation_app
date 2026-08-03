"""Query layer. Every SELECT in the application is built here.

Nothing outside this module writes SQL, which keeps three things in one place:
the soft-delete predicate, the effective-date logic for prices and costs, and
the scope predicates that make ``quote.view_own`` a WHERE clause rather than a
post-filter.

All parameters are bound through SQLAlchemy expressions — there is no string
interpolation into SQL anywhere in this file.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session, selectinload

from modules.constants import PriceTierCode
from modules.models import (
    Customer,
    CustomerAddress,
    CustomerContact,
    PriceTier,
    Product,
    ProductCost,
    ProductPrice,
    ProductVariant,
    User,
)


#: Escape character for LIKE patterns. Must be passed to every ``ilike`` call
#: as ``escape=LIKE_ESCAPE``: escaping the term is useless on its own, because
#: without an explicit ESCAPE clause the backslash is just another character
#: and "50%" would still match everything.
LIKE_ESCAPE = "\\"


def _like(term: str) -> str:
    """Escape a user-supplied search term for LIKE.

    Without this, searching for "50% cotton" matches every row and an
    underscore matches any character.
    """
    escaped = (
        term.replace(LIKE_ESCAPE, LIKE_ESCAPE * 2)
        .replace("%", f"{LIKE_ESCAPE}%")
        .replace("_", f"{LIKE_ESCAPE}_")
    )
    return f"%{escaped}%"


# --------------------------------------------------------------------------- #
# Customers
# --------------------------------------------------------------------------- #

def customer_query(include_deleted: bool = False) -> Select[tuple[Customer]]:
    stmt = select(Customer)
    if not include_deleted:
        stmt = stmt.where(Customer.deleted_at.is_(None))
    return stmt.order_by(Customer.company_name)


def search_customers(
    session: Session,
    term: str | None = None,
    status: str | None = None,
    assigned_user_id: int | None = None,
    limit: int = 200,
) -> list[Customer]:
    stmt = customer_query()
    if term:
        pattern = _like(term.strip())
        stmt = stmt.where(
            or_(
                Customer.company_name.ilike(pattern, escape=LIKE_ESCAPE),
                Customer.customer_number.ilike(pattern, escape=LIKE_ESCAPE),
                Customer.notes.ilike(pattern, escape=LIKE_ESCAPE),
                Customer.id.in_(
                    select(CustomerContact.customer_id).where(
                        or_(
                            CustomerContact.name.ilike(pattern, escape=LIKE_ESCAPE),
                            CustomerContact.email.ilike(pattern, escape=LIKE_ESCAPE),
                        )
                    )
                ),
            )
        )
    if status:
        stmt = stmt.where(Customer.status == status)
    if assigned_user_id is not None:
        stmt = stmt.where(Customer.assigned_sales_user_id == assigned_user_id)
    return list(session.execute(stmt.limit(limit)).scalars())


def get_customer(session: Session, customer_id: int) -> Customer | None:
    """Load a customer with its contacts and addresses.

    ``populate_existing`` forces the loaded state to be refreshed rather than
    returning whatever the identity map already holds. The session factory sets
    ``expire_on_commit=False`` — which Streamlit's render-after-write flow needs
    — so without this a customer fetched after a write in the same session comes
    back with a stale contacts or addresses collection.
    """
    return session.execute(
        select(Customer)
        .where(Customer.id == customer_id, Customer.deleted_at.is_(None))
        .options(
            selectinload(Customer.contacts),
            selectinload(Customer.addresses),
        )
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()


def customer_number_exists(
    session: Session, number: str, exclude_id: int | None = None
) -> bool:
    stmt = select(Customer.id).where(func.lower(Customer.customer_number) == number.lower())
    if exclude_id is not None:
        stmt = stmt.where(Customer.id != exclude_id)
    return session.execute(stmt.limit(1)).first() is not None


def next_customer_number(session: Session, prefix: str = "CUST-") -> str:
    """Suggest the next number in a simple prefixed sequence.

    A suggestion only — customer numbers are frequently dictated by an existing
    accounting system, so the field stays editable and uniqueness is enforced by
    the constraint rather than by this function.
    """
    highest = 0
    numbers = session.execute(
        select(Customer.customer_number).where(Customer.customer_number.like(f"{prefix}%"))
    ).scalars()
    for number in numbers:
        tail = number[len(prefix):]
        if tail.isdigit():
            highest = max(highest, int(tail))
    return f"{prefix}{highest + 1:04d}"


def find_default_address(
    session: Session, customer_id: int, address_type: str
) -> CustomerAddress | None:
    """Query the default address of a type.

    Prefer this inside services over :func:`default_address`, which reads an
    already-loaded relationship and can be stale within a session that has just
    written to it.
    """
    stmt = (
        select(CustomerAddress)
        .where(
            CustomerAddress.customer_id == customer_id,
            CustomerAddress.address_type == address_type,
        )
        .order_by(CustomerAddress.is_default.desc(), CustomerAddress.id)
    )
    return session.execute(stmt).scalars().first()


def default_address(customer: Customer, address_type: str) -> CustomerAddress | None:
    """Read the default address from an already-loaded customer. UI use only."""
    matching = [a for a in customer.addresses if a.address_type == address_type]
    if not matching:
        return None
    return next((a for a in matching if a.is_default), matching[0])


def primary_contact(customer: Customer) -> CustomerContact | None:
    active = [c for c in customer.contacts if c.is_active]
    if not active:
        return None
    return next((c for c in active if c.is_primary), active[0])


# --------------------------------------------------------------------------- #
# Products & variants
# --------------------------------------------------------------------------- #

def product_query(include_inactive: bool = False) -> Select[tuple[Product]]:
    stmt = select(Product).where(Product.deleted_at.is_(None))
    if not include_inactive:
        stmt = stmt.where(Product.is_active.is_(True))
    return stmt.order_by(Product.name)


def search_products(
    session: Session,
    term: str | None = None,
    include_inactive: bool = False,
    limit: int = 500,
) -> list[Product]:
    stmt = product_query(include_inactive).options(selectinload(Product.variants))
    if term:
        pattern = _like(term.strip())
        stmt = stmt.where(
            or_(
                Product.name.ilike(pattern, escape=LIKE_ESCAPE),
                Product.item_number.ilike(pattern, escape=LIKE_ESCAPE),
                Product.size_label.ilike(pattern, escape=LIKE_ESCAPE),
            )
        )
    return list(session.execute(stmt.limit(limit)).scalars())


def get_product(session: Session, product_id: int) -> Product | None:
    return session.execute(
        select(Product)
        .where(Product.id == product_id, Product.deleted_at.is_(None))
        .options(selectinload(Product.variants))
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()


def get_variant(session: Session, variant_id: int) -> ProductVariant | None:
    return session.execute(
        select(ProductVariant)
        .where(ProductVariant.id == variant_id, ProductVariant.deleted_at.is_(None))
        .options(selectinload(ProductVariant.product))
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()


def find_variant_by_natural_key(
    session: Session,
    size_label: str,
    depth: str | None,
    flute: str | None,
    case_pack: int,
    board_quality: str,
) -> ProductVariant | None:
    """Resolve a workbook row to exactly one variant.

    The key is ``(size, depth, flute, case pack, board quality)``. Board quality
    is compared case-insensitively but is otherwise matched literally — two
    qualities that differ by a single grammage are different products, not
    variations in spelling.
    """
    stmt = (
        select(ProductVariant)
        .join(Product, ProductVariant.product_id == Product.id)
        .where(
            func.lower(Product.size_label) == size_label.casefold(),
            func.lower(ProductVariant.board_quality) == board_quality.casefold(),
            ProductVariant.case_pack == case_pack,
            Product.deleted_at.is_(None),
            ProductVariant.deleted_at.is_(None),
        )
    )
    if flute:
        stmt = stmt.where(func.lower(Product.flute) == flute.casefold())
    return session.execute(stmt).scalars().first()


def find_product_by_size(session: Session, size_label: str) -> Product | None:
    return session.execute(
        select(Product).where(
            func.lower(Product.size_label) == size_label.casefold(),
            Product.deleted_at.is_(None),
        )
    ).scalars().first()


def variants_for_product(session: Session, product_id: int) -> list[ProductVariant]:
    return list(
        session.execute(
            select(ProductVariant)
            .where(
                ProductVariant.product_id == product_id,
                ProductVariant.deleted_at.is_(None),
            )
            .order_by(ProductVariant.board_quality)
        ).scalars()
    )


def catalogue_counts(session: Session) -> dict[str, int]:
    """Products / variants / prices, for the import summary and seed checks."""
    return {
        "products": session.execute(
            select(func.count(Product.id)).where(Product.deleted_at.is_(None))
        ).scalar_one(),
        "variants": session.execute(
            select(func.count(ProductVariant.id)).where(
                ProductVariant.deleted_at.is_(None)
            )
        ).scalar_one(),
        "prices": session.execute(select(func.count(ProductPrice.id))).scalar_one(),
    }


# --------------------------------------------------------------------------- #
# Price tiers
# --------------------------------------------------------------------------- #

def get_price_tiers(session: Session, include_inactive: bool = False) -> list[PriceTier]:
    stmt = select(PriceTier).order_by(PriceTier.sort_order)
    if not include_inactive:
        stmt = stmt.where(PriceTier.is_active.is_(True))
    return list(session.execute(stmt).scalars())


def get_price_tier(session: Session, code: str) -> PriceTier | None:
    return session.execute(
        select(PriceTier).where(PriceTier.code == code)
    ).scalar_one_or_none()


def price_tier_map(session: Session) -> dict[str, PriceTier]:
    return {t.code: t for t in get_price_tiers(session, include_inactive=True)}


# --------------------------------------------------------------------------- #
# Prices — effective-date resolution
# --------------------------------------------------------------------------- #

def _effective_on(stmt: Select, model, on_date: dt.date) -> Select:  # noqa: ANN001
    """Restrict to rows in force on ``on_date``.

    An open ``effective_to`` means "still current". This predicate is the single
    definition of what "the price on a date" means, so a quotation raised today
    and the same quotation reprinted next year resolve identically.
    """
    return stmt.where(
        model.effective_from <= on_date,
        or_(model.effective_to.is_(None), model.effective_to >= on_date),
    )


def get_effective_price(
    session: Session,
    product_variant_id: int,
    price_tier_code: str,
    on_date: dt.date | None = None,
    currency: str = "USD",
) -> ProductPrice | None:
    """The price in force for a variant and tier on a given date.

    Returns ``None`` rather than falling back to another tier or another
    currency. A missing price is a condition the caller must surface to the
    user (``PRICE_MISSING``), not something to paper over with a substitute
    that would quietly misprice a quotation.
    """
    on_date = on_date or dt.date.today()
    stmt = (
        select(ProductPrice)
        .join(PriceTier, ProductPrice.price_tier_id == PriceTier.id)
        .where(
            ProductPrice.product_variant_id == product_variant_id,
            PriceTier.code == price_tier_code,
            ProductPrice.currency == currency.upper(),
            ProductPrice.is_active.is_(True),
        )
        .order_by(ProductPrice.effective_from.desc(), ProductPrice.id.desc())
    )
    return session.execute(_effective_on(stmt, ProductPrice, on_date)).scalars().first()


def get_latest_price(
    session: Session,
    product_variant_id: int,
    price_tier_code: str,
    currency: str = "USD",
) -> ProductPrice | None:
    """The most recent price regardless of date.

    Used to tell "this price has expired" apart from "this variant was never
    priced" — the two need different messages and only one of them is an
    approval trigger.
    """
    return session.execute(
        select(ProductPrice)
        .join(PriceTier, ProductPrice.price_tier_id == PriceTier.id)
        .where(
            ProductPrice.product_variant_id == product_variant_id,
            PriceTier.code == price_tier_code,
            ProductPrice.currency == currency.upper(),
        )
        .order_by(ProductPrice.effective_from.desc(), ProductPrice.id.desc())
    ).scalars().first()


def get_standard_price(
    session: Session,
    product_variant_id: int,
    on_date: dt.date | None = None,
    currency: str = "USD",
) -> ProductPrice | None:
    """The standard-tier price, for the savings calculation."""
    return get_effective_price(
        session, product_variant_id, PriceTierCode.STANDARD.value, on_date, currency
    )


def price_history(
    session: Session, product_variant_id: int, price_tier_code: str | None = None
) -> list[ProductPrice]:
    stmt = (
        select(ProductPrice)
        .options(selectinload(ProductPrice.tier))
        .where(ProductPrice.product_variant_id == product_variant_id)
        .order_by(ProductPrice.effective_from.desc(), ProductPrice.id.desc())
    )
    if price_tier_code:
        stmt = stmt.join(PriceTier, ProductPrice.price_tier_id == PriceTier.id).where(
            PriceTier.code == price_tier_code
        )
    return list(session.execute(stmt).scalars())


def current_prices_for_variant(
    session: Session,
    product_variant_id: int,
    on_date: dt.date | None = None,
    currency: str = "USD",
) -> dict[str, ProductPrice]:
    """``{tier_code: price}`` for every tier in force on a date."""
    on_date = on_date or dt.date.today()
    found: dict[str, ProductPrice] = {}
    for tier in get_price_tiers(session):
        price = get_effective_price(
            session, product_variant_id, tier.code, on_date, currency
        )
        if price is not None:
            found[tier.code] = price
    return found


def supersede_price(price: ProductPrice, new_effective_from: dt.date) -> None:
    """Close an existing price the day before its replacement takes effect.

    Mutating ``effective_to`` is the *only* change the immutability guard allows
    on a price row; amounts and identity are frozen. This is the mechanism the
    whole append-only history rests on.

    ``is_active`` is deliberately left alone. The two flags mean different
    things and conflating them breaks history:

    * ``effective_from``/``effective_to`` — *when* this price applied. A
      superseded price is still the correct answer for a date inside its range,
      which is what lets a quotation raised in June reprint correctly in
      December.
    * ``is_active`` — whether the row should be considered at all. Reserved for
      a price entered in error and withdrawn, which must never resolve for any
      date.

    Clearing ``is_active`` on supersession would make every historical price
    invisible to :func:`get_effective_price` and silently reprice old
    quotations at whatever is current.
    """
    price.effective_to = new_effective_from - dt.timedelta(days=1)


# --------------------------------------------------------------------------- #
# Costs
# --------------------------------------------------------------------------- #

def get_effective_cost(
    session: Session,
    product_variant_id: int,
    on_date: dt.date | None = None,
    currency: str = "USD",
) -> ProductCost | None:
    on_date = on_date or dt.date.today()
    stmt = (
        select(ProductCost)
        .where(
            ProductCost.product_variant_id == product_variant_id,
            ProductCost.currency == currency.upper(),
        )
        .order_by(ProductCost.effective_from.desc(), ProductCost.id.desc())
    )
    return session.execute(_effective_on(stmt, ProductCost, on_date)).scalars().first()


def cost_history(session: Session, product_variant_id: int) -> list[ProductCost]:
    return list(
        session.execute(
            select(ProductCost)
            .where(ProductCost.product_variant_id == product_variant_id)
            .order_by(ProductCost.effective_from.desc(), ProductCost.id.desc())
        ).scalars()
    )


def supersede_cost(cost: ProductCost, new_effective_from: dt.date) -> None:
    cost.effective_to = new_effective_from - dt.timedelta(days=1)


def margin_inputs(
    session: Session,
    product_variant_id: int,
    on_date: dt.date | None = None,
    currency: str = "USD",
) -> Decimal | None:
    """Cost per pack in force on a date, or ``None`` when none is recorded.

    ``None`` propagates all the way to the UI as an absent margin rather than a
    zero one — see ``calculation_engine.safe_margin_pct``.
    """
    cost = get_effective_cost(session, product_variant_id, on_date, currency)
    return cost.cost_per_pack if cost else None


# --------------------------------------------------------------------------- #
# Users
# --------------------------------------------------------------------------- #

def active_users(session: Session) -> list[User]:
    return list(
        session.execute(
            select(User)
            .where(User.is_active.is_(True), User.deleted_at.is_(None))
            .order_by(User.employee_name)
        ).scalars()
    )


def sales_users(session: Session) -> list[User]:
    """Users who can own a quotation.

    Filtered by the permission rather than by role name, so a custom role that
    grants ``quote.create`` appears here without this function needing to know
    about it.
    """
    from modules.constants import Perm
    from modules.models import Permission, Role, RolePermission, UserRole

    return list(
        session.execute(
            select(User)
            .where(
                User.is_active.is_(True),
                User.deleted_at.is_(None),
                User.id.in_(
                    select(UserRole.user_id)
                    .join(Role, Role.id == UserRole.role_id)
                    .join(RolePermission, RolePermission.role_id == Role.id)
                    .join(Permission, Permission.id == RolePermission.permission_id)
                    .where(Permission.code == Perm.QUOTE_CREATE.value)
                ),
            )
            .order_by(User.employee_name)
        ).scalars()
    )
