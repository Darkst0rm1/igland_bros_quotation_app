"""Customer, contact and address operations.

Every entry point checks permission and writes an audit row. Pages call these
functions; they never construct or mutate an ORM object themselves.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from modules.audit_service import record_audit, record_field_changes
from modules.authorization import AuthUser, require
from modules.constants import AddressType, AuditAction, EntityType, Perm
from modules.models import Customer, CustomerAddress, CustomerContact
from modules.repositories import customer_number_exists, get_customer
from modules.validation import AddressInput, ContactInput, CustomerInput

log = logging.getLogger(__name__)


class CustomerError(ValueError):
    """A customer operation that failed a business rule. Safe to show the user."""


def _snapshot(customer: Customer) -> dict[str, object]:
    return {
        "customer_number": customer.customer_number,
        "company_name": customer.company_name,
        "default_currency": customer.default_currency,
        "payment_terms": customer.payment_terms,
        "payment_terms_days": customer.payment_terms_days,
        "assigned_sales_user_id": customer.assigned_sales_user_id,
        "status": str(customer.status),
        "notes": customer.notes,
    }


# --------------------------------------------------------------------------- #
# Customers
# --------------------------------------------------------------------------- #

def create_customer(session: Session, user: AuthUser, data: CustomerInput) -> Customer:
    require(user, Perm.CUSTOMER_CREATE)

    if customer_number_exists(session, data.customer_number):
        raise CustomerError(
            f"Customer number {data.customer_number!r} is already in use."
        )

    customer = Customer(
        customer_number=data.customer_number,
        company_name=data.company_name,
        default_currency=data.default_currency,
        payment_terms=data.payment_terms,
        payment_terms_days=data.payment_terms_days,
        assigned_sales_user_id=data.assigned_sales_user_id,
        status=data.status,
        notes=data.notes,
        created_by_id=user.id,
        updated_by_id=user.id,
    )
    session.add(customer)
    session.flush()

    record_audit(
        session, user, AuditAction.CUSTOMER_CREATED, EntityType.CUSTOMER, customer.id,
        new_value=_snapshot(customer),
    )
    log.info("Customer created: %s (%s)", customer.company_name, customer.customer_number)
    return customer


def update_customer(
    session: Session, user: AuthUser, customer_id: int, data: CustomerInput
) -> Customer:
    require(user, Perm.CUSTOMER_EDIT)

    customer = get_customer(session, customer_id)
    if customer is None:
        raise CustomerError("That customer no longer exists.")

    if customer_number_exists(session, data.customer_number, exclude_id=customer_id):
        raise CustomerError(
            f"Customer number {data.customer_number!r} is already in use."
        )

    before = _snapshot(customer)
    customer.customer_number = data.customer_number
    customer.company_name = data.company_name
    customer.default_currency = data.default_currency
    customer.payment_terms = data.payment_terms
    customer.payment_terms_days = data.payment_terms_days
    customer.assigned_sales_user_id = data.assigned_sales_user_id
    customer.status = data.status
    customer.notes = data.notes
    customer.updated_by_id = user.id
    session.flush()

    # Note this does NOT touch any quotation. Issued quotations carry their own
    # snapshot of the customer name and addresses precisely so that renaming a
    # customer cannot alter what was sent to them.
    record_field_changes(
        session, user, AuditAction.CUSTOMER_EDITED, EntityType.CUSTOMER, customer.id,
        before, _snapshot(customer),
    )
    return customer


def deactivate_customer(
    session: Session, user: AuthUser, customer_id: int, reason: str
) -> Customer:
    """Soft-delete. Quotation history is retained and remains readable."""
    require(user, Perm.CUSTOMER_DELETE)
    if not reason or not reason.strip():
        raise CustomerError("A reason is required to remove a customer.")

    customer = get_customer(session, customer_id)
    if customer is None:
        raise CustomerError("That customer no longer exists.")

    import datetime as dt

    customer.deleted_at = dt.datetime.now(dt.UTC)
    customer.updated_by_id = user.id
    session.flush()

    record_audit(
        session, user, AuditAction.CUSTOMER_EDITED, EntityType.CUSTOMER, customer.id,
        old_value={"deleted_at": None}, new_value={"deleted_at": "set"}, reason=reason,
    )
    return customer


# --------------------------------------------------------------------------- #
# Contacts
# --------------------------------------------------------------------------- #

def add_contact(
    session: Session, user: AuthUser, customer_id: int, data: ContactInput
) -> CustomerContact:
    require(user, Perm.CUSTOMER_EDIT)

    customer = get_customer(session, customer_id)
    if customer is None:
        raise CustomerError("That customer no longer exists.")

    contact = CustomerContact(
        customer_id=customer_id,
        name=data.name,
        title=data.title,
        email=data.email,
        phone=data.phone,
        is_primary=data.is_primary,
        is_active=data.is_active,
    )
    session.add(contact)
    session.flush()

    if data.is_primary:
        _demote_other_primaries(session, customer_id, contact.id)

    record_audit(
        session, user, AuditAction.CUSTOMER_EDITED, EntityType.CUSTOMER, customer_id,
        new_value={"contact_added": data.name},
    )
    return contact


def update_contact(
    session: Session, user: AuthUser, contact_id: int, data: ContactInput
) -> CustomerContact:
    require(user, Perm.CUSTOMER_EDIT)

    contact = session.get(CustomerContact, contact_id)
    if contact is None:
        raise CustomerError("That contact no longer exists.")

    before = {
        "name": contact.name, "title": contact.title, "email": contact.email,
        "phone": contact.phone, "is_primary": contact.is_primary,
        "is_active": contact.is_active,
    }
    contact.name = data.name
    contact.title = data.title
    contact.email = data.email
    contact.phone = data.phone
    contact.is_primary = data.is_primary
    contact.is_active = data.is_active
    session.flush()

    if data.is_primary:
        _demote_other_primaries(session, contact.customer_id, contact.id)

    record_field_changes(
        session, user, AuditAction.CUSTOMER_EDITED, EntityType.CUSTOMER,
        contact.customer_id, before,
        {
            "name": contact.name, "title": contact.title, "email": contact.email,
            "phone": contact.phone, "is_primary": contact.is_primary,
            "is_active": contact.is_active,
        },
    )
    return contact


def _demote_other_primaries(
    session: Session, customer_id: int, keep_contact_id: int
) -> None:
    """Exactly one primary contact per customer.

    Enforced here rather than by a constraint because "at most one row where
    is_primary" needs a partial unique index, which SQLite does not support —
    the dev and test databases would then behave differently from production.

    Issued as an explicit UPDATE rather than by walking ``customer.contacts``:
    SQLAlchemy's identity map returns the already-loaded customer with its
    previously-loaded collection, so a contact added earlier in the same
    session may be missing from it and would silently escape demotion.
    """
    session.query(CustomerContact).filter(
        CustomerContact.customer_id == customer_id,
        CustomerContact.id != keep_contact_id,
        CustomerContact.is_primary.is_(True),
    ).update({"is_primary": False}, synchronize_session="fetch")


# --------------------------------------------------------------------------- #
# Addresses
# --------------------------------------------------------------------------- #

def add_address(
    session: Session, user: AuthUser, customer_id: int, data: AddressInput
) -> CustomerAddress:
    require(user, Perm.CUSTOMER_EDIT)

    customer = get_customer(session, customer_id)
    if customer is None:
        raise CustomerError("That customer no longer exists.")

    address = CustomerAddress(
        customer_id=customer_id,
        address_type=data.address_type,
        label=data.label,
        line1=data.line1,
        line2=data.line2,
        city=data.city,
        province=data.province,
        postal_code=data.postal_code,
        country=data.country,
        is_default=data.is_default,
    )
    session.add(address)
    session.flush()

    if data.is_default:
        _demote_other_defaults(session, address)

    record_audit(
        session, user, AuditAction.CUSTOMER_EDITED, EntityType.CUSTOMER, customer_id,
        new_value={"address_added": str(data.address_type)},
    )
    return address


def update_address(
    session: Session, user: AuthUser, address_id: int, data: AddressInput
) -> CustomerAddress:
    require(user, Perm.CUSTOMER_EDIT)

    address = session.get(CustomerAddress, address_id)
    if address is None:
        raise CustomerError("That address no longer exists.")

    address.address_type = data.address_type
    address.label = data.label
    address.line1 = data.line1
    address.line2 = data.line2
    address.city = data.city
    address.province = data.province
    address.postal_code = data.postal_code
    address.country = data.country
    address.is_default = data.is_default
    session.flush()

    if data.is_default:
        _demote_other_defaults(session, address)

    record_audit(
        session, user, AuditAction.CUSTOMER_EDITED, EntityType.CUSTOMER,
        address.customer_id, new_value={"address_updated": address.id},
    )
    return address


def _demote_other_defaults(session: Session, keep: CustomerAddress) -> None:
    """One default address per type. Same reasoning as _demote_other_primaries."""
    session.query(CustomerAddress).filter(
        CustomerAddress.customer_id == keep.customer_id,
        CustomerAddress.address_type == keep.address_type,
        CustomerAddress.id != keep.id,
        CustomerAddress.is_default.is_(True),
    ).update({"is_default": False}, synchronize_session="fetch")


def copy_billing_to_shipping(
    session: Session, user: AuthUser, customer_id: int
) -> CustomerAddress:
    """Create a shipping address mirroring the default billing address."""
    require(user, Perm.CUSTOMER_EDIT)

    from modules.repositories import find_default_address

    customer = get_customer(session, customer_id)
    if customer is None:
        raise CustomerError("That customer no longer exists.")

    # Queried rather than read off customer.addresses, which the identity map
    # may have left stale after an address was added in this same session.
    billing = find_default_address(session, customer_id, AddressType.BILLING)
    if billing is None:
        raise CustomerError("This customer has no billing address to copy.")

    return add_address(
        session, user, customer_id,
        AddressInput(
            address_type=AddressType.SHIPPING,
            label=billing.label,
            line1=billing.line1,
            line2=billing.line2,
            city=billing.city,
            province=billing.province,
            postal_code=billing.postal_code,
            country=billing.country,
            is_default=True,
        ),
    )
