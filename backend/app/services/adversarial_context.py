"""Phase 17 — adversarial context builder.

Reuses app/services/qualification_context.py's build_qualification_context()
verbatim for every ICP/hard-rule/business-model/commercial-signal/score/
evidence field — this module adds only what is new to Phase 17: a compact
view of the first-pass Phase 16 qualification being challenged. No field
already computed by Phase 11-16 is re-derived here.
"""
from __future__ import annotations

from app.schemas.adversarial_review import AdversarialContext, FirstPassBrief
from app.schemas.business_model import BusinessModelClassificationResult
from app.schemas.canonical_icp import CanonicalICP
from app.schemas.commercial_signal import CommercialSignalResult
from app.schemas.evidence import EvidenceRecord
from app.schemas.hard_rule_result import HardRuleEvaluation
from app.schemas.llm_qualification import LLMQualificationResult
from app.schemas.scoring import LeadScoreResult
from app.services.qualification_context import build_qualification_context


def build_first_pass_brief(qualification_id: str, qualification: LLMQualificationResult) -> FirstPassBrief:
    return FirstPassBrief(
        qualification_id=qualification_id,
        decision=qualification.decision,
        confidence=qualification.confidence,
        summary=qualification.summary,
        supporting_evidence_ids=qualification.supporting_evidence_ids,
        risk_evidence_ids=qualification.risk_evidence_ids,
        missing_evidence=qualification.missing_evidence,
        commercial_fit_explanation=qualification.commercial_fit_explanation,
        uncertainties=qualification.uncertainties,
    )


def build_adversarial_context(
    icp: CanonicalICP,
    company_id: str,
    company_evidence: list[EvidenceRecord],
    hard_rule_evaluation: HardRuleEvaluation,
    business_model: BusinessModelClassificationResult | None,
    commercial_signals: list[CommercialSignalResult],
    score: LeadScoreResult,
    qualification_id: str,
    qualification: LLMQualificationResult,
    person_id: str | None = None,
    person_evidence: list[EvidenceRecord] | None = None,
) -> AdversarialContext:
    base = build_qualification_context(
        icp=icp,
        company_id=company_id,
        company_evidence=company_evidence,
        hard_rule_evaluation=hard_rule_evaluation,
        business_model=business_model,
        commercial_signals=commercial_signals,
        score=score,
        person_id=person_id,
        person_evidence=person_evidence,
    )
    return AdversarialContext(base=base, first_pass=build_first_pass_brief(qualification_id, qualification))
