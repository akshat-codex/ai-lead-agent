"""Phase 5 — provider registry.

A simple in-memory lookup: provider_id -> adapter, and capability -> the
adapters that support it. No routing, scoring, or provider selection
intelligence here — that is Phase 27 (Provider Reliability Router). This
registry only answers "what do we have" and "swap this one out."
"""
from __future__ import annotations

from app.providers.base import ProviderAdapter
from app.providers.contracts import ProviderCapability


class ProviderNotFoundError(KeyError):
    """Raised by get() when no provider is registered under that id."""


class ProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, ProviderAdapter] = {}

    def register(self, adapter: ProviderAdapter) -> None:
        """Registers a new provider. Raises if the id is already taken —
        use replace() when swapping a provider deliberately."""
        if adapter.provider_id in self._providers:
            raise ValueError(f"provider '{adapter.provider_id}' is already registered")
        self._providers[adapter.provider_id] = adapter

    def replace(self, adapter: ProviderAdapter) -> None:
        """Registers a provider, replacing any existing one with the same id.

        This is what lets a real provider swap in for a mock (or one vendor
        swap for another) without any change to code that only ever calls
        the registry by provider_id or capability.
        """
        self._providers[adapter.provider_id] = adapter

    def unregister(self, provider_id: str) -> None:
        self._providers.pop(provider_id, None)

    def get(self, provider_id: str) -> ProviderAdapter:
        try:
            return self._providers[provider_id]
        except KeyError:
            raise ProviderNotFoundError(provider_id) from None

    def find_by_capability(self, capability: ProviderCapability) -> tuple[ProviderAdapter, ...]:
        """Never raises for an unsupported/unmatched capability — an empty
        tuple is the clean, expected result when nothing matches."""
        return tuple(p for p in self._providers.values() if p.supports(capability))

    def list_providers(self) -> tuple[ProviderAdapter, ...]:
        return tuple(self._providers.values())
