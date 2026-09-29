"""Phase 14 — Commercial Signal Extraction.

Deterministically extracts structured, evidence-backed commercial/growth
signals ("this company runs Meta ad campaigns", "this company mentions a
subscription model") from a company's Phase 11 evidence. This is an
observation layer, not a judgment: it never scores, qualifies, or reads an
ICP or manager feedback (there is no parameter for any of them), and it
never decides a signal is "good" — see docs/quality-contract.md's
separation of evidence from qualification, extended here into commercial
behavior.

SIGNAL_DEFINITIONS is deliberately just data (signal_type -> keyword
phrases), not hardcoded logic — the engine below is generic over whatever
entries this table holds, so the framework is extensible per-ICP later
without touching this module. It is explicitly NOT the current manager
sheet reinterpreted as universal truth: nothing here maps a signal to
GOOD_FIT/NOT_FIT (that is forbidden until Phase 25, and even then belongs
to a ranking layer, not this one).

Status is computed by literally reusing Phase 11's compute_field_status()
(app/services/evidence_engine.py) — the same "2+ agreeing sources or a
stated confidence -> SUPPORTED, one weak source -> INSUFFICIENT,
disagreement -> CONFLICT" rule that governs every other fact in this
system, applied here to a signal's presence/absence "fact" instead of a
company attribute.

No keyword-only false positives: every phrase below names an unambiguous,
company-specific behavior ("built on shopify") rather than a bare
technology/category name ("shopify") that could appear in unrelated text —
see the module docstring examples in the Phase 14 task itself. Matching is
restricted to fields that describe what a company actually does
(_ALLOWED_SIGNAL_FIELDS); "industry" and identity/geography/size fields are
never scanned, so a shared industry or company name never becomes a
signal.
"""
from __future__ import annotations

import re

from app.schemas.commercial_signal import CommercialSignalResult
from app.schemas.evidence import ConfidenceLevel, EvidenceRecord, EvidenceStatus
from app.services.evidence_engine import compute_field_status
from app.services.icp_normalization import clean_text

# Only fields that describe a company's own products/services or
# commercial characteristics are scanned — never identity, geography,
# size, or "industry" (a category, not an observed behavior).
_ALLOWED_SIGNAL_FIELDS = frozenset({"products_services", "business_model", "company_type"})

# Multi-word, specific attribution phrases only. A bare word like
# "shopify" or "growth" appears too often in unrelated text to trust on
# its own; every phrase here names the behavior happening *at this
# company*, not just a mention of a related concept.
SIGNAL_DEFINITIONS: dict[str, tuple[str, ...]] = {
    "DTC": ("direct-to-consumer", "direct to consumer", "dtc brand", "d2c brand"),
    "ECOMMERCE": ("ecommerce store", "e-commerce store", "online store", "online shop"),
    "SHOPIFY": ("built on shopify", "shopify store", "shopify-powered", "runs on shopify", "uses shopify"),
    "META_ADVERTISING": ("meta advertising", "facebook ads", "instagram ads", "runs meta ads", "meta ad campaigns"),
    "TIKTOK_ADVERTISING": ("tiktok ads", "tiktok advertising", "tiktok ad campaigns"),
    "GOOGLE_ADVERTISING": ("google ads", "google advertising", "google ad campaigns"),
    "INFLUENCER_MARKETING": ("influencer marketing", "influencer partnerships", "influencer campaigns"),
    "CREATOR_MARKETING": ("creator marketing", "creator partnerships", "creator campaigns"),
    "EMAIL_CRM": ("email marketing", "crm platform", "email and crm", "email/crm"),
    "SUBSCRIPTION": ("subscription model", "subscription service", "subscription-based", "recurring subscription"),
    "REPEAT_PURCHASES": ("repeat purchase", "repeat customers", "repeat buyers"),
    "PERFORMANCE_MARKETING": ("performance marketing",),
    "GROWTH_MARKETING": ("growth marketing",),
    "RETAIL_EXPANSION": ("retail expansion", "expanding into retail", "expanding to retail"),
    "INTERNATIONAL_EXPANSION": ("international expansion", "expanding internationally", "global expansion"),
    "NEW_PRODUCT_LAUNCH": ("new product launch", "launched a new product", "new product line"),
    "FUNDING": ("raised funding", "series a", "series b", "seed funding", "venture funding"),
    "MARKETING_HIRING": ("hiring a marketing", "marketing hire", "hiring for marketing"),
    "DISTRIBUTION_PARTNERSHIP": ("distribution partnership", "distribution deal", "distribution agreement"),
    "CONSUMER_BRAND": ("consumer brand", "consumer-facing brand"),
}

# Explicit-absence phrasing, defined only where a negative statement is a
# realistic, unambiguous thing evidence might say (per the task's own
# subscription example). Undefined for a signal simply means "no negation
# vocabulary modeled yet" — not that the signal can never conflict; adding
# an entry here is the extension point.
SIGNAL_NEGATION_PHRASES: dict[str, tuple[str, ...]] = {
    "SUBSCRIPTION": ("no subscription", "not a subscription", "one-time purchase only", "non-subscription"),
    "ECOMMERCE": ("no online store", "not sold online", "wholesale only, no online store"),
}


def _keyword_pattern(phrase: str) -> re.Pattern[str]:
    return re.compile(r"\b" + re.escape(phrase) + r"\b")


_POSITIVE_PATTERNS: dict[str, tuple[tuple[re.Pattern[str], str], ...]] = {
    signal_type: tuple((_keyword_pattern(phrase), phrase) for phrase in phrases)
    for signal_type, phrases in SIGNAL_DEFINITIONS.items()
}
_NEGATIVE_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    signal_type: tuple(_keyword_pattern(phrase) for phrase in phrases)
    for signal_type, phrases in SIGNAL_NEGATION_PHRASES.items()
}


def _as_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return " ".join(str(v) for v in value)
    if isinstance(value, dict):
        return " ".join(str(v) for v in value.values())
    return str(value)


def _verdict_for_record(signal_type: str, record: EvidenceRecord) -> tuple[EvidenceRecord, str | None] | None:
    """Returns (synthetic_record, matched_phrase) where synthetic_record is
    `record` with .value replaced by True/False — reusing the exact same
    identity/provenance (id, provider_id, retrieved_at) so downstream
    provenance and temporal tracking need no separate bookkeeping. Returns
    None when this record says nothing about this signal at all.

    A negation phrase wins over a positive one if a single record somehow
    contains both — an explicit denial is the more specific statement.
    """
    text = clean_text(_as_text(record.value)).lower()
    if not text:
        return None

    for pattern in _NEGATIVE_PATTERNS.get(signal_type, ()):
        if pattern.search(text):
            return record.model_copy(update={"value": False}), None

    for pattern, phrase in _POSITIVE_PATTERNS.get(signal_type, ()):
        if pattern.search(text):
            return record.model_copy(update={"value": True}), phrase

    return None


def extract_commercial_signals(
    company_id: str,
    evidence_records: list[EvidenceRecord],
) -> tuple[CommercialSignalResult, ...]:
    """Pure and deterministic: the same evidence always produces the same
    signals. Takes only a company id and its evidence — there is no ICP or
    manager-feedback parameter, by design; neither can influence this
    function even by accident.
    """
    relevant = [r for r in evidence_records if r.field in _ALLOWED_SIGNAL_FIELDS]

    results: list[CommercialSignalResult] = []
    for signal_type in SIGNAL_DEFINITIONS:
        verdict_records: list[EvidenceRecord] = []
        matched_phrases: dict[str, str] = {}
        for record in relevant:
            verdict = _verdict_for_record(signal_type, record)
            if verdict is None:
                continue
            synthetic, phrase = verdict
            verdict_records.append(synthetic)
            if phrase is not None:
                matched_phrases[record.id] = phrase

        if not verdict_records:
            continue  # never mentioned at all -> absent from the result, not UNKNOWN-but-listed

        status = compute_field_status(signal_type, verdict_records)
        evidence_ids = tuple(dict.fromkeys(r.id for r in verdict_records))
        provider_ids = tuple(
            dict.fromkeys(r.source_provider_id for r in verdict_records if r.source_provider_id)
        )
        positive_records = [r for r in verdict_records if r.value is True]
        timestamps = [r.retrieved_at for r in verdict_records]

        if status == EvidenceStatus.CONFLICT:
            confidence = ConfidenceLevel.UNKNOWN
            value = None
            explanation = f"Evidence conflicts on whether {signal_type} applies — some sources affirm it, others deny it."
        else:
            confidence = ConfidenceLevel.MEDIUM if len(positive_records) >= 2 else ConfidenceLevel.LOW
            value = next(iter(matched_phrases.values()), None)
            if status == EvidenceStatus.SUPPORTED:
                explanation = f"{len(positive_records)} independent source(s) support {signal_type}."
            else:
                explanation = f"A single, uncorroborated source mentions {signal_type}."

        results.append(
            CommercialSignalResult(
                company_id=company_id,
                signal_type=signal_type,
                status=status,
                confidence=confidence,
                value=value,
                provider_ids=provider_ids,
                evidence_ids=evidence_ids,
                first_seen=min(timestamps) if timestamps else None,
                last_seen=max(timestamps) if timestamps else None,
                explanation=explanation,
            )
        )

    return tuple(results)
