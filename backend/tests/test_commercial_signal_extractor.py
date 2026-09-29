from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app.schemas.evidence import ConfidenceLevel, EntityType, EvidenceRecord, EvidenceStatus, SourceType
from app.services.commercial_signal_extractor import SIGNAL_DEFINITIONS, extract_commercial_signals


def _evidence(field: str, value, **overrides) -> EvidenceRecord:
    base = dict(
        id=str(uuid4()),
        entity_type=EntityType.COMPANY,
        entity_id="company-1",
        field=field,
        value=value,
        source_provider_id="provider-a",
        source_type=SourceType.PROVIDER,
        external_id="ext-1",
        retrieved_at=datetime.now(timezone.utc),
        confidence=ConfidenceLevel.UNKNOWN,
        created_at=datetime.now(timezone.utc),
    )
    base.update(overrides)
    return EvidenceRecord(**base)


def _find(results, signal_type):
    return next((r for r in results if r.signal_type == signal_type), None)


# --- each major signal category ---------------------------------------


def test_all_signal_categories_are_covered_by_the_definition_table():
    expected = {
        "DTC", "ECOMMERCE", "SHOPIFY", "META_ADVERTISING", "TIKTOK_ADVERTISING", "GOOGLE_ADVERTISING",
        "INFLUENCER_MARKETING", "CREATOR_MARKETING", "EMAIL_CRM", "SUBSCRIPTION", "REPEAT_PURCHASES",
        "PERFORMANCE_MARKETING", "GROWTH_MARKETING", "RETAIL_EXPANSION", "INTERNATIONAL_EXPANSION",
        "NEW_PRODUCT_LAUNCH", "FUNDING", "MARKETING_HIRING", "DISTRIBUTION_PARTNERSHIP", "CONSUMER_BRAND",
    }
    assert expected <= set(SIGNAL_DEFINITIONS.keys())


def test_each_signal_definition_is_individually_detectable():
    for signal_type, phrases in SIGNAL_DEFINITIONS.items():
        evidence = [_evidence("products_services", f"Description mentioning {phrases[0]} explicitly")]
        results = extract_commercial_signals("company-1", evidence)
        found = _find(results, signal_type)
        assert found is not None, f"{signal_type} was not detected from its own keyword phrase"
        assert found.status in (EvidenceStatus.SUPPORTED, EvidenceStatus.INSUFFICIENT)


# --- supported / insufficient / unknown --------------------------------


def test_single_source_is_insufficient_not_supported():
    evidence = [_evidence("products_services", "We run meta advertising campaigns")]
    results = extract_commercial_signals("company-1", evidence)
    signal = _find(results, "META_ADVERTISING")
    assert signal.status == EvidenceStatus.INSUFFICIENT
    assert signal.confidence == ConfidenceLevel.LOW


def test_two_corroborating_sources_are_supported():
    evidence = [
        _evidence("products_services", "We run meta advertising campaigns", source_provider_id="provider-a"),
        _evidence("business_model", "Growth driven by meta advertising", source_provider_id="provider-b"),
    ]
    results = extract_commercial_signals("company-1", evidence)
    signal = _find(results, "META_ADVERTISING")
    assert signal.status == EvidenceStatus.SUPPORTED
    assert signal.confidence == ConfidenceLevel.MEDIUM


def test_signal_never_mentioned_is_absent_from_results_not_listed_as_unknown():
    evidence = [_evidence("products_services", "We run meta advertising campaigns")]
    results = extract_commercial_signals("company-1", evidence)
    assert _find(results, "FUNDING") is None


def test_no_evidence_at_all_produces_no_signals():
    results = extract_commercial_signals("company-1", [])
    assert results == ()


# --- conflicting evidence -----------------------------------------------


def test_conflicting_subscription_evidence_is_marked_conflict():
    evidence = [
        _evidence("business_model", "Our subscription model drives revenue", source_provider_id="provider-a"),
        _evidence("business_model", "We have no subscription — one-time purchase only", source_provider_id="provider-b"),
    ]
    results = extract_commercial_signals("company-1", evidence)
    signal = _find(results, "SUBSCRIPTION")
    assert signal.status == EvidenceStatus.CONFLICT
    assert signal.confidence == ConfidenceLevel.UNKNOWN
    assert set(signal.evidence_ids) == {e.id for e in evidence}


def test_conflict_never_silently_chosen_as_absence():
    evidence = [
        _evidence("business_model", "Our subscription model drives revenue", source_provider_id="provider-a"),
        _evidence("business_model", "We have no subscription — one-time purchase only", source_provider_id="provider-b"),
    ]
    results = extract_commercial_signals("company-1", evidence)
    signal = _find(results, "SUBSCRIPTION")
    # value must not be populated as if a side had won
    assert signal.value is None


def test_both_conflicting_evidence_ids_preserved():
    evidence = [
        _evidence("business_model", "Our subscription model drives revenue", source_provider_id="provider-a"),
        _evidence("business_model", "We have no subscription — one-time purchase only", source_provider_id="provider-b"),
    ]
    results = extract_commercial_signals("company-1", evidence)
    signal = _find(results, "SUBSCRIPTION")
    assert len(signal.evidence_ids) == 2


# --- multiple signals for one company / multiple records for one signal ---


def test_multiple_distinct_signals_for_one_company():
    evidence = [
        _evidence("products_services", "We run meta advertising campaigns and a subscription model"),
        _evidence("business_model", "Direct-to-consumer skincare with a retail expansion planned"),
    ]
    results = extract_commercial_signals("company-1", evidence)
    signal_types = {r.signal_type for r in results}
    assert {"META_ADVERTISING", "SUBSCRIPTION", "DTC", "RETAIL_EXPANSION"} <= signal_types


def test_multiple_evidence_records_for_one_signal_are_all_preserved():
    evidence = [
        _evidence("products_services", "We run meta advertising campaigns", source_provider_id="provider-a"),
        _evidence("products_services", "Also runs meta advertising campaigns", source_provider_id="provider-b"),
        _evidence("business_model", "Primarily meta advertising campaigns", source_provider_id="provider-c"),
    ]
    results = extract_commercial_signals("company-1", evidence)
    signal = _find(results, "META_ADVERTISING")
    assert len(signal.evidence_ids) == 3
    assert len(signal.provider_ids) == 3


# --- temporal fields ---------------------------------------------------


def test_first_seen_and_last_seen_span_the_contributing_evidence():
    older = datetime.now(timezone.utc) - timedelta(days=10)
    newer = datetime.now(timezone.utc)
    evidence = [
        _evidence("products_services", "We run meta advertising campaigns", retrieved_at=older, source_provider_id="provider-a"),
        _evidence("business_model", "Continuing meta advertising campaigns", retrieved_at=newer, source_provider_id="provider-b"),
    ]
    results = extract_commercial_signals("company-1", evidence)
    signal = _find(results, "META_ADVERTISING")
    assert signal.first_seen == older
    assert signal.last_seen == newer


# --- no keyword-only false positives -----------------------------------


def test_industry_mention_never_creates_a_signal():
    evidence = [_evidence("industry", "Shopify")]  # even if industry field literally said "Shopify"
    results = extract_commercial_signals("company-1", evidence)
    assert _find(results, "SHOPIFY") is None


def test_bare_shopify_mention_does_not_prove_shopify_usage():
    """The task's own example: "Shopify" inside generic text must not
    automatically prove the company uses it — only an explicit
    company-attributed phrase does."""
    evidence = [_evidence("products_services", "Shopify is a major ecommerce platform in this space")]
    results = extract_commercial_signals("company-1", evidence)
    assert _find(results, "SHOPIFY") is None


def test_explicit_shopify_attribution_does_produce_a_signal():
    evidence = [_evidence("products_services", "Our store is built on Shopify")]
    results = extract_commercial_signals("company-1", evidence)
    assert _find(results, "SHOPIFY") is not None


def test_generic_context_does_not_become_company_behavior():
    evidence = [_evidence("products_services", "Meta is a major advertising platform")]
    results = extract_commercial_signals("company-1", evidence)
    assert _find(results, "META_ADVERTISING") is None


def test_domain_and_identity_fields_never_scanned_for_signals():
    evidence = [
        _evidence("domain", "meta-advertising-agency.invalid"),
        _evidence("company_identity", "Direct To Consumer Holdings Inc"),
    ]
    results = extract_commercial_signals("company-1", evidence)
    assert results == ()


# --- business model separation ----------------------------------------


def test_dtc_signal_is_a_distinct_field_from_business_model_classification():
    """This module never imports or calls Phase 13's classifier — DTC here
    is an independently-detected behavioral signal, not a copy of it."""
    import app.services.commercial_signal_extractor as extractor_module

    assert "business_model_classifier" not in extractor_module.__file__
    assert not hasattr(extractor_module, "classify_business_model")


# --- no ICP / manager feedback involvement ------------------------------


def test_extractor_signature_has_no_icp_or_feedback_parameter():
    import inspect

    params = inspect.signature(extract_commercial_signals).parameters
    assert set(params.keys()) == {"company_id", "evidence_records"}


def test_no_signal_is_mapped_to_a_fitness_verdict():
    """Structural guarantee: nothing in the module source hard-codes a
    signal -> GOOD_FIT/NOT_FIT mapping."""
    import inspect

    source = inspect.getsource(extract_commercial_signals)
    for forbidden in ("GOOD_FIT", "NOT_FIT", "WEAK_FIT", "PASS", "FAIL", "SCORE"):
        assert forbidden not in source


# --- no fabrication / determinism ---------------------------------------


def test_no_fabricated_signal_without_a_literal_phrase():
    evidence = [_evidence("products_services", "High quality products for happy customers")]
    results = extract_commercial_signals("company-1", evidence)
    assert results == ()


def test_extraction_is_deterministic():
    evidence = [_evidence("products_services", "We run meta advertising campaigns and a subscription model")]
    first = extract_commercial_signals("company-1", evidence)
    second = extract_commercial_signals("company-1", evidence)
    assert first == second
