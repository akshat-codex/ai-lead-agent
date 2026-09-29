"""Phase 16 — LLM provider abstraction. Widened in Phase 10 (additive,
type-hint only — no behavioral change) to also cover
DiscoveryStrategyRequest (app/schemas/discovery_strategy.py): `_call` was
always given whatever pydantic context object the caller built, with no
runtime type enforcement, so this only makes the existing contract honest
about the third shape it now also accepts.

Deliberately separate from app/providers/ (Phase 5): that abstraction is
shaped around capability-scoped data lookups returning NormalizedRecord
lists (company/people discovery, enrichment, search). An LLM call here is a
different shape entirely — one structured request in, one structured JSON
judgment out — so it gets its own small interface instead of being forced
into ProviderCapability/NormalizedRecord.

Every current mock implementation still only answers QualificationContext/
AdversarialContext (see mock.py) — this module fixes the contract so a real
vendor SDK (OpenAIProvider) or a future mock can be dropped in without any
change to the callers, which only ever call `.qualify()`.

Widened again for deep discovery mode's DeepPrescreenContext (see
app/schemas/deep_prescreen.py) — additive, type-hint only, no behavioral
change to this file, the exact same "widen the union, nothing else moves"
pattern DiscoveryStrategyRequest itself was added by.
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod

from pydantic import BaseModel, ConfigDict

from app.schemas.adversarial_review import AdversarialContext
from app.schemas.deep_prescreen import DeepPrescreenContext
from app.schemas.discovery_strategy import DiscoveryStrategyRequest
from app.schemas.llm_qualification import QualificationContext

AnyLLMContext = QualificationContext | AdversarialContext | DiscoveryStrategyRequest | DeepPrescreenContext


class LLMProviderErrorCode:
    TIMEOUT = "TIMEOUT"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    EMPTY_RESPONSE = "EMPTY_RESPONSE"
    RATE_LIMITED = "RATE_LIMITED"


class LLMProviderError(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: str
    message: str
    retryable: bool = False


class LLMProviderResponse(BaseModel):
    """The raw result of one provider call — `raw_text` is whatever the
    provider returned, still unparsed and unvalidated. Parsing into
    RawQualificationOutput and validating evidence ids both happen one
    layer up, in app/services/llm_qualification.py, so every provider
    (mock or real) is held to the exact same validation regardless of how
    it produced its text."""

    model_config = ConfigDict(frozen=True)

    provider_id: str
    model_id: str
    success: bool
    raw_text: str | None = None
    error: LLMProviderError | None = None
    latency_ms: float | None = None


class LLMProvider(ABC):
    """LLMProvider -> qualify(context) -> LLMProviderResponse.

    Mirrors app/providers/base.py's ProviderAdapter.run() pattern: callers
    use only `qualify()`, never `_call()` directly, and a misbehaving
    implementation can never raise into or crash the caller — any
    unexpected exception becomes a clean, retryable-false failure response.
    """

    def __init__(self, provider_id: str, model_id: str) -> None:
        self.provider_id = provider_id
        self.model_id = model_id

    @abstractmethod
    def _call(self, context: AnyLLMContext) -> LLMProviderResponse:
        """Performs the actual (mock or real) call and returns raw text.
        Implementations may raise; qualify() converts any exception into a
        clean failure response."""
        raise NotImplementedError

    def qualify(self, context: AnyLLMContext) -> LLMProviderResponse:
        started = time.monotonic()
        try:
            response = self._call(context)
        except Exception as exc:  # a single misbehaving provider must never propagate
            elapsed_ms = (time.monotonic() - started) * 1000
            return LLMProviderResponse(
                provider_id=self.provider_id,
                model_id=self.model_id,
                success=False,
                latency_ms=elapsed_ms,
                error=LLMProviderError(code=LLMProviderErrorCode.PROVIDER_ERROR, message=str(exc), retryable=False),
            )
        elapsed_ms = (time.monotonic() - started) * 1000
        return response.model_copy(update={"latency_ms": elapsed_ms})
