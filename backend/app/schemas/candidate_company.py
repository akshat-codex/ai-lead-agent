"""Phase 6 — candidate company domain model.

A CandidateCompany is NOT the canonical Company entity — Phase 7 (Company
Entity Resolution) owns that. This is a single, unqualified,
un-deduplicated-across-providers sighting of a company from one provider
during one discovery run. It may well refer to the same real company as
another candidate (from this run or a past one); resolving that identity
question is Phase 7's job, not this one's.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class CompanyDiscoveryQuery(BaseModel):
    """The COMPANY_DISCOVERY capability's input shape.

    Phase 5 left ProviderRequest.query an opaque dict because no capability
    had defined its shape yet — this is company discovery's. Only hard-rule
    fields that describe *what a company looks like* are included:
    exclusions/custom_rules are evaluated against a candidate by the Phase 3
    Hard ICP Rule Engine, not used to shape a discovery query.

    allowed_titles is deliberately the ONE exception to "company-shape
    fields only" above (it remains a person-level hard-rule field
    everywhere else — Phase 9's concern, never evaluated against a
    company candidate itself). It exists here purely as an OPTIONAL
    discovery-query SIGNAL: a web-search provider (see app/providers/
    tavily.py's hiring-signal query type) can use the ICP's own target
    roles to search real job-board postings ("<role> <location>" on
    site:jobs.lever.co/site:boards.greenhouse.io — confirmed live to
    surface real, named companies actively hiring for that role) as a
    genuine discovery signal for "actively scaling/hiring" ICPs. This
    never changes what a candidate qualifies against — the Hard ICP Rule
    Engine's own allowed_titles evaluation (a PERSON-level rule, checked
    against discovered people, never companies) is completely unchanged
    and unaware this field also reached here.
    """

    model_config = ConfigDict(frozen=True)

    industries: tuple[str, ...] = ()
    geography_codes: tuple[str, ...] = ()
    geography_unrecognized: tuple[str, ...] = ()
    company_types: tuple[str, ...] = ()
    allowed_titles: tuple[str, ...] = ()
    min_employees: int | None = None
    max_employees: int | None = None
    limit: int = 20
    # Phase 40 — the subset of `industries` that is a Phase 39 AI-declared
    # compound-intersection phrase (see CanonicalHardRules.
    # industry_combination_terms's own docstring). Always a subset of
    # `industries`, never a separate universe of terms; defaults to empty
    # for every existing caller/provider that doesn't populate it, exactly
    # like every other additive field in this schema. Read only by
    # app/providers/explorium.py's keyword-truncation logic to prioritize
    # retaining these terms over other AI-proposed independent synonyms
    # when a long term list must be capped — never sent to Explorium as a
    # distinct filter, never used for structured/naics matching (which
    # already treats every industries term identically, unaffected by
    # this field).
    combination_terms: tuple[str, ...] = ()


class CandidateCompany(BaseModel):
    """One discovered candidate. Presence here means only "a provider
    returned it" — never "it matches the ICP." See
    app/services/company_discovery.py for why qualification never happens
    here."""

    model_config = ConfigDict(frozen=True)

    id: str
    icp_id: str
    icp_version: int
    provider_id: str
    external_id: str
    name: str
    domain: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)
    discovered_at: datetime
