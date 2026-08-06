"""SQLAlchemy ORM models — 29 tables.

Schema reference: docs/PHASE1_ARCHITECTURE.md §3.

House rules, applied without exception:

* Money, quantities, prices and rates use :mod:`modules.database` column
  helpers, which are exact decimals on every backend. There is no ``Float``
  anywhere in this file, and a test asserts that.
* Enum columns are stored as their *value* (``values_callable``) as a VARCHAR
  with a CHECK constraint, so adding a member is a code change and not a
  PostgreSQL ``ALTER TYPE``.
* Master data is soft-deleted. Transactional records are never deleted.
* Issued quotations are immutable; see :func:`_guard_locked_quotation` at the
  bottom of this module and ``revision_service``.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
    func,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from modules.constants import (
    AddressType,
    ContainerSize,
    ContainerType,
    FreightMethod,
    Incoterm,
    LoadingMethod,
    ApprovalDecision,
    ChargeType,
    CustomerResponse,
    CustomerStatus,
    ImportJobStatus,
    ImportRowAction,
    ImportRowStatus,
    PricingBasis,
    QuotationStatus,
    SendMethod,
    TermSection,
)
from modules.database import (
    Base,
    fx_rate,
    money,
    percentage,
    quantity,
    tax_rate,
    unit_price,
)

#: JSON on SQLite, JSONB on PostgreSQL.
JSONType = JSON().with_variant(JSONB(), "postgresql")


def _enum(python_enum: type, length: int = 40) -> SAEnum:
    """VARCHAR + CHECK constraint storing the enum's value."""
    return SAEnum(
        python_enum,
        native_enum=False,
        length=length,
        validate_strings=True,
        values_callable=lambda e: [m.value for m in e],
    )


# --------------------------------------------------------------------------- #
# Mixins
# --------------------------------------------------------------------------- #

class TimestampMixin:
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class SoftDeleteMixin:
    deleted_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None


# --------------------------------------------------------------------------- #
# 1-5. Identity & access
# --------------------------------------------------------------------------- #

class User(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    employee_name: Mapped[str] = mapped_column(String(160), nullable=False)
    job_title: Mapped[str | None] = mapped_column(String(120))

    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    must_change_password: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    password_changed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    failed_login_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_until: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    last_login_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    #: Team lead, used by the ``quote.view_team`` scope.
    manager_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))

    # The join conditions are spelled out because user_roles and
    # user_permissions each carry a second FK to users (assigned_by / granted_by),
    # which makes the secondary join ambiguous if left to inference.
    roles: Mapped[list[Role]] = relationship(
        secondary="user_roles",
        primaryjoin="User.id == UserRole.user_id",
        secondaryjoin="Role.id == UserRole.role_id",
        back_populates="users",
        lazy="selectin",
    )
    #: Permissions granted to this user individually, on top of their roles.
    #: This is how a Sales Employee gets ``cost.view`` "when permission is
    #: granted" without promoting them to Sales Manager.
    extra_permissions: Mapped[list[Permission]] = relationship(
        secondary="user_permissions",
        primaryjoin="User.id == UserPermission.user_id",
        secondaryjoin="Permission.id == UserPermission.permission_id",
        lazy="selectin",
    )

    manager: Mapped[User | None] = relationship(
        remote_side="User.id", foreign_keys=[manager_id]
    )

    __table_args__ = (
        Index("ix_users_active", "is_active"),
    )


class Role(Base, TimestampMixin):
    __tablename__ = "roles"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(40), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # --- approval limits (Finance configures these) ---------------------- #
    #: NULL means "no limit". Where a user holds several roles the *most
    #: permissive* value applies; see authorization.effective_limits().
    max_discount_pct: Mapped[Decimal | None] = mapped_column(percentage())
    max_quote_value: Mapped[Decimal | None] = mapped_column(money())
    min_margin_pct: Mapped[Decimal | None] = mapped_column(percentage())
    can_override_warnings: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )

    users: Mapped[list[User]] = relationship(
        secondary="user_roles",
        primaryjoin="Role.id == UserRole.role_id",
        secondaryjoin="User.id == UserRole.user_id",
        back_populates="roles",
    )
    permissions: Mapped[list[Permission]] = relationship(
        secondary="role_permissions", back_populates="roles", lazy="selectin"
    )


class Permission(Base):
    __tablename__ = "permissions"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(60), nullable=False, unique=True)
    category: Mapped[str] = mapped_column(String(40), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)

    roles: Mapped[list[Role]] = relationship(
        secondary="role_permissions", back_populates="permissions"
    )


class RolePermission(Base):
    """Which role grants which permission.

    Not in the brief's table list, but ``permissions`` and ``user_roles`` alone
    cannot express the grant.
    """

    __tablename__ = "role_permissions"

    role_id: Mapped[int] = mapped_column(
        ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True
    )
    permission_id: Mapped[int] = mapped_column(
        ForeignKey("permissions.id", ondelete="CASCADE"), primary_key=True
    )


class UserRole(Base):
    __tablename__ = "user_roles"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    role_id: Mapped[int] = mapped_column(
        ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True
    )
    assigned_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    assigned_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))


class UserPermission(Base):
    """Per-user permission grants layered on top of role grants."""

    __tablename__ = "user_permissions"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    permission_id: Mapped[int] = mapped_column(
        ForeignKey("permissions.id", ondelete="CASCADE"), primary_key=True
    )
    granted_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    granted_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    reason: Mapped[str | None] = mapped_column(Text)


# --------------------------------------------------------------------------- #
# 6-9. Configuration
# --------------------------------------------------------------------------- #

class CompanySettings(Base, TimestampMixin):
    """Single-row company identity and document defaults.

    Nothing about the company is compiled into code. Seeded values are marked
    ``is_placeholder`` and the Company Settings page warns until they are
    replaced.
    """

    __tablename__ = "company_settings"

    id: Mapped[int] = mapped_column(primary_key=True)

    legal_name: Mapped[str] = mapped_column(String(200), nullable=False)
    trading_name: Mapped[str | None] = mapped_column(String(200))
    address_line1: Mapped[str | None] = mapped_column(String(200))
    address_line2: Mapped[str | None] = mapped_column(String(200))
    city: Mapped[str | None] = mapped_column(String(120))
    province: Mapped[str | None] = mapped_column(String(120))
    postal_code: Mapped[str | None] = mapped_column(String(40))
    country: Mapped[str | None] = mapped_column(String(120))
    phone: Mapped[str | None] = mapped_column(String(60))
    email: Mapped[str | None] = mapped_column(String(255))
    website: Mapped[str | None] = mapped_column(String(255))
    tax_number: Mapped[str | None] = mapped_column(String(80))

    logo_key: Mapped[str | None] = mapped_column(String(500))
    signature_image_key: Mapped[str | None] = mapped_column(String(500))
    signature_name: Mapped[str | None] = mapped_column(String(160))
    signature_title: Mapped[str | None] = mapped_column(String(120))

    default_currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    default_tax_rate_id: Mapped[int | None] = mapped_column(ForeignKey("tax_rates.id"))
    default_quote_validity_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=30
    )
    #: Placeholders: {YYYY} {YY} {MM} {SEQ:04d}. Validated on save.
    quote_number_format: Mapped[str] = mapped_column(
        String(80), nullable=False, default="QT-{YYYY}-{SEQ:04d}"
    )
    #: USD 200 per size per colour, from the reference workbook's Notes row.
    printing_plate_rate: Mapped[Decimal] = mapped_column(
        money(), nullable=False, default=Decimal("200.00")
    )
    printing_plate_currency: Mapped[str] = mapped_column(
        String(3), nullable=False, default="USD"
    )

    pdf_page_size: Mapped[str] = mapped_column(String(10), nullable=False, default="A4")
    pdf_footer_text: Mapped[str | None] = mapped_column(Text)
    pdf_confidentiality_text: Mapped[str | None] = mapped_column(Text)
    pdf_thank_you_text: Mapped[str | None] = mapped_column(Text)
    #: Ordered list of product-table column keys. The reference PDF quotes per
    #: case in CAD; the company quotes per pack and per piece FOB, so the column set
    #: has to be configuration rather than a fixed layout.
    pdf_column_set: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    pdf_show_acceptance_line: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )

    is_placeholder: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    updated_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))

    __table_args__ = (
        CheckConstraint("id = 1", name="singleton"),
    )


class AppSetting(Base, TimestampMixin):
    """Open-ended tunables: thresholds, tolerances, feature flags.

    Kept separate from :class:`CompanySettings` so adding a threshold is a data
    change rather than a migration.
    """

    __tablename__ = "app_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    value_json: Mapped[Any] = mapped_column(JSONType, nullable=False)
    value_type: Mapped[str] = mapped_column(String(20), nullable=False, default="string")
    category: Mapped[str] = mapped_column(String(40), nullable=False, default="general")
    description: Mapped[str | None] = mapped_column(Text)
    updated_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))


class TaxRate(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "tax_rates"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(40), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    rate_pct: Mapped[Decimal] = mapped_column(tax_rate(), nullable=False)
    country: Mapped[str | None] = mapped_column(String(120))
    region: Mapped[str | None] = mapped_column(String(120))
    effective_from: Mapped[dt.date | None] = mapped_column(Date)
    effective_to: Mapped[dt.date | None] = mapped_column(Date)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))


class ExchangeRate(Base, TimestampMixin):
    __tablename__ = "exchange_rates"

    id: Mapped[int] = mapped_column(primary_key=True)
    from_currency: Mapped[str] = mapped_column(String(3), nullable=False)
    to_currency: Mapped[str] = mapped_column(String(3), nullable=False)
    rate: Mapped[Decimal] = mapped_column(fx_rate(), nullable=False)
    rate_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    source: Mapped[str | None] = mapped_column(String(120))
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))

    __table_args__ = (
        UniqueConstraint("from_currency", "to_currency", "rate_date"),
        Index("ix_exchange_rates_lookup", "from_currency", "to_currency", "rate_date"),
    )


# --------------------------------------------------------------------------- #
# 10-12. Customers
# --------------------------------------------------------------------------- #

class Customer(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_number: Mapped[str] = mapped_column(String(40), nullable=False, unique=True)
    company_name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)

    default_currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    default_tax_rate_id: Mapped[int | None] = mapped_column(ForeignKey("tax_rates.id"))
    payment_terms: Mapped[str | None] = mapped_column(String(200))
    #: Agreed credit days. Quoting beyond this triggers approval.
    payment_terms_days: Mapped[int | None] = mapped_column(Integer)

    assigned_sales_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), index=True
    )
    status: Mapped[CustomerStatus] = mapped_column(
        _enum(CustomerStatus), nullable=False, default=CustomerStatus.PROSPECT
    )
    notes: Mapped[str | None] = mapped_column(Text)

    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    updated_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))

    contacts: Mapped[list[CustomerContact]] = relationship(
        back_populates="customer", cascade="all, delete-orphan"
    )
    addresses: Mapped[list[CustomerAddress]] = relationship(
        back_populates="customer", cascade="all, delete-orphan"
    )


class CustomerContact(Base, TimestampMixin):
    __tablename__ = "customer_contacts"

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    title: Mapped[str | None] = mapped_column(String(120))
    email: Mapped[str | None] = mapped_column(String(255))
    phone: Mapped[str | None] = mapped_column(String(60))
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    notes: Mapped[str | None] = mapped_column(Text)

    customer: Mapped[Customer] = relationship(back_populates="contacts")


class CustomerAddress(Base, TimestampMixin):
    __tablename__ = "customer_addresses"

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    address_type: Mapped[AddressType] = mapped_column(_enum(AddressType), nullable=False)
    label: Mapped[str | None] = mapped_column(String(80))
    line1: Mapped[str | None] = mapped_column(String(200))
    line2: Mapped[str | None] = mapped_column(String(200))
    city: Mapped[str | None] = mapped_column(String(120))
    province: Mapped[str | None] = mapped_column(String(120))
    postal_code: Mapped[str | None] = mapped_column(String(40))
    country: Mapped[str | None] = mapped_column(String(120))
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    customer: Mapped[Customer] = relationship(back_populates="addresses")

    def as_text(self) -> str:
        """Flatten for the snapshot stored on a quotation."""
        parts = [
            self.line1,
            self.line2,
            " ".join(p for p in (self.city, self.province, self.postal_code) if p) or None,
            self.country,
        ]
        return "\n".join(p for p in parts if p)


# --------------------------------------------------------------------------- #
# 13-17. Catalogue, pricing & cost
# --------------------------------------------------------------------------- #

class Product(Base, TimestampMixin, SoftDeleteMixin):
    """The physical shape: size, depth, flute, perforation, lock style.

    Board quality and case pack live on :class:`ProductVariant`, because in the
    reference workbook ``14" White`` exists at two different board qualities
    while being geometrically identical.
    """

    __tablename__ = "products"

    id: Mapped[int] = mapped_column(primary_key=True)
    item_number: Mapped[str] = mapped_column(String(60), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    category: Mapped[str | None] = mapped_column(String(80), index=True)

    #: As written in the source workbook, e.g. ``12" White``.
    size_label: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    length_in: Mapped[Decimal | None] = mapped_column(quantity())
    width_in: Mapped[Decimal | None] = mapped_column(quantity())
    depth_in: Mapped[Decimal | None] = mapped_column(quantity())
    flute: Mapped[str | None] = mapped_column(String(20), index=True)

    unit_of_measure: Mapped[str] = mapped_column(String(20), nullable=False, default="PACK")

    #: Boxes in one bundle. Bundle composition is a property of the box, not of
    #: the container it travels in, which is why it lives here rather than on
    #: ``ProductContainerCapacity`` — the same bundle holds the same count in a
    #: 20 ft and a 40 ft high cube.
    #:
    #: Nullable, and it stays null until someone states it. The price list
    #: counts in packs and pieces and never mentions bundles, and the capacity
    #: workbook gives bundles per container without saying what a bundle holds.
    #: Nothing that depends on it — the bundle price, the container fill — is
    #: shown for a product where it is unset, rather than being derived from a
    #: guess.
    units_per_bundle: Mapped[Decimal | None] = mapped_column(quantity())

    printing_method: Mapped[str | None] = mapped_column(String(80))
    material: Mapped[str | None] = mapped_column(String(120))
    finish: Mapped[str | None] = mapped_column(String(120))
    is_perforated: Mapped[bool | None] = mapped_column(Boolean)
    lock_style: Mapped[str | None] = mapped_column(String(80))

    notes: Mapped[str | None] = mapped_column(Text)
    image_key: Mapped[str | None] = mapped_column(String(500))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    updated_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))

    variants: Mapped[list[ProductVariant]] = relationship(
        back_populates="product", cascade="all, delete-orphan"
    )


class ProductVariant(Base, TimestampMixin, SoftDeleteMixin):
    """Shape + board specification. This is what gets quoted.

    The workbook's natural key is ``(size_label, depth, flute, case_pack,
    board_quality)``, which resolves to exactly one variant. Quotation lines
    reference a variant and never a product, so two board qualities of the same
    size can never be conflated.
    """

    __tablename__ = "product_variants"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True
    )
    variant_item_number: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)

    #: e.g. ``WT110 HPFL160 KM135``. Read per row on import — never inferred
    #: from a workbook section heading, because the "alternative quality" block
    #: in the reference file contains two different qualities.
    board_quality: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    case_pack: Mapped[int] = mapped_column(Integer, nullable=False)
    num_colours: Mapped[int | None] = mapped_column(Integer)
    moq_packs: Mapped[Decimal | None] = mapped_column(quantity())
    moq_pieces: Mapped[Decimal | None] = mapped_column(quantity())
    #: Overrides the composed spec string on the PDF, e.g.
    #: "White/Kraft 3-4C, Perforated / No-Lock".
    spec_text_override: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    updated_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))

    product: Mapped[Product] = relationship(back_populates="variants")
    prices: Mapped[list[ProductPrice]] = relationship(back_populates="variant")
    costs: Mapped[list[ProductCost]] = relationship(back_populates="variant")

    __table_args__ = (
        UniqueConstraint("product_id", "board_quality", "case_pack"),
        CheckConstraint("case_pack > 0", name="case_pack_positive"),
    )


class PriceTier(Base, TimestampMixin):
    """Standard / Three Containers / Eight Containers / Custom.

    ``min_containers`` drives the quantity warnings declaratively, so a future
    twelve-container tier is a data row rather than a code change.
    """

    __tablename__ = "price_tiers"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(40), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    min_containers: Mapped[int | None] = mapped_column(Integer)
    requires_approval: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class ProductPrice(Base):
    """Append-only price history.

    An import never UPDATEs a row here. Superseding sets ``effective_to`` on the
    old row and inserts a new one, so an issued quotation can always resolve the
    price it was actually built from. Enforced by ``_guard_price_immutability``.
    """

    __tablename__ = "product_prices"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_variant_id: Mapped[int] = mapped_column(
        ForeignKey("product_variants.id"), nullable=False, index=True
    )
    price_tier_id: Mapped[int] = mapped_column(ForeignKey("price_tiers.id"), nullable=False)

    #: Both columns are imported verbatim and neither is derived from the other.
    #: In the reference workbook they disagree by up to one rounding unit on 25
    #: of 69 price pairs — see docs/PHASE1_REFERENCE_ANALYSIS.md §1.2.
    price_per_pack: Mapped[Decimal] = mapped_column(unit_price(), nullable=False)
    price_per_piece: Mapped[Decimal] = mapped_column(unit_price(), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")

    effective_from: Mapped[dt.date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[dt.date | None] = mapped_column(Date)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    source_workbook_name: Mapped[str | None] = mapped_column(String(255))
    source_sheet_name: Mapped[str | None] = mapped_column(String(120))
    source_row_no: Mapped[int | None] = mapped_column(Integer)
    import_job_id: Mapped[int | None] = mapped_column(ForeignKey("import_jobs.id"))

    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    variant: Mapped[ProductVariant] = relationship(back_populates="prices")
    tier: Mapped[PriceTier] = relationship()

    __table_args__ = (
        UniqueConstraint(
            "product_variant_id", "price_tier_id", "currency", "effective_from"
        ),
        Index(
            "ix_product_prices_lookup",
            "product_variant_id", "price_tier_id", "currency", "effective_from",
        ),
        CheckConstraint("price_per_pack > 0", name="pack_price_positive"),
        CheckConstraint("price_per_piece > 0", name="piece_price_positive"),
    )


class ProductCost(Base):
    """Internal cost, entered manually per variant (decision: architecture §15.4).

    Effective-dated on the same append-only pattern as sell prices so that a
    historical quotation's margin stays reproducible. Never appears on a
    customer PDF; visibility is gated by ``cost.view``.
    """

    __tablename__ = "product_costs"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_variant_id: Mapped[int] = mapped_column(
        ForeignKey("product_variants.id"), nullable=False, index=True
    )
    cost_per_pack: Mapped[Decimal] = mapped_column(unit_price(), nullable=False)
    cost_per_piece: Mapped[Decimal | None] = mapped_column(unit_price())
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    effective_from: Mapped[dt.date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[dt.date | None] = mapped_column(Date)
    source_note: Mapped[str | None] = mapped_column(Text)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    variant: Mapped[ProductVariant] = relationship(back_populates="costs")

    __table_args__ = (
        UniqueConstraint("product_variant_id", "currency", "effective_from"),
        CheckConstraint("cost_per_pack >= 0", name="cost_non_negative"),
    )


# --------------------------------------------------------------------------- #
# 18-24. Quotations
# --------------------------------------------------------------------------- #

class Quotation(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "quotations"

    id: Mapped[int] = mapped_column(primary_key=True)

    #: Revision family. Rev 0 points at itself once flushed.
    root_quotation_id: Mapped[int | None] = mapped_column(
        ForeignKey("quotations.id"), index=True
    )
    quote_number: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_current_revision: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    #: Set when the quotation is issued. From then on every edit routes through
    #: revision_service; ``_guard_locked_quotation`` rejects direct writes.
    is_locked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    issued_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    status: Mapped[QuotationStatus] = mapped_column(
        _enum(QuotationStatus), nullable=False, default=QuotationStatus.DRAFT, index=True
    )
    quote_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    valid_until: Mapped[dt.date | None] = mapped_column(Date, index=True)

    # --- customer, with snapshots ---------------------------------------- #
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id"), nullable=False, index=True
    )
    customer_contact_id: Mapped[int | None] = mapped_column(
        ForeignKey("customer_contacts.id")
    )
    customer_name_snapshot: Mapped[str | None] = mapped_column(String(200))
    contact_name: Mapped[str | None] = mapped_column(String(160))
    contact_email: Mapped[str | None] = mapped_column(String(255))
    contact_phone: Mapped[str | None] = mapped_column(String(60))
    billing_address_text: Mapped[str | None] = mapped_column(Text)
    shipping_address_text: Mapped[str | None] = mapped_column(Text)

    project_name: Mapped[str | None] = mapped_column(String(200), index=True)
    brand: Mapped[str | None] = mapped_column(String(160))
    distributor: Mapped[str | None] = mapped_column(String(160))
    customer_po_ref: Mapped[str | None] = mapped_column(String(120))

    sales_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"), nullable=False, index=True
    )

    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    exchange_rate: Mapped[Decimal] = mapped_column(
        fx_rate(), nullable=False, default=Decimal("1")
    )

    quote_discount_pct: Mapped[Decimal] = mapped_column(
        percentage(), nullable=False, default=Decimal("0")
    )
    quote_discount_amount: Mapped[Decimal] = mapped_column(
        money(), nullable=False, default=Decimal("0")
    )
    tax_rate_id: Mapped[int | None] = mapped_column(ForeignKey("tax_rates.id"))
    tax_rate_pct: Mapped[Decimal] = mapped_column(
        tax_rate(), nullable=False, default=Decimal("0")
    )

    subtotal: Mapped[Decimal] = mapped_column(money(), nullable=False, default=Decimal("0"))
    charges_total: Mapped[Decimal] = mapped_column(
        money(), nullable=False, default=Decimal("0")
    )
    tax_amount: Mapped[Decimal] = mapped_column(
        money(), nullable=False, default=Decimal("0")
    )
    grand_total: Mapped[Decimal] = mapped_column(
        money(), nullable=False, default=Decimal("0")
    )

    #: NULL when costs are not populated — margins are absent, not zero.
    total_cost: Mapped[Decimal | None] = mapped_column(money())
    gross_profit: Mapped[Decimal | None] = mapped_column(money())
    gross_margin_pct: Mapped[Decimal | None] = mapped_column(percentage())

    requires_approval: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    internal_notes: Mapped[str | None] = mapped_column(Text)
    customer_notes: Mapped[str | None] = mapped_column(Text)

    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    updated_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))

    items: Mapped[list[QuotationItem]] = relationship(
        back_populates="quotation",
        cascade="all, delete-orphan",
        order_by="QuotationItem.sort_order",
    )
    charges: Mapped[list[QuotationCharge]] = relationship(
        back_populates="quotation", cascade="all, delete-orphan"
    )
    terms: Mapped[list[QuotationTerm]] = relationship(
        back_populates="quotation",
        cascade="all, delete-orphan",
        order_by="QuotationTerm.sort_order",
    )
    #: Optional. A quotation without one behaves exactly as it did before
    #: container shipping existed.
    shipment: Mapped[QuotationShipment | None] = relationship(
        back_populates="quotation", cascade="all, delete-orphan", uselist=False
    )
    approvals: Mapped[list[Approval]] = relationship(back_populates="quotation")
    response_logs: Mapped[list[CustomerResponseLog]] = relationship(
        back_populates="quotation"
    )

    __table_args__ = (
        UniqueConstraint("quote_number", "revision_no"),
        Index("ix_quotations_status_date", "status", "quote_date"),
        Index("ix_quotations_current", "is_current_revision", "status"),
        CheckConstraint("revision_no >= 0", name="revision_non_negative"),
    )

    @property
    def revision_label(self) -> str:
        return f"Rev {self.revision_no}"

    @property
    def display_number(self) -> str:
        return f"{self.quote_number} {self.revision_label}"


class QuotationItem(Base, TimestampMixin):
    __tablename__ = "quotation_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    quotation_id: Mapped[int] = mapped_column(
        ForeignKey("quotations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    line_no: Mapped[int] = mapped_column(Integer, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    product_variant_id: Mapped[int | None] = mapped_column(
        ForeignKey("product_variants.id")
    )
    #: The exact historical price row this line was built from. Together with
    #: the denormalised prices below, this is what makes an issued quotation
    #: reproducible after the price list moves on.
    product_price_id: Mapped[int | None] = mapped_column(ForeignKey("product_prices.id"))

    is_custom_product: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    custom_description: Mapped[str | None] = mapped_column(Text)
    description_override: Mapped[str | None] = mapped_column(Text)
    spec_text_override: Mapped[str | None] = mapped_column(Text)

    # --- specification snapshot at quote time ---------------------------- #
    item_number_snapshot: Mapped[str | None] = mapped_column(String(80))
    size_label: Mapped[str | None] = mapped_column(String(80))
    depth_in: Mapped[Decimal | None] = mapped_column(quantity())
    flute: Mapped[str | None] = mapped_column(String(20))
    board_quality: Mapped[str | None] = mapped_column(String(120))
    case_pack: Mapped[int | None] = mapped_column(Integer)
    printing_method: Mapped[str | None] = mapped_column(String(80))
    num_colours: Mapped[int | None] = mapped_column(Integer)
    moq_packs: Mapped[Decimal | None] = mapped_column(quantity())

    # --- pricing ---------------------------------------------------------- #
    price_tier_id: Mapped[int | None] = mapped_column(ForeignKey("price_tiers.id"))
    pricing_basis: Mapped[PricingBasis] = mapped_column(
        _enum(PricingBasis), nullable=False, default=PricingBasis.PACK
    )
    quantity_packs: Mapped[Decimal] = mapped_column(
        quantity(), nullable=False, default=Decimal("0")
    )
    quantity_pieces: Mapped[Decimal] = mapped_column(
        quantity(), nullable=False, default=Decimal("0")
    )
    container_count: Mapped[Decimal] = mapped_column(
        quantity(), nullable=False, default=Decimal("0")
    )

    price_per_pack: Mapped[Decimal] = mapped_column(unit_price(), nullable=False)
    price_per_piece: Mapped[Decimal] = mapped_column(unit_price(), nullable=False)
    is_custom_price: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    custom_price_reason: Mapped[str | None] = mapped_column(Text)

    line_discount_pct: Mapped[Decimal] = mapped_column(
        percentage(), nullable=False, default=Decimal("0")
    )
    line_discount_amount: Mapped[Decimal] = mapped_column(
        money(), nullable=False, default=Decimal("0")
    )
    gross_line_total: Mapped[Decimal] = mapped_column(
        money(), nullable=False, default=Decimal("0")
    )
    net_line_total: Mapped[Decimal] = mapped_column(
        money(), nullable=False, default=Decimal("0")
    )

    # --- internal only ---------------------------------------------------- #
    unit_cost_per_pack: Mapped[Decimal | None] = mapped_column(unit_price())
    line_cost_total: Mapped[Decimal | None] = mapped_column(money())

    customer_remarks: Mapped[str | None] = mapped_column(Text)
    internal_remarks: Mapped[str | None] = mapped_column(Text)

    quotation: Mapped[Quotation] = relationship(back_populates="items")
    variant: Mapped[ProductVariant | None] = relationship()
    price_record: Mapped[ProductPrice | None] = relationship()
    tier: Mapped[PriceTier | None] = relationship()

    __table_args__ = (
        UniqueConstraint("quotation_id", "line_no"),
        CheckConstraint("quantity_packs >= 0", name="qty_packs_non_negative"),
        CheckConstraint("quantity_pieces >= 0", name="qty_pieces_non_negative"),
    )


class QuotationCharge(Base, TimestampMixin):
    __tablename__ = "quotation_charges"

    id: Mapped[int] = mapped_column(primary_key=True)
    quotation_id: Mapped[int] = mapped_column(
        ForeignKey("quotations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    charge_type: Mapped[ChargeType] = mapped_column(_enum(ChargeType), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    quantity_value: Mapped[Decimal] = mapped_column(
        quantity(), nullable=False, default=Decimal("1")
    )
    rate: Mapped[Decimal] = mapped_column(money(), nullable=False, default=Decimal("0"))
    amount: Mapped[Decimal] = mapped_column(money(), nullable=False, default=Decimal("0"))
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    exchange_rate: Mapped[Decimal] = mapped_column(
        fx_rate(), nullable=False, default=Decimal("1")
    )
    is_taxable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_customer_visible: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    internal_note: Mapped[str | None] = mapped_column(Text)
    #: 'manual' or 'plate_calculator'
    source: Mapped[str] = mapped_column(String(30), nullable=False, default="manual")

    quotation: Mapped[Quotation] = relationship(back_populates="charges")


class TermTemplate(Base, TimestampMixin, SoftDeleteMixin):
    """Reusable master terms. Editing a quotation's terms never touches these."""

    __tablename__ = "term_templates"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(60), nullable=False, unique=True)
    section: Mapped[TermSection] = mapped_column(_enum(TermSection), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body_text: Mapped[str] = mapped_column(Text, nullable=False)
    #: Pre-ticked on a new quotation. Not every term belongs on every quote.
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    updated_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))


class QuotationTerm(Base, TimestampMixin):
    """A term as it appears on one quotation — an editable copy, not a link."""

    __tablename__ = "quotation_terms"

    id: Mapped[int] = mapped_column(primary_key=True)
    quotation_id: Mapped[int] = mapped_column(
        ForeignKey("quotations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    term_template_id: Mapped[int | None] = mapped_column(ForeignKey("term_templates.id"))
    section: Mapped[TermSection] = mapped_column(_enum(TermSection), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body_text: Mapped[str] = mapped_column(Text, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_customer_visible: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )

    quotation: Mapped[Quotation] = relationship(back_populates="terms")


class QuotationRevision(Base):
    """Immutable snapshot pair for every issued revision.

    ``snapshot_json`` is a full serialisation of the quotation with its items,
    charges and terms, deliberately independent of the live tables — a later
    schema change cannot alter what an issued quotation said.
    """

    __tablename__ = "quotation_revisions"

    id: Mapped[int] = mapped_column(primary_key=True)
    root_quotation_id: Mapped[int] = mapped_column(
        ForeignKey("quotations.id"), nullable=False, index=True
    )
    quotation_id: Mapped[int] = mapped_column(ForeignKey("quotations.id"), nullable=False)
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False)

    snapshot_json: Mapped[Any] = mapped_column(JSONType, nullable=False)
    previous_snapshot_json: Mapped[Any | None] = mapped_column(JSONType)
    previous_total: Mapped[Decimal | None] = mapped_column(money())
    new_total: Mapped[Decimal | None] = mapped_column(money())
    change_reason: Mapped[str | None] = mapped_column(Text)

    previous_pdf_attachment_id: Mapped[int | None] = mapped_column(
        ForeignKey("attachments.id")
    )
    new_pdf_attachment_id: Mapped[int | None] = mapped_column(ForeignKey("attachments.id"))

    changed_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    changed_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("root_quotation_id", "revision_no"),
    )


class Approval(Base):
    __tablename__ = "approvals"

    id: Mapped[int] = mapped_column(primary_key=True)
    quotation_id: Mapped[int] = mapped_column(
        ForeignKey("quotations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    requested_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    requested_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    #: Which rules fired, so the approver sees why it landed with them.
    triggered_rules_json: Mapped[Any | None] = mapped_column(JSONType)

    approver_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True)
    decided_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    decision: Mapped[ApprovalDecision] = mapped_column(
        _enum(ApprovalDecision), nullable=False, default=ApprovalDecision.PENDING
    )
    comments: Mapped[str | None] = mapped_column(Text)
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    override_reason: Mapped[str | None] = mapped_column(Text)

    quotation: Mapped[Quotation] = relationship(back_populates="approvals")


# --------------------------------------------------------------------------- #
# 25-29. Operations
# --------------------------------------------------------------------------- #

class CustomerResponseLog(Base, TimestampMixin):
    """Manual record of what happened after the PDF left the building.

    Customers do not interact with this application, so every field here is
    entered by an employee. There is no automatic tracking of any kind.
    """

    __tablename__ = "customer_response_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    quotation_id: Mapped[int] = mapped_column(
        ForeignKey("quotations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    date_sent: Mapped[dt.date | None] = mapped_column(Date)
    sent_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    send_method: Mapped[SendMethod | None] = mapped_column(_enum(SendMethod))
    customer_contact_id: Mapped[int | None] = mapped_column(
        ForeignKey("customer_contacts.id")
    )

    response: Mapped[CustomerResponse] = mapped_column(
        _enum(CustomerResponse), nullable=False, default=CustomerResponse.NO_RESPONSE
    )
    response_date: Mapped[dt.date | None] = mapped_column(Date)
    loss_reason: Mapped[str | None] = mapped_column(Text)
    competitor: Mapped[str | None] = mapped_column(String(160))
    follow_up_date: Mapped[dt.date | None] = mapped_column(Date, index=True)
    notes: Mapped[str | None] = mapped_column(Text)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))

    quotation: Mapped[Quotation] = relationship(back_populates="response_logs")


class Attachment(Base):
    """Polymorphic file reference.

    ``storage_key`` is an object-storage key, not a local path: the Community
    Cloud filesystem is rebuilt on every redeploy.
    """

    __tablename__ = "attachments"

    id: Mapped[int] = mapped_column(primary_key=True)
    entity_type: Mapped[str] = mapped_column(String(40), nullable=False)
    entity_id: Mapped[int | None] = mapped_column(Integer)
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(500), nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(120))
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    sha256: Mapped[str | None] = mapped_column(String(64), index=True)
    is_customer_visible: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    uploaded_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    uploaded_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_attachments_entity", "entity_type", "entity_id"),
    )


class ImportJob(Base):
    __tablename__ = "import_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    storage_key: Mapped[str | None] = mapped_column(String(500))
    sha256: Mapped[str | None] = mapped_column(String(64))
    sheet_name: Mapped[str | None] = mapped_column(String(120))
    effective_from: Mapped[dt.date | None] = mapped_column(Date)

    status: Mapped[ImportJobStatus] = mapped_column(
        _enum(ImportJobStatus), nullable=False, default=ImportJobStatus.PENDING
    )
    rows_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rows_created: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rows_updated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rows_skipped: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rows_failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    summary_json: Mapped[Any | None] = mapped_column(JSONType)
    error_text: Mapped[str | None] = mapped_column(Text)

    uploaded_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    started_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    rows: Mapped[list[ImportRow]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )


class ImportRow(Base):
    __tablename__ = "import_rows"

    id: Mapped[int] = mapped_column(primary_key=True)
    import_job_id: Mapped[int] = mapped_column(
        ForeignKey("import_jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_row_no: Mapped[int] = mapped_column(Integer, nullable=False)
    #: The workbook's section heading, e.g. "alternative quality". Recorded for
    #: the audit summary only — board quality is always read from the row's own
    #: Quality column, never inferred from this.
    section_label: Mapped[str | None] = mapped_column(String(120))

    raw_json: Mapped[Any | None] = mapped_column(JSONType)
    normalized_json: Mapped[Any | None] = mapped_column(JSONType)
    action: Mapped[ImportRowAction | None] = mapped_column(_enum(ImportRowAction))
    status: Mapped[ImportRowStatus] = mapped_column(
        _enum(ImportRowStatus), nullable=False, default=ImportRowStatus.OK
    )
    error_message: Mapped[str | None] = mapped_column(Text)
    product_variant_id: Mapped[int | None] = mapped_column(
        ForeignKey("product_variants.id")
    )
    created_price_ids: Mapped[Any | None] = mapped_column(JSONType)

    job: Mapped[ImportJob] = relationship(back_populates="rows")


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True)
    #: Kept alongside user_id so the trail survives the user record changing.
    username_snapshot: Mapped[str | None] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    entity_type: Mapped[str | None] = mapped_column(String(40), index=True)
    entity_id: Mapped[int | None] = mapped_column(Integer, index=True)

    old_value_json: Mapped[Any | None] = mapped_column(JSONType)
    new_value_json: Mapped[Any | None] = mapped_column(JSONType)
    reason: Mapped[str | None] = mapped_column(Text)
    page: Mapped[str | None] = mapped_column(String(80))
    session_id: Mapped[str | None] = mapped_column(String(64))
    ip_address: Mapped[str | None] = mapped_column(String(64))

    occurred_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )

    __table_args__ = (
        Index("ix_audit_logs_entity", "entity_type", "entity_id", "occurred_at"),
    )


class DocumentSequence(Base):
    """Quote-number allocation.

    Deriving the next number from ``MAX(quote_number)`` races under concurrent
    users. Allocation takes a row lock inside the same transaction that inserts
    the quotation.
    """

    __tablename__ = "document_sequences"

    id: Mapped[int] = mapped_column(primary_key=True)
    #: e.g. ``QUOTE:2026``
    scope_key: Mapped[str] = mapped_column(String(60), nullable=False, unique=True)
    last_value: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(),
        nullable=False,
    )


# --------------------------------------------------------------------------- #
# 32-36. Container shipping
# --------------------------------------------------------------------------- #

class ShippingLine(Base, TimestampMixin, SoftDeleteMixin):
    """Carrier master data, maintained from Company Settings."""

    __tablename__ = "shipping_lines"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    notes: Mapped[str | None] = mapped_column(Text)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))


class ProductContainerCapacity(Base, TimestampMixin):
    """How much of a product fits in a container.

    Keyed on the **product**, not the variant: capacity is a function of the
    box's geometry, and the two board qualities of a given size are
    dimensionally identical. The source workbook gives one figure per size,
    which agrees with that.

    ``bundles_per_container`` is the authoritative imported figure, and it is
    the only one the workbook supplies. Converting it into packs — which is
    what a quotation counts in — needs to know what a bundle holds, and that
    lives on :attr:`Product.units_per_bundle`, because a bundle contains the
    same number of boxes whatever container it travels in. Until it is set,
    packs and pieces per container are reported as unavailable rather than
    guessed.
    """

    __tablename__ = "product_container_capacity"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True
    )
    container_size: Mapped[ContainerSize] = mapped_column(
        _enum(ContainerSize), nullable=False, default=ContainerSize.FORTY_FT_HC
    )
    container_type: Mapped[ContainerType] = mapped_column(
        _enum(ContainerType), nullable=False, default=ContainerType.DRY
    )

    bundles_per_container: Mapped[Decimal] = mapped_column(quantity(), nullable=False)
    pallets_per_container: Mapped[Decimal | None] = mapped_column(quantity())

    source_workbook_name: Mapped[str | None] = mapped_column(String(255))
    source_row_no: Mapped[int | None] = mapped_column(Integer)
    #: Set when the imported figure departs from the trend of its neighbours.
    #: Recorded rather than corrected — see docs/SHIPPING.md.
    is_anomalous: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    anomaly_note: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)

    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))

    product: Mapped[Product] = relationship()

    __table_args__ = (
        UniqueConstraint("product_id", "container_size", "container_type"),
        CheckConstraint("bundles_per_container > 0", name="bundles_positive"),
    )

    @property
    def pieces_per_container(self) -> Decimal | None:
        """``None`` until the product says how many boxes are in a bundle."""
        per_bundle = self.product.units_per_bundle if self.product else None
        if per_bundle is None:
            return None
        return self.bundles_per_container * per_bundle

    @property
    def packs_per_container(self) -> Decimal | None:
        """What a quotation actually counts in. ``None`` if either input is.

        A quotation is written in packs, the workbook counts containers in
        bundles, and nothing connects the two but the bundle size. Returning
        ``None`` keeps a container estimate off the screen entirely rather
        than showing one derived from an assumed bundle.
        """
        pieces = self.pieces_per_container
        case_pack = None
        if self.product is not None:
            case_pack = next(
                (v.case_pack for v in self.product.variants if v.case_pack), None
            )
        if pieces is None or not case_pack:
            return None
        return pieces / Decimal(case_pack)


class QuotationShipment(Base, TimestampMixin):
    """The shipping arrangement for one quotation. At most one per quotation.

    Optional throughout: a quotation without a shipment behaves exactly as it
    did before container shipping existed.
    """

    __tablename__ = "quotation_shipments"

    id: Mapped[int] = mapped_column(primary_key=True)
    quotation_id: Mapped[int] = mapped_column(
        ForeignKey("quotations.id", ondelete="CASCADE"),
        nullable=False, unique=True, index=True,
    )

    incoterm: Mapped[Incoterm | None] = mapped_column(_enum(Incoterm, length=10))
    incoterm_place: Mapped[str | None] = mapped_column(String(160))
    origin_country: Mapped[str | None] = mapped_column(String(120))
    port_of_loading: Mapped[str | None] = mapped_column(String(160))
    port_of_discharge: Mapped[str | None] = mapped_column(String(160))
    final_destination: Mapped[str | None] = mapped_column(String(160))

    freight_method: Mapped[FreightMethod] = mapped_column(
        _enum(FreightMethod), nullable=False, default=FreightMethod.INCLUDED
    )
    #: Sum of the container rows' freight. Maintained by shipping_service.
    total_freight: Mapped[Decimal] = mapped_column(
        money(), nullable=False, default=Decimal("0")
    )
    freight_currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    freight_taxable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    loading_method: Mapped[LoadingMethod | None] = mapped_column(_enum(LoadingMethod))
    shipping_notes: Mapped[str | None] = mapped_column(Text)

    #: The document section is opt-in per quotation, so existing quotations and
    #: anyone who does not want it produce byte-identical output.
    show_on_document: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    customer_visible_freight: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )

    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    updated_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))

    quotation: Mapped[Quotation] = relationship(back_populates="shipment")
    containers: Mapped[list[ShipmentContainer]] = relationship(
        back_populates="shipment",
        cascade="all, delete-orphan",
        order_by="ShipmentContainer.sort_order",
    )

    @property
    def total_containers(self) -> Decimal:
        return sum((c.container_count for c in self.containers), Decimal("0"))


class ShipmentContainer(Base, TimestampMixin):
    """One container configuration. A shipment may have several."""

    __tablename__ = "shipment_containers"

    id: Mapped[int] = mapped_column(primary_key=True)
    quotation_shipment_id: Mapped[int] = mapped_column(
        ForeignKey("quotation_shipments.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    shipping_line_id: Mapped[int | None] = mapped_column(ForeignKey("shipping_lines.id"))
    #: Used when the carrier is not on the managed list ("Other").
    custom_shipping_line: Mapped[str | None] = mapped_column(String(120))

    container_size: Mapped[ContainerSize] = mapped_column(
        _enum(ContainerSize), nullable=False, default=ContainerSize.FORTY_FT_HC
    )
    custom_container_size: Mapped[str | None] = mapped_column(String(60))
    container_type: Mapped[ContainerType] = mapped_column(
        _enum(ContainerType), nullable=False, default=ContainerType.DRY
    )
    custom_container_type: Mapped[str | None] = mapped_column(String(60))
    container_count: Mapped[Decimal] = mapped_column(
        quantity(), nullable=False, default=Decimal("1")
    )

    freight_cost: Mapped[Decimal] = mapped_column(
        money(), nullable=False, default=Decimal("0")
    )
    freight_currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")

    port_of_loading: Mapped[str | None] = mapped_column(String(160))
    port_of_discharge: Mapped[str | None] = mapped_column(String(160))
    estimated_departure: Mapped[dt.date | None] = mapped_column(Date)
    estimated_arrival: Mapped[dt.date | None] = mapped_column(Date)
    transit_days: Mapped[int | None] = mapped_column(Integer)

    loading_method: Mapped[LoadingMethod | None] = mapped_column(_enum(LoadingMethod))
    #: Overrides the global max_items_per_container setting for this row.
    maximum_product_items: Mapped[int | None] = mapped_column(Integer)
    notes: Mapped[str | None] = mapped_column(Text)

    shipment: Mapped[QuotationShipment] = relationship(back_populates="containers")
    shipping_line: Mapped[ShippingLine | None] = relationship()
    allocations: Mapped[list[ShipmentProductAllocation]] = relationship(
        back_populates="container", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint("container_count > 0", name="container_count_positive"),
        CheckConstraint("freight_cost >= 0", name="freight_non_negative"),
    )

    @property
    def carrier_name(self) -> str:
        if self.shipping_line is not None:
            return self.shipping_line.name
        return self.custom_shipping_line or "—"

    @property
    def size_label(self) -> str:
        from modules.constants import CONTAINER_SIZE_LABELS

        if self.container_size is ContainerSize.CUSTOM and self.custom_container_size:
            return self.custom_container_size
        return CONTAINER_SIZE_LABELS[self.container_size]

    @property
    def type_label(self) -> str:
        from modules.constants import CONTAINER_TYPE_LABELS

        if self.container_type is ContainerType.CUSTOM and self.custom_container_type:
            return self.custom_container_type
        return CONTAINER_TYPE_LABELS[self.container_type]


class ShipmentProductAllocation(Base, TimestampMixin):
    """Which quotation line travels in which container, and how much of it."""

    __tablename__ = "shipment_product_allocations"

    id: Mapped[int] = mapped_column(primary_key=True)
    shipment_container_id: Mapped[int] = mapped_column(
        ForeignKey("shipment_containers.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    quotation_item_id: Mapped[int] = mapped_column(
        ForeignKey("quotation_items.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )

    quantity_per_container: Mapped[Decimal] = mapped_column(
        quantity(), nullable=False, default=Decimal("0")
    )
    total_allocated_quantity: Mapped[Decimal] = mapped_column(
        quantity(), nullable=False, default=Decimal("0")
    )

    bundles_per_container: Mapped[Decimal | None] = mapped_column(quantity())
    pallets_per_container: Mapped[Decimal | None] = mapped_column(quantity())
    cases_per_container: Mapped[Decimal | None] = mapped_column(quantity())
    pieces_per_container: Mapped[Decimal | None] = mapped_column(quantity())

    #: Freight apportioned to this allocation, for landed cost. Internal only.
    allocated_freight: Mapped[Decimal | None] = mapped_column(money())
    notes: Mapped[str | None] = mapped_column(Text)

    container: Mapped[ShipmentContainer] = relationship(back_populates="allocations")
    item: Mapped[QuotationItem] = relationship()

    __table_args__ = (
        UniqueConstraint("shipment_container_id", "quotation_item_id"),
    )


# --------------------------------------------------------------------------- #
# Immutability guards
# --------------------------------------------------------------------------- #

class ImmutableRecordError(RuntimeError):
    """Raised when code attempts to mutate a record that must never change."""


#: Fields that stay writable on a locked (issued) quotation. Everything the
#: customer saw is frozen; what remains is the internal lifecycle — recording
#: that they accepted it, that it expired, or that a newer revision now
#: supersedes it. Commercial content is changed by creating a revision.
_LOCKED_QUOTATION_WRITABLE = frozenset({
    "status",
    "is_current_revision",
    "is_locked",
    "issued_at",
    "internal_notes",
    "updated_at",
    "updated_by_id",
    "deleted_at",
    "root_quotation_id",
})

#: Price identity and amounts never change. Superseding an old price sets
#: ``effective_to`` (and ``is_active``), which is why those two stay writable.
_PRICE_IMMUTABLE_FIELDS = frozenset({
    "product_variant_id",
    "price_tier_id",
    "price_per_pack",
    "price_per_piece",
    "currency",
    "effective_from",
})

_COST_IMMUTABLE_FIELDS = frozenset({
    "product_variant_id",
    "cost_per_pack",
    "cost_per_piece",
    "currency",
    "effective_from",
})


def _changed_fields(session: Any, obj: Any) -> set[str]:
    """Names of attributes with a pending change on ``obj``."""
    from sqlalchemy import inspect as sa_inspect

    changed: set[str] = set()
    state = sa_inspect(obj)
    for attr in state.mapper.column_attrs:
        history = state.get_history(attr.key, True)
        if history.has_changes():
            changed.add(attr.key)
    return changed


@event.listens_for(Session, "before_flush")
def _guard_immutability(session: Session, _flush_context: Any, _instances: Any) -> None:
    """Refuse writes that would rewrite history.

    This sits at the session level rather than in the service layer on purpose:
    it catches the accidental path as well as the deliberate one, including a
    stray edit made from a REPL or a future page that forgets to go through
    ``revision_service``.
    """
    for obj in session.dirty:
        if not session.is_modified(obj, include_collections=False):
            continue

        if isinstance(obj, Quotation) and obj.is_locked:
            illegal = _changed_fields(session, obj) - _LOCKED_QUOTATION_WRITABLE
            if illegal:
                raise ImmutableRecordError(
                    f"Quotation {obj.quote_number} {obj.revision_label} has been issued "
                    f"and cannot be edited (attempted: {', '.join(sorted(illegal))}). "
                    "Create a new revision instead."
                )

        elif isinstance(obj, ProductPrice):
            illegal = _changed_fields(session, obj) & _PRICE_IMMUTABLE_FIELDS
            if illegal:
                raise ImmutableRecordError(
                    f"Price history is append-only: price #{obj.id} cannot change "
                    f"({', '.join(sorted(illegal))}). Supersede it by setting "
                    "effective_to and inserting a new price."
                )

        elif isinstance(obj, ProductCost):
            illegal = _changed_fields(session, obj) & _COST_IMMUTABLE_FIELDS
            if illegal:
                raise ImmutableRecordError(
                    f"Cost history is append-only: cost #{obj.id} cannot change "
                    f"({', '.join(sorted(illegal))}). Supersede it instead."
                )

        elif isinstance(obj, QuotationRevision):
            raise ImmutableRecordError(
                f"Revision snapshot #{obj.id} is immutable and cannot be edited."
            )

        elif isinstance(obj, QuotationShipment):
            parent = obj.quotation
            if parent is not None and parent.is_locked:
                raise ImmutableRecordError(
                    f"The shipping details of issued quotation "
                    f"{parent.quote_number} {parent.revision_label} cannot be "
                    "edited. Create a new revision instead."
                )

        elif isinstance(obj, (ShipmentContainer, ShipmentProductAllocation)):
            shipment = (
                obj.shipment if isinstance(obj, ShipmentContainer)
                else (obj.container.shipment if obj.container else None)
            )
            parent = shipment.quotation if shipment else None
            if parent is not None and parent.is_locked:
                raise ImmutableRecordError(
                    f"{type(obj).__name__} belongs to issued quotation "
                    f"{parent.quote_number} {parent.revision_label} and cannot be "
                    "edited. Create a new revision instead."
                )

        elif isinstance(obj, (QuotationItem, QuotationCharge, QuotationTerm)):
            parent = obj.quotation
            if parent is not None and parent.is_locked:
                raise ImmutableRecordError(
                    f"{type(obj).__name__} belongs to issued quotation "
                    f"{parent.quote_number} {parent.revision_label} and cannot be "
                    "edited. Create a new revision instead."
                )

    for obj in list(session.new) + list(session.deleted):
        if isinstance(obj, (QuotationItem, QuotationCharge, QuotationTerm)):
            parent = obj.quotation
            if parent is not None and parent.is_locked:
                verb = "added to" if obj in session.new else "removed from"
                raise ImmutableRecordError(
                    f"{type(obj).__name__} cannot be {verb} issued quotation "
                    f"{parent.quote_number} {parent.revision_label}. "
                    "Create a new revision instead."
                )
        elif isinstance(obj, QuotationRevision) and obj in session.deleted:
            raise ImmutableRecordError("Revision snapshots cannot be deleted.")
