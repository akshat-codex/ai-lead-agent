from datetime import datetime, timezone
from uuid import uuid4

from app.schemas.business_model import BusinessModel, ClassificationStatus
from app.schemas.evidence import ConfidenceLevel, EntityType, EvidenceRecord, SourceType
from app.services.business_model_classifier import classify_business_model


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


# --- each concrete category ------------------------------------------


def test_b2b_from_company_type():
    result = classify_business_model("company-1", [_evidence("company_type", "B2B")])
    assert result.primary_model == BusinessModel.B2B
    assert result.status == ClassificationStatus.CLASSIFIED


def test_b2c_from_products_services_description():
    result = classify_business_model("company-1", [_evidence("products_services", "A consumer-facing skincare line")])
    assert result.primary_model == BusinessModel.B2C


def test_dtc_from_company_type():
    result = classify_business_model("company-1", [_evidence("company_type", "D2C")])
    assert result.primary_model == BusinessModel.DTC


def test_b2b2c_from_business_description():
    result = classify_business_model("company-1", [_evidence("products_services", "A B2B2C platform for retailers and shoppers")])
    assert result.primary_model == BusinessModel.B2B2C


def test_marketplace_from_products_services():
    result = classify_business_model("company-1", [_evidence("products_services", "An online marketplace connecting sellers and buyers")])
    assert result.primary_model == BusinessModel.MARKETPLACE


def test_agency_from_company_type():
    result = classify_business_model("company-1", [_evidence("company_type", "Agency")])
    assert result.primary_model == BusinessModel.AGENCY


def test_consultancy_from_products_services():
    result = classify_business_model("company-1", [_evidence("products_services", "Management consultancy services")])
    assert result.primary_model == BusinessModel.CONSULTANCY


def test_wholesale_from_products_services():
    result = classify_business_model("company-1", [_evidence("products_services", "We operate as a wholesale distributor")])
    assert result.primary_model == BusinessModel.WHOLESALE


def test_retail_from_products_services():
    result = classify_business_model("company-1", [_evidence("products_services", "A retail storefront chain")])
    assert result.primary_model == BusinessModel.RETAIL


def test_service_from_products_services():
    result = classify_business_model("company-1", [_evidence("products_services", "A professional services firm")])
    assert result.primary_model == BusinessModel.SERVICE


# --- multiple legitimate models -> HYBRID -----------------------------


def test_hybrid_from_two_independent_signals():
    evidence = [
        _evidence("company_type", "D2C", source_provider_id="provider-a"),
        _evidence("products_services", "We also run a wholesale distribution channel", source_provider_id="provider-b"),
    ]
    result = classify_business_model("company-1", evidence)
    assert result.primary_model == BusinessModel.HYBRID
    assert set(result.secondary_models) == {BusinessModel.DTC, BusinessModel.WHOLESALE}
    assert result.status == ClassificationStatus.CLASSIFIED


def test_hybrid_never_forces_a_single_label():
    evidence = [
        _evidence("company_type", "B2B"),
        _evidence("products_services", "Also sold via retail storefronts"),
    ]
    result = classify_business_model("company-1", evidence)
    assert result.primary_model == BusinessModel.HYBRID
    assert BusinessModel.B2B in result.secondary_models
    assert BusinessModel.RETAIL in result.secondary_models


# --- insufficient evidence -> UNKNOWN --------------------------------


def test_no_evidence_at_all_is_unknown():
    result = classify_business_model("company-1", [])
    assert result.primary_model == BusinessModel.UNKNOWN
    assert result.status == ClassificationStatus.INSUFFICIENT_EVIDENCE
    assert result.confidence == ConfidenceLevel.UNKNOWN


def test_irrelevant_evidence_only_is_unknown():
    evidence = [_evidence("domain", "example-test.invalid"), _evidence("employee_count", 50)]
    result = classify_business_model("company-1", evidence)
    assert result.primary_model == BusinessModel.UNKNOWN
    assert result.status == ClassificationStatus.INSUFFICIENT_EVIDENCE


# --- conflicting evidence -----------------------------------------------


def test_conflicting_company_type_is_unknown_not_hybrid():
    evidence = [
        _evidence("company_type", "B2B", source_provider_id="provider-a"),
        _evidence("company_type", "B2C", source_provider_id="provider-b"),
    ]
    result = classify_business_model("company-1", evidence)
    assert result.primary_model == BusinessModel.UNKNOWN
    assert result.status == ClassificationStatus.CONFLICTING_EVIDENCE
    assert set(result.conflicting_evidence_ids) == {e.id for e in evidence}
    assert result.primary_model != BusinessModel.HYBRID  # never a synonym for "conflicting"


def test_conflicting_company_type_does_not_block_other_independent_evidence():
    evidence = [
        _evidence("company_type", "B2B", source_provider_id="provider-a"),
        _evidence("company_type", "B2C", source_provider_id="provider-b"),
        _evidence("products_services", "We operate a wholesale distribution channel", source_provider_id="provider-c"),
    ]
    result = classify_business_model("company-1", evidence)
    assert result.primary_model == BusinessModel.WHOLESALE
    assert result.status == ClassificationStatus.CLASSIFIED
    assert len(result.conflicting_evidence_ids) == 2  # conflict is still reported, transparently
    assert result.primary_model != BusinessModel.HYBRID


def test_neither_conflicting_value_is_arbitrarily_chosen():
    evidence = [
        _evidence("company_type", "B2B", source_provider_id="provider-a"),
        _evidence("company_type", "B2C", source_provider_id="provider-b"),
    ]
    result = classify_business_model("company-1", evidence)
    assert result.primary_model not in (BusinessModel.B2B, BusinessModel.B2C)


# --- industry != business model -----------------------------------------


def test_healthcare_industry_alone_does_not_imply_b2b():
    result = classify_business_model("company-1", [_evidence("industry", "Healthcare")])
    assert result.primary_model == BusinessModel.UNKNOWN


def test_fmcg_industry_alone_does_not_imply_dtc():
    result = classify_business_model("company-1", [_evidence("industry", "FMCG")])
    assert result.primary_model == BusinessModel.UNKNOWN


def test_industry_evidence_never_contributes_supporting_ids():
    evidence = [_evidence("industry", "Skincare"), _evidence("company_type", "D2C")]
    result = classify_business_model("company-1", evidence)
    industry_id = evidence[0].id
    assert industry_id not in result.supporting_evidence_ids


# --- no fabrication ---------------------------------------------------


def test_shopify_mention_does_not_imply_dtc():
    """Using an ecommerce platform doesn't itself prove any specific
    business model — using it is common across B2B, B2C, wholesale, and
    retail-plus-online sellers alike."""
    result = classify_business_model("company-1", [_evidence("products_services", "We use Shopify to run our online shop")])
    assert result.primary_model == BusinessModel.UNKNOWN


def test_no_classification_without_a_literal_supporting_keyword():
    result = classify_business_model("company-1", [_evidence("products_services", "High-quality skincare for everyone")])
    assert result.primary_model == BusinessModel.UNKNOWN


# --- supporting evidence ids -------------------------------------------


def test_supporting_evidence_ids_match_the_matching_records():
    ev = _evidence("company_type", "Marketplace")
    result = classify_business_model("company-1", [ev])
    assert result.supporting_evidence_ids == (ev.id,)


# --- no ICP / soft preference involvement -------------------------------


def test_classifier_signature_has_no_icp_parameter():
    import inspect

    params = inspect.signature(classify_business_model).parameters
    assert set(params.keys()) == {"company_id", "evidence_records"}


# --- determinism -----------------------------------------------------------


def test_classification_is_deterministic():
    evidence = [_evidence("company_type", "D2C"), _evidence("products_services", "Also a wholesale distributor")]
    first = classify_business_model("company-1", evidence)
    second = classify_business_model("company-1", evidence)
    assert first == second


# --- confidence reflects corroboration -----------------------------------


def test_confidence_is_low_for_a_single_source():
    result = classify_business_model("company-1", [_evidence("company_type", "B2B")])
    assert result.confidence == ConfidenceLevel.LOW


def test_confidence_is_medium_for_corroborating_sources():
    evidence = [
        _evidence("company_type", "B2B", source_provider_id="provider-a"),
        _evidence("products_services", "A B2B software platform", source_provider_id="provider-b"),
    ]
    result = classify_business_model("company-1", evidence)
    assert result.confidence == ConfidenceLevel.MEDIUM
