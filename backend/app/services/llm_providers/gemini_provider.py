"""Phase 23 — Gemini provider, scoped to discovery-strategy interpretation
ONLY (app/services/discovery_strategy.py::interpret_icp).

Temporary A/B test provider: mirrors app/services/llm_providers/
openai_provider.py's OpenAIProvider exactly (same LLMProvider interface,
same anti-fabrication discipline — a strict JSON response schema so the
model cannot return a field the schema doesn't allow), but talks to
Google's Generative Language REST API (generateContent) instead of
OpenAI's Chat Completions endpoint.

Deliberately narrow: only implements the DiscoveryStrategyRequest branch
of AnyLLMContext. It is never registered as the shared get_llm_provider()
(app/services/llm_providers/default_registry.py, unchanged by this file)
— Phase 16 qualification and Phase 17 adversarial review continue to use
OpenAIProvider/the mock exactly as before. See
get_discovery_strategy_llm_provider() in default_registry.py for the one
place this class is ever selected, and how switching back to OpenAI for
discovery-strategy interpretation needs no code change, only removing
GEMINI_API_KEY (or setting DISCOVERY_STRATEGY_LLM_PROVIDER=openai) from
the environment.

The API key is supplied at construction (read once, from Settings, by
default_registry.py) and never read from the environment inside _call(),
never included in any LLMProviderResponse field, never logged.
"""
from __future__ import annotations

import logging

import httpx

from app.schemas.discovery_strategy import DiscoveryStrategyRequest
from app.services.discovery_strategy import build_discovery_strategy_prompt
from app.services.llm_providers.base import (
    AnyLLMContext,
    LLMProvider,
    LLMProviderError,
    LLMProviderErrorCode,
    LLMProviderResponse,
)

# Mirrors openai_provider.py's DISCOVERY_STRATEGY_RESPONSE_SCHEMA field-for
# -field, translated into Gemini's OpenAPI-subset responseSchema shape
# (Gemini's structured-output schema has no "additionalProperties" or
# "required"-as-a-sibling-list support the same way OpenAI's does — Gemini
# infers required-ness from the "required" array, which IS supported; there
# is no direct equivalent of "additionalProperties: false", so this relies
# on the same anti-fabrication discipline as the ICP-interpretation PROMPT
# already provides (never a schema field shaped like a resolved Explorium
# taxonomy value), not a Gemini-side hard block on extra fields the way
# OpenAI's strict mode provides). Deliberately has NO field shaped like a
# resolved Explorium taxonomy value, identical to the OpenAI schema this
# mirrors.
_DISCOVERY_STRATEGY_GEMINI_SCHEMA: dict = {
    "type": "OBJECT",
    "properties": {
        "industry_terms": {"type": "ARRAY", "items": {"type": "STRING"}},
        "company_type_terms": {"type": "ARRAY", "items": {"type": "STRING"}},
        "exclusion_terms": {"type": "ARRAY", "items": {"type": "STRING"}},
        "geography_notes": {"type": "ARRAY", "items": {"type": "STRING"}},
        "unsupported_intent": {"type": "ARRAY", "items": {"type": "STRING"}},
        "confidence": {"type": "NUMBER"},
        "reasoning": {"type": "STRING"},
    },
    "required": [
        "industry_terms",
        "company_type_terms",
        "exclusion_terms",
        "geography_notes",
        "unsupported_intent",
        "confidence",
        "reasoning",
    ],
}


logger = logging.getLogger(__name__)


class GeminiProvider(LLMProvider):
    """Scoped to DiscoveryStrategyRequest only — _call() raises for any
    other context shape rather than silently guessing a prompt/schema for
    it, since this class is never registered as the shared
    get_llm_provider() and has no reason to ever receive one."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://generativelanguage.googleapis.com/v1beta",
        model_id: str = "gemini-3.6-flash",
        provider_id: str = "gemini-llm-v1",
        timeout_seconds: float = 30.0,
    ) -> None:
        super().__init__(provider_id=provider_id, model_id=model_id)
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds

    def _call(self, context: AnyLLMContext) -> LLMProviderResponse:
        if not isinstance(context, DiscoveryStrategyRequest):
            return LLMProviderResponse(
                provider_id=self.provider_id,
                model_id=self.model_id,
                success=False,
                error=LLMProviderError(
                    code=LLMProviderErrorCode.PROVIDER_ERROR,
                    message="GeminiProvider is scoped to DiscoveryStrategyRequest only (Phase 23 A/B test scope).",
                    retryable=False,
                ),
            )

        prompt = build_discovery_strategy_prompt(context)

        try:
            response = httpx.post(
                f"{self._base_url}/models/{self.model_id}:generateContent",
                params={"key": self._api_key},
                headers={"Content-Type": "application/json"},
                json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {
                        "responseMimeType": "application/json",
                        "responseSchema": _DISCOVERY_STRATEGY_GEMINI_SCHEMA,
                    },
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
                error=LLMProviderError(code=LLMProviderErrorCode.RATE_LIMITED, message="Gemini rate limit exceeded", retryable=True),
            )
        if response.status_code != 200:
            return LLMProviderResponse(
                provider_id=self.provider_id,
                model_id=self.model_id,
                success=False,
                error=LLMProviderError(
                    code=LLMProviderErrorCode.PROVIDER_ERROR,
                    message=f"Gemini returned HTTP {response.status_code}: {response.text[:500]}",
                    retryable=response.status_code >= 500,
                ),
            )

        try:
            body = response.json()
            raw_text = body["candidates"][0]["content"]["parts"][0]["text"]
            # Phase 24 one-shot E2E diagnostic ONLY — observability, no
            # behavior change: does not touch raw_text, control flow, or
            # the returned LLMProviderResponse. usageMetadata is already
            # part of the SAME response body this call already received;
            # logging it makes zero additional network calls. Intended to
            # be removed after the one-shot live test this was added for.
            logger.info("gemini_usage_metadata=%s model_version=%s", body.get("usageMetadata"), body.get("modelVersion"))
        except (KeyError, IndexError, ValueError) as exc:
            return LLMProviderResponse(
                provider_id=self.provider_id,
                model_id=self.model_id,
                success=False,
                error=LLMProviderError(
                    code=LLMProviderErrorCode.PROVIDER_ERROR,
                    message=f"could not read Gemini response shape: {exc}",
                    retryable=False,
                ),
            )

        if raw_text is None or not raw_text.strip():
            return LLMProviderResponse(provider_id=self.provider_id, model_id=self.model_id, success=True, raw_text="")

        return LLMProviderResponse(provider_id=self.provider_id, model_id=self.model_id, success=True, raw_text=raw_text)
