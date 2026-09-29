"""Phase 9 — real OpenAI provider for LLM qualification (Phase 16) and
adversarial review (Phase 17). Extended in Phase 10 for discovery-strategy
interpretation (DiscoveryStrategyRequest) — same provider instance, same
adapter, a third additive branch alongside the existing two.

This is the first non-mock LLMProvider in the codebase. It never becomes a
second source of truth: app/services/llm_qualification.py already enforces
the hard gate (FAIL/HOLD never reach any provider, this one included) and
already rejects any response citing an evidence id not present in the
supplied context; app/services/discovery_strategy.py (Phase 10) never lets
this provider's output be treated as a verified Explorium taxonomy value —
see that module's own docstring. This adapter's only job is to turn a
QualificationContext, AdversarialContext, or DiscoveryStrategyRequest into
one OpenAI Chat Completions call and hand back raw, unparsed, unvalidated
text — exactly like every other LLMProvider, mock or real (see
app/services/llm_providers/base.py).

Anti-fabrication is enforced structurally, not just by prompt wording:
`response_format` uses OpenAI's strict JSON Schema mode
(https://developers.openai.com/api/docs/guides/structured-outputs), built
directly from RawQualificationOutput's own schema — the model cannot return
a field OpenAI's own JSON generation doesn't structurally allow. This
guarantees well-formed output; it does NOT guarantee factual correctness or
that cited evidence ids are real — that check still happens exactly once,
in app/services/llm_qualification.py, identically for every provider.

The system prompt used here is unchanged from the one
app/services/llm_qualification.py already builds
(_SYSTEM_INSTRUCTIONS + build_prompt(context)) — this adapter does not
introduce a second, competing prompt; it sends the same instructions +
context payload every other provider (including the mock) would receive.

The API key is supplied at construction (read once, from Settings, by
app/services/llm_providers/default_registry.py) and never read from the
environment inside _call(), never included in any LLMProviderResponse
field, and never written to a log line.
"""
from __future__ import annotations

import json

import httpx

from app.schemas.adversarial_review import AdversarialContext
from app.schemas.deep_prescreen import DeepPrescreenContext
from app.schemas.discovery_strategy import DiscoveryStrategyRequest
from app.schemas.llm_qualification import QualificationContext
from app.services.adversarial_review import build_adversarial_prompt
from app.services.deep_prescreen import build_prescreen_prompt
from app.services.discovery_strategy import build_discovery_strategy_prompt
from app.services.llm_providers.base import (
    LLMProvider,
    LLMProviderError,
    LLMProviderErrorCode,
    LLMProviderResponse,
)
from app.services.llm_qualification import build_prompt

AnyLLMContext = QualificationContext | AdversarialContext | DiscoveryStrategyRequest | DeepPrescreenContext

QUALIFICATION_RESPONSE_SCHEMA: dict = {
    "name": "qualification_output",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "decision": {"type": "string", "enum": ["GOOD_FIT", "WEAK_FIT", "NOT_FIT", "HOLD"]},
            "confidence": {"type": "number", "minimum": 0, "maximum": 100},
            "reason_codes": {"type": "array", "items": {"type": "string"}},
            "summary": {"type": "string", "minLength": 1, "maxLength": 2000},
            "supporting_evidence_ids": {"type": "array", "items": {"type": "string"}},
            "risk_evidence_ids": {"type": "array", "items": {"type": "string"}},
            "missing_evidence": {"type": "array", "items": {"type": "string"}},
            "commercial_fit_explanation": {"type": "string", "maxLength": 2000},
            "hard_rule_acknowledgement": {"type": "string", "maxLength": 1000},
            "uncertainties": {"type": "array", "items": {"type": "string"}},
        },
        "required": [
            "decision",
            "confidence",
            "reason_codes",
            "summary",
            "supporting_evidence_ids",
            "risk_evidence_ids",
            "missing_evidence",
            "commercial_fit_explanation",
            "hard_rule_acknowledgement",
            "uncertainties",
        ],
    },
}
"""Deliberately omits QualificationDecision.REJECT from the enum (unlike
RawQualificationOutput's pydantic-level validator, which accepts REJECT at
parse time and then rejects it) — REJECT is a backend-only outcome for a
hard FAIL that the LLM should never even see as a legal option to choose."""

ADVERSARIAL_RESPONSE_SCHEMA: dict = {
    "name": "adversarial_output",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "adversarial_result": {"type": "string", "enum": ["SURVIVES", "WEAKENED", "DISPROVED", "HOLD"]},
            "confidence": {"type": "number", "minimum": 0, "maximum": 100},
            "contradictions": {"type": "array", "items": {"type": "string"}},
            "risk_codes": {"type": "array", "items": {"type": "string"}},
            "supporting_evidence_ids": {"type": "array", "items": {"type": "string"}},
            "contradicting_evidence_ids": {"type": "array", "items": {"type": "string"}},
            "unsupported_claims": {"type": "array", "items": {"type": "string"}},
            "missing_evidence": {"type": "array", "items": {"type": "string"}},
            "reasoning_summary": {"type": "string", "minLength": 1, "maxLength": 2000},
            "recommendation": {"type": "string", "maxLength": 1000},
        },
        "required": [
            "adversarial_result",
            "confidence",
            "contradictions",
            "risk_codes",
            "supporting_evidence_ids",
            "contradicting_evidence_ids",
            "unsupported_claims",
            "missing_evidence",
            "reasoning_summary",
            "recommendation",
        ],
    },
}
"""Deliberately omits AdversarialResult.NOT_EXECUTED — backend-only for a
hard FAIL/HOLD, never a legal LLM choice. Phase 9 does not otherwise touch
Phase 17: this schema exists only so registering OpenAIProvider as the
shared get_llm_provider() (see default_registry.py's own docstring on
reusing one instance for both call shapes) does not silently break
adversarial review, which already depends on the same provider today."""


DISCOVERY_STRATEGY_RESPONSE_SCHEMA: dict = {
    "name": "discovery_strategy_output",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "industry_terms": {"type": "array", "items": {"type": "string"}},
            "combination_industry_terms": {"type": "array", "items": {"type": "string"}},
            "company_type_terms": {"type": "array", "items": {"type": "string"}},
            "exclusion_terms": {"type": "array", "items": {"type": "string"}},
            "geography_notes": {"type": "array", "items": {"type": "string"}},
            "unsupported_intent": {"type": "array", "items": {"type": "string"}},
            "confidence": {"type": "number", "minimum": 0, "maximum": 100},
            "reasoning": {"type": "string", "minLength": 1, "maxLength": 1000},
        },
        "required": [
            "industry_terms",
            "combination_industry_terms",
            "company_type_terms",
            "exclusion_terms",
            "geography_notes",
            "unsupported_intent",
            "confidence",
            "reasoning",
        ],
    },
}
"""Deliberately has NO field shaped like a resolved Explorium taxonomy
value (no `linkedin_category`, no `naics_category`, no enum of "valid"
categories) — every array here is free-text candidate terms only, the
exact same shape CompanyDiscoveryQuery.industries/company_types already
accept. There is no static taxonomy list anywhere in this codebase to
constrain the schema against in the first place (Explorium's categories
are resolved live, per-term, via _lookup_category's autocomplete call) —
so the schema enforces "propose terms" as the only legal shape, never
"declare a category," structurally rather than by prompt wording alone."""


DEEP_PRESCREEN_RESPONSE_SCHEMA: dict = {
    "name": "deep_prescreen_output",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "verdict": {"type": "string", "enum": ["RELEVANT", "NOT_RELEVANT"]},
            "reason": {"type": "string", "minLength": 1, "maxLength": 500},
            "confidence": {"type": "number", "minimum": 0, "maximum": 100},
        },
        "required": ["verdict", "reason", "confidence"],
    },
}
"""Deep discovery mode's relevance pre-screen (see app/schemas/
deep_prescreen.py). `verdict`'s enum is deliberately RELEVANT/NOT_RELEVANT
only — never PASS/FAIL/HOLD or GOOD_FIT/WEAK_FIT/NOT_FIT, so this schema
structurally cannot be confused with QUALIFICATION_RESPONSE_SCHEMA even at
the raw-JSON level, matching this schema's own module-level "vocabulary
separation" discipline."""


def _response_format_for(context: AnyLLMContext) -> dict:
    if isinstance(context, AdversarialContext):
        schema = ADVERSARIAL_RESPONSE_SCHEMA
    elif isinstance(context, DiscoveryStrategyRequest):
        schema = DISCOVERY_STRATEGY_RESPONSE_SCHEMA
    elif isinstance(context, DeepPrescreenContext):
        schema = DEEP_PRESCREEN_RESPONSE_SCHEMA
    else:
        schema = QUALIFICATION_RESPONSE_SCHEMA
    return {"type": "json_schema", "json_schema": schema}


class OpenAIProvider(LLMProvider):
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        model_id: str = "gpt-5-nano",
        provider_id: str = "openai-llm-v1",
        timeout_seconds: float = 30.0,
    ) -> None:
        super().__init__(provider_id=provider_id, model_id=model_id)
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds

    def _prompt_for(self, context: AnyLLMContext) -> str:
        if isinstance(context, AdversarialContext):
            return build_adversarial_prompt(context)
        if isinstance(context, DiscoveryStrategyRequest):
            return build_discovery_strategy_prompt(context)
        if isinstance(context, DeepPrescreenContext):
            return build_prescreen_prompt(context)
        return build_prompt(context)

    def _call(self, context: AnyLLMContext) -> LLMProviderResponse:
        prompt = self._prompt_for(context)

        try:
            response = httpx.post(
                f"{self._base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"},
                json={
                    "model": self.model_id,
                    "messages": [{"role": "user", "content": prompt}],
                    "response_format": _response_format_for(context),
                },
                timeout=self._timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            return LLMProviderResponse(
                provider_id=self.provider_id,
                model_id=self.model_id,
                success=False,
                error=LLMProviderError(code=LLMProviderErrorCode.TIMEOUT, message=str(exc), retryable=True),
            )
        except httpx.HTTPError as exc:
            return LLMProviderResponse(
                provider_id=self.provider_id,
                model_id=self.model_id,
                success=False,
                error=LLMProviderError(code=LLMProviderErrorCode.PROVIDER_ERROR, message=str(exc), retryable=True),
            )

        if response.status_code == 429:
            return LLMProviderResponse(
                provider_id=self.provider_id,
                model_id=self.model_id,
                success=False,
                error=LLMProviderError(code=LLMProviderErrorCode.RATE_LIMITED, message="OpenAI rate limit exceeded", retryable=True),
            )
        if response.status_code != 200:
            return LLMProviderResponse(
                provider_id=self.provider_id,
                model_id=self.model_id,
                success=False,
                error=LLMProviderError(
                    code=LLMProviderErrorCode.PROVIDER_ERROR,
                    message=f"OpenAI returned HTTP {response.status_code}: {response.text[:500]}",
                    retryable=response.status_code >= 500,
                ),
            )

        try:
            body = response.json()
            raw_text = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, ValueError, json.JSONDecodeError) as exc:
            return LLMProviderResponse(
                provider_id=self.provider_id,
                model_id=self.model_id,
                success=False,
                error=LLMProviderError(
                    code=LLMProviderErrorCode.PROVIDER_ERROR,
                    message=f"could not read OpenAI response shape: {exc}",
                    retryable=False,
                ),
            )

        if raw_text is None or not raw_text.strip():
            return LLMProviderResponse(provider_id=self.provider_id, model_id=self.model_id, success=True, raw_text="")

        return LLMProviderResponse(provider_id=self.provider_id, model_id=self.model_id, success=True, raw_text=raw_text)
