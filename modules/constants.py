"""Enumerations and the status-transition table.

Everything the rest of the application compares against by name lives here, so
that a typo becomes an AttributeError at import rather than a silently-failing
string comparison at runtime.

This module imports nothing from the project.
"""

from __future__ import annotations

from enum import StrEnum


# --------------------------------------------------------------------------- #
# Roles
# --------------------------------------------------------------------------- #

class RoleCode(StrEnum):
    SALES = "SALES"
    SALES_MANAGER = "SALES_MANAGER"
    FINANCE = "FINANCE"
    PRICING_ADMIN = "PRICING_ADMIN"
    SYS_ADMIN = "SYS_ADMIN"


ROLE_DISPLAY_NAMES: dict[RoleCode, str] = {
    RoleCode.SALES: "Sales Employee",
    RoleCode.SALES_MANAGER: "Sales Manager",
    RoleCode.FINANCE: "Finance",
    RoleCode.PRICING_ADMIN: "Pricing Administrator",
    RoleCode.SYS_ADMIN: "System Administrator",
}


# --------------------------------------------------------------------------- #
# Permissions
# --------------------------------------------------------------------------- #

class Perm(StrEnum):
    """Permission codes. Granted to roles; resolved to a flat set at login.

    Checked inside the service layer — never only in the page. Hiding a widget
    is a UX courtesy; the service check is the control.
    """

    # Quotations
    QUOTE_CREATE = "quote.create"
    QUOTE_EDIT_OWN_DRAFT = "quote.edit_own_draft"
    QUOTE_EDIT_ANY_DRAFT = "quote.edit_any_draft"
    QUOTE_VIEW_OWN = "quote.view_own"
    QUOTE_VIEW_TEAM = "quote.view_team"
    QUOTE_VIEW_ALL = "quote.view_all"
    QUOTE_SUBMIT_FOR_APPROVAL = "quote.submit_for_approval"
    QUOTE_APPROVE = "quote.approve"
    QUOTE_REJECT = "quote.reject"
    QUOTE_RETURN_FOR_REVISION = "quote.return_for_revision"
    QUOTE_OVERRIDE_WARNING = "quote.override_warning"
    QUOTE_APPROVE_CUSTOM_PRICE = "quote.approve_custom_price"
    QUOTE_GENERATE_PDF = "quote.generate_pdf"
    QUOTE_UPDATE_STATUS = "quote.update_status"
    QUOTE_CREATE_REVISION = "quote.create_revision"
    QUOTE_CANCEL = "quote.cancel"
    QUOTE_EXPORT = "quote.export"
    #: Remove an unissued draft. Deletion is soft throughout — the row stays,
    #: its number stays consumed, and an administrator can restore it.
    QUOTE_DELETE_DRAFT = "quote.delete_draft"
    #: Remove a quotation that has already been issued, and restore anything
    #: deleted. Separate because an issued quotation is a record of what was
    #: actually sent to a customer, which the ordinary delete must not touch.
    QUOTE_DELETE_ANY = "quote.delete_any"

    # Internal financials
    COST_VIEW = "cost.view"
    COST_MANAGE = "cost.manage"
    MARGIN_VIEW = "margin.view"

    # Customers
    CUSTOMER_VIEW = "customer.view"
    CUSTOMER_CREATE = "customer.create"
    CUSTOMER_EDIT = "customer.edit"
    CUSTOMER_DELETE = "customer.delete"

    # Catalogue & pricing
    PRODUCT_VIEW = "product.view"
    PRODUCT_CREATE = "product.create"
    PRODUCT_EDIT = "product.edit"
    PRICE_VIEW = "price.view"
    PRICE_MANAGE = "price.manage"
    PRICE_IMPORT = "price.import"
    PRICE_MANAGE_TIERS = "price.manage_tiers"
    PLATE_RATE_MANAGE = "plate_rate.manage"

    # Container shipping
    SHIPMENT_EDIT = "shipment.edit"
    SHIPMENT_VIEW_FREIGHT = "shipment.view_freight"
    SHIPMENT_EDIT_FREIGHT = "shipment.edit_freight"
    SHIPPING_LINE_MANAGE = "shipping_line.manage"

    # Finance configuration
    TAX_MANAGE = "tax.manage"
    FX_MANAGE = "fx.manage"
    APPROVAL_LIMITS_MANAGE = "approval_limits.manage"

    # Terms
    TERMS_MANAGE_TEMPLATES = "terms.manage_templates"

    # Reporting
    REPORT_VIEW = "report.view"
    REPORT_VIEW_ALL = "report.view_all"

    # Administration
    USER_MANAGE = "user.manage"
    ROLE_MANAGE = "role.manage"
    SETTINGS_MANAGE = "settings.manage"
    AUDIT_VIEW_OWN = "audit.view_own"
    AUDIT_VIEW_ALL = "audit.view_all"


PERMISSION_CATEGORIES: dict[Perm, str] = {
    **{p: "Quotations" for p in (
        Perm.QUOTE_CREATE, Perm.QUOTE_EDIT_OWN_DRAFT, Perm.QUOTE_EDIT_ANY_DRAFT,
        Perm.QUOTE_VIEW_OWN, Perm.QUOTE_VIEW_TEAM, Perm.QUOTE_VIEW_ALL,
        Perm.QUOTE_SUBMIT_FOR_APPROVAL, Perm.QUOTE_APPROVE, Perm.QUOTE_REJECT,
        Perm.QUOTE_RETURN_FOR_REVISION, Perm.QUOTE_OVERRIDE_WARNING,
        Perm.QUOTE_APPROVE_CUSTOM_PRICE, Perm.QUOTE_GENERATE_PDF,
        Perm.QUOTE_UPDATE_STATUS, Perm.QUOTE_CREATE_REVISION, Perm.QUOTE_CANCEL,
        Perm.QUOTE_EXPORT, Perm.QUOTE_DELETE_DRAFT, Perm.QUOTE_DELETE_ANY,
    )},
    **{p: "Internal financials" for p in (
        Perm.COST_VIEW, Perm.COST_MANAGE, Perm.MARGIN_VIEW,
    )},
    **{p: "Customers" for p in (
        Perm.CUSTOMER_VIEW, Perm.CUSTOMER_CREATE, Perm.CUSTOMER_EDIT, Perm.CUSTOMER_DELETE,
    )},
    **{p: "Catalogue & pricing" for p in (
        Perm.PRODUCT_VIEW, Perm.PRODUCT_CREATE, Perm.PRODUCT_EDIT, Perm.PRICE_VIEW,
        Perm.PRICE_MANAGE, Perm.PRICE_IMPORT, Perm.PRICE_MANAGE_TIERS, Perm.PLATE_RATE_MANAGE,
    )},
    **{p: "Container shipping" for p in (
        Perm.SHIPMENT_EDIT, Perm.SHIPMENT_VIEW_FREIGHT,
        Perm.SHIPMENT_EDIT_FREIGHT, Perm.SHIPPING_LINE_MANAGE,
    )},
    **{p: "Finance configuration" for p in (
        Perm.TAX_MANAGE, Perm.FX_MANAGE, Perm.APPROVAL_LIMITS_MANAGE,
    )},
    Perm.TERMS_MANAGE_TEMPLATES: "Terms",
    **{p: "Reporting" for p in (Perm.REPORT_VIEW, Perm.REPORT_VIEW_ALL)},
    **{p: "Administration" for p in (
        Perm.USER_MANAGE, Perm.ROLE_MANAGE, Perm.SETTINGS_MANAGE,
        Perm.AUDIT_VIEW_OWN, Perm.AUDIT_VIEW_ALL,
    )},
}


#: The permission matrix from docs/PHASE1_ARCHITECTURE.md §7.
#: ``COST_VIEW`` and ``MARGIN_VIEW`` are deliberately absent from SALES — the
#: brief grants them to a sales employee only "unless permission is granted",
#: so they are assigned per user rather than baked into the role.
ROLE_PERMISSIONS: dict[RoleCode, frozenset[Perm]] = {
    RoleCode.SALES: frozenset({
        Perm.QUOTE_CREATE, Perm.QUOTE_EDIT_OWN_DRAFT, Perm.QUOTE_VIEW_OWN,
        Perm.QUOTE_SUBMIT_FOR_APPROVAL, Perm.QUOTE_GENERATE_PDF,
        Perm.QUOTE_UPDATE_STATUS, Perm.QUOTE_CREATE_REVISION, Perm.QUOTE_EXPORT,
        Perm.QUOTE_DELETE_DRAFT,
        Perm.CUSTOMER_VIEW, Perm.CUSTOMER_CREATE, Perm.CUSTOMER_EDIT,
        Perm.PRODUCT_VIEW, Perm.PRICE_VIEW,
        Perm.SHIPMENT_EDIT,
        Perm.REPORT_VIEW, Perm.AUDIT_VIEW_OWN,
    }),
    RoleCode.SALES_MANAGER: frozenset({
        Perm.QUOTE_CREATE, Perm.QUOTE_EDIT_OWN_DRAFT, Perm.QUOTE_EDIT_ANY_DRAFT,
        Perm.QUOTE_VIEW_OWN, Perm.QUOTE_VIEW_TEAM,
        Perm.QUOTE_SUBMIT_FOR_APPROVAL, Perm.QUOTE_APPROVE, Perm.QUOTE_REJECT,
        Perm.QUOTE_RETURN_FOR_REVISION, Perm.QUOTE_OVERRIDE_WARNING,
        Perm.QUOTE_APPROVE_CUSTOM_PRICE, Perm.QUOTE_GENERATE_PDF,
        Perm.QUOTE_UPDATE_STATUS, Perm.QUOTE_CREATE_REVISION, Perm.QUOTE_CANCEL,
        Perm.QUOTE_EXPORT, Perm.QUOTE_DELETE_DRAFT,
        Perm.COST_VIEW, Perm.MARGIN_VIEW,
        Perm.CUSTOMER_VIEW, Perm.CUSTOMER_CREATE, Perm.CUSTOMER_EDIT, Perm.CUSTOMER_DELETE,
        Perm.PRODUCT_VIEW, Perm.PRICE_VIEW,
        Perm.SHIPMENT_EDIT, Perm.SHIPMENT_VIEW_FREIGHT, Perm.SHIPMENT_EDIT_FREIGHT,
        Perm.TERMS_MANAGE_TEMPLATES,
        Perm.REPORT_VIEW, Perm.REPORT_VIEW_ALL,
        Perm.AUDIT_VIEW_OWN, Perm.AUDIT_VIEW_ALL,
    }),
    RoleCode.FINANCE: frozenset({
        Perm.QUOTE_VIEW_OWN, Perm.QUOTE_VIEW_TEAM, Perm.QUOTE_VIEW_ALL,
        Perm.QUOTE_APPROVE_CUSTOM_PRICE, Perm.QUOTE_GENERATE_PDF, Perm.QUOTE_EXPORT,
        Perm.COST_VIEW, Perm.COST_MANAGE, Perm.MARGIN_VIEW,
        Perm.CUSTOMER_VIEW, Perm.CUSTOMER_CREATE, Perm.CUSTOMER_EDIT,
        Perm.PRODUCT_VIEW, Perm.PRICE_VIEW,
        Perm.SHIPMENT_VIEW_FREIGHT, Perm.SHIPMENT_EDIT_FREIGHT,
        Perm.PLATE_RATE_MANAGE, Perm.TAX_MANAGE, Perm.FX_MANAGE,
        Perm.APPROVAL_LIMITS_MANAGE, Perm.TERMS_MANAGE_TEMPLATES,
        Perm.REPORT_VIEW, Perm.REPORT_VIEW_ALL,
        Perm.AUDIT_VIEW_OWN, Perm.AUDIT_VIEW_ALL,
    }),
    RoleCode.PRICING_ADMIN: frozenset({
        Perm.PRODUCT_VIEW, Perm.PRODUCT_CREATE, Perm.PRODUCT_EDIT,
        Perm.PRICE_VIEW, Perm.PRICE_MANAGE, Perm.PRICE_IMPORT, Perm.PRICE_MANAGE_TIERS,
        Perm.PLATE_RATE_MANAGE, Perm.COST_MANAGE,
        Perm.REPORT_VIEW, Perm.AUDIT_VIEW_OWN,
    }),
    # Every permission. Note that this still does not let a System Administrator
    # approve their own quotation: approval_service rejects self-approval by
    # identity, before any permission is consulted.
    RoleCode.SYS_ADMIN: frozenset(Perm),
}


# --------------------------------------------------------------------------- #
# Quotation status
# --------------------------------------------------------------------------- #

class QuotationStatus(StrEnum):
    DRAFT = "DRAFT"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED_INTERNALLY = "REJECTED_INTERNALLY"
    SENT_TO_CUSTOMER = "SENT_TO_CUSTOMER"
    ACCEPTED = "ACCEPTED"
    REVISION_REQUIRED = "REVISION_REQUIRED"
    LOST = "LOST"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


STATUS_DISPLAY_NAMES: dict[QuotationStatus, str] = {
    QuotationStatus.DRAFT: "Draft",
    QuotationStatus.PENDING_APPROVAL: "Pending Approval",
    QuotationStatus.APPROVED: "Approved",
    QuotationStatus.REJECTED_INTERNALLY: "Rejected Internally",
    QuotationStatus.SENT_TO_CUSTOMER: "Sent to Customer",
    QuotationStatus.ACCEPTED: "Accepted",
    QuotationStatus.REVISION_REQUIRED: "Revision Required",
    QuotationStatus.LOST: "Lost",
    QuotationStatus.EXPIRED: "Expired",
    QuotationStatus.CANCELLED: "Cancelled",
}

#: The only legal status moves. ``quotation_service.change_status`` is the sole
#: writer of ``quotations.status`` and refuses anything not listed here.
STATUS_TRANSITIONS: dict[QuotationStatus, frozenset[QuotationStatus]] = {
    QuotationStatus.DRAFT: frozenset({
        QuotationStatus.PENDING_APPROVAL,
        QuotationStatus.APPROVED,          # only when no approval rule fired
        QuotationStatus.CANCELLED,
    }),
    QuotationStatus.PENDING_APPROVAL: frozenset({
        QuotationStatus.APPROVED,
        QuotationStatus.REJECTED_INTERNALLY,
        QuotationStatus.REVISION_REQUIRED,
        QuotationStatus.CANCELLED,
    }),
    QuotationStatus.APPROVED: frozenset({
        QuotationStatus.SENT_TO_CUSTOMER,
        QuotationStatus.REVISION_REQUIRED,
        QuotationStatus.EXPIRED,
        QuotationStatus.CANCELLED,
    }),
    QuotationStatus.REJECTED_INTERNALLY: frozenset({
        QuotationStatus.DRAFT,             # reopened for rework
        QuotationStatus.CANCELLED,
    }),
    QuotationStatus.SENT_TO_CUSTOMER: frozenset({
        QuotationStatus.ACCEPTED,
        QuotationStatus.LOST,
        QuotationStatus.REVISION_REQUIRED,
        QuotationStatus.EXPIRED,
        QuotationStatus.CANCELLED,
    }),
    QuotationStatus.REVISION_REQUIRED: frozenset({
        QuotationStatus.DRAFT,             # via a new revision
        QuotationStatus.CANCELLED,
    }),
    QuotationStatus.ACCEPTED: frozenset(),   # terminal
    QuotationStatus.LOST: frozenset(),       # terminal
    QuotationStatus.CANCELLED: frozenset(),  # terminal
    QuotationStatus.EXPIRED: frozenset({
        QuotationStatus.REVISION_REQUIRED,   # re-quote at current prices
        QuotationStatus.CANCELLED,
    }),
}

#: Statuses whose transition requires a mandatory explanatory note.
STATUSES_REQUIRING_NOTE: frozenset[QuotationStatus] = frozenset({
    QuotationStatus.REJECTED_INTERNALLY,
    QuotationStatus.REVISION_REQUIRED,
    QuotationStatus.LOST,
    QuotationStatus.CANCELLED,
})

#: Statuses at or beyond which the quotation has left the building. Editing one
#: creates a new revision instead of mutating it.
ISSUED_STATUSES: frozenset[QuotationStatus] = frozenset({
    QuotationStatus.SENT_TO_CUSTOMER,
    QuotationStatus.ACCEPTED,
    QuotationStatus.LOST,
    QuotationStatus.EXPIRED,
})


# --------------------------------------------------------------------------- #
# Pricing
# --------------------------------------------------------------------------- #

class PriceTierCode(StrEnum):
    STANDARD = "STANDARD"
    THREE_CONTAINER = "THREE_CONTAINER"
    EIGHT_CONTAINER = "EIGHT_CONTAINER"
    CUSTOM = "CUSTOM"


#: ``min_containers`` seeds for price_tiers. Drives the quantity warnings
#: declaratively, so a future twelve-container tier is a data row, not a code
#: change. The selected tier is authoritative: quantity NEVER re-selects it.
PRICE_TIER_SEED: dict[PriceTierCode, dict[str, object]] = {
    PriceTierCode.STANDARD: {
        "name": "Standard", "min_containers": None,
        "requires_approval": False, "sort_order": 10,
    },
    PriceTierCode.THREE_CONTAINER: {
        "name": "Three Containers", "min_containers": 3,
        "requires_approval": False, "sort_order": 20,
    },
    PriceTierCode.EIGHT_CONTAINER: {
        "name": "Eight Containers", "min_containers": 8,
        "requires_approval": False, "sort_order": 30,
    },
    PriceTierCode.CUSTOM: {
        "name": "Custom", "min_containers": None,
        "requires_approval": True, "sort_order": 40,
    },
}


class PricingBasis(StrEnum):
    """Which price column drove a line's money.

    This exists because the reference workbook's pack and piece prices disagree
    by up to one rounding unit on 25 of its 69 price pairs (see
    docs/PHASE1_REFERENCE_ANALYSIS.md §1.2). Quoting by packs and quoting by
    pieces therefore produce different totals, and an issued quotation has to
    record which route produced its figures to stay reproducible.
    """

    PACK = "PACK"
    PIECE = "PIECE"


class PriceWarningCode(StrEnum):
    TIER_CONTAINERS_SHORT = "TIER_CONTAINERS_SHORT"
    PRICE_EXPIRED = "PRICE_EXPIRED"
    PRICE_MISSING = "PRICE_MISSING"
    PIECE_PACK_MISMATCH = "PIECE_PACK_MISMATCH"
    CUSTOM_PRICE_BELOW_FLOOR = "CUSTOM_PRICE_BELOW_FLOOR"
    BELOW_MOQ = "BELOW_MOQ"
    DUPLICATE_LINE = "DUPLICATE_LINE"
    MIX_LIMIT = "MIX_LIMIT"
    DUPLICATE_FREIGHT = "DUPLICATE_FREIGHT"
    CONTAINER_CAPACITY_UNKNOWN = "CONTAINER_CAPACITY_UNKNOWN"


class WarningSeverity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    BLOCKING = "BLOCKING"


# --------------------------------------------------------------------------- #
# Container shipping
# --------------------------------------------------------------------------- #

class ContainerSize(StrEnum):
    TWENTY_FT = "20FT"
    FORTY_FT = "40FT"
    FORTY_FT_HC = "40FT_HC"
    FORTY_FIVE_FT_HC = "45FT_HC"
    CUSTOM = "CUSTOM"


CONTAINER_SIZE_LABELS: dict[ContainerSize, str] = {
    ContainerSize.TWENTY_FT: "20 ft",
    ContainerSize.FORTY_FT: "40 ft",
    ContainerSize.FORTY_FT_HC: "40 ft High Cube",
    ContainerSize.FORTY_FIVE_FT_HC: "45 ft High Cube",
    ContainerSize.CUSTOM: "Custom",
}


class ContainerType(StrEnum):
    DRY = "DRY"
    HIGH_CUBE = "HIGH_CUBE"
    REFRIGERATED = "REFRIGERATED"
    OPEN_TOP = "OPEN_TOP"
    FLAT_RACK = "FLAT_RACK"
    CUSTOM = "CUSTOM"


CONTAINER_TYPE_LABELS: dict[ContainerType, str] = {
    ContainerType.DRY: "Dry",
    ContainerType.HIGH_CUBE: "High Cube",
    ContainerType.REFRIGERATED: "Refrigerated",
    ContainerType.OPEN_TOP: "Open Top",
    ContainerType.FLAT_RACK: "Flat Rack",
    ContainerType.CUSTOM: "Custom",
}

#: The reference price list ships in 40' high-cube dry containers, floor loaded,
#: so those are the defaults. Both remain editable per container row.
DEFAULT_CONTAINER_SIZE = ContainerSize.FORTY_FT_HC
DEFAULT_CONTAINER_TYPE = ContainerType.DRY


class FreightMethod(StrEnum):
    """How container freight relates to the price the customer is quoted.

    Only ``ADDED_SEPARATELY`` produces a quotation charge. The other two are
    recorded against the shipment and never become charges, because
    ``calculation_engine.compute_totals`` adds **every** charge to the grand
    total regardless of customer visibility — an "internal only" charge is
    still money the customer pays, just not itemised. Making included or
    internal freight a charge would silently inflate the quotation.
    """

    INCLUDED = "INCLUDED"
    ADDED_SEPARATELY = "ADDED_SEPARATELY"
    INTERNAL_ONLY = "INTERNAL_ONLY"


FREIGHT_METHOD_LABELS: dict[FreightMethod, str] = {
    FreightMethod.INCLUDED: "Freight included in the price",
    FreightMethod.ADDED_SEPARATELY: "Freight added as a separate charge",
    FreightMethod.INTERNAL_ONLY: "Freight internal only (margin and landed cost)",
}

#: Marks the single quotation charge derived from a shipment. Reconciled to at
#: most one row so freight can never be counted twice.
CHARGE_SOURCE_SHIPMENT = "shipment"


class Incoterm(StrEnum):
    EXW = "EXW"
    FCA = "FCA"
    FAS = "FAS"
    FOB = "FOB"
    CFR = "CFR"
    CIF = "CIF"
    CPT = "CPT"
    CIP = "CIP"
    DAP = "DAP"
    DPU = "DPU"
    DDP = "DDP"


#: From the reference price list: "FOB Çerkezköy (Türkiye) (INCOTERMS 2020)".
DEFAULT_INCOTERM = Incoterm.FOB


class LoadingMethod(StrEnum):
    FLOOR_LOADED = "FLOOR_LOADED"
    PALLETISED = "PALLETISED"
    SLIP_SHEET = "SLIP_SHEET"
    OTHER = "OTHER"


LOADING_METHOD_LABELS: dict[LoadingMethod, str] = {
    LoadingMethod.FLOOR_LOADED: "Floor loaded",
    LoadingMethod.PALLETISED: "Palletised",
    LoadingMethod.SLIP_SHEET: "Slip sheet",
    LoadingMethod.OTHER: "Other",
}

DEFAULT_LOADING_METHOD = LoadingMethod.FLOOR_LOADED

#: Seeded carriers. Maintained as data from Company Settings, so this list is a
#: starting point rather than a fixed set.
DEFAULT_SHIPPING_LINES: tuple[str, ...] = (
    "Maersk", "MSC", "CMA CGM", "Hapag-Lloyd", "COSCO",
    "ONE", "Evergreen", "Yang Ming", "ZIM",
)


# --------------------------------------------------------------------------- #
# Charges
# --------------------------------------------------------------------------- #

class ChargeType(StrEnum):
    PRINTING_PLATES = "PRINTING_PLATES"
    CUTTING_DIES = "CUTTING_DIES"
    TOOLING = "TOOLING"
    ARTWORK = "ARTWORK"
    SETUP = "SETUP"
    FREIGHT = "FREIGHT"
    BROKERAGE = "BROKERAGE"
    DUTY = "DUTY"
    PALLETS = "PALLETS"
    SAMPLES = "SAMPLES"
    RUSH_PRODUCTION = "RUSH_PRODUCTION"
    FUEL_SURCHARGE = "FUEL_SURCHARGE"
    OTHER = "OTHER"


CHARGE_TYPE_DISPLAY_NAMES: dict[ChargeType, str] = {
    ChargeType.PRINTING_PLATES: "Printing plates",
    ChargeType.CUTTING_DIES: "Cutting dies",
    ChargeType.TOOLING: "Tooling",
    ChargeType.ARTWORK: "Artwork",
    ChargeType.SETUP: "Setup",
    ChargeType.FREIGHT: "Freight",
    ChargeType.BROKERAGE: "Brokerage",
    ChargeType.DUTY: "Duty",
    ChargeType.PALLETS: "Pallets",
    ChargeType.SAMPLES: "Samples",
    ChargeType.RUSH_PRODUCTION: "Rush production",
    ChargeType.FUEL_SURCHARGE: "Fuel surcharge",
    ChargeType.OTHER: "Other",
}


# --------------------------------------------------------------------------- #
# Terms
# --------------------------------------------------------------------------- #

class TermSection(StrEnum):
    PAYMENT_TERMS = "PAYMENT_TERMS"
    QUOTATION_VALIDITY = "QUOTATION_VALIDITY"
    LEAD_TIME = "LEAD_TIME"
    PRODUCTION_TIME = "PRODUCTION_TIME"
    FREIGHT = "FREIGHT"
    DELIVERY_TERMS = "DELIVERY_TERMS"
    INCOTERMS = "INCOTERMS"
    CONTAINER_TYPE = "CONTAINER_TYPE"
    LOADING_METHOD = "LOADING_METHOD"
    CONTAINER_MIX_LIMIT = "CONTAINER_MIX_LIMIT"
    PRINTING = "PRINTING"
    PRINTING_PLATE_CHARGES = "PRINTING_PLATE_CHARGES"
    ARTWORK_APPROVAL = "ARTWORK_APPROVAL"
    STRUCTURAL_APPROVAL = "STRUCTURAL_APPROVAL"
    RAW_MATERIAL_ADJUSTMENT = "RAW_MATERIAL_ADJUSTMENT"
    PURCHASE_ORDER = "PURCHASE_ORDER"
    MOQ = "MOQ"
    OVERRUN_UNDERRUN = "OVERRUN_UNDERRUN"
    GENERAL_NOTES = "GENERAL_NOTES"


# --------------------------------------------------------------------------- #
# Approval
# --------------------------------------------------------------------------- #

class ApprovalDecision(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    RETURNED_FOR_REVISION = "RETURNED_FOR_REVISION"


class ApprovalTrigger(StrEnum):
    CUSTOM_PRICE_USED = "CUSTOM_PRICE_USED"
    PRICE_MANUALLY_OVERRIDDEN = "PRICE_MANUALLY_OVERRIDDEN"
    DISCOUNT_ABOVE_LIMIT = "DISCOUNT_ABOVE_LIMIT"
    MARGIN_BELOW_THRESHOLD = "MARGIN_BELOW_THRESHOLD"
    VALUE_ABOVE_AUTHORITY = "VALUE_ABOVE_AUTHORITY"
    PAYMENT_TERMS_EXCEEDED = "PAYMENT_TERMS_EXCEEDED"
    EXPIRED_PRICE_USED = "EXPIRED_PRICE_USED"
    WARNING_OVERRIDE_REQUESTED = "WARNING_OVERRIDE_REQUESTED"


# --------------------------------------------------------------------------- #
# Customers, responses, imports
# --------------------------------------------------------------------------- #

class CustomerStatus(StrEnum):
    ACTIVE = "ACTIVE"
    PROSPECT = "PROSPECT"
    INACTIVE = "INACTIVE"
    ON_HOLD = "ON_HOLD"


class AddressType(StrEnum):
    BILLING = "BILLING"
    SHIPPING = "SHIPPING"


class SendMethod(StrEnum):
    EMAIL = "EMAIL"
    COURIER = "COURIER"
    IN_PERSON = "IN_PERSON"
    PORTAL_UPLOAD = "PORTAL_UPLOAD"   # the customer's own portal, not ours
    OTHER = "OTHER"


class CustomerResponse(StrEnum):
    NO_RESPONSE = "NO_RESPONSE"
    ACCEPTED = "ACCEPTED"
    REVISION_REQUESTED = "REVISION_REQUESTED"
    LOST = "LOST"


class ImportJobStatus(StrEnum):
    PENDING = "PENDING"
    PREVIEWED = "PREVIEWED"
    COMMITTED = "COMMITTED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class ImportRowAction(StrEnum):
    CREATE = "CREATE"
    UPDATE = "UPDATE"
    SKIP = "SKIP"


class ImportRowStatus(StrEnum):
    OK = "OK"
    ERROR = "ERROR"
    DUPLICATE = "DUPLICATE"


# --------------------------------------------------------------------------- #
# Audit
# --------------------------------------------------------------------------- #

class AuditAction(StrEnum):
    LOGIN = "LOGIN"
    LOGIN_FAILED = "LOGIN_FAILED"
    LOGOUT = "LOGOUT"
    SESSION_EXPIRED = "SESSION_EXPIRED"
    PASSWORD_CHANGED = "PASSWORD_CHANGED"
    PASSWORD_RESET = "PASSWORD_RESET"

    QUOTATION_CREATED = "QUOTATION_CREATED"
    QUOTATION_EDITED = "QUOTATION_EDITED"
    QUOTATION_ITEM_ADDED = "QUOTATION_ITEM_ADDED"
    QUOTATION_ITEM_REMOVED = "QUOTATION_ITEM_REMOVED"
    QUANTITY_CHANGED = "QUANTITY_CHANGED"
    PRICE_CHANGED = "PRICE_CHANGED"
    PRICE_OVERRIDDEN = "PRICE_OVERRIDDEN"
    DISCOUNT_CHANGED = "DISCOUNT_CHANGED"
    CUSTOM_PRICE_USED = "CUSTOM_PRICE_USED"
    TERMS_CHANGED = "TERMS_CHANGED"
    APPROVAL_REQUESTED = "APPROVAL_REQUESTED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    WARNING_OVERRIDDEN = "WARNING_OVERRIDDEN"
    PDF_GENERATED = "PDF_GENERATED"
    STATUS_CHANGED = "STATUS_CHANGED"
    REVISION_CREATED = "REVISION_CREATED"
    CUSTOMER_RESPONSE_LOGGED = "CUSTOMER_RESPONSE_LOGGED"
    QUOTATION_DELETED = "QUOTATION_DELETED"
    QUOTATION_RESTORED = "QUOTATION_RESTORED"

    CUSTOMER_CREATED = "CUSTOMER_CREATED"
    CUSTOMER_EDITED = "CUSTOMER_EDITED"
    PRODUCT_CREATED = "PRODUCT_CREATED"
    PRODUCT_EDITED = "PRODUCT_EDITED"
    COST_CHANGED = "COST_CHANGED"
    PRICE_LIST_IMPORTED = "PRICE_LIST_IMPORTED"
    CONTAINER_CAPACITY_IMPORTED = "CONTAINER_CAPACITY_IMPORTED"
    SHIPMENT_EDITED = "SHIPMENT_EDITED"
    CONTAINER_ADDED = "CONTAINER_ADDED"
    CONTAINER_REMOVED = "CONTAINER_REMOVED"
    FREIGHT_CHANGED = "FREIGHT_CHANGED"
    IMPORT_FAILED = "IMPORT_FAILED"

    SETTINGS_CHANGED = "SETTINGS_CHANGED"
    USER_CREATED = "USER_CREATED"
    USER_EDITED = "USER_EDITED"
    USER_DISABLED = "USER_DISABLED"
    ROLE_ASSIGNED = "ROLE_ASSIGNED"
    ROLE_REVOKED = "ROLE_REVOKED"
    PERMISSION_DENIED = "PERMISSION_DENIED"


# --------------------------------------------------------------------------- #
# Misc
# --------------------------------------------------------------------------- #

class EntityType(StrEnum):
    """Target of an audit row or a polymorphic attachment."""

    USER = "USER"
    ROLE = "ROLE"
    CUSTOMER = "CUSTOMER"
    PRODUCT = "PRODUCT"
    PRODUCT_VARIANT = "PRODUCT_VARIANT"
    PRODUCT_PRICE = "PRODUCT_PRICE"
    PRODUCT_COST = "PRODUCT_COST"
    QUOTATION = "QUOTATION"
    QUOTATION_ITEM = "QUOTATION_ITEM"
    QUOTATION_CHARGE = "QUOTATION_CHARGE"
    QUOTATION_TERM = "QUOTATION_TERM"
    APPROVAL = "APPROVAL"
    IMPORT_JOB = "IMPORT_JOB"
    COMPANY_SETTINGS = "COMPANY_SETTINGS"
    APP_SETTING = "APP_SETTING"
    TERM_TEMPLATE = "TERM_TEMPLATE"
    SHIPPING_LINE = "SHIPPING_LINE"
    QUOTATION_SHIPMENT = "QUOTATION_SHIPMENT"
    SHIPMENT_CONTAINER = "SHIPMENT_CONTAINER"
    PRODUCT_CONTAINER_CAPACITY = "PRODUCT_CONTAINER_CAPACITY"
    SESSION = "SESSION"


#: Fallback currencies offered in the UI. The operative default lives in
#: company_settings.default_currency; the workbook's plate note is USD.
SUPPORTED_CURRENCIES: tuple[str, ...] = ("USD", "EUR", "CAD", "GBP", "TRY")

DEFAULT_CURRENCY = "USD"
