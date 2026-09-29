"""Phase 16 — LLM qualification orchestrator. Extended in Phase 12 with a
second, narrower gate for selective semantic verification.

The hard gate is enforced here, structurally, before any provider is ever
called:

    FAIL                                -> HARD_REJECTED, decision=REJECT, LLM never invoked
    HOLD                                -> HARD_HOLD, decision=HOLD, LLM never invoked
    PASS + structured discovery match   -> STRUCTURED_MATCH_TRUSTED, decision=GOOD_FIT
                                            (backend-assigned), LLM never invoked
    PASS + keyword-fallback/unknown     -> the LLM IS invoked and may return
                                            GOOD_FIT/WEAK_FIT/NOT_FIT/HOLD

This mirrors Phase 15's "final_score is None unless PASS" guarantee: no
matter what a provider ever returns, a FAIL/HOLD hard-rule result can never
reach a qualify() call, so it structurally cannot be talked into GOOD_FIT.
The Phase 12 addition narrows further, WITHOUT weakening this guarantee:
among PASS candidates, only ones whose industry evidence has no real,
live-verified structured taxonomy match (app/services/
qualification_context.py::_discovery_match_type == "keyword_fallback" or
"unknown") ever reach a live provider call — exactly the population most
at risk of being an "agency that merely mentions the ICP's industry"
false positive (see docs/phase12-audit or the Phase 10-continuation
discovery-quality evaluation this phase is a direct response to). A
structured match still had to clear the SAME deterministic hard rules as
every other candidate; skipping the extra LLM call trusts that real,
already-verified taxonomy evidence, it does not bypass qualification.

Anti-hallucination is enforced in one place, after the provider responds:
every id in supporting_evidence_ids/risk_evidence_ids must be a member of
the evidence ids actually supplied in the QualificationContext. A single
fabricated id fails the entire attempt (status=INVALID_EVIDENCE_IDS) rather
than being silently dropped — a provider that invents even one citation is
not trustworthy for this decision.

PROMPT_VERSION is bumped whenever the prompt template or required-field
contract changes, so qualification history can be filtered/audited by
which prompt version produced it.
"""
from __future__ import annotations

import json

from pydantic import ValidationError

from app.schemas.hard_rule_result import OverallResult
from app.schemas.llm_qualification import (
    LLMQualificationResult,
    QualificationContext,
    QualificationDecision,
    QualificationExecutionStatus,
    RawQualificationOutput,
)
from app.services.llm_providers.base import LLMProvider, LLMProviderErrorCode

PROMPT_VERSION = "phase16-v5"  # bumped in Phase 15: added discovery_structured_match_scope/total_results (observability)

_SYSTEM_INSTRUCTIONS = (
    "You are a lead qualification assistant. Use ONLY the supplied evidence and structured "
    "summaries. Every factual claim must cite an evidence id from the supplied list. Never "
    "invent a person, company, title, employee count, business model, signal, URL, or evidence "
    "id. If the evidence is insufficient to decide, return HOLD rather than guessing. The "
    "hard-rule result supplied to you is authoritative and final: acknowledge it, never contest "
    "or reinterpret it. "
    "When discovery_match_type is 'keyword_fallback', this candidate was found via a low-precision "
    "keyword/substring search, not a verified industry-taxonomy match — discovery_keyword_terms lists "
    "the exact search term(s) that matched somewhere on the company's data (e.g. its site content), and "
    "discovery_keyword_term_sources tells you, per term at the same position, whether it came from an "
    "unmatched ICP INDUSTRY term or a COMPANY_TYPE term — these are different claims: an industry-source "
    "term (e.g. 'Entertainment') is a broad sector match, while a company_type-source term (e.g. 'D2C') "
    "is a claim about what KIND of business this is, which is often the more decisive signal for judging "
    "genuine fit. Pay special attention to whether the evidence shows the company GENUINELY operates in "
    "the ICP's stated industry/company type (e.g. actually sells its own product, actually is the type "
    "of business described), versus merely mentioning one of discovery_keyword_terms without being that "
    "kind of business (e.g. an agency, consultancy, or staffing firm whose site mentions a term like "
    "'healthcare' or 'SaaS' because it serves or discusses that industry, without being part of it). "
    "Base this judgment only on the supplied evidence — never on assumptions about what a company "
    "name or domain implies. "
    "Respond with strict JSON matching the required schema only."
)


def build_prompt(context: QualificationContext) -> str:
    """A compact, deterministic textual rendering of the context — the
    same context always renders to the same prompt string. Kept as plain,
    dense text (not verbose prose) to minimize tokens while preserving
    every field a real vendor call would need."""
    payload = {
        "icp": {
            "id": context.icp_id,
            "version": context.icp_version,
            "industries": context.icp_industries,
            "geography": context.icp_geography,
            "employee_range": context.icp_employee_range,
            "allowed_titles": context.icp_allowed_titles,
            "company_types": context.icp_company_types,
            "exclusions": context.icp_exclusions,
            "soft_preferences": context.icp_soft_preferences,
        },
        "company_id": context.company_id,
        "person_id": context.person_id,
        "discovery_match_type": context.discovery_match_type,
        "discovery_keyword_terms": context.discovery_keyword_terms,
        "discovery_keyword_term_sources": context.discovery_keyword_term_sources,
        "discovery_structured_match_scope": context.discovery_structured_match_scope,
        "discovery_structured_match_total_results": context.discovery_structured_match_total_results,
        "hard_rule_result": context.hard_rule_result.value,
        "rule_results": [r.model_dump() for r in context.rule_results],
        "reason_codes": [c.value for c in context.reason_codes],
        "business_model_summary": context.business_model_summary,
        "commercial_signal_summary": context.commercial_signal_summary,
        "scores": {
            "icp_score": context.icp_score,
            "commercial_score": context.commercial_score,
            "evidence_score": context.evidence_score,
            "freshness_score": context.freshness_score,
            "identity_confidence": context.identity_confidence,
            "final_score": context.final_score,
        },
        "evidence": [e.model_dump() for e in context.evidence],
        "conflicting_fields": context.conflicting_fields,
        "missing_critical_fields": context.missing_critical_fields,
    }
    return _SYSTEM_INSTRUCTIONS + "\n\n" + json.dumps(payload, sort_keys=True)


def _score_snapshot(context: QualificationContext) -> dict[str, float | None]:
    return {
        "icp_score": context.icp_score,
        "commercial_score": context.commercial_score,
        "evidence_score": context.evidence_score,
        "freshness_score": context.freshness_score,
        "identity_confidence": context.identity_confidence,
        "final_score": context.final_score,
    }


def _failure_result(
    context: QualificationContext,
    provider: LLMProvider,
    status: QualificationExecutionStatus,
    error_message: str,
) -> LLMQualificationResult:
    return LLMQualificationResult(
        icp_id=context.icp_id,
        icp_version=context.icp_version,
        company_id=context.company_id,
        person_id=context.person_id,
        hard_rule_result=context.hard_rule_result,
        status=status,
        decision=None,
        confidence=None,
        reason_codes=(),
        summary="",
        supporting_evidence_ids=(),
        risk_evidence_ids=(),
        missing_evidence=(),
        commercial_fit_explanation="",
        hard_rule_acknowledgement="",
        uncertainties=(),
        provider_id=provider.provider_id,
        model_id=provider.model_id,
        prompt_version=PROMPT_VERSION,
        score_snapshot=_score_snapshot(context),
        error_message=error_message,
    )


def _gated_result(context: QualificationContext, provider: LLMProvider) -> LLMQualificationResult | None:
    """Returns a structural, LLM-free result for FAIL/HOLD; None when the
    candidate is eligible and the caller should proceed to actually call
    the provider."""
    if context.hard_rule_result == OverallResult.FAIL:
        return LLMQualificationResult(
            icp_id=context.icp_id,
            icp_version=context.icp_version,
            company_id=context.company_id,
            person_id=context.person_id,
            hard_rule_result=context.hard_rule_result,
            status=QualificationExecutionStatus.HARD_REJECTED,
            decision=QualificationDecision.REJECT,
            confidence=None,
            reason_codes=tuple(c.value for c in context.reason_codes),
            summary="Hard ICP validation failed; rejected without LLM involvement.",
            supporting_evidence_ids=(),
            risk_evidence_ids=(),
            missing_evidence=(),
            commercial_fit_explanation="",
            hard_rule_acknowledgement="Hard ICP result is FAIL — this can never be overridden by scoring or an LLM.",
            uncertainties=(),
            provider_id=provider.provider_id,
            model_id=provider.model_id,
            prompt_version=PROMPT_VERSION,
            score_snapshot=_score_snapshot(context),
            error_message=None,
        )
    if context.hard_rule_result == OverallResult.HOLD:
        return LLMQualificationResult(
            icp_id=context.icp_id,
            icp_version=context.icp_version,
            company_id=context.company_id,
            person_id=context.person_id,
            hard_rule_result=context.hard_rule_result,
            status=QualificationExecutionStatus.HARD_HOLD,
            decision=QualificationDecision.HOLD,
            confidence=None,
            reason_codes=tuple(c.value for c in context.reason_codes),
            summary="Hard ICP validation is unresolved; held without LLM involvement.",
            supporting_evidence_ids=(),
            risk_evidence_ids=(),
            missing_evidence=(),
            commercial_fit_explanation="",
            hard_rule_acknowledgement="Hard ICP result is HOLD — this can never be silently upgraded to an accept.",
            uncertainties=(),
            provider_id=provider.provider_id,
            model_id=provider.model_id,
            prompt_version=PROMPT_VERSION,
            score_snapshot=_score_snapshot(context),
            error_message=None,
        )
    if context.hard_rule_result == OverallResult.PASS and context.discovery_match_type == "structured":
        # Phase 12 — selective semantic verification: a hard-rule PASS
        # whose industry evidence came from a real, live-verified
        # structured Explorium taxonomy match (see
        # app/services/qualification_context.py::_discovery_match_type)
        # is trusted without the extra AI call — GOOD_FIT here is
        # backend-assigned (see QualificationDecision.GOOD_FIT's own
        # comment), never an LLM judgment. A "keyword_fallback" or
        # "unknown" match type falls through below and proceeds to a real
        # provider call exactly as every PASS candidate already did
        # before this phase — this branch narrows, it never widens, when
        # the LLM is actually consulted.
        return LLMQualificationResult(
            icp_id=context.icp_id,
            icp_version=context.icp_version,
            company_id=context.company_id,
            person_id=context.person_id,
            hard_rule_result=context.hard_rule_result,
            status=QualificationExecutionStatus.STRUCTURED_MATCH_TRUSTED,
            decision=QualificationDecision.GOOD_FIT,
            confidence=None,
            reason_codes=tuple(c.value for c in context.reason_codes),
            summary="Hard ICP validation passed on a structured Explorium taxonomy match; trusted without semantic verification.",
            supporting_evidence_ids=(),
            risk_evidence_ids=(),
            missing_evidence=(),
            commercial_fit_explanation="",
            hard_rule_acknowledgement="Hard ICP result is PASS, backed by a real structured taxonomy match — no LLM verification needed.",
            uncertainties=(),
            provider_id=provider.provider_id,
            model_id=provider.model_id,
            prompt_version=PROMPT_VERSION,
            score_snapshot=_score_snapshot(context),
            error_message=None,
        )
    return None


_TIMEOUT_CODES = frozenset({LLMProviderErrorCode.TIMEOUT})


def qualify_lead(context: QualificationContext, provider: LLMProvider) -> LLMQualificationResult:
    """Pure orchestration: no database access. The caller (app/api/llm_qualification.py)
    owns persistence, mirroring every other *_engine/service in this codebase.
    """
    gated = _gated_result(context, provider)
    if gated is not None:
        return gated

    response = provider.qualify(context)

    if not response.success:
        error = response.error
        code = error.code if error else LLMProviderErrorCode.PROVIDER_ERROR
        message = error.message if error else "unknown provider failure"
        status = QualificationExecutionStatus.PROVIDER_TIMEOUT if code in _TIMEOUT_CODES else QualificationExecutionStatus.PROVIDER_ERROR
        return _failure_result(context, provider, status, message)

    if not response.raw_text or not response.raw_text.strip():
        return _failure_result(context, provider, QualificationExecutionStatus.EMPTY_RESPONSE, "provider returned an empty response")

    try:
        parsed_json = json.loads(response.raw_text)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        return _failure_result(context, provider, QualificationExecutionStatus.MALFORMED_OUTPUT, f"could not parse JSON: {exc}")

    try:
        output = RawQualificationOutput.model_validate(parsed_json)
    except ValidationError as exc:
        return _failure_result(context, provider, QualificationExecutionStatus.SCHEMA_INVALID, f"schema validation failed: {exc}")

    known_ids = context.known_evidence_ids()
    cited_ids = {*output.supporting_evidence_ids, *output.risk_evidence_ids}
    fabricated_ids = cited_ids - known_ids
    if fabricated_ids:
        return _failure_result(
            context,
            provider,
            QualificationExecutionStatus.INVALID_EVIDENCE_IDS,
            f"response cited evidence ids not present in the supplied context: {sorted(fabricated_ids)}",
        )

    return LLMQualificationResult(
        icp_id=context.icp_id,
        icp_version=context.icp_version,
        company_id=context.company_id,
        person_id=context.person_id,
        hard_rule_result=context.hard_rule_result,
        status=QualificationExecutionStatus.SUCCESS,
        decision=output.decision,
        confidence=output.confidence,
        reason_codes=tuple(output.reason_codes),
        summary=output.summary,
        supporting_evidence_ids=tuple(output.supporting_evidence_ids),
        risk_evidence_ids=tuple(output.risk_evidence_ids),
        missing_evidence=tuple(output.missing_evidence),
        commercial_fit_explanation=output.commercial_fit_explanation,
        hard_rule_acknowledgement=output.hard_rule_acknowledgement,
        uncertainties=tuple(output.uncertainties),
        provider_id=provider.provider_id,
        model_id=provider.model_id,
        prompt_version=PROMPT_VERSION,
        score_snapshot=_score_snapshot(context),
        error_message=None,
    )
