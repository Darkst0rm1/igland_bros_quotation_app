"""Serving images to a customer without handing out storage access.

Everything is proxied through this service. The customer never learns a storage
key, a bucket name, an object URL or a database id — they present a quotation
token and an opaque line reference, and get bytes back or a placeholder.

No redirect to signed storage URLs. A redirect puts the object URL in the
browser's history and in the Referer of anything it loads, and it outlives the
page; proxying costs a little bandwidth and leaks nothing.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from modules.models import Quotation, QuotationItem, QuoteAccessToken
from modules.storage import StorageError, get_storage
from portal.projection import line_ref

log = logging.getLogger("portal")

#: Raster only. An SVG is a document that can carry script and external
#: references, so it is refused rather than sanitised — a sanitiser is a
#: parser, and a parser is an attack surface we do not need.
ALLOWED_IMAGE_TYPES: dict[str, str] = {
    "png": "image/png",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
}

#: Refuse anything implausible for a product photograph before it reaches a
#: browser, whatever the storage layer happens to hold.
MAX_IMAGE_BYTES = 5 * 1024 * 1024

#: A neutral grey square. Inlined so a missing product image needs no network
#: request, no external file and no separate 404.
PLACEHOLDER_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000010000000100802000000900691"
    "68000000174944415428cf63fcffff3f0326c8286cd4a8118a19000d2e04fd52"
    "0b7d0e0000000049454e44ae426082"
)


@dataclass(frozen=True)
class ImagePayload:
    content: bytes
    media_type: str
    is_placeholder: bool = False


def placeholder() -> ImagePayload:
    return ImagePayload(PLACEHOLDER_PNG, "image/png", is_placeholder=True)


def sniff_image_type(data: bytes) -> str | None:
    """Identify the format from its magic bytes, never from a filename.

    The stored key's extension is attacker-influenced in the general case and
    says nothing about the content; the first bytes do.
    """
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def resolve_item_by_ref(
    token: QuoteAccessToken, quotation: Quotation, ref: str
) -> QuotationItem | None:
    """Map an opaque reference back to a line on *this* quotation.

    The reference is an HMAC over the token hash and the line id, so it is only
    ever recomputed against lines belonging to the quotation this token opens.
    A reference from another quote — or another token for the same quote —
    simply matches nothing. There is no id to tamper with and no key to guess.
    """
    candidate = (ref or "").strip()
    if not candidate or len(candidate) != 16 or not candidate.isalnum():
        return None
    for item in quotation.items:
        if line_ref(token.token_hash, item.id) == candidate:
            return item
    return None


def image_key_for(item: QuotationItem) -> str | None:
    """The stored key for a line's product photograph, if there is one."""
    variant = item.variant
    if variant is None or variant.product is None:
        return None
    return variant.product.image_key or None


def load_product_image(item: QuotationItem) -> ImagePayload:
    """Fetch, validate and return the image, or a placeholder.

    Every failure — no key, storage error, unknown format, oversized — returns
    the placeholder rather than an error, because a broken picture must not
    stop a customer reading their quotation.
    """
    key = image_key_for(item)
    if not key:
        return placeholder()

    try:
        data = get_storage().get(key)
    except StorageError:
        # Deliberately not logging the key: it is internal detail.
        log.info("Product image unavailable for line %s", item.id)
        return placeholder()
    except Exception:  # noqa: BLE001 — a broken image is never fatal
        log.warning("Unexpected error loading a product image")
        return placeholder()

    if len(data) > MAX_IMAGE_BYTES:
        log.warning(
            "Refusing product image for line %s: %d bytes exceeds the limit",
            item.id, len(data),
        )
        return placeholder()

    kind = sniff_image_type(data)
    if kind is None:
        # Covers SVG, HTML, PDF and anything else masquerading as a photograph.
        log.warning("Refusing product image for line %s: unrecognised format", item.id)
        return placeholder()

    return ImagePayload(data, ALLOWED_IMAGE_TYPES[kind])


def load_company_logo(logo_key: str | None) -> ImagePayload | None:
    """The company logo, under the same validation. ``None`` when unset."""
    if not logo_key:
        return None
    try:
        data = get_storage().get(logo_key)
    except Exception:  # noqa: BLE001
        return None
    if len(data) > MAX_IMAGE_BYTES:
        return None
    kind = sniff_image_type(data)
    if kind is None:
        return None
    return ImagePayload(data, ALLOWED_IMAGE_TYPES[kind])
