"""Phase 13 — Business Model Classification.

Deterministically classifies a canonical company's business model(s) from
Phase 11 evidence — a commercial-*understanding* layer, never a fitness
judgment. This module never reads an ICP or soft preferences (there is no
parameter for either), and never produces anything resembling GOOD_FIT/
NOT_FIT/PASS/FAIL/HOLD/a score.

Deliberately conservative: a model is only ever attached when evidence
literally names it (or a near-unambiguous synonym) — never inferred from
industry, company name, or "what's typical for this kind of company."
"industry" is explicitly excluded from the fields this module reads (see
the task's own Healthcare/FMCG example), and a keyword like "Shopify" is
deliberately absent from the keyword table below: using an ecommerce
platform doesn't itself prove DTC, B2C, wholesale, or anything else.

HYBRID vs. conflict — these are NOT the same thing:
  - HYBRID means two or more *independent* pieces of evidence each
    unambiguously name a different model (e.g. company_type says "D2C" and
    a products/services description separately mentions a wholesale
    channel) — genuinely multiple models, both real.
  - A conflict is when a single-valued field (company_type) has two
    *disagreeing* values from different sources (e.g. "B2B" vs "B2C") —
    that is contradictory information about one fact, not evidence of two
    business models, so it is reported via conflicting_evidence_ids and
    excluded from driving a classification, never silently turned into
    HYBRID or an arbitrary pick of one side.
"""
from __future__ import annotations

import re
from collections import defaultdict

from app.schemas.business_model import BusinessModel, BusinessModelClassificationResult, ClassificationStatus
from app.schemas.evidence import ConfidenceLevel, EvidenceRecord, EvidenceStatus
from app.services.evidence_engine import compute_field_status
from app.services.icp_normalization import clean_text

# Fields that describe identity, geography, or size — never business model.
# "industry" is deliberately excluded: Healthcare/FMCG/etc. never
# determines B2B/B2C/DTC on its own.
_EXCLUDED_FIELDS = frozenset(
    {"company_identity", "domain", "country", "employee_count", "employee_range", "linkedin_id", "industry"}
)

# Treated as a single-valued assertion (like an identity field) — if
# providers disagree here, that is a genuine conflict, not a hybrid signal.
_SINGLE_VALUE_FIELD = "company_type"

# Deliberately narrow, specific phrases. A bare word like "service" or
# "consumer" shows up in too much unrelated text to trust on its own; every
# entry here is a real name for a business model or a near-unambiguous
# synonym of one.
_MODEL_KEYWORDS: dict[BusinessModel, tuple[str, ...]] = {
    BusinessModel.DTC: ("dtc", "d2c", "direct-to-consumer", "direct to consumer"),
    BusinessModel.B2C: ("b2c", "business-to-consumer", "business to consumer", "consumer-facing"),
    BusinessModel.B2B: ("b2b", "business-to-business", "business to business", "enterprise customers"),
    BusinessModel.B2B2C: ("b2b2c",),
    BusinessModel.MARKETPLACE: ("marketplace", "multi-vendor", "multi-sided platform"),
    BusinessModel.AGENCY: ("agency",),
    BusinessModel.CONSULTANCY: ("consultancy", "consulting"),
    BusinessModel.WHOLESALE: ("wholesale", "distributor", "distribution partner"),
    BusinessModel.RETAIL: ("retail", "storefront", "brick-and-mortar", "brick and mortar"),
    BusinessModel.SERVICE: ("service provider", "professional services", "managed service"),
}


def _keyword_pattern(keyword: str) -> re.Pattern[str]:
    # Word-boundary matching — without it, "b2b" (a real keyword) matches
    # as a raw substring of "b2b2c" (a different, more specific keyword),
    # which would wrongly flag both models from one unambiguous mention.
    return re.compile(r"\b" + re.escape(keyword) + r"\b")


_MODEL_KEYWORD_PATTERNS: dict[BusinessModel, tuple[re.Pattern[str], ...]] = {
    model: tuple(_keyword_pattern(keyword) for keyword in keywords) for model, keywords in _MODEL_KEYWORDS.items()
}


def _as_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return " ".join(str(v) for v in value)
    if isinstance(value, dict):
        return " ".join(str(v) for v in value.values())
    return str(value)


def _matched_models_for_record(record: EvidenceRecord) -> set[BusinessModel]:
    text = clean_text(_as_text(record.value)).lower()
    if not text:
        return set()
    return {
        model
        for model, patterns in _MODEL_KEYWORD_PATTERNS.items()
        if any(pattern.search(text) for pattern in patterns)
    }


def classify_business_model(
    company_id: str,
    evidence_records: list[EvidenceRecord],
) -> BusinessModelClassificationResult:
    """Pure and deterministic: the same evidence always produces the same
    classification. There is no ICP or soft-preference input by design —
    both are structurally impossible to read here."""

    relevant = [r for r in evidence_records if r.field not in _EXCLUDED_FIELDS]

    # 1. A genuine conflict on the single-valued company_type field must
    #    never be silently resolved into a HYBRID or an arbitrary pick.
    single_value_records = [r for r in relevant if r.field == _SINGLE_VALUE_FIELD]
    single_value_status = compute_field_status(_SINGLE_VALUE_FIELD, single_value_records)

    conflicting_evidence_ids: tuple[str, ...] = ()
    usable_records = relevant
    if single_value_status == EvidenceStatus.CONFLICT:
        conflicting_evidence_ids = tuple(r.id for r in single_value_records)
        # The conflicting field's own signal is excluded entirely — other,
        # independent evidence may still classify the company; this field
        # alone must not, in either direction.
        usable_records = [r for r in relevant if r.field != _SINGLE_VALUE_FIELD]

    # 2. Collect keyword matches from every remaining, usable record.
    evidence_by_model: dict[BusinessModel, list[EvidenceRecord]] = defaultdict(list)
    for record in usable_records:
        for model in _matched_models_for_record(record):
            evidence_by_model[model].append(record)

    if not evidence_by_model:
        status = (
            ClassificationStatus.CONFLICTING_EVIDENCE
            if conflicting_evidence_ids
            else ClassificationStatus.INSUFFICIENT_EVIDENCE
        )
        explanation = (
            "Company type evidence conflicts and no other business-model evidence resolves it."
            if conflicting_evidence_ids
            else "No evidence supports any business model."
        )
        return BusinessModelClassificationResult(
            company_id=company_id,
            primary_model=BusinessModel.UNKNOWN,
            secondary_models=(),
            status=status,
            confidence=ConfidenceLevel.UNKNOWN,
            supporting_evidence_ids=(),
            conflicting_evidence_ids=conflicting_evidence_ids,
            explanation=explanation,
        )

    matched_models = sorted(evidence_by_model.keys(), key=lambda m: m.value)
    supporting_evidence_ids = tuple(
        dict.fromkeys(record.id for records in evidence_by_model.values() for record in records)
    )
    confidence = (
        ConfidenceLevel.MEDIUM
        if any(len(records) >= 2 for records in evidence_by_model.values())
        else ConfidenceLevel.LOW
    )

    if len(matched_models) == 1:
        primary = matched_models[0]
        secondary: tuple[BusinessModel, ...] = ()
        explanation = f"Evidence supports {primary.value}."
    else:
        primary = BusinessModel.HYBRID
        secondary = tuple(matched_models)
        explanation = (
            f"Evidence independently supports multiple models: {', '.join(m.value for m in matched_models)}."
        )

    return BusinessModelClassificationResult(
        company_id=company_id,
        primary_model=primary,
        secondary_models=secondary,
        status=ClassificationStatus.CLASSIFIED,
        confidence=confidence,
        supporting_evidence_ids=supporting_evidence_ids,
        conflicting_evidence_ids=conflicting_evidence_ids,
        explanation=explanation,
    )
