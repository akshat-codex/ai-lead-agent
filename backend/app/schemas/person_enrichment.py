"""Phase 5 (Contact Enrichment) — Person Enrichment contracts.

Mirrors app/schemas/company_enrichment.py's shape for the PERSON_ENRICHMENT
capability, with one deliberate difference: individual facts are not stored
in a dedicated fact table here. They are written directly as EvidenceCreate
rows (app/schemas/evidence.py) into the already entity-agnostic EvidenceModel
— see app/services/person_enrichment.py. This module only defines the run's
input query shape and its persisted status/outcome record.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict

from app.providers.contracts import ProviderError


class PersonEnrichmentQuery(BaseModel):
    """The PERSON_ENRICHMENT capability's input shape.

    Every field is optional — whatever is already known about the person
    (from imported discovery evidence) is passed through unchanged; nothing
    here is invented to fill a gap.
    """

    model_config = ConfigDict(frozen=True)

    full_name: str | None = None
    email: str | None = None
    # Phone has no discovery source anywhere in this codebase today (Apollo
    # deliberately never requests/returns one — see app/providers/apollo.py's
    # own module docstring) — this is populated only from a phone number a
    # human has already recorded as evidence directly (e.g. via
    # POST /api/v1/evidence), read back the same way email is on a second
    # /enrich call (see app/api/people.py's own _latest_evidence_value use).
    # AbstractPhoneVerificationProvider verifies whatever is here; it never
    # discovers a phone number itself.
    phone: str | None = None
    linkedin_id: str | None = None
    company_domain: str | None = None
    company_name: str | None = None


class PersonEnrichmentRunStatus(str, Enum):
    COMPLETED = "COMPLETED"
    PARTIAL_FAILURE = "PARTIAL_FAILURE"
    FAILED = "FAILED"
    # No PERSON_ENRICHMENT provider is registered (e.g. no Apollo API key
    # configured) — never silently treated as success, never a 500.
    UNAVAILABLE = "UNAVAILABLE"
    # Phase 4 (AI/UX + live-safety audit) — a real PERSON_ENRICHMENT
    # provider IS registered, but every evidence record this person has
    # traces back to a mock-prefixed provider (app/providers/mocks.py) —
    # e.g. the person was only ever discovered via MockPeopleDataProvider
    # because Unipile isn't fully configured. Calling a real, paid
    # provider (Apollo) with a fabricated name/company would spend a real
    # API call on input that was never real to begin with. See
    # app/services/person_enrichment.py::is_entity_mock_sourced.
    MOCK_SOURCED_SKIPPED = "MOCK_SOURCED_SKIPPED"


class PersonEnrichmentOutcome(BaseModel):
    """The full, in-memory result of one person's enrichment attempt."""

    model_config = ConfigDict(frozen=True)

    person_id: str
    status: PersonEnrichmentRunStatus
    provider_id: str | None = None
    error: ProviderError | None = None
    fields_returned: int = 0


# --- API read/request schemas ---------------------------------------------


class PersonEnrichmentRunRead(BaseModel):
    id: str
    person_id: str
    status: str
    provider_id: str | None
    error_code: str | None
    error_message: str | None
    started_at: datetime
