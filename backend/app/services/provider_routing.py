"""Phase 27 — provider routing and adaptive search.

Pure and DB-free, mirroring every other *_engine/service module: given an
already-ordered list of candidate ProviderAdapters (exactly what Phase 5's
own `ProviderRegistry.find_by_capability()` returns — never re-derived or
re-filtered here), an already-loaded Phase 26 EffectiveConfiguration, and
an already-loaded provider->signal linkage map, returns a deterministic
ProviderRoutingResult with no side effects, no provider call, and no
registry mutation. The caller (app/api/provider_routing.py and the
discovery/enrichment services it wires in) owns loading the registry
candidates and the effective configuration.

DEFAULT BEHAVIOR IS EXACTLY UNCHANGED WHEN NOTHING IS ACTIVE: if
`effective_config.is_default` is True (Phase 26's own flag — no
optimization has ever been applied for this ICP/version, or everything
that was has since been rolled back), `route_providers()` returns the
candidates in their EXACT original order, tagged DEFAULT_ORDER, and
`ProviderRoutingResult.is_default` is True. This is a structural
guarantee, not a coincidence of the scoring math below: the reordering
step is skipped entirely in that branch.

NEVER FABRICATED PROVIDER RELIABILITY: prioritization only ever moves a
provider earlier when `provider_signal_names` (built by the caller from
real Phase 14 CommercialSignalModel.provider_ids rows — i.e. "this
provider's own evidence is what produced this signal_type for at least
one company") contains a signal name an ACTIVE, approved Phase 26
provider_priority_hint or soft_preference_hint names. A provider with no
such historical linkage is never moved, regardless of how confident the
optimization is — there is simply no evidence connecting it to the
pattern, and confidence about the PATTERN is not evidence about THIS
PROVIDER's contribution to it.

HARD-RULE SAFETY: this module has no notion of a hard rule, a canonical
identity, an evidence record, or a score — it only reorders provider
objects it's handed. There is no code path here that could weaken Phase
3/12's authority, because nothing here ever produces a hard-rule-shaped
output at all.

STABLE SORT, NEVER PROVIDER REMOVAL: Python's `sorted()` is stable, so
providers with equal routing weight keep their original relative order
(itself a form of determinism preservation) — and every candidate handed
in is guaranteed to appear exactly once in the output. A provider that
later fails when actually called is Phase 5's own `run()` failure-capture
job, entirely unaffected by routing order; this module only decides WHICH
ORDER TO TRY THEM IN, never whether a failed one gets removed or retried.
"""
from __future__ import annotations

from app.providers.base import ProviderAdapter
from app.providers.contracts import ProviderCapability
from app.schemas.optimization_application import EffectiveConfiguration
from app.schemas.provider_routing import ProviderRoutingEntry, ProviderRoutingResult, RoutingReasonCode


def _prioritized_signal_names(effective_config: EffectiveConfiguration) -> frozenset[str]:
    """Only INCREASE-direction hints ever move a provider earlier — a
    DECREASE-direction hint is used only to deprioritize (see
    _deprioritized_signal_names), never to promote."""
    names = set()
    for adjustment in (*effective_config.provider_priority_hints, *effective_config.soft_preference_hints):
        if adjustment.expected_effect.value.startswith("INCREASE") and adjustment.signal_name is not None:
            names.add(adjustment.signal_name)
    return frozenset(names)


def _deprioritized_signal_names(effective_config: EffectiveConfiguration) -> frozenset[str]:
    names = set()
    for adjustment in (*effective_config.provider_priority_hints, *effective_config.soft_preference_hints):
        if adjustment.expected_effect.value.startswith("DECREASE") and adjustment.signal_name is not None:
            names.add(adjustment.signal_name)
    return frozenset(names)


def _applied_optimization_ids(effective_config: EffectiveConfiguration) -> tuple[str, ...]:
    ids = [
        a.application_id
        for a in (
            *effective_config.provider_priority_hints,
            *effective_config.soft_preference_hints,
            *effective_config.ranking_adjustments,
            *effective_config.verification_hints,
        )
    ]
    return tuple(dict.fromkeys(ids))


def route_providers(
    icp_id: str,
    icp_version: int,
    capability: ProviderCapability,
    candidates: tuple[ProviderAdapter, ...],
    effective_config: EffectiveConfiguration,
    provider_signal_names: dict[str, frozenset[str]],
    generated_at,
) -> ProviderRoutingResult:
    """`candidates` must already be exactly what
    ProviderRegistry.find_by_capability(capability) returned — this
    function never queries a registry itself. `provider_signal_names` maps
    provider_id -> the signal names real Phase 14 evidence has ever
    attributed to that provider (empty dict/missing key is a normal,
    honest "no known linkage," never an error).
    """
    if effective_config.is_default or not candidates:
        entries = tuple(
            ProviderRoutingEntry(
                provider_id=p.provider_id,
                original_index=i,
                routed_index=i,
                reason_codes=(RoutingReasonCode.DEFAULT_ORDER,),
                matched_signal_names=(),
                explanation="No active optimization for this ICP/version; registry order preserved unchanged.",
            )
            for i, p in enumerate(candidates)
        )
        return ProviderRoutingResult(
            icp_id=icp_id,
            icp_version=icp_version,
            capability=capability,
            is_default=True,
            ordered_provider_ids=tuple(p.provider_id for p in candidates),
            entries=entries,
            applied_optimization_ids=(),
            generated_at=generated_at,
        )

    prioritized_signals = _prioritized_signal_names(effective_config)
    deprioritized_signals = _deprioritized_signal_names(effective_config)

    scored: list[tuple[int, int, ProviderAdapter, tuple[RoutingReasonCode, ...], tuple[str, ...], str]] = []
    for original_index, provider in enumerate(candidates):
        provider_signals = provider_signal_names.get(provider.provider_id, frozenset())
        matched_priority = sorted(provider_signals & prioritized_signals)
        matched_deprioritized = sorted(provider_signals & deprioritized_signals)

        if matched_priority:
            weight = -1  # sorts earlier (stable sort keeps registry order among ties)
            reasons = (RoutingReasonCode.PRIORITIZED_BY_OPTIMIZATION,)
            explanation = (
                f"Provider has historical evidence linkage to prioritized signal(s) "
                f"{', '.join(matched_priority)} from an active, approved optimization."
            )
            matched = tuple(matched_priority)
        elif matched_deprioritized:
            weight = 1  # sorts later
            reasons = (RoutingReasonCode.DEPRIORITIZED_BY_OPTIMIZATION,)
            explanation = (
                f"Provider has historical evidence linkage to deprioritized signal(s) "
                f"{', '.join(matched_deprioritized)} from an active, approved optimization."
            )
            matched = tuple(matched_deprioritized)
        else:
            weight = 0
            reasons = (RoutingReasonCode.NO_SIGNAL_LINKAGE,)
            explanation = "An optimization is active, but this provider has no historical evidence linkage to any of its signals; order unchanged for this provider."
            matched = ()

        scored.append((weight, original_index, provider, reasons, matched, explanation))

    # Stable sort on (weight, original_index): equal-weight providers keep
    # their exact original relative order, so a routing pass with zero
    # matching providers reproduces the original order byte-for-byte.
    scored.sort(key=lambda row: (row[0], row[1]))

    entries = []
    ordered_ids = []
    any_reordered = False
    for routed_index, (weight, original_index, provider, reasons, matched, explanation) in enumerate(scored):
        if routed_index != original_index:
            any_reordered = True
        entries.append(
            ProviderRoutingEntry(
                provider_id=provider.provider_id,
                original_index=original_index,
                routed_index=routed_index,
                reason_codes=reasons,
                matched_signal_names=matched,
                explanation=explanation,
            )
        )
        ordered_ids.append(provider.provider_id)

    return ProviderRoutingResult(
        icp_id=icp_id,
        icp_version=icp_version,
        capability=capability,
        is_default=not any_reordered,
        ordered_provider_ids=tuple(ordered_ids),
        entries=tuple(entries),
        applied_optimization_ids=_applied_optimization_ids(effective_config) if any_reordered else (),
        generated_at=generated_at,
    )


def ordered_providers(routing_result: ProviderRoutingResult, candidates: tuple[ProviderAdapter, ...]) -> tuple[ProviderAdapter, ...]:
    """Applies a ProviderRoutingResult's order to the actual adapter
    objects — used by the discovery/enrichment call sites that need real
    ProviderAdapter instances to call `.run()` on, not just ids."""
    by_id = {p.provider_id: p for p in candidates}
    return tuple(by_id[pid] for pid in routing_result.ordered_provider_ids if pid in by_id)
