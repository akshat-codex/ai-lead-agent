"""Phase 16 — deterministic mock LLM provider.

No paid provider integration is required to test this phase (per the task's
PROVIDER / MODEL ABSTRACTION section). MockLLMProvider is fully
deterministic and configurable per-instance, so tests can exercise every
execution-status branch (malformed JSON, schema violations, fabricated
evidence ids, timeouts, provider errors, empty responses) without any
network call or SDK.
"""
from __future__ import annotations

from app.schemas.llm_qualification import QualificationContext
from app.services.llm_providers.base import (
    LLMProvider,
    LLMProviderError,
    LLMProviderErrorCode,
    LLMProviderResponse,
)


class MockLLMProvider(LLMProvider):
    """Returns a fixed, caller-supplied JSON string (or triggers a
    specific failure mode) regardless of context — "deterministic" here
    means byte-for-byte reproducible across calls with the same
    configuration, exactly what a test needs and nothing a real vendor
    call could promise.
    """

    def __init__(
        self,
        provider_id: str = "mock-llm",
        model_id: str = "mock-llm-v1",
        response_text: str | None = None,
        raise_timeout: bool = False,
        raise_provider_error: bool = False,
        return_empty: bool = False,
    ) -> None:
        super().__init__(provider_id=provider_id, model_id=model_id)
        self._response_text = response_text
        self._raise_timeout = raise_timeout
        self._raise_provider_error = raise_provider_error
        self._return_empty = return_empty

    def _call(self, context: QualificationContext) -> LLMProviderResponse:
        if self._raise_timeout:
            return LLMProviderResponse(
                provider_id=self.provider_id,
                model_id=self.model_id,
                success=False,
                error=LLMProviderError(code=LLMProviderErrorCode.TIMEOUT, message="mock provider timed out", retryable=True),
            )
        if self._raise_provider_error:
            return LLMProviderResponse(
                provider_id=self.provider_id,
                model_id=self.model_id,
                success=False,
                error=LLMProviderError(code=LLMProviderErrorCode.PROVIDER_ERROR, message="mock provider failed", retryable=True),
            )
        if self._return_empty:
            return LLMProviderResponse(provider_id=self.provider_id, model_id=self.model_id, success=True, raw_text="")

        return LLMProviderResponse(
            provider_id=self.provider_id,
            model_id=self.model_id,
            success=True,
            raw_text=self._response_text or "",
        )


def build_good_fit_response(context: QualificationContext) -> str:
    """A helper for tests/defaults: a well-formed response that cites only
    real evidence ids drawn from the given context, never inventing any."""
    import json

    supporting = [e.id for e in context.evidence[:2]]
    return json.dumps(
        {
            "decision": "GOOD_FIT",
            "confidence": 82,
            "reason_codes": ["STRONG_ICP_ALIGNMENT"],
            "summary": "Evidence-backed alignment with the stated ICP and commercial preferences.",
            "supporting_evidence_ids": supporting,
            "risk_evidence_ids": [],
            "missing_evidence": [],
            "commercial_fit_explanation": "Business model and commercial signals match the ICP's stated preferences.",
            "hard_rule_acknowledgement": f"Hard ICP result is {context.hard_rule_result.value}; treated as authoritative.",
            "uncertainties": [],
        }
    )


def build_relevant_deep_prescreen_response() -> str:
    """A helper for the default (no real LLM key configured) provider: a
    well-formed RawDeepPrescreenOutput. Defaults to RELEVANT, matching this
    codebase's own explicit "when uncertain, let the candidate through"
    policy for deep mode (see app/services/deep_prescreen.py's own
    _SYSTEM_INSTRUCTIONS) — a mock standing in for "no real judgment was
    made" should never fabricate a NOT_RELEVANT verdict that would drop a
    real candidate."""
    import json

    return json.dumps(
        {
            "verdict": "RELEVANT",
            "reason": "No real LLM provider is configured; deep pre-screen defaults to letting the candidate through.",
            "confidence": 0,
        }
    )


def build_no_expansion_discovery_strategy_response() -> str:
    """A helper for the default (no real LLM key configured) provider: a
    well-formed RawDiscoveryStrategyOutput that proposes NO additional
    terms at all. Discovery-strategy interpretation is a pure term-
    expansion convenience (see app/schemas/discovery_strategy.py) — a
    response with empty term lists is a valid, honest SUCCESS output
    (merge_strategy_into_hard_rules is a no-op on it), never a fabricated
    guess at what the ICP's terms might expand to without a real model."""
    import json

    return json.dumps(
        {
            "industry_terms": [],
            "combination_industry_terms": [],
            "company_type_terms": [],
            "exclusion_terms": [],
            "geography_notes": [],
            "unsupported_intent": [],
            "confidence": 0,
            "reasoning": "No real LLM provider is configured; term expansion was skipped and the ICP's own terms are used unexpanded.",
        }
    )
