"""Phase 9 — candidate person domain model.

A CandidatePerson is NOT a verified identity — Phase 10 (Person Identity
Resolution) owns that question. This is a single, unverified sighting of a
person from one provider during one discovery run, tied to an
already-resolved canonical company (Phase 7) and the specific ICP/version
whose allowed titles drove the search. Its title is exactly whatever the
provider reported, raw and unverified; nothing here is ever invented.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class PeopleDiscoveryQuery(BaseModel):
    """The PEOPLE_DISCOVERY capability's input shape.

    `titles` comes directly from the CanonicalICP's allowed_titles —
    never hard-coded — so the same query-building logic works for any ICP,
    however its title list is populated.
    """

    model_config = ConfigDict(frozen=True)

    titles: tuple[str, ...] = ()
    company_domain: str | None = None
    company_name: str | None = None
    limit: int = 20


class CandidatePerson(BaseModel):
    """One discovered candidate. Presence here means only "a provider
    returned it" — never "this person is confirmed to hold this title at
    this company." See app/services/people_discovery.py for why
    verification never happens here."""

    model_config = ConfigDict(frozen=True)

    id: str
    company_id: str
    icp_id: str
    icp_version: int
    provider_id: str
    external_id: str
    name: str
    title: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)
    discovered_at: datetime
