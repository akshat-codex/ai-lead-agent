from datetime import datetime, timedelta, timezone

from app.schemas.evidence import ConfidenceLevel, EntityType, EvidenceRecord, EvidenceStatus, SourceType
from app.services.evidence_engine import (
    compute_field_status,
    group_by_field,
    summarize_entity,
    summarize_field,
)


def _record(**overrides) -> EvidenceRecord:
    base = dict(
        id="ev-1",
        entity_type=EntityType.COMPANY,
        entity_id="company-1",
        field="industry",
        value="Skincare",
        source_provider_id="mock-company-data-v1",
        source_type=SourceType.PROVIDER,
        external_id="ext-1",
        retrieved_at=datetime.now(timezone.utc),
        confidence=ConfidenceLevel.UNKNOWN,
        created_at=datetime.now(timezone.utc),
    )
    base.update(overrides)
    return EvidenceRecord(**base)


# --- grouping --------------------------------------------------------


def test_group_by_field_separates_fields():
    records = [_record(field="industry"), _record(field="domain", value="acme.com")]
    grouped = group_by_field(records)
    assert set(grouped.keys()) == {"industry", "domain"}
    assert len(grouped["industry"]) == 1


# --- status: missing / unknown -----------------------------------------


def test_no_records_is_unknown():
    assert compute_field_status("industry", []) == EvidenceStatus.UNKNOWN


# --- status: single record, unknown confidence -> insufficient -----------


def test_single_record_with_unknown_confidence_is_insufficient():
    status = compute_field_status("business_model", [_record(field="business_model", value="Subscription")])
    assert status == EvidenceStatus.INSUFFICIENT


def test_single_record_with_high_confidence_is_supported():
    status = compute_field_status(
        "business_model",
        [_record(field="business_model", value="Subscription", confidence=ConfidenceLevel.HIGH)],
    )
    assert status == EvidenceStatus.SUPPORTED


# --- status: single record, trusted structured provider -------------------


def test_single_record_from_trusted_structured_provider_on_industry_is_supported_structured():
    status = compute_field_status(
        "industry",
        [_record(field="industry", value="Skincare", source_provider_id="explorium-company-discovery-v1")],
    )
    assert status == EvidenceStatus.SUPPORTED_STRUCTURED


def test_single_record_from_trusted_structured_provider_on_country_is_supported_structured():
    status = compute_field_status(
        "country",
        [_record(field="country", value="united states", source_provider_id="explorium-company-discovery-v1")],
    )
    assert status == EvidenceStatus.SUPPORTED_STRUCTURED


def test_single_record_from_untrusted_provider_on_industry_stays_insufficient():
    """The same field, same shape, but an untrusted source_provider_id —
    the allowlist boundary must hold: it is the provider that's trusted,
    not the field alone."""
    status = compute_field_status(
        "industry",
        [_record(field="industry", value="Skincare", source_provider_id="some-other-provider-v1")],
    )
    assert status == EvidenceStatus.INSUFFICIENT


def test_single_record_from_trusted_provider_on_an_untrusted_field_stays_insufficient():
    """The same trusted provider, but a field not on the allowlist — this
    is not a blanket "trust everything this provider says," only the
    specific fields named."""
    status = compute_field_status(
        "domain",
        [_record(field="domain", value="example.com", source_provider_id="explorium-company-discovery-v1")],
    )
    assert status == EvidenceStatus.INSUFFICIENT


def test_supported_structured_never_appears_for_two_records_it_upgrades_to_supported():
    """Corroboration still takes priority — two agreeing records from a
    trusted structured provider is real SUPPORTED, not SUPPORTED_STRUCTURED
    (which only ever describes the single-record case)."""
    records = [
        _record(field="industry", value="Skincare", source_provider_id="explorium-company-discovery-v1", external_id="a"),
        _record(field="industry", value="Skincare", source_provider_id="explorium-company-discovery-v1", external_id="b"),
    ]
    assert compute_field_status("industry", records) == EvidenceStatus.SUPPORTED


def test_conflicting_values_from_trusted_provider_still_conflict():
    """A trusted provider disagreeing with itself (or another source) must
    still CONFLICT — the allowlist only affects the single-record,
    all-agreeing case, never overrides disagreement."""
    records = [
        _record(field="industry", value="Skincare", source_provider_id="explorium-company-discovery-v1", external_id="a"),
        _record(field="industry", value="Fintech", source_provider_id="explorium-company-discovery-v1", external_id="b"),
    ]
    assert compute_field_status("industry", records) == EvidenceStatus.CONFLICT


# --- status: multiple sources -------------------------------------------


def test_two_agreeing_sources_is_supported_even_without_stated_confidence():
    records = [
        _record(field="industry", value="Skincare", source_provider_id="provider-a"),
        _record(field="industry", value="Skincare", source_provider_id="provider-b"),
    ]
    assert compute_field_status("industry", records) == EvidenceStatus.SUPPORTED


def test_conflicting_values_is_conflict():
    records = [
        _record(field="employee_range", value={"min": 11, "max": 50}, source_provider_id="provider-a"),
        _record(field="employee_range", value={"min": 51, "max": 200}, source_provider_id="provider-b"),
    ]
    assert compute_field_status("employee_range", records) == EvidenceStatus.CONFLICT


def test_both_conflicting_records_are_preserved_not_collapsed():
    records = [
        _record(field="employee_range", value={"min": 11, "max": 50}, source_provider_id="provider-a"),
        _record(field="employee_range", value={"min": 51, "max": 200}, source_provider_id="provider-b"),
    ]
    summary = summarize_field("employee_range", records)
    assert summary.status == EvidenceStatus.CONFLICT
    assert len(summary.records) == 2
    values = {tuple(sorted(r.value.items())) for r in summary.records}
    assert len(values) == 2  # neither value discarded


# --- normalized-value comparison (geography) ------------------------------


def test_us_and_united_states_are_recognized_as_agreeing():
    records = [
        _record(field="country", value="US", source_provider_id="provider-a"),
        _record(field="country", value="United States", source_provider_id="provider-b"),
    ]
    assert compute_field_status("country", records) == EvidenceStatus.SUPPORTED


def test_country_and_an_unrelated_country_still_conflict():
    records = [
        _record(field="country", value="US", source_provider_id="provider-a"),
        _record(field="country", value="Germany", source_provider_id="provider-b"),
    ]
    assert compute_field_status("country", records) == EvidenceStatus.CONFLICT


def test_geography_normalization_does_not_broaden_a_region_into_a_country_match():
    """"Europe" must never be treated as agreeing with "Germany" — that
    would be exactly the kind of silent broadening the task forbids."""
    records = [
        _record(field="country", value="Europe", source_provider_id="provider-a"),
        _record(field="country", value="Germany", source_provider_id="provider-b"),
    ]
    assert compute_field_status("country", records) == EvidenceStatus.CONFLICT


def test_stored_raw_values_are_never_altered_by_normalized_comparison():
    records = [
        _record(field="country", value="US", source_provider_id="provider-a"),
        _record(field="country", value="United States", source_provider_id="provider-b"),
    ]
    summary = summarize_field("country", records)
    raw_values = {r.value for r in summary.records}
    assert raw_values == {"US", "United States"}  # both preserved verbatim


# --- entity summary / completeness -----------------------------------


def test_entity_summary_includes_critical_fields_with_zero_evidence_as_unknown():
    summary = summarize_entity(EntityType.COMPANY, "company-1", [])
    field_names = {f.field for f in summary.fields}
    assert "domain" in field_names
    assert all(f.status == EvidenceStatus.UNKNOWN for f in summary.fields)
    assert summary.completeness == 0.0


def test_completeness_reflects_covered_critical_fields():
    records = [
        _record(field="company_identity", value="Example Test Co"),
        _record(field="domain", value="example-test.invalid"),
    ]
    summary = summarize_entity(EntityType.COMPANY, "company-1", records)
    # 2 of 9 critical company fields covered (Phase 7P added revenue_range
    # as the 9th critical field, alongside company_identity, domain,
    # industry, employee_range, country, company_type, business_model,
    # linkedin_id).
    assert summary.completeness == 2 / 9


def test_completeness_counts_a_conflicting_field_as_covered():
    """A field with contested evidence is not "no evidence" — completeness
    measures whether we have *something* to reason about, not agreement."""
    records = [
        _record(field="employee_range", value={"min": 11, "max": 50}, source_provider_id="a"),
        _record(field="employee_range", value={"min": 51, "max": 200}, source_provider_id="b"),
    ]
    summary = summarize_entity(EntityType.COMPANY, "company-1", records)
    employee_field = next(f for f in summary.fields if f.field == "employee_range")
    assert employee_field.status == EvidenceStatus.CONFLICT
    assert summary.completeness == 1 / 9


def test_revenue_range_is_a_critical_field_shown_as_unknown_when_absent():
    """Phase 7P: revenue_range must appear in a company's field summary
    (as UNKNOWN) even with zero evidence, exactly like domain/industry/
    employee_range already do — not silently absent from the picture."""
    summary = summarize_entity(EntityType.COMPANY, "company-1", [])
    revenue_field = next(f for f in summary.fields if f.field == "revenue_range")
    assert revenue_field.status == EvidenceStatus.UNKNOWN


def test_revenue_range_evidence_counts_toward_completeness():
    records = [_record(field="revenue_range", value="1M-10M")]
    summary = summarize_entity(EntityType.COMPANY, "company-1", records)
    revenue_field = next(f for f in summary.fields if f.field == "revenue_range")
    assert revenue_field.status == EvidenceStatus.INSUFFICIENT  # single, uncorroborated record
    assert summary.completeness == 1 / 9


def test_person_entity_uses_person_critical_fields():
    summary = summarize_entity(EntityType.PERSON, "person-1", [])
    field_names = {f.field for f in summary.fields}
    assert field_names == {"person_identity", "current_title", "company_association", "linkedin_id"}


# --- no qualification status leaks in -------------------------------------


def test_no_qualification_style_status_exists():
    status_values = {status.value for status in EvidenceStatus}
    assert status_values.isdisjoint({"ACCEPTED_LEAD", "QUALIFIED", "PASS", "FAIL"})


# --- determinism -----------------------------------------------------------


def test_summary_is_deterministic_for_identical_input():
    records = [_record(field="industry", value="Skincare")]
    first = summarize_entity(EntityType.COMPANY, "company-1", records)
    second = summarize_entity(EntityType.COMPANY, "company-1", records)
    assert first == second


# --- per-field freshness (additive, never affects status) ------------------


def _field_summary(fields, field_name):
    return next(f for f in fields if f.field == field_name)


def test_field_with_no_records_has_no_freshness_score():
    summary = summarize_field("industry", [])
    assert summary.freshness_score is None
    assert summary.is_stale is False


def test_fresh_record_gets_full_freshness_credit():
    now = datetime(2026, 9, 30, tzinfo=timezone.utc)
    record = _record(field="industry", retrieved_at=datetime(2026, 9, 25, tzinfo=timezone.utc))
    summary = summarize_field("industry", [record], now=now)
    assert summary.freshness_score == 100.0
    assert summary.is_stale is False


def test_very_old_record_decays_to_zero_and_is_flagged_stale():
    now = datetime(2026, 9, 30, tzinfo=timezone.utc)
    record = _record(field="industry", retrieved_at=datetime(2025, 1, 1, tzinfo=timezone.utc))
    summary = summarize_field("industry", [record], now=now)
    assert summary.freshness_score == 0.0
    assert summary.is_stale is True


def test_partially_aged_record_decays_linearly_between_the_two_bounds():
    now = datetime(2026, 9, 30, tzinfo=timezone.utc)
    # Default FreshnessConfig: full credit <=30 days, zero credit >=180 days.
    # 105 days is the exact midpoint of that span -> 50.0.
    record = _record(field="industry", retrieved_at=now - __import__("datetime").timedelta(days=105))
    summary = summarize_field("industry", [record], now=now)
    assert summary.freshness_score == 50.0
    assert summary.is_stale is False


def test_freshness_uses_this_fields_own_newest_record_not_a_global_one():
    """Two different fields on the same entity must decay independently —
    a fresh domain record must not make a stale industry record look fresh
    just because they belong to the same company."""
    now = datetime(2026, 9, 30, tzinfo=timezone.utc)
    records = [
        _record(id="ev-1", field="industry", retrieved_at=datetime(2025, 1, 1, tzinfo=timezone.utc)),
        _record(id="ev-2", field="domain", value="acme.com", retrieved_at=now),
    ]
    summary = summarize_entity(EntityType.COMPANY, "company-1", records, now=now)
    assert _field_summary(summary.fields, "industry").freshness_score == 0.0
    assert _field_summary(summary.fields, "domain").freshness_score == 100.0


def test_stale_field_still_keeps_its_original_status_unchanged():
    """The core safety property: an old single-source record must still
    resolve to the exact same EvidenceStatus it always did — freshness is
    observability, never a hidden second gate on hard-rule PASS/HOLD."""
    now = datetime(2026, 9, 30, tzinfo=timezone.utc)
    old_record = _record(field="business_model", value="Subscription", retrieved_at=datetime(2025, 1, 1, tzinfo=timezone.utc))
    summary = summarize_field("business_model", [old_record], now=now)
    assert summary.status == compute_field_status("business_model", [old_record])
    assert summary.status == EvidenceStatus.INSUFFICIENT
    assert summary.is_stale is True
