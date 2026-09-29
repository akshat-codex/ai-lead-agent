"""Phase 10 — deterministic person identity normalization.

Whitespace/case cleanup only, reused from Phase 2's clean_text() rather
than reimplemented. Deliberately does NOT attempt to strip titles or
generational suffixes (Dr., Jr., III, ...) the way Phase 7's company-name
normalization strips legal suffixes (Inc., Corp.) — guessing which tokens
in a *person's* name are "noise" risks conflating two different people, so
normalization here is pure cleanup, never a standalone identity signal.
"""
from __future__ import annotations

import re

from app.services.icp_normalization import clean_text

_URL_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]*://")
_LINKEDIN_HOST_RE = re.compile(r"^(www\.)?linkedin\.com/")


def normalize_person_name(raw: str | None) -> str | None:
    """"Jane Testperson" -> "jane testperson". Cleanup only — see
    app/services/person_resolution.py for why this must never be a
    standalone identity signal."""
    if not raw:
        return None
    cleaned = clean_text(raw).lower()
    return cleaned or None


def normalize_linkedin_identifier(raw: str | None) -> str | None:
    """"https://www.linkedin.com/in/johnsmith/", "linkedin.com/in/johnsmith",
    and "in/johnsmith" all -> "in/johnsmith".

    Only ever applied when a provider *actually* supplies one — this
    module never invents or infers a LinkedIn identity from a name.
    """
    if not raw:
        return None
    cleaned = clean_text(raw).lower()
    if not cleaned:
        return None
    cleaned = _URL_SCHEME_RE.sub("", cleaned)
    cleaned = _LINKEDIN_HOST_RE.sub("", cleaned)
    cleaned = cleaned.rstrip("/")
    return cleaned or None


def extract_linkedin_identifier(attributes: dict) -> str | None:
    """Looks for a LinkedIn identifier under either of the conventional
    attribute keys a provider might use, without guessing at others."""
    raw = attributes.get("linkedin_id") or attributes.get("linkedin_url")
    return normalize_linkedin_identifier(raw)
