"""Phase 11 — importing existing Phase 6-10 records as evidence.

No new provider calls happen here: every function below reads rows already
persisted by earlier phases and converts them into EvidenceCreate objects
using app/schemas/evidence.py's contract. This is the concrete meaning of
"reuse existing enrichment facts and identity information, do not
duplicate provider calls" — the Evidence Engine is a lens over data the
pipeline already collected, never a new collection stage.

Every source produced this way is honestly labeled SourceType.PROVIDER,
even for a field like "linkedin_id" — the data came from a provider
*telling us* a LinkedIn handle, not from independently observing it on
linkedin.com, and the source type must not overstate that.
"""
from __future__ import annotations

import json

from sqlalchemy.orm import Session

from app.models.company import CompanyResolutionModel
from app.models.discovery import DiscoveryCandidateModel
from app.models.enrichment import EnrichmentFactModel
from app.models.people_discovery import PeopleDiscoveryCandidateModel
from app.models.person import PersonResolutionModel
from app.schemas.evidence import ConfidenceLevel, EntityType, EvidenceCreate, SourceType
from app.services.person_identity import extract_linkedin_identifier

# Phase 11 (industry search -> taxonomy bridge): the JSON key this module
# reads back out of an EvidenceRecord.evidence_text string it wrote here —
# see _industry_match_provenance below and
# app/services/hard_icp_validation.py's own read of this exact key.
INDUSTRY_MATCH_PROVENANCE_KEY = "industry_match"

# Phase 13B (keyword discovery provenance): the sibling JSON key for the
# LOW-precision counterpart of the above — see _industry_match_provenance's
# keyword_match_terms handling below and
# app/services/qualification_context.py's own read of this exact key.
#
# Phase 37 correction: a single industry EvidenceRecord's evidence_text CAN
# now carry BOTH keys — app/providers/explorium.py's own
# _merge_cross_branch_attributes merges a later branch's provenance into an
# earlier branch's record when the SAME real company (same external_id) is
# genuinely returned by more than one branch in one call (e.g. a
# structured Healthcare hit AND a keyword SaaS hit for the same company).
# That record honestly reflects evidence from both branches, and this
# module must preserve both rather than picking one — this is exactly
# what enables hard_icp_validation.py's cross-branch corroboration check.
KEYWORD_MATCH_PROVENANCE_KEY = "keyword_match"


def _industry_match_provenance(attributes: dict | None) -> str | None:
    """Carries app/providers/explorium.py's discovery-branch provenance
    tags into the industry EvidenceRecord's own existing `evidence_text`
    field — "the supporting artifact/pointer" per docs/evidence-policy.md's
    own evidence-record contract, exactly the field that contract already
    exists for. No new schema field, no new DB column: this reuses an
    existing, already-audited field for its documented purpose.

    Two independently-present provenance shapes:

      - industry_match_branch/industry_match_terms (Phase 11): present
        for a record with a real, live-verified exact linkedin_category/
        naics_category match on at least one ICP term.
      - keyword_match_terms (Phase 13B): present for a record with a
        LOW-precision website_keywords substring-fallback match — the
        exact term(s) that were actually in that round's keyword OR-list,
        so a downstream consumer (see
        app/services/qualification_context.py::_discovery_match_type) can
        tell Phase 12's LLM verification prompt precisely which term(s)
        this specific candidate was found by, not just that it was a
        keyword-fallback match.

    Phase 37: BOTH can be present on the same record (a genuine
    cross-branch merge — see this module's own KEYWORD_MATCH_PROVENANCE_KEY
    comment); when so, both payloads are written into one JSON object, one
    per key, so neither branch's provenance is ever lost.

    Returns None (the field's existing default) when the source attributes
    carry neither tag — e.g. a non-Explorium provider, or a keyword-less
    request shape — so this function is a pure no-op for every evidence
    record that predates Phase 11/13B."""
    attributes = attributes or {}
    combined_payload: dict[str, object] = {}

    branch = attributes.get("industry_match_branch")
    terms = attributes.get("industry_match_terms")
    if branch and terms:
        # EvidenceModel.evidence_text is a 2000-char column (see
        # app/models/evidence.py) — truncated defensively, matching the
        # same discipline already used elsewhere in this codebase (e.g.
        # app/models/batch.py's discovery_error_message[:2000]);
        # realistically never hit given Phase 10's own term-count cap, but
        # never silently fail a DB write over it either.
        payload = {"taxonomy_field": branch, "icp_terms": list(terms)}
        # Phase 13D: additive only — absent (key never added) when
        # term_origin was never supplied to explorium.py::execute() (every
        # pre-Phase-13D caller), so this extension never changes the JSON
        # shape any existing reader (e.g. hard_icp_validation.py's Phase
        # 11 bridge) already parses.
        origins = attributes.get("industry_match_term_origins")
        if origins:
            payload["icp_term_origins"] = list(origins)
        # Phase 15: observability only — see explorium.py's own
        # industry_match_scope comment for exactly why this is NOT a
        # "broad vs strong" classification, only a record of whether
        # total_results is safely attributable to one category.
        scope = attributes.get("industry_match_scope")
        if scope:
            payload["match_scope"] = scope
        resolved_count = attributes.get("industry_match_resolved_category_count")
        if isinstance(resolved_count, int):
            payload["resolved_category_count"] = resolved_count
        # Live-test audit finding — the ACTUAL resolved taxonomy label
        # string(s) this branch matched on (see
        # app/providers/explorium.py's own industry_match_resolved_values
        # comment for why this is OBSERVABILITY ONLY, never read by
        # hard_icp_validation.py's own trust logic — a resolved
        # linkedin_category/naics_category value and the returned
        # naics_description text were confirmed to share no identifier
        # space this codebase could compare without inventing a
        # taxonomy relationship). Carried through purely so a human
        # inspecting evidence_text can see which real taxonomy value(s)
        # this branch's request matched on.
        resolved_values = attributes.get("industry_match_resolved_values")
        if isinstance(resolved_values, list):
            payload["resolved_values"] = list(resolved_values)
        total_results = attributes.get("industry_match_branch_total_results")
        if isinstance(total_results, int):
            payload["branch_total_results"] = total_results
        combined_payload[INDUSTRY_MATCH_PROVENANCE_KEY] = payload

    keyword_terms = attributes.get("keyword_match_terms")
    if keyword_terms:
        payload = {"terms": list(keyword_terms)}
        # Phase 13D: which real Explorium request field each term came
        # from (unmatched industry vs. company_type) — see
        # app/providers/explorium.py's own keyword_match_term_sources
        # comment for why this distinction matters to Phase 12's prompt.
        sources = attributes.get("keyword_match_term_sources")
        if sources:
            payload["term_sources"] = list(sources)
        origins = attributes.get("keyword_match_term_origins")
        if origins:
            payload["term_origins"] = list(origins)
        combined_payload[KEYWORD_MATCH_PROVENANCE_KEY] = payload

    if not combined_payload:
        return None
    return json.dumps(combined_payload)[:2000]


# Phase 41 (P1 follow-up) — must match app/providers/explorium.py's own
# company_type_match_branch/company_type_match_terms/
# company_type_match_resolved_category_count attribute keys exactly. See
# _company_type_structured_match below for what this bridges.
COMPANY_TYPE_MATCH_PROVENANCE_KEY = "company_type_match"


def _company_type_structured_match(attributes: dict | None) -> tuple[str, str] | None:
    """Root-cause fix for the P1 audit's company_type finding: Explorium's
    /businesses response has no field that states a company's type (see
    app/providers/explorium.py's own _BUSINESS_ATTRIBUTE_MAP — "company_type"
    is not a key in it, unlike naics_description -> industry), so unlike
    industry/country/employee_range there is no observed attribute value
    this module could import as company_type evidence. What Explorium DOES
    give us, real and live-verified (see app/providers/explorium.py's
    _plan_company_type_branches docstring): a company_type-shaped ICP term
    (e.g. "D2C", "Nonprofit") can resolve to a real, exact
    linkedin_category/naics_category taxonomy match — Explorium's own
    backend classified this specific company under that category, the
    exact same server-side guarantee industry's structured branch already
    relies on (see _industry_match_provenance above). That is genuine
    evidence of company_type membership, not a keyword guess — this
    function surfaces it, using ONLY the ICP's own literal term as the
    value (never an invented taxonomy label Explorium never returned).

    Ambiguity guard, identical in spirit to
    app/services/hard_icp_validation.py::_bridged_industry_terms's own
    "never bridge on a same-branch OR-list confound" discipline: Explorium
    merges multiple resolved category VALUES into one branch response with
    no per-value attribution, so a branch that resolved more than one
    distinct company_type term is NOT safe to attribute to any single one
    of them. Only surfaced when exactly one term resolved into this
    branch (company_type_match_resolved_category_count == 1) AND exactly
    one ICP term was covered by it — the un-confusable case. Unlike
    industry, the company_type hard rule treats multiple allowed_values as
    ALTERNATIVES (app/services/hard_rule_engine.py::_evaluate_choice_field
    uses `any(...)`), so a single unambiguous term is already sufficient
    to satisfy the rule — no compound/intersection handling is needed
    here the way Phase 39 needed for industry.

    Returns (term, taxonomy_field) when safe to surface, else None — never
    a fabricated or best-guess value. The low-precision keyword-fallback
    tier (company_type_match_term_sources == "company_type" on the
    industry record's keyword_match payload) is deliberately never read
    here; it stays exactly as low-precision-labeled as before this fix."""
    attributes = attributes or {}
    branch = attributes.get("company_type_match_branch")
    terms = attributes.get("company_type_match_terms")
    resolved_count = attributes.get("company_type_match_resolved_category_count")
    if not branch or not isinstance(terms, list) or len(terms) != 1:
        return None
    if resolved_count != 1:
        return None
    term = terms[0]
    if not isinstance(term, str) or not term.strip():
        return None
    return term, str(branch)


def _company_type_match_provenance(attributes: dict | None) -> str | None:
    """The company_type analogue of _industry_match_provenance's JSON
    evidence_text payload — records WHY this value is trusted (a real
    structured taxonomy match, not an observed company attribute) so a
    reviewer or future consumer can tell it apart from a stronger,
    provider-stated fact. Returns None whenever
    _company_type_structured_match itself returns None."""
    match = _company_type_structured_match(attributes)
    if match is None:
        return None
    term, taxonomy_field = match
    payload = {COMPANY_TYPE_MATCH_PROVENANCE_KEY: {"taxonomy_field": taxonomy_field, "icp_term": term}}
    return json.dumps(payload)[:2000]


def _confidence_from_float(value: float | None) -> ConfidenceLevel:
    """Buckets a provider-supplied 0-1 confidence into the structured
    scale — never invented when the source gives none (see the None case,
    which is what every current mock actually returns)."""
    if value is None:
        return ConfidenceLevel.UNKNOWN
    if value >= 0.8:
        return ConfidenceLevel.HIGH
    if value >= 0.5:
        return ConfidenceLevel.MEDIUM
    return ConfidenceLevel.LOW


def collect_company_evidence(db: Session, company_id: str) -> list[EvidenceCreate]:
    """Builds (not yet persisted) evidence entries for a company from
    existing Phase 6 discovery and Phase 8 enrichment records."""
    candidates: list[EvidenceCreate] = []

    for fact in db.query(EnrichmentFactModel).filter(EnrichmentFactModel.company_id == company_id).all():
        candidates.append(
            EvidenceCreate(
                entity_type=EntityType.COMPANY,
                entity_id=company_id,
                field=fact.field,
                value=fact.value,
                source_provider_id=fact.provider_id,
                source_type=SourceType.PROVIDER,
                external_id=fact.external_id,
                retrieved_at=fact.retrieved_at,
                confidence=_confidence_from_float(fact.confidence),
            )
        )

    resolved_candidate_ids = {
        row.candidate_id
        for row in db.query(CompanyResolutionModel)
        .filter(CompanyResolutionModel.canonical_company_id == company_id)
        .all()
    }
    if resolved_candidate_ids:
        discovery_rows = (
            db.query(DiscoveryCandidateModel)
            .filter(DiscoveryCandidateModel.id.in_(resolved_candidate_ids))
            .all()
        )
        for row in discovery_rows:
            candidates.append(
                EvidenceCreate(
                    entity_type=EntityType.COMPANY,
                    entity_id=company_id,
                    field="company_identity",
                    value=row.name,
                    source_provider_id=row.provider_id,
                    source_type=SourceType.PROVIDER,
                    external_id=row.external_id,
                    retrieved_at=row.discovered_at,
                    confidence=ConfidenceLevel.UNKNOWN,
                )
            )
            if row.domain:
                candidates.append(
                    EvidenceCreate(
                        entity_type=EntityType.COMPANY,
                        entity_id=company_id,
                        field="domain",
                        value=row.domain,
                        source_provider_id=row.provider_id,
                        source_type=SourceType.PROVIDER,
                        external_id=row.external_id,
                        retrieved_at=row.discovered_at,
                        confidence=ConfidenceLevel.UNKNOWN,
                    )
                )
            # A precise count, when a discovery provider states one — kept
            # as its own field, distinct from enrichment's "employee_range"
            # (a range is not a count; Phase 12 must never convert one into
            # the other by guessing a bound).
            employee_count = (row.attributes or {}).get("employee_count")
            if isinstance(employee_count, int):
                candidates.append(
                    EvidenceCreate(
                        entity_type=EntityType.COMPANY,
                        entity_id=company_id,
                        field="employee_count",
                        value=employee_count,
                        source_provider_id=row.provider_id,
                        source_type=SourceType.PROVIDER,
                        external_id=row.external_id,
                        retrieved_at=row.discovered_at,
                        confidence=ConfidenceLevel.UNKNOWN,
                    )
                )
            # industry/country: a real discovery provider (e.g. Explorium)
            # may already state these directly on the candidate — Phase 12's
            # hard-rule engine reads evidence fields "industry"/"country"
            # (see app/services/hard_icp_validation.py), so without this,
            # hard validation could never resolve them from discovery data
            # alone and would HOLD forever regardless of what a provider
            # actually returned.
            industry = (row.attributes or {}).get("industry")
            if industry:
                candidates.append(
                    EvidenceCreate(
                        entity_type=EntityType.COMPANY,
                        entity_id=company_id,
                        field="industry",
                        value=industry,
                        source_provider_id=row.provider_id,
                        source_type=SourceType.PROVIDER,
                        external_id=row.external_id,
                        retrieved_at=row.discovered_at,
                        confidence=ConfidenceLevel.UNKNOWN,
                        evidence_text=_industry_match_provenance(row.attributes),
                    )
                )
            country = (row.attributes or {}).get("country")
            if country:
                candidates.append(
                    EvidenceCreate(
                        entity_type=EntityType.COMPANY,
                        entity_id=company_id,
                        field="country",
                        value=country,
                        source_provider_id=row.provider_id,
                        source_type=SourceType.PROVIDER,
                        external_id=row.external_id,
                        retrieved_at=row.discovered_at,
                        confidence=ConfidenceLevel.UNKNOWN,
                    )
                )
            # P1 fix — company_type: see _company_type_structured_match's
            # own docstring for the full root cause. Only ever written
            # when this candidate's discovery attributes carry an
            # unambiguous structured taxonomy match for exactly one ICP
            # company_type term — never from the low-precision keyword
            # tier, and never a value Explorium itself didn't classify
            # this company under.
            company_type_match = _company_type_structured_match(row.attributes)
            if company_type_match is not None:
                company_type_term, _taxonomy_field = company_type_match
                candidates.append(
                    EvidenceCreate(
                        entity_type=EntityType.COMPANY,
                        entity_id=company_id,
                        field="company_type",
                        value=company_type_term,
                        source_provider_id=row.provider_id,
                        source_type=SourceType.PROVIDER,
                        external_id=row.external_id,
                        retrieved_at=row.discovered_at,
                        confidence=ConfidenceLevel.UNKNOWN,
                        evidence_text=_company_type_match_provenance(row.attributes),
                    )
                )
            # Phase 7G: revenue_range is a documented Explorium response
            # field (yearly_revenue_range) already present in every real
            # discovery response at no extra API-call cost. Same "string
            # range, never coerced into a fabricated number" discipline as
            # employee_range above. No existing hard-rule engine reads a
            # "revenue_range" evidence field, so this is additive
            # observability only — it does not change any PASS/FAIL/HOLD
            # outcome.
            revenue_range = (row.attributes or {}).get("revenue_range")
            if revenue_range:
                candidates.append(
                    EvidenceCreate(
                        entity_type=EntityType.COMPANY,
                        entity_id=company_id,
                        field="revenue_range",
                        value=revenue_range,
                        source_provider_id=row.provider_id,
                        source_type=SourceType.PROVIDER,
                        external_id=row.external_id,
                        retrieved_at=row.discovered_at,
                        confidence=ConfidenceLevel.UNKNOWN,
                    )
                )
            # Phase 7N: employee_range is a string range (e.g. "11-50")
            # already mapped by a real discovery provider (e.g. Explorium's
            # number_of_employees_range) into attributes["employee_range"],
            # distinct from the precise "employee_count" int field above —
            # same "a range is not a count" discipline: this is a separate
            # evidence field, never coerced into or merged with
            # employee_count. Previously computed by the adapter and then
            # silently discarded here, so it never became visible,
            # auditable evidence even when a provider actually supplied it.
            employee_range = (row.attributes or {}).get("employee_range")
            if employee_range:
                candidates.append(
                    EvidenceCreate(
                        entity_type=EntityType.COMPANY,
                        entity_id=company_id,
                        field="employee_range",
                        value=employee_range,
                        source_provider_id=row.provider_id,
                        source_type=SourceType.PROVIDER,
                        external_id=row.external_id,
                        retrieved_at=row.discovered_at,
                        confidence=ConfidenceLevel.UNKNOWN,
                    )
                )
            # Phase 7N: company-level linkedin_id — a real discovery
            # provider (e.g. Explorium's linkedin_profile) may already
            # state this directly on the candidate. The person-level
            # equivalent has been preserved as evidence since Phase 11
            # (see collect_person_evidence below); this closes the same
            # gap for companies, which was previously silently dropped
            # even when a provider actually supplied it.
            linkedin_id = (row.attributes or {}).get("linkedin_id")
            if linkedin_id:
                candidates.append(
                    EvidenceCreate(
                        entity_type=EntityType.COMPANY,
                        entity_id=company_id,
                        field="linkedin_id",
                        value=linkedin_id,
                        source_provider_id=row.provider_id,
                        source_type=SourceType.PROVIDER,
                        external_id=row.external_id,
                        retrieved_at=row.discovered_at,
                        confidence=ConfidenceLevel.UNKNOWN,
                    )
                )
            # Phase 35: a free-text company description — today only
            # app/providers/hermes.py's _import_hermes_records populates
            # attributes["description"] (Hermes's own agent-read summary,
            # e.g. "SaaS platform serving healthcare providers"), but this
            # is a plain, generic evidence field like every other one
            # above, not Hermes-specific: ANY discovery provider that ever
            # supplies free descriptive text can use it the same way.
            # Never itself a hard-rule field Phase 3 reads directly — see
            # app/services/hard_icp_validation.py::_free_text_industry_bridge
            # for the one place this evidence is read, and why reading raw
            # description text is safe there (never fuzzy/semantic
            # matching, only literal substring containment of the ICP's
            # OWN already-stated terms).
            description = (row.attributes or {}).get("description")
            if description:
                candidates.append(
                    EvidenceCreate(
                        entity_type=EntityType.COMPANY,
                        entity_id=company_id,
                        field="description",
                        value=description,
                        source_provider_id=row.provider_id,
                        source_type=SourceType.PROVIDER,
                        external_id=row.external_id,
                        retrieved_at=row.discovered_at,
                        confidence=ConfidenceLevel.UNKNOWN,
                    )
                )

    return candidates


def collect_person_evidence(db: Session, person_id: str) -> list[EvidenceCreate]:
    """Builds (not yet persisted) evidence entries for a person from
    existing Phase 9 discovery records, via the Phase 10 resolution
    linking a candidate sighting to this canonical person."""
    candidates: list[EvidenceCreate] = []

    resolved_candidate_ids = {
        row.candidate_id
        for row in db.query(PersonResolutionModel)
        .filter(PersonResolutionModel.canonical_person_id == person_id)
        .all()
    }
    if not resolved_candidate_ids:
        return candidates

    discovery_rows = (
        db.query(PeopleDiscoveryCandidateModel)
        .filter(PeopleDiscoveryCandidateModel.id.in_(resolved_candidate_ids))
        .all()
    )
    for row in discovery_rows:
        candidates.append(
            EvidenceCreate(
                entity_type=EntityType.PERSON,
                entity_id=person_id,
                field="person_identity",
                value=row.name,
                source_provider_id=row.provider_id,
                source_type=SourceType.PROVIDER,
                external_id=row.external_id,
                retrieved_at=row.discovered_at,
                confidence=ConfidenceLevel.UNKNOWN,
            )
        )
        if row.title:
            candidates.append(
                EvidenceCreate(
                    entity_type=EntityType.PERSON,
                    entity_id=person_id,
                    field="current_title",
                    value=row.title,
                    source_provider_id=row.provider_id,
                    source_type=SourceType.PROVIDER,
                    external_id=row.external_id,
                    retrieved_at=row.discovered_at,
                    confidence=ConfidenceLevel.UNKNOWN,
                )
            )
        if row.company_id:
            candidates.append(
                EvidenceCreate(
                    entity_type=EntityType.PERSON,
                    entity_id=person_id,
                    field="company_association",
                    value=row.company_id,
                    source_provider_id=row.provider_id,
                    source_type=SourceType.PROVIDER,
                    external_id=row.external_id,
                    retrieved_at=row.discovered_at,
                    confidence=ConfidenceLevel.UNKNOWN,
                )
            )
        linkedin_id = extract_linkedin_identifier(row.attributes or {})
        if linkedin_id:
            candidates.append(
                EvidenceCreate(
                    entity_type=EntityType.PERSON,
                    entity_id=person_id,
                    field="linkedin_id",
                    value=linkedin_id,
                    source_provider_id=row.provider_id,
                    source_type=SourceType.PROVIDER,
                    external_id=row.external_id,
                    retrieved_at=row.discovered_at,
                    confidence=ConfidenceLevel.UNKNOWN,
                )
            )

    return candidates
