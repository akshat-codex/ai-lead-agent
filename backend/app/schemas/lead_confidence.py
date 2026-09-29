"""Phase 28 — Evidence-Backed Lead Confidence contracts.

This is a READ-ONLY classification layer over what Phases 11/12/15/16/17/
18/19/20/22 already computed — it introduces no new scoring, qualification,
identity, or ranking logic, and reuses Phase 20's own
build_pipeline_snapshot() unchanged for every evidence/hard-rule/score/
qualification/adversarial/verification field. The only new thing this
module adds is a single, honest ReadinessLevel label plus the exact
evidence ids that back it — a compact answer to "how much can I trust
this lead, and what's it built on?"

THE HARD GATE IS ABSOLUTE HERE TOO: a hard-rule FAIL or HOLD can NEVER be
reported as VERIFIED or PARTIALLY_VERIFIED, regardless of how much
supporting evidence, how positive a qualification, or how enthusiastic a
human ACCEPT exists. See app/services/lead_confidence.py's
classify_readiness() — the hard-rule check happens first: a FAIL always
maps to INSUFFICIENT_EVIDENCE-or-worse territory (never VERIFIED/
PARTIALLY_VERIFIED), and CONFLICTED is reserved specifically for
unresolved evidence conflicts, which take priority over even a HOLD.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict


class ReadinessLevel(str, Enum):
    """How much a lead's current state can be trusted, derived ONLY from
    evidence/hard-rule/qualification/adversarial/human-review signals that
    already exist — never fabricated, never inferred beyond what those
    phases already established."""

    VERIFIED = "VERIFIED"  # hard PASS, no evidence conflicts, critical fields SUPPORTED, no unresolved verification
    PARTIALLY_VERIFIED = "PARTIALLY_VERIFIED"  # hard PASS, but some critical fields missing/insufficient (no conflicts)
    CONFLICTED = "CONFLICTED"  # unresolved evidence conflicts exist, regardless of hard-rule result
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"  # hard HOLD, or hard PASS with too little evidence to assess
    UNKNOWN = "UNKNOWN"  # no pipeline data exists yet for this (icp, company, person) at all


class ReadinessReasonCode(str, Enum):
    HARD_RULE_FAIL = "HARD_RULE_FAIL"
    HARD_RULE_HOLD = "HARD_RULE_HOLD"
    HARD_RULE_PASS = "HARD_RULE_PASS"
    HARD_RULE_UNKNOWN = "HARD_RULE_UNKNOWN"
    EVIDENCE_CONFLICTS_PRESENT = "EVIDENCE_CONFLICTS_PRESENT"
    CRITICAL_FIELDS_MISSING = "CRITICAL_FIELDS_MISSING"
    CRITICAL_FIELDS_FULLY_SUPPORTED = "CRITICAL_FIELDS_FULLY_SUPPORTED"
    VERIFICATION_UNRESOLVED = "VERIFICATION_UNRESOLVED"
    QUALIFICATION_ADVERSARIAL_DISAGREEMENT = "QUALIFICATION_ADVERSARIAL_DISAGREEMENT"
    QUALIFICATION_MISSING = "QUALIFICATION_MISSING"
    HUMAN_ACCEPTED = "HUMAN_ACCEPTED"
    HUMAN_REJECTED = "HUMAN_REJECTED"
    NO_PIPELINE_DATA = "NO_PIPELINE_DATA"
    DUPLICATE_OCCURRENCE = "DUPLICATE_OCCURRENCE"


class SupportingEvidenceItem(BaseModel):
    """One critical field and the exact evidence ids currently backing
    (or failing to back) it — the concrete answer to "show exactly which
    evidence supports this confidence result." A field with zero ids and
    status UNKNOWN is not hidden; it is listed as honestly unsupported."""

    model_config = ConfigDict(frozen=True)

    field: str
    entity_type: str
    status: str  # SUPPORTED | CONFLICT | INSUFFICIENT | UNKNOWN, verbatim from Phase 11
    evidence_ids: tuple[str, ...]


class LeadConfidenceResult(BaseModel):
    """The full, in-memory result of one confidence/readiness assessment.
    Always produced, even with zero pipeline data (UNKNOWN) — never a
    guessed value in place of an honest gap."""

    model_config = ConfigDict(frozen=True)

    icp_id: str
    icp_version: int
    company_id: str
    person_id: str | None
    lead_id: str | None  # None when no Phase 19 canonical lead exists yet for this pair

    readiness: ReadinessLevel
    reason_codes: tuple[ReadinessReasonCode, ...]

    hard_rule_result: str | None
    qualification_decision: str | None
    adversarial_result: str | None
    human_review_decision: str | None
    verification_status: str | None
    ranking_tier: str | None

    supporting_evidence: tuple[SupportingEvidenceItem, ...]
    conflicting_fields: tuple[str, ...]
    missing_critical_fields: tuple[str, ...]

    explanation: str
    generated_at: datetime


class LeadConfidenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    icp_id: str
    company_id: str
    person_id: str | None = None


class LeadConfidenceSnapshotRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    icp_id: str
    icp_version: int
    company_id: str
    person_id: str | None
    lead_id: str | None
    readiness: str
    result: dict
    created_at: datetime
