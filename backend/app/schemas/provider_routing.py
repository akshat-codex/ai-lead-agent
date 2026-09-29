"""Phase 27 — Provider Routing & Adaptive Search contracts.

Routing REORDERS the providers Phase 5's own
ProviderRegistry.find_by_capability() already returned for a capability —
it never adds a provider that isn't registered, never removes one that
is, and never changes what capability it's queried for. This is the
concrete meaning of "provider selection must remain capability-based" and
"failed/unavailable providers must degrade safely to existing fallback
behavior": every provider Phase 5 would have called is still called, in
the same all-of-them-run loop discovery/enrichment already use — only the
ORDER in which they appear (and therefore, for a caller that stops early
or that a real vendor call would bill by request count, which one is
tried first) can change, and only when there is a genuinely active,
approved optimization to justify it.

NEVER FABRICATED RELIABILITY: a provider is only ever prioritized because
of a real Phase 14 CommercialSignalModel.provider_ids linkage — i.e. that
specific provider is the one whose evidence actually produced the signal
an approved Phase 26 optimization names. No numeric reliability score is
invented; see app/services/provider_routing.py's own docstring for the
exact, auditable derivation.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict

from app.providers.contracts import ProviderCapability


class RoutingReasonCode(str, Enum):
    DEFAULT_ORDER = "DEFAULT_ORDER"  # no active optimization - registry order preserved unchanged
    PRIORITIZED_BY_OPTIMIZATION = "PRIORITIZED_BY_OPTIMIZATION"
    DEPRIORITIZED_BY_OPTIMIZATION = "DEPRIORITIZED_BY_OPTIMIZATION"
    NO_SIGNAL_LINKAGE = "NO_SIGNAL_LINKAGE"  # an active optimization exists but this provider has no historical link to it


class ProviderRoutingEntry(BaseModel):
    """One provider's position and rationale in a routing decision. Every
    provider Phase 5's registry returned for this capability appears here
    exactly once — routing never drops a provider from consideration."""

    model_config = ConfigDict(frozen=True)

    provider_id: str
    original_index: int  # position in the registry's own find_by_capability() order
    routed_index: int  # position after routing - equals original_index under default behavior
    reason_codes: tuple[RoutingReasonCode, ...]
    matched_signal_names: tuple[str, ...]
    explanation: str


class ProviderRoutingResult(BaseModel):
    """The full, in-memory result of one routing decision for one
    capability. `is_default` is True whenever the routed order is
    byte-identical to the original registry order — including whenever no
    optimization is active at all, which is the required default-
    unchanged-behavior guarantee."""

    model_config = ConfigDict(frozen=True)

    icp_id: str
    icp_version: int
    capability: ProviderCapability
    is_default: bool
    ordered_provider_ids: tuple[str, ...]
    entries: tuple[ProviderRoutingEntry, ...]
    applied_optimization_ids: tuple[str, ...]
    generated_at: datetime
