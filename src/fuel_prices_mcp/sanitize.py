"""Sanitisation of every value that reaches a tool result.

Station names, brands, addresses and amenities are self-reported by forecourt
operators and relayed by the Fuel Finder API, so they are untrusted third-party
input that ends up in a language model's context (indirect prompt injection)
and often in a terminal. Strings are scrubbed of control characters, ANSI/OSC
escape sequences, bidirectional overrides and zero-width characters (Trojan
Source and hidden-instruction tricks), collapsed to one line and length-capped.
Values with a known shape (postcodes, identifiers, timestamps, prices,
coordinates) are validated against that shape and dropped if they do not fit.
"""

from __future__ import annotations

import math
import re
import unicodedata
from datetime import datetime

# CSI (ESC [ ... final), OSC (ESC ] ... BEL or ST) and other two-byte ESC sequences.
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?|\x1b[@-Z\\-_]")

# Cc control, Cf format (bidi overrides, zero-width), Cs surrogate, Co private use,
# Cn unassigned, Zl/Zp line/paragraph separators.
_DROP_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})

# Full UK postcode, e.g. "LL57 2DG", "SW1A 1AA" (shape check, not an existence check).
POSTCODE_RE = re.compile(r"^([A-Z]{1,2}[0-9][A-Z0-9]?) ?([0-9][A-Z]{2})$")
# Outward code only, e.g. "LL57", "SW1A".
OUTCODE_RE = re.compile(r"^[A-Z]{1,2}[0-9][A-Z0-9]?$")

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_AMENITY_RE = re.compile(r"^[a-z0-9][a-z0-9_]{0,47}$")
_TIMESTAMP_RE = re.compile(r"^[0-9T:.+\-Z ]{10,40}$")

# Pence per litre. UK road fuel has never been outside this band; anything else is a
# data-entry error (pounds instead of pence, tenths of a penny) and is dropped.
MIN_PRICE_PPL = 50.0
MAX_PRICE_PPL = 500.0

# Bounding box for Great Britain, Northern Ireland, Scilly, Shetland and the Channel Islands.
UK_LAT = (49.0, 61.0)
UK_LON = (-8.7, 2.0)


def clean_text(value: str, *, max_len: int) -> str:
    """Return a single-line, printable version of ``value`` capped at ``max_len`` characters."""
    value = _ANSI_RE.sub("", value)
    out: list[str] = []
    for ch in unicodedata.normalize("NFC", value):
        if ch.isspace():  # line breaks, tabs and Unicode spaces become plain word separators
            out.append(" ")
        elif unicodedata.category(ch) not in _DROP_CATEGORIES:
            out.append(ch)
    text = " ".join("".join(out).split())
    if len(text) > max_len:
        text = text[: max_len - 1] + "…"
    return text


def clean_optional_text(value: object, *, max_len: int) -> str | None:
    """``clean_text`` for values that may be missing or of the wrong type."""
    if not isinstance(value, str):
        return None
    return clean_text(value, max_len=max_len) or None


def clean_identifier(value: object) -> str | None:
    """Opaque upstream identifiers (node IDs): short, ASCII, no separators or spaces."""
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    if isinstance(value, str) and _IDENTIFIER_RE.fullmatch(value):
        return value
    return None


def clean_postcode(value: object) -> str | None:
    """Return a UK postcode in canonical ``OUT IN`` form, or None if it is not shaped like one."""
    if not isinstance(value, str) or len(value) > 16:
        return None
    match = POSTCODE_RE.fullmatch(" ".join(value.upper().split()))
    if match is None:
        return None
    return f"{match.group(1)} {match.group(2)}"


def clean_amenities(value: object, *, limit: int = 32) -> list[str]:
    """Amenity codes such as ``car_wash``. Anything that is not a short snake_case code is dropped."""
    if not isinstance(value, list):
        return []
    seen: dict[str, None] = {}
    for item in value:
        if isinstance(item, str):
            code = item.strip().lower()
            if _AMENITY_RE.fullmatch(code):
                seen.setdefault(code, None)
        if len(seen) >= limit:
            break
    return list(seen)


def clean_timestamp(value: object) -> str | None:
    """Pass an ISO 8601 timestamp through unmodified if it is one, else None.

    The Fair Use Policy asks aggregators not to alter timestamps, so a valid
    value is returned exactly as received rather than re-formatted.
    """
    if not isinstance(value, str) or not _TIMESTAMP_RE.fullmatch(value):
        return None
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value


def clean_price(value: object) -> float | None:
    """A finite, plausible pence-per-litre price, or None."""
    price = _number(value)
    if price is None or not math.isfinite(price) or not MIN_PRICE_PPL <= price <= MAX_PRICE_PPL:
        return None
    return price  # not rounded: prices pass through as reported


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    if isinstance(value, str) and len(value) > 32:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def clean_coordinates(lat: object, lon: object) -> tuple[float, float] | None:
    """Finite coordinates inside the UK bounding box, or None. Numeric strings are accepted."""
    lat_f, lon_f = _number(lat), _number(lon)
    if lat_f is None or lon_f is None:
        return None
    if not (math.isfinite(lat_f) and math.isfinite(lon_f)):
        return None
    if not (UK_LAT[0] <= lat_f <= UK_LAT[1] and UK_LON[0] <= lon_f <= UK_LON[1]):
        return None
    return lat_f, lon_f
