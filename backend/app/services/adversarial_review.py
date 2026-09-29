"""Phase 17 — adversarial review orchestrator.

The hard gate is enforced here first, structurally, exactly like Phase 16:

    first-pass hard_rule_result == FAIL or HOLD -> HARD_BLOCKED,
        adversarial_result=NOT_EXECUTED, adversarial LLM never invoked
    first-pass hard_rule_result == PASS -> the adversarial LLM is invoked
        and may return SURVIVES/WEAKENED/DISPROVED/HOLD

A FAIL/HOLD result is already final (Phase 3/12's authority); there is
nothing for an adversarial pass to challenge that isn't already blocked,
and calling an LLM on it would risk exactly the kind of "the model
reinterpreted a hard rule" failure this project forbids.

Anti-hallucination is enforced identically to Phase 16: every evidence id
cited in supporting_evidence_ids or contradicting_evidence_ids must be a
member of the evidence ids in the underlying QualificationContext (the
exact same evidence the first pass saw — this module never introduces a
second evidence set). A single fabricated id fails the whole attempt.

ADVERSARIAL_PROMPT_VERSION is independent of Phase 16's PROMPT_VERSION so
either can be bumped without affecting the other's audit trail.
"""
from __future__ import annotations

import json

from pydantic import ValidationError

from app.schemas.adversarial_review import (
    AdversarialContext,
    AdversarialExecutionStatus,
    AdversarialResult,
    AdversarialReviewResult,
    RawAdversarialOutput,
)
from app.schemas.hard_rule_result import OverallResult
from app.services.llm_providers.base import LLMProvider, LLMProviderErrorCode

ADVERSARIAL_PROMPT_VERSION = "phase17-v1"

_SYSTEM_INSTRUCTIONS = (
    "You are an adversarial lead-qualification reviewer. Your job is to actively try to DISPROVE "
    "the first-pass qualification decision supplied below, using ONLY the supplied evidence. Do "
    "not simply ask whether the first pass 'looks right' — independently inspect the evidence for "
    "identity problems, ICP mismatches, business-model mismatches, weak or unsupported commercial "
    "signals, evidence conflicts, staleness, and any claim in the first pass that supplied evidence "
    "does not actually support. Never invent a fact, evidence id, person, company, title, employee "
    "count, business model, or signal. Missing evidence is not a contradiction: use HOLD or WEAKENED "
    "for missing/insufficient evidence, and reserve DISPROVED only for a genuine, evidence-backed "
    "contradiction of the first pass or a material fit problem you can cite. The hard-rule result "
    "supplied to you is authoritative and final: acknowledge it, never contest or reinterpret it. "
    "Respond with strict JSON matching the required schema only."
)


def build_adversarial_prompt(context: AdversarialContext) -> str:
    """Structurally different from Phase 16's build_prompt(): framed
    around challenging an existing decision (first_pass block front and
    center) rather than producing a first opinion from a blank slate."""
    base = context.base
    payload = {
        "instruction": "Challenge the first-pass qualification below. Look for reasons this lead might NOT fit.",
        "first_pass_qualification": {
            "qualification_id": context.first_pass.qualification_id,
            "decision": context.first_pass.decision.value if context.first_pass.decision else None,
            "confidence": context.first_pass.confidence,
            "summary": context.first_pass.summary,
            "supporting_evidence_ids": context.first_pass.supporting_evidence_ids,
            "risk_evidence_ids": context.first_pass.risk_evidence_ids,
            "missing_evidence": context.first_pass.missing_evidence,
            "commercial_fit_explanation": context.first_pass.commercial_fit_explanation,
            "uncertainties": context.first_pass.uncertainties,
        },
        "icp": {
            "id": base.icp_id,
            "version": base.icp_version,
            "industries": base.icp_industries,
            "geography": base.icp_geography,
            "employee_range": base.icp_employee_range,
            "allowed_titles": base.icp_allowed_titles,
            "company_types": base.icp_company_types,
            "exclusions": base.icp_exclusions,
            "soft_preferences": base.icp_soft_preferences,
        },
        "company_id": base.company_id,
        "person_id": base.person_id,
        "hard_rule_result": base.hard_rule_result.value,
        "rule_results": [r.model_dump() for r in base.rule_results],
        "reason_codes": [c.value for c in base.reason_codes],
        "business_model_summary": base.business_model_summary,
        "commercial_signal_summary": base.commercial_signal_summary,
        "scores": {
            "icp_score": base.icp_score,
            "commercial_score": base.commercial_score,
            "evidence_score": base.evidence_score,
            "freshness_score": base.freshness_score,
            "identity_confidence": base.identity_confidence,
            "final_score": base.final_score,
        },
        "evidence": [e.model_dump() for e in base.evidence],
        "conflicting_fields": base.conflicting_fields,
        "missing_critical_fields": base.missing_critical_fields,
    }
    return _SYSTEM_INSTRUCTIONS + "\n\n" + json.dumps(payload, sort_keys=True)


def _failure_result(
    context: AdversarialContext,
    provider: LLMProvider,
    status: AdversarialExecutionStatus,
    error_message: str,
) -> AdversarialReviewResult:
    return AdversarialReviewResult(
        icp_id=context.icp_id,
        icp_version=context.icp_version,
        company_id=context.company_id,
        person_id=context.person_id,
        qualification_id=context.first_pass.qualification_id,
        hard_rule_result=context.hard_rule_result,
        status=status,
        adversarial_result=None,
        confidence=None,
        contradictions=(),
        risk_codes=(),
        supporting_evidence_ids=(),
        contradicting_evidence_ids=(),
        unsupported_claims=(),
        missing_evidence=(),
        reasoning_summary="",
        recommendation="",
        provider_id=provider.provider_id,
        model_id=provider.model_id,
        prompt_version=ADVERSARIAL_PROMPT_VERSION,
        error_message=error_message,
    )


def _gated_result(context: AdversarialContext, provider: LLMProvider) -> AdversarialReviewResult | None:
    """Returns a structural, LLM-free result when the first-pass hard-rule
    result is FAIL/HOLD; None when the caller should proceed to actually
    invoke the adversarial provider."""
    if context.hard_rule_result in (OverallResult.FAIL, OverallResult.HOLD):
        return AdversarialReviewResult(
            icp_id=context.icp_id,
            icp_version=context.icp_version,
            company_id=context.company_id,
            person_id=context.person_id,
            qualification_id=context.first_pass.qualification_id,
            hard_rule_result=context.hard_rule_result,
            status=AdversarialExecutionStatus.HARD_BLOCKED,
            adversarial_result=AdversarialResult.NOT_EXECUTED,
            confidence=None,
            contradictions=(),
            risk_codes=(),
            supporting_evidence_ids=(),
            contradicting_evidence_ids=(),
            unsupported_claims=(),
            missing_evidence=(),
            reasoning_summary=(
                f"Hard ICP result is {context.hard_rule_result.value}; already final per Phase 3/12 — "
                "adversarial review is never invoked and cannot change it."
            ),
            recommendation="",
            provider_id=provider.provider_id,
            model_id=provider.model_id,
            prompt_version=ADVERSARIAL_PROMPT_VERSION,
            error_message=None,
        )
    return None


_TIMEOUT_CODES = frozenset({LLMProviderErrorCode.TIMEOUT})


def run_adversarial_review(context: AdversarialContext, provider: LLMProvider) -> AdversarialReviewResult:
    """Pure orchestration: no database access. The caller (app/api/adversarial_review.py)
    owns persistence, mirroring app/services/llm_qualification.py's qualify_lead().
    """
    gated = _gated_result(context, provider)
    if gated is not None:
        return gated

    response = provider.qualify(context)

    if not response.success:
        error = response.error
        code = error.code if error else LLMProviderErrorCode.PROVIDER_ERROR
        message = error.message if error else "unknown provider failure"
        status = (
            AdversarialExecutionStatus.PROVIDER_TIMEOUT
            if code in _TIMEOUT_CODES
            else AdversarialExecutionStatus.PROVIDER_ERROR
        )
        return _failure_result(context, provider, status, message)

    if not response.raw_text or not response.raw_text.strip():
        return _failure_result(context, provider, AdversarialExecutionStatus.EMPTY_RESPONSE, "provider returned an empty response")

    try:
        parsed_json = json.loads(response.raw_text)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        return _failure_result(context, provider, AdversarialExecutionStatus.MALFORMED_OUTPUT, f"could not parse JSON: {exc}")

    try:
        output = RawAdversarialOutput.model_validate(parsed_json)
    except ValidationError as exc:
        return _failure_result(context, provider, AdversarialExecutionStatus.SCHEMA_INVALID, f"schema validation failed: {exc}")

    known_ids = context.known_evidence_ids()
    cited_ids = {*output.supporting_evidence_ids, *output.contradicting_evidence_ids}
    fabricated_ids = cited_ids - known_ids
    if fabricated_ids:
        return _failure_result(
            context,
            provider,
            AdversarialExecutionStatus.INVALID_EVIDENCE_IDS,
            f"response cited evidence ids not present in the supplied context: {sorted(fabricated_ids)}",
        )

    return AdversarialReviewResult(
        icp_id=context.icp_id,
        icp_version=context.icp_version,
        company_id=context.company_id,
        person_id=context.person_id,
        qualification_id=context.first_pass.qualification_id,
        hard_rule_result=context.hard_rule_result,
        status=AdversarialExecutionStatus.SUCCESS,
        adversarial_result=output.adversarial_result,
        confidence=output.confidence,
        contradictions=tuple(output.contradictions),
        risk_codes=tuple(output.risk_codes),
        supporting_evidence_ids=tuple(output.supporting_evidence_ids),
        contradicting_evidence_ids=tuple(output.contradicting_evidence_ids),
        unsupported_claims=tuple(output.unsupported_claims),
        missing_evidence=tuple(output.missing_evidence),
        reasoning_summary=output.reasoning_summary,
        recommendation=output.recommendation,
        provider_id=provider.provider_id,
        model_id=provider.model_id,
        prompt_version=ADVERSARIAL_PROMPT_VERSION,
        error_message=None,
    )
