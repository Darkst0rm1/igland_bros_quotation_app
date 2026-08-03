"""Producing a quotation document, in either format, under the release gate.

The employee chooses PDF or Word. Both go through the same gate, the same
audit entry and the same storage path — there is no route by which a Word file
escapes a control the PDF is subject to.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy.orm import Session

from modules import (
    approval_service,
    docx_generator,
    document_model,
    pdf_generator,
    settings_service,
)
from modules.audit_service import record_audit
from modules.authorization import AuthUser, require
from modules.constants import AuditAction, EntityType, Perm
from modules.models import Attachment, Quotation
from modules.storage import build_key, get_storage, sha256_of

log = logging.getLogger(__name__)


class DocumentFormat(StrEnum):
    PDF = "PDF"
    DOCX = "DOCX"


FORMAT_LABELS = {DocumentFormat.PDF: "PDF", DocumentFormat.DOCX: "Word (.docx)"}

MIME_TYPES = {
    DocumentFormat.PDF: "application/pdf",
    DocumentFormat.DOCX: (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    ),
}

EXTENSIONS = {DocumentFormat.PDF: ".pdf", DocumentFormat.DOCX: ".docx"}


@dataclass(frozen=True)
class GeneratedDocument:
    filename: str
    mime_type: str
    data: bytes
    is_draft: bool
    document_format: DocumentFormat

    @property
    def size_bytes(self) -> int:
        return len(self.data)


def can_release(session: Session, quotation: Quotation) -> bool:
    return not approval_service.release_blockers(session, quotation)


def generate(
    session: Session,
    user: AuthUser,
    quotation: Quotation,
    document_format: DocumentFormat = DocumentFormat.PDF,
    *,
    draft: bool | None = None,
    store: bool | None = None,
    approver_name: str = "",
) -> GeneratedDocument:
    """Render a quotation.

    ``draft=None`` means "final if the quotation may be released, otherwise a
    DRAFT-marked copy". Asking for a final document that is not permitted
    raises rather than quietly downgrading it — an employee who thinks they
    have sent a firm offer must not be holding a draft.
    """
    require(user, Perm.QUOTE_GENERATE_PDF)

    releasable = can_release(session, quotation)
    if draft is False and not releasable:
        approval_service.assert_release_allowed(session, quotation)
    is_draft = (not releasable) if draft is None else draft

    settings = settings_service.get_company_settings(session)
    page_size = (settings.pdf_page_size if settings else "A4") or "A4"

    model = document_model.build_document(
        session, quotation,
        prepared_by=user.employee_name,
        prepared_by_title=user.job_title or "",
        approved_by=approver_name,
        force_draft=is_draft,
    )

    renderer = pdf_generator if document_format is DocumentFormat.PDF else docx_generator
    data = renderer.render(model, page_size=page_size)

    suffix = "_DRAFT" if is_draft else ""
    filename = f"{model.file_stem}{suffix}{EXTENSIONS[document_format]}"

    generated = GeneratedDocument(
        filename=filename,
        mime_type=MIME_TYPES[document_format],
        data=data,
        is_draft=is_draft,
        document_format=document_format,
    )

    # A draft is a working copy and is not archived; only issued documents are
    # evidence worth keeping.
    should_store = (not is_draft) if store is None else store
    if should_store:
        store_document(session, user, quotation, generated)

    record_audit(
        session, user, AuditAction.PDF_GENERATED, EntityType.QUOTATION, quotation.id,
        new_value={
            "format": document_format.value,
            "filename": filename,
            "draft": is_draft,
            "bytes": len(data),
        },
    )
    log.info(
        "Generated %s for %s (%s)",
        document_format.value, quotation.display_number,
        "draft" if is_draft else "final",
    )
    return generated


def store_document(
    session: Session,
    user: AuthUser,
    quotation: Quotation,
    generated: GeneratedDocument,
) -> Attachment:
    """Archive an issued document and record its hash.

    The SHA-256 is what lets a document produced months ago be checked against
    what is on file — particularly relevant for the Word format, which the
    recipient can edit.
    """
    key = build_key("quotations", generated.filename, identifier=quotation.id)
    get_storage().put(key, generated.data, generated.mime_type)

    attachment = Attachment(
        entity_type=EntityType.QUOTATION.value,
        entity_id=quotation.id,
        file_name=generated.filename,
        storage_key=key,
        content_type=generated.mime_type,
        size_bytes=generated.size_bytes,
        sha256=sha256_of(generated.data),
        is_customer_visible=True,
        uploaded_by_id=user.id,
    )
    session.add(attachment)
    session.flush()
    return attachment


def stored_documents(session: Session, quotation_id: int) -> list[Attachment]:
    from sqlalchemy import select

    return list(
        session.execute(
            select(Attachment)
            .where(
                Attachment.entity_type == EntityType.QUOTATION.value,
                Attachment.entity_id == quotation_id,
            )
            .order_by(Attachment.uploaded_at.desc())
        ).scalars()
    )


def fetch(session: Session, user: AuthUser, attachment_id: int) -> GeneratedDocument:
    """Re-download an archived document.

    Served through the application after a permission check rather than by
    handing out a storage URL — a presigned link would be a capability that
    outlives the session and escapes role checks entirely.
    """
    require(user, Perm.QUOTE_GENERATE_PDF)

    attachment = session.get(Attachment, attachment_id)
    if attachment is None:
        raise FileNotFoundError("That document is no longer on file.")

    data = get_storage().get(attachment.storage_key)
    fmt = (
        DocumentFormat.PDF
        if attachment.file_name.lower().endswith(".pdf")
        else DocumentFormat.DOCX
    )
    return GeneratedDocument(
        filename=attachment.file_name,
        mime_type=attachment.content_type or MIME_TYPES[fmt],
        data=data,
        is_draft="_DRAFT" in attachment.file_name,
        document_format=fmt,
    )
