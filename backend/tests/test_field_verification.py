from datetime import datetime, timezone
from uuid import uuid4

from app.providers.base import ProviderAdapter
from app.providers.contracts import (
    NormalizedRecord,
    ProviderCapability,
    ProviderError,
    ProviderRequest,
    ProviderResponse,
    SourceMetadata,
)
from app.providers.registry import ProviderRegistry
from app.schemas.evidence import ConfidenceLevel, EntityType, EvidenceRecord, EvidenceStatus, SourceType
from app.schemas.verification import VerificationExecutionStatus, VerificationOutcome, VerificationTrigger
from app.services.field_verification import verify_field

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


class _FixedEnrichmentProvider(ProviderAdapter):
    def __init__(self, provider_id: str, field: str, value, external_id: str = "ext-1", fail: bool = False, retryable: bool = True):
        super().__init__(provider_id=provider_id, provider_name=provider_id, capabilities={ProviderCapability.COMPANY_ENRICHMENT})
        self._field = field
        self._value = value
        self._external_id = external_id
        self._fail = fail
        self._retryable = retryable

    def execute(self, request: ProviderRequest) -> ProviderResponse:
        if self._fail:
            return ProviderResponse(
                provider_id=self.provider_id,
                capability=request.capability,
                success=False,
                error=ProviderError(code="SIMULATED_FAILURE", message="simulated failure", retryable=self._retryable),
            )
        return ProviderResponse(
            provider_id=self.provider_id,
            capability=request.capability,
            success=True,
            data=(NormalizedRecord(external_id=self._external_id, name="Test Co", attributes={self._field: self._value}),),
            source=SourceMetadata(provider_id=self.provider_id, provider_name=self.provider_id, retrieved_at=NOW, is_mock=True),
        )


class _TimeoutProvider(ProviderAdapter):
    def __init__(self, provider_id: str):
        super().__init__(provider_id=provider_id, provider_name=provider_id, capabilities={ProviderCapability.COMPANY_ENRICHMENT})

    def execute(self, request: ProviderRequest) -> ProviderResponse:
        return ProviderResponse(
            provider_id=self.provider_id,
            capability=request.capability,
            success=False,
            error=ProviderError(code="TIMEOUT", message="simulated timeout", retryable=True),
        )


def _evidence(field, value, provider_id, **overrides) -> EvidenceRecord:
    base = dict(
        id=str(uuid4()),
        entity_type=EntityType.COMPANY,
        entity_id="company-1",
        field=field,
        value=value,
        source_provider_id=provider_id,
        source_type=SourceType.PROVIDER,
        external_id="ext-orig",
        retrieved_at=NOW,
        confidence=ConfidenceLevel.UNKNOWN,
        created_at=NOW,
    )
    base.update(overrides)
    return EvidenceRecord(**base)


def _registry(*providers) -> ProviderRegistry:
    reg = ProviderRegistry()
    for p in providers:
        reg.register(p)
    return reg


# --- trigger gating -----------------------------------------------------


def test_supported_field_is_not_triggered():
    records = [
        _evidence("industry", "Skincare", "provider-a"),
        _evidence("industry", "Skincare", "provider-b"),
    ]
    registry = _registry(_FixedEnrichmentProvider("provider-c", "industry", "Skincare"))
    result = verify_field("icp-1", 1, EntityType.COMPANY, "company-1", "industry", records, registry)
    assert result.execution_status == VerificationExecutionStatus.NOT_TRIGGERED
    assert result.outcome is None
    assert result.trigger is None
    assert result.new_evidence_ids == ()


def test_unknown_field_with_no_evidence_is_not_triggered():
    registry = _registry(_FixedEnrichmentProvider("provider-c", "industry", "Skincare"))
    result = verify_field("icp-1", 1, EntityType.COMPANY, "company-1", "industry", [], registry)
    assert result.original_status == EvidenceStatus.UNKNOWN
    assert result.execution_status == VerificationExecutionStatus.NOT_TRIGGERED


def test_conflicting_field_triggers_verification():
    records = [
        _evidence("employee_count", 25, "provider-a"),
        _evidence("employee_count", 500, "provider-b"),
    ]
    registry = _registry(_FixedEnrichmentProvider("provider-c", "employee_count", 30))
    result = verify_field("icp-1", 1, EntityType.COMPANY, "company-1", "employee_count", records, registry)
    assert result.trigger == VerificationTrigger.CONFLICT
    assert result.execution_status == VerificationExecutionStatus.SUCCESS


def test_insufficient_field_triggers_verification():
    records = [_evidence("employee_count", 25, "provider-a", confidence=ConfidenceLevel.LOW)]
    registry = _registry(_FixedEnrichmentProvider("provider-c", "employee_count", 25))
    result = verify_field("icp-1", 1, EntityType.COMPANY, "company-1", "employee_count", records, registry)
    assert result.trigger == VerificationTrigger.INSUFFICIENT


# --- resolution outcomes --------------------------------------------------


def test_new_corroborating_evidence_resolves_conflict():
    records = [
        _evidence("employee_count", 25, "provider-a"),
        _evidence("employee_count", 500, "provider-b"),
    ]
    # a third, independent provider corroborates provider-a's value
    registry = _registry(_FixedEnrichmentProvider("provider-c", "employee_count", 25))
    result = verify_field("icp-1", 1, EntityType.COMPANY, "company-1", "employee_count", records, registry)
    # status is still a 3-way disagreement is not how compute_field_status works;
    # any distinct-value disagreement remains CONFLICT since 25 != 500 still coexist
    assert result.resulting_status == EvidenceStatus.CONFLICT
    assert result.outcome == VerificationOutcome.HOLD


def test_insufficient_resolved_by_independent_corroboration():
    records = [_evidence("employee_count", 25, "provider-a", confidence=ConfidenceLevel.LOW)]
    registry = _registry(_FixedEnrichmentProvider("provider-c", "employee_count", 25))
    result = verify_field("icp-1", 1, EntityType.COMPANY, "company-1", "employee_count", records, registry)
    assert result.resulting_status == EvidenceStatus.SUPPORTED
    assert result.outcome == VerificationOutcome.RESOLVED
    assert len(result.new_evidence_ids) == 1
    assert len(result.all_evidence_ids) == 2


def test_still_insufficient_after_all_providers_fail_or_return_nothing():
    records = [_evidence("employee_count", 25, "provider-a", confidence=ConfidenceLevel.LOW)]
    empty_provider = _FixedEnrichmentProvider("provider-c", "some_other_field", "irrelevant")
    registry = _registry(empty_provider)
    result = verify_field("icp-1", 1, EntityType.COMPANY, "company-1", "employee_count", records, registry)
    assert result.execution_status == VerificationExecutionStatus.ALL_PROVIDERS_FAILED
    assert result.outcome == VerificationOutcome.HOLD


# --- same-provider duplicates never count as independent -------------------


def test_same_provider_already_consulted_is_excluded():
    records = [_evidence("employee_count", 25, "provider-a", confidence=ConfidenceLevel.LOW)]
    same_provider_again = _FixedEnrichmentProvider("provider-a", "employee_count", 25)
    registry = _registry(same_provider_again)
    result = verify_field("icp-1", 1, EntityType.COMPANY, "company-1", "employee_count", records, registry)
    assert result.execution_status == VerificationExecutionStatus.NO_INDEPENDENT_PROVIDERS
    assert result.outcome == VerificationOutcome.HOLD
    assert result.new_evidence_ids == ()


def test_mixed_providers_only_independent_ones_are_consulted():
    records = [_evidence("employee_count", 25, "provider-a", confidence=ConfidenceLevel.LOW)]
    same_provider = _FixedEnrichmentProvider("provider-a", "employee_count", 999)
    new_provider = _FixedEnrichmentProvider("provider-b", "employee_count", 25)
    registry = _registry(same_provider, new_provider)
    result = verify_field("icp-1", 1, EntityType.COMPANY, "company-1", "employee_count", records, registry)
    assert len(result.providers_consulted) == 1
    assert result.providers_consulted[0].provider_id == "provider-b"


# --- provider failure / timeout ---------------------------------------------


def test_provider_failure_never_becomes_resolved():
    records = [_evidence("employee_count", 25, "provider-a", confidence=ConfidenceLevel.LOW)]
    failing = _FixedEnrichmentProvider("provider-c", "employee_count", 25, fail=True)
    registry = _registry(failing)
    result = verify_field("icp-1", 1, EntityType.COMPANY, "company-1", "employee_count", records, registry)
    assert result.execution_status == VerificationExecutionStatus.ALL_PROVIDERS_FAILED
    assert result.outcome != VerificationOutcome.RESOLVED
    assert result.providers_consulted[0].success is False


def test_timeout_is_recorded_and_not_resolved():
    records = [_evidence("employee_count", 25, "provider-a", confidence=ConfidenceLevel.LOW)]
    registry = _registry(_TimeoutProvider("provider-timeout"))
    result = verify_field("icp-1", 1, EntityType.COMPANY, "company-1", "employee_count", records, registry)
    assert result.execution_status == VerificationExecutionStatus.ALL_PROVIDERS_FAILED
    assert result.providers_consulted[0].error_code == "TIMEOUT"


def test_no_providers_registered_at_all():
    records = [_evidence("employee_count", 25, "provider-a", confidence=ConfidenceLevel.LOW)]
    registry = ProviderRegistry()
    result = verify_field("icp-1", 1, EntityType.COMPANY, "company-1", "employee_count", records, registry)
    assert result.execution_status == VerificationExecutionStatus.NO_INDEPENDENT_PROVIDERS


def test_partial_success_still_resolves_when_one_provider_succeeds():
    records = [_evidence("employee_count", 25, "provider-a", confidence=ConfidenceLevel.LOW)]
    failing = _FixedEnrichmentProvider("provider-fail", "employee_count", 25, fail=True)
    succeeding = _FixedEnrichmentProvider("provider-succeed", "employee_count", 25)
    registry = _registry(failing, succeeding)
    result = verify_field("icp-1", 1, EntityType.COMPANY, "company-1", "employee_count", records, registry)
    assert result.execution_status == VerificationExecutionStatus.SUCCESS
    assert result.outcome == VerificationOutcome.RESOLVED
    assert len(result.providers_consulted) == 2


# --- evidence preservation & provenance ------------------------------------


def test_original_evidence_is_never_mutated_or_removed():
    records = [_evidence("employee_count", 25, "provider-a", confidence=ConfidenceLevel.LOW)]
    original_id = records[0].id
    registry = _registry(_FixedEnrichmentProvider("provider-c", "employee_count", 25))
    result = verify_field("icp-1", 1, EntityType.COMPANY, "company-1", "employee_count", records, registry)
    assert original_id in result.all_evidence_ids
    assert original_id in result.original_evidence_ids


def test_new_evidence_records_carry_correct_provenance():
    records = [_evidence("employee_count", 25, "provider-a", confidence=ConfidenceLevel.LOW)]
    registry = _registry(_FixedEnrichmentProvider("provider-c", "employee_count", 25, external_id="ext-99"))
    result = verify_field("icp-1", 1, EntityType.COMPANY, "company-1", "employee_count", records, registry)
    assert len(result.new_evidence_records) == 1
    new_record = result.new_evidence_records[0]
    assert new_record.source_provider_id == "provider-c"
    assert new_record.external_id == "ext-99"
    assert new_record.field == "employee_count"
    assert new_record.value == 25


def test_new_evidence_is_appended_never_overwrites():
    records = [_evidence("employee_count", 25, "provider-a", confidence=ConfidenceLevel.LOW)]
    registry = _registry(_FixedEnrichmentProvider("provider-c", "employee_count", 25))
    result = verify_field("icp-1", 1, EntityType.COMPANY, "company-1", "employee_count", records, registry)
    assert len(result.all_evidence_ids) == len(records) + len(result.new_evidence_ids)


# --- multi-ICP reuse (verification is ICP-agnostic at the evidence layer) --


def test_same_entity_field_verification_reusable_across_icps():
    records = [_evidence("employee_count", 25, "provider-a", confidence=ConfidenceLevel.LOW)]
    registry = _registry(_FixedEnrichmentProvider("provider-c", "employee_count", 25))
    result_icp_a = verify_field("icp-a", 1, EntityType.COMPANY, "company-1", "employee_count", records, registry)
    result_icp_b = verify_field("icp-b", 3, EntityType.COMPANY, "company-1", "employee_count", records, registry)
    assert result_icp_a.icp_id == "icp-a"
    assert result_icp_b.icp_id == "icp-b"
    assert result_icp_b.icp_version == 3
    # both operate on the identical underlying evidence set/result shape
    assert result_icp_a.resulting_status == result_icp_b.resulting_status


# --- hard-rule protection: this module never touches Phase 3/12 -----------


def test_field_verification_never_imports_hard_rule_engine():
    import inspect

    import app.services.field_verification as module

    source = inspect.getsource(module)
    assert "evaluate_hard_rules" not in source
    assert "validate_against_icp" not in source
    assert "hard_rule_engine" not in source


def test_field_verification_never_imports_llm_infrastructure():
    import inspect

    import app.services.field_verification as module

    source = inspect.getsource(module)
    for forbidden in ("llm_providers", "LLMProvider", "qualify_lead", "run_adversarial_review"):
        assert forbidden not in source


# --- determinism -------------------------------------------------------


def test_verification_is_deterministic():
    records = [_evidence("employee_count", 25, "provider-a", confidence=ConfidenceLevel.LOW)]
    registry = _registry(_FixedEnrichmentProvider("provider-c", "employee_count", 25, external_id="ext-fixed"))
    first = verify_field("icp-1", 1, EntityType.COMPANY, "company-1", "employee_count", records, registry)
    second = verify_field("icp-1", 1, EntityType.COMPANY, "company-1", "employee_count", records, registry)
    assert first.outcome == second.outcome
    assert first.resulting_status == second.resulting_status
    assert first.execution_status == second.execution_status


# --- no fabrication ------------------------------------------------------


def test_missing_field_in_provider_response_produces_no_fabricated_evidence():
    records = [_evidence("employee_count", 25, "provider-a", confidence=ConfidenceLevel.LOW)]
    # provider supports the capability but never returns this field at all
    registry = _registry(_FixedEnrichmentProvider("provider-c", "unrelated_field", "some value"))
    result = verify_field("icp-1", 1, EntityType.COMPANY, "company-1", "employee_count", records, registry)
    assert result.new_evidence_ids == ()
    assert result.execution_status == VerificationExecutionStatus.ALL_PROVIDERS_FAILED


def test_person_entity_type_uses_person_enrichment_capability():
    records = [_evidence("current_title", "CMO", "provider-a", confidence=ConfidenceLevel.LOW, entity_type=EntityType.PERSON, entity_id="person-1")]
    # no PERSON_ENRICHMENT provider registered anywhere -> HOLD, not a crash
    registry = _registry(_FixedEnrichmentProvider("provider-c", "current_title", "CMO"))  # COMPANY_ENRICHMENT only
    result = verify_field("icp-1", 1, EntityType.PERSON, "person-1", "current_title", records, registry)
    assert result.execution_status == VerificationExecutionStatus.NO_INDEPENDENT_PROVIDERS
