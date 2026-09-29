"""Phase 5 — the ProviderAdapter base class.

Every current (mock) and future (real) provider subclasses this. Callers —
the registry, and eventually the Phase 6+ pipeline — only ever call
`run()`, never `execute()` directly and never a vendor SDK. That single
entry point is what makes swapping one provider for another invisible to
the rest of the system.
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Iterable

from app.providers.contracts import (
    ProviderCapability,
    ProviderError,
    ProviderReliabilityProfile,
    ProviderRequest,
    ProviderResponse,
)


class ProviderErrorCode:
    """Well-known error codes this module itself produces.

    Not a closed set — a real provider adapter (Phase 6+) may introduce its
    own vendor-specific codes on ProviderError.code; this class only
    documents the ones the abstraction layer itself is responsible for.
    """

    UNSUPPORTED_CAPABILITY = "UNSUPPORTED_CAPABILITY"
    PROVIDER_ERROR = "PROVIDER_ERROR"


class ProviderAdapter(ABC):
    def __init__(
        self,
        provider_id: str,
        provider_name: str,
        capabilities: Iterable[ProviderCapability],
        reliability_profile: ProviderReliabilityProfile | None = None,
    ) -> None:
        self.provider_id = provider_id
        self.provider_name = provider_name
        self.capabilities = frozenset(capabilities)
        self.reliability_profile = reliability_profile or ProviderReliabilityProfile()

    def supports(self, capability: ProviderCapability) -> bool:
        return capability in self.capabilities

    @abstractmethod
    def execute(self, request: ProviderRequest) -> ProviderResponse:
        """Performs the actual (mock or real) call.

        Implementations must map any vendor-specific field into a
        NormalizedRecord before returning — a vendor's own key names must
        never reach the caller. Prefer raising on unexpected internal
        failure; `run()` converts it into a clean ProviderResponse.
        """
        raise NotImplementedError

    def run(self, request: ProviderRequest) -> ProviderResponse:
        """The only entry point callers should use.

        Rejects an unsupported capability before calling execute() at all,
        times the call, and converts any unexpected exception into a clean
        failure response — one misbehaving provider can never raise into,
        or crash, the caller.
        """
        if not self.supports(request.capability):
            return ProviderResponse(
                provider_id=self.provider_id,
                capability=request.capability,
                success=False,
                error=ProviderError(
                    code=ProviderErrorCode.UNSUPPORTED_CAPABILITY,
                    message=f"Provider '{self.provider_id}' does not support {request.capability.value}.",
                    retryable=False,
                ),
            )

        started = time.monotonic()
        try:
            response = self.execute(request)
        except Exception as exc:  # a single misbehaving provider must never propagate
            elapsed_ms = (time.monotonic() - started) * 1000
            return ProviderResponse(
                provider_id=self.provider_id,
                capability=request.capability,
                success=False,
                latency_ms=elapsed_ms,
                error=ProviderError(
                    code=ProviderErrorCode.PROVIDER_ERROR,
                    message=str(exc),
                    retryable=False,
                ),
            )

        elapsed_ms = (time.monotonic() - started) * 1000
        return response.model_copy(update={"latency_ms": elapsed_ms})
