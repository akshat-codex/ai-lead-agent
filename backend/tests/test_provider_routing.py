from datetime import datetime, timezone

from app.providers.base import ProviderAdapter
from app.providers.contracts import ProviderCapability, ProviderRequest, ProviderResponse
from app.schemas.optimization import ExpectedEffect, OptimizationRecommendationType
from app.schemas.optimization_application import EffectiveAdjustment, EffectiveConfiguration
from app.schemas.provider_routing import RoutingReasonCode
from app.services.provider_routing import ordered_providers, route_providers

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


class _FakeProvider(ProviderAdapter):
    def __init__(self, provider_id: str):
        super().__init__(provider_id=provider_id, provider_name=provider_id, capabilities={ProviderCapability.COMPANY_DISCOVERY})

    def execute(self, request: ProviderRequest) -> ProviderResponse:
        return ProviderResponse(provider_id=self.provider_id, capability=request.capability, success=True)


def _default_config(icp_id="icp-1", icp_version=1) -> EffectiveConfiguration:
    return EffectiveConfiguration(
        icp_id=icp_id, icp_version=icp_version, is_default=True,
        ranking_adjustments=(), soft_preference_hints=(), provider_priority_hints=(), verification_hints=(),
        generated_at=NOW,
    )


def _adjustment(signal_name, effect, application_id="app-1") -> EffectiveAdjustment:
    return EffectiveAdjustment(
        recommendation_type=OptimizationRecommendationType.PROVIDER_PRIORITY_HINT,
        signal_name=signal_name, reason_code=None, magnitude_hint=1.0, expected_effect=effect,
        application_id=application_id, applied_at=NOW,
    )


def _active_config(provider_priority_hints=(), soft_preference_hints=()) -> EffectiveConfiguration:
    return EffectiveConfiguration(
        icp_id="icp-1", icp_version=1, is_default=False,
        ranking_adjustments=(), soft_preference_hints=soft_preference_hints,
        provider_priority_hints=provider_priority_hints, verification_hints=(),
        generated_at=NOW,
    )


# --- default routing: no active optimization -----------------------------


def test_default_routing_preserves_registry_order():
    providers = (_FakeProvider("p1"), _FakeProvider("p2"), _FakeProvider("p3"))
    result = route_providers("icp-1", 1, ProviderCapability.COMPANY_DISCOVERY, providers, _default_config(), {}, NOW)
    assert result.is_default is True
    assert result.ordered_provider_ids == ("p1", "p2", "p3")
    assert all(e.reason_codes == (RoutingReasonCode.DEFAULT_ORDER,) for e in result.entries)


def test_default_routing_with_no_candidates():
    result = route_providers("icp-1", 1, ProviderCapability.COMPANY_DISCOVERY, (), _default_config(), {}, NOW)
    assert result.is_default is True
    assert result.ordered_provider_ids == ()


def test_empty_candidates_with_active_config_still_returns_default():
    result = route_providers("icp-1", 1, ProviderCapability.COMPANY_DISCOVERY, (), _active_config(), {}, NOW)
    assert result.is_default is True
    assert result.ordered_provider_ids == ()


# --- approved optimization reorders providers -----------------------------


def test_active_optimization_prioritizes_linked_provider():
    providers = (_FakeProvider("p1"), _FakeProvider("p2"), _FakeProvider("p3"))
    config = _active_config(provider_priority_hints=(_adjustment("commercial_signal:META_ADVERTISING", ExpectedEffect.INCREASE_PROVIDER_USAGE),))
    signal_map = {"p3": frozenset({"commercial_signal:META_ADVERTISING"})}
    result = route_providers("icp-1", 1, ProviderCapability.COMPANY_DISCOVERY, providers, config, signal_map, NOW)
    assert result.is_default is False
    assert result.ordered_provider_ids[0] == "p3"


def test_active_optimization_deprioritizes_linked_provider():
    providers = (_FakeProvider("p1"), _FakeProvider("p2"))
    config = _active_config(provider_priority_hints=(_adjustment("commercial_signal:AGENCY_ONLY", ExpectedEffect.DECREASE_PROVIDER_USAGE),))
    signal_map = {"p1": frozenset({"commercial_signal:AGENCY_ONLY"})}
    result = route_providers("icp-1", 1, ProviderCapability.COMPANY_DISCOVERY, providers, config, signal_map, NOW)
    assert result.ordered_provider_ids == ("p2", "p1")


def test_no_signal_linkage_leaves_provider_unmoved():
    providers = (_FakeProvider("p1"), _FakeProvider("p2"))
    config = _active_config(provider_priority_hints=(_adjustment("commercial_signal:META_ADVERTISING", ExpectedEffect.INCREASE_PROVIDER_USAGE),))
    result = route_providers("icp-1", 1, ProviderCapability.COMPANY_DISCOVERY, providers, config, {}, NOW)
    # no provider has the signal linkage -> nothing moves, order preserved
    assert result.ordered_provider_ids == ("p1", "p2")
    assert result.is_default is True
    assert all(e.reason_codes == (RoutingReasonCode.NO_SIGNAL_LINKAGE,) for e in result.entries)


def test_all_candidates_present_exactly_once_after_routing():
    providers = (_FakeProvider("p1"), _FakeProvider("p2"), _FakeProvider("p3"))
    config = _active_config(provider_priority_hints=(_adjustment("commercial_signal:X", ExpectedEffect.INCREASE_PROVIDER_USAGE),))
    signal_map = {"p2": frozenset({"commercial_signal:X"})}
    result = route_providers("icp-1", 1, ProviderCapability.COMPANY_DISCOVERY, providers, config, signal_map, NOW)
    assert set(result.ordered_provider_ids) == {"p1", "p2", "p3"}
    assert len(result.ordered_provider_ids) == 3  # no duplication, no dropped provider


# --- ICP isolation ------------------------------------------------------


def test_routing_result_carries_the_requested_icp_scope():
    providers = (_FakeProvider("p1"),)
    result = route_providers("icp-a", 3, ProviderCapability.COMPANY_DISCOVERY, providers, _default_config("icp-a", 3), {}, NOW)
    assert result.icp_id == "icp-a"
    assert result.icp_version == 3


# --- provider failure / fallback: routing never removes a provider --------


def test_deprioritized_provider_is_still_included_never_removed():
    providers = (_FakeProvider("p1"), _FakeProvider("p2"))
    config = _active_config(provider_priority_hints=(_adjustment("commercial_signal:BAD", ExpectedEffect.DECREASE_PROVIDER_USAGE),))
    signal_map = {"p1": frozenset({"commercial_signal:BAD"})}
    result = route_providers("icp-1", 1, ProviderCapability.COMPANY_DISCOVERY, providers, config, signal_map, NOW)
    assert "p1" in result.ordered_provider_ids  # deprioritized, never dropped


def test_ordered_providers_maps_ids_back_to_real_adapters():
    p1, p2 = _FakeProvider("p1"), _FakeProvider("p2")
    config = _active_config(provider_priority_hints=(_adjustment("commercial_signal:X", ExpectedEffect.INCREASE_PROVIDER_USAGE),))
    signal_map = {"p2": frozenset({"commercial_signal:X"})}
    result = route_providers("icp-1", 1, ProviderCapability.COMPANY_DISCOVERY, (p1, p2), config, signal_map, NOW)
    real_order = ordered_providers(result, (p1, p2))
    assert real_order[0] is p2
    assert real_order[1] is p1


# --- determinism -------------------------------------------------------


def test_routing_is_deterministic():
    providers = (_FakeProvider("p1"), _FakeProvider("p2"), _FakeProvider("p3"))
    config = _active_config(provider_priority_hints=(_adjustment("commercial_signal:X", ExpectedEffect.INCREASE_PROVIDER_USAGE),))
    signal_map = {"p3": frozenset({"commercial_signal:X"})}
    first = route_providers("icp-1", 1, ProviderCapability.COMPANY_DISCOVERY, providers, config, signal_map, NOW)
    second = route_providers("icp-1", 1, ProviderCapability.COMPANY_DISCOVERY, providers, config, signal_map, NOW)
    assert first.ordered_provider_ids == second.ordered_provider_ids
    assert first.entries == second.entries


def test_stable_sort_preserves_relative_order_among_equal_weight_providers():
    providers = (_FakeProvider("p1"), _FakeProvider("p2"), _FakeProvider("p3"))
    config = _active_config(provider_priority_hints=(_adjustment("commercial_signal:NONE_MATCH", ExpectedEffect.INCREASE_PROVIDER_USAGE),))
    result = route_providers("icp-1", 1, ProviderCapability.COMPANY_DISCOVERY, providers, config, {}, NOW)
    assert result.ordered_provider_ids == ("p1", "p2", "p3")  # no matches -> original order preserved exactly


# --- hard-rule safety: structural, no hard-rule concept exists here --------


def test_module_never_imports_hard_rule_scoring_or_identity_logic():
    import ast
    import inspect

    import app.services.provider_routing as module

    tree = ast.parse(inspect.getsource(module))
    imported_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported_names.update(alias.name for alias in node.names)

    forbidden = {
        "evaluate_hard_rules", "validate_against_icp", "score_lead", "qualify_lead",
        "run_adversarial_review", "decide_review", "resolve_candidate",
    }
    assert not (forbidden & imported_names)


def test_module_never_imports_a_database_session_or_provider_registry():
    import ast
    import inspect

    import app.services.provider_routing as module

    tree = ast.parse(inspect.getsource(module))
    imported_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported_names.update(alias.name for alias in node.names)

    # This module only ever receives already-instantiated ProviderAdapter
    # objects and an already-loaded EffectiveConfiguration/signal map from
    # its caller — it never imports the registry (to look providers up
    # itself) or a DB session (to load anything itself).
    forbidden = {"Session", "get_db", "ProviderRegistry", "get_provider_registry"}
    assert not (forbidden & imported_names)
