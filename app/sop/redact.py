"""PII redaction for stored transcripts and debug output.

The extractor is the only component that needs the caller's raw message. Everything downstream
(stored transcript, responder history, extractor history, email drafter, trace files, UI debug panel)
gets the output of :func:`redact_pii` or :func:`masked_extraction` instead.
"""

from __future__ import annotations

import re
from typing import Any

from app.sop.state import Extraction
from app.tools.data import _MONTHS

EMAIL_TOKEN = "[EMAIL]"
PHONE_TOKEN = "[PHONE]"
DATE_TOKEN = "[DATE]"
ID4_TOKEN = "[ID4]"

MASKED_FIELDS: tuple[str, ...] = ("dob", "phone", "email", "id_last4")
REDACTED_VALUE = "[redacted]"

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

# Dates must carry a day component: "January", "January 2026" and "2026-01" are case hints the
# extractor needs to see in history and must survive.
_YEAR = r"(?:19|20)\d{2}"
_MONTH_NAMES = "|".join(sorted(_MONTHS, key=len, reverse=True))
_DAY = r"\d{1,2}(?:st|nd|rd|th)?"
ISO_DATE_RE = re.compile(rf"\b{_YEAR}[-/.](?:0?[1-9]|1[0-2])[-/.](?:0?[1-9]|[12]\d|3[01])\b")
NUMERIC_DATE_RE = re.compile(rf"\b\d{{1,2}}[-/.]\d{{1,2}}[-/.]{_YEAR}\b")
MONTH_DAY_RE = re.compile(
    rf"\b(?:{_MONTH_NAMES})\.?\s+{_DAY}\b(?:,?\s+{_YEAR}\b)?",
    re.IGNORECASE,
)
DAY_MONTH_RE = re.compile(
    rf"\b{_DAY}\s+(?:{_MONTH_NAMES})\.?\b(?:,?\s+{_YEAR}\b)?",
    re.IGNORECASE,
)
DATE_RES = (ISO_DATE_RE, NUMERIC_DATE_RE, MONTH_DAY_RE, DAY_MONTH_RE)

# US phone shapes: 7 digits, 10 digits, or 11 with a leading 1, with optional separators/parens.
# The lookarounds keep it from eating amounts (1450.00) or ids (POL-9921, CL-2048).
PHONE_RE = re.compile(
    r"(?<![\w.-])"
    r"(?:\+?1[\s.-]?)?"
    r"(?:\(\d{3}\)\s?|\d{3}[\s.-]?)?"
    r"\d{3}[\s.-]?\d{4}"
    r"(?![\w.-])"
)


def redact_pii(text: str, extraction: Extraction | None) -> str:
    """Replace emails, phone numbers and full dates with tokens.

    With ``extraction`` given, the caller's exact ``id_last4`` (whole word) and ``dob`` value are
    replaced too. Names, policy numbers and ``CL-`` case ids are never touched.
    """
    if not text:
        return text
    out = EMAIL_RE.sub(EMAIL_TOKEN, text)
    for pattern in DATE_RES:
        out = pattern.sub(DATE_TOKEN, out)
    out = PHONE_RE.sub(PHONE_TOKEN, out)
    if extraction is not None:
        if extraction.dob:
            out = re.sub(re.escape(extraction.dob), DATE_TOKEN, out, flags=re.IGNORECASE)
        if extraction.id_last4:
            out = re.sub(rf"\b{re.escape(extraction.id_last4)}\b", ID4_TOKEN, out)
    return out


def masked_extraction(extraction: Extraction) -> dict[str, Any]:
    """``model_dump()`` with raw identity values replaced; the key set is unchanged."""
    data = extraction.model_dump()
    for key in MASKED_FIELDS:
        if data.get(key) is not None:
            data[key] = REDACTED_VALUE
    return data
