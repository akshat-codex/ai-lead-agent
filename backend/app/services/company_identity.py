"""Phase 7 — deterministic company identity normalization.

Domain and company-name normalization are new here (Phase 2's normalization
engine only ever normalized ICP fields, never a company's own identity).
Basic whitespace cleanup is reused from there via clean_text() rather than
reimplemented, since that piece really is identical regardless of what
string is being normalized — see the Phase 7 task's instruction to reuse
existing normalization utilities where appropriate.
"""
from __future__ import annotations

import re

from app.services.icp_normalization import clean_text

_URL_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]*://")
_WWW_PREFIX_RE = re.compile(r"^www\.")

# Common legal-entity suffixes to strip when comparing company names. Not
# exhaustive — this is cleanup for the "ABC Inc." vs "ABC Corporation" case
# from the task, not a substitute for a real identity signal like domain.
_LEGAL_SUFFIXES = frozenset(
    {
        "inc",
        "incorporated",
        "corp",
        "corporation",
        "co",
        "company",
        "ltd",
        "limited",
        "llc",
        "llp",
        "plc",
        "gmbh",
        "srl",
        "sa",
        "bv",
        "ag",
    }
)
_NAME_PUNCTUATION_RE = re.compile(r"[.,]")


def normalize_domain(raw: str | None) -> str | None:
    """"https://www.abc.com/", "www.abc.com", "ABC.com" all -> "abc.com".

    Returns None for a missing/blank input — "no domain known" is a real,
    distinct state from any specific domain value, never coerced into one.
    """
    if not raw:
        return None
    cleaned = clean_text(raw).lower()
    if not cleaned:
        return None
    cleaned = _URL_SCHEME_RE.sub("", cleaned)
    cleaned = _WWW_PREFIX_RE.sub("", cleaned)
    cleaned = cleaned.split("/")[0].split("?")[0]
    cleaned = cleaned.rstrip(".")
    return cleaned or None


def normalize_company_name(raw: str | None) -> str | None:
    """"ABC Inc.", "ABC, Inc.", "ABC Corporation" all -> "abc".

    This is cleanup only — it must never be treated as a standalone identity
    signal (see app/services/company_resolution.py's docstring): two
    different real companies can easily normalize to the same short name.
    """
    if not raw:
        return None
    cleaned = _NAME_PUNCTUATION_RE.sub("", clean_text(raw).lower())
    tokens = cleaned.split()
    while tokens and tokens[-1] in _LEGAL_SUFFIXES:
        tokens.pop()
    normalized = " ".join(tokens)
    return normalized or None
