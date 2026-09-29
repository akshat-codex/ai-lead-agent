"""Phase 23 API — lead export and output contract.

Reuses Phase 22's own `_compute_ranking()` UNCHANGED to get the ordered,
tiered lead list — no second ranking implementation. For each ranked
lead, this module loads the additional Phase 7/10/11/12/19/20 rows the
export contract requires beyond what ranking itself needed (canonical
identity, evidence-backed field values, hard-rule reason codes, human-
review reason codes, dedup provenance, resolution provenance) and calls
the unchanged, pure app/services/lead_export.build_exported_lead().

Never mutates any row it reads — export is read-only by construction: no
`db.add`/`db.commit` appears anywhere in this file except (implicitly)
none at all, matching the task's "export must not mutate source data" rule.
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from app.api.ranking import _compute_ranking
from app.db.session import get_db
from app.models.adversarial_review import AdversarialReviewModel
from app.models.company import CanonicalCompanyModel, CompanyResolutionModel
from app.models.evidence import EvidenceModel
from app.models.field_verification import FieldVerificationModel
from app.models.hard_icp_validation import HardIcpValidationModel
from app.models.human_review import HumanReviewModel
from app.models.icp import ICPModel
from app.models.lead import LeadDeduplicationModel
from app.models.llm_qualification import LLMQualificationModel
from app.models.person import CanonicalPersonModel, PersonResolutionModel
from app.models.scoring import LeadScoreModel
from app.schemas.evidence import EntityType, EvidenceRecord, EvidenceStatus
from app.schemas.export import (
    ExportEvidenceSummary,
    ExportFormat,
    ExportIdentity,
    ExportMetadata,
    ExportProvenance,
    ExportResult,
)
from app.services.evidence_engine import summarize_entity
from app.services.lead_export import build_exported_lead, render_export

router = APIRouter(prefix="/api/v1/exports", tags=["export"])


def _load_evidence(db: Session, entity_type: EntityType, entity_id: str) -> list[EvidenceRecord]:
    rows = (
        db.query(EvidenceModel)
        .filter(EvidenceModel.entity_type == entity_type.value, EvidenceModel.entity_id == entity_id)
        .all()
    )
    return [EvidenceRecord.model_validate(row, from_attributes=True) for row in rows]


def _evidence_summary(db: Session, company_id: str, person_id: str | None) -> ExportEvidenceSummary:
    company_evidence = _load_evidence(db, EntityType.COMPANY, company_id)
    company_summary = summarize_entity(EntityType.COMPANY, company_id, company_evidence)

    verified: dict[str, str] = {}
    conflicting: list[str] = []
    missing: list[str] = []
    evidence_ids: list[str] = []

    for field_summary in company_summary.fields:
        if field_summary.status == EvidenceStatus.SUPPORTED:
            verified[field_summary.field] = str(field_summary.records[0].value)
            evidence_ids.extend(r.id for r in field_summary.records)
        elif field_summary.status == EvidenceStatus.CONFLICT:
            conflicting.append(field_summary.field)
            evidence_ids.extend(r.id for r in field_summary.records)
        elif field_summary.status == EvidenceStatus.UNKNOWN:
            missing.append(field_summary.field)

    if person_id is not None:
        person_evidence = _load_evidence(db, EntityType.PERSON, person_id)
        person_summary = summarize_entity(EntityType.PERSON, person_id, person_evidence)
        for field_summary in person_summary.fields:
            key = f"person.{field_summary.field}"
            if field_summary.status == EvidenceStatus.SUPPORTED:
                verified[key] = str(field_summary.records[0].value)
                evidence_ids.extend(r.id for r in field_summary.records)
            elif field_summary.status == EvidenceStatus.CONFLICT:
                conflicting.append(key)
                evidence_ids.extend(r.id for r in field_summary.records)
            elif field_summary.status == EvidenceStatus.UNKNOWN:
                missing.append(key)

    return ExportEvidenceSummary(
        verified_fields=verified,
        conflicting_fields=tuple(dict.fromkeys(conflicting)),
        missing_critical_fields=tuple(dict.fromkeys(missing)),
        evidence_ids=tuple(dict.fromkeys(evidence_ids)),
    )


def _identity(db: Session, company_id: str, person_id: str | None) -> ExportIdentity:
    company_row = db.get(CanonicalCompanyModel, company_id)
    person_row = db.get(CanonicalPersonModel, person_id) if person_id is not None else None

    return ExportIdentity(
        company_id=company_id,
        company_name=company_row.canonical_name if company_row is not None else None,
        company_domain=company_row.canonical_domain if company_row is not None else None,
        person_id=person_id,
        person_name=person_row.canonical_name if person_row is not None else None,
        person_title=None,  # canonical person rows do not persist a current title (Phase 10 scope) - never guessed
        person_linkedin_id=person_row.linkedin_id if person_row is not None else None,
    )


def _latest(rows: list) -> object | None:
    """Same tie-safe pattern as Phase 22's ranking API — see its own
    docstring for why `.order_by(col.desc()).first()` is unsafe under
    SQLite's timestamp resolution."""
    return rows[-1] if rows else None


def _hard_validation_row(db: Session, icp_id: str, company_id: str, person_id: str | None) -> HardIcpValidationModel | None:
    query = db.query(HardIcpValidationModel).filter(
        HardIcpValidationModel.icp_id == icp_id, HardIcpValidationModel.company_id == company_id
    )
    query = query.filter(HardIcpValidationModel.person_id == person_id) if person_id else query.filter(HardIcpValidationModel.person_id.is_(None))
    return _latest(query.order_by(HardIcpValidationModel.validated_at).all())


def _score_row(db: Session, icp_id: str, company_id: str, person_id: str | None) -> LeadScoreModel | None:
    query = db.query(LeadScoreModel).filter(LeadScoreModel.icp_id == icp_id, LeadScoreModel.company_id == company_id)
    query = query.filter(LeadScoreModel.person_id == person_id) if person_id else query.filter(LeadScoreModel.person_id.is_(None))
    return _latest(query.order_by(LeadScoreModel.scored_at).all())


def _qualification_row(db: Session, icp_id: str, company_id: str, person_id: str | None) -> LLMQualificationModel | None:
    query = db.query(LLMQualificationModel).filter(
        LLMQualificationModel.icp_id == icp_id, LLMQualificationModel.company_id == company_id
    )
    query = (
        query.filter(LLMQualificationModel.person_id == person_id)
        if person_id
        else query.filter(LLMQualificationModel.person_id.is_(None))
    )
    return _latest(query.order_by(LLMQualificationModel.created_at).all())


def _human_review_row(db: Session, icp_id: str, lead_id: str) -> HumanReviewModel | None:
    return _latest(
        db.query(HumanReviewModel)
        .filter(HumanReviewModel.icp_id == icp_id, HumanReviewModel.lead_id == lead_id)
        .order_by(HumanReviewModel.created_at)
        .all()
    )


def _verification_ids(db, company_id: str, person_id: str | None) -> tuple[str, ...]:
    entity_ids = [company_id] + ([person_id] if person_id else [])
    rows = db.query(FieldVerificationModel).filter(FieldVerificationModel.entity_id.in_(entity_ids)).all()
    return tuple(row.id for row in rows)


def _deduplication_rows(db: Session, lead_id: str) -> list[LeadDeduplicationModel]:
    return db.query(LeadDeduplicationModel).filter(LeadDeduplicationModel.lead_id == lead_id).all()


def _source_provider_ids(company_row: CanonicalCompanyModel | None, person_row: CanonicalPersonModel | None) -> tuple[str, ...]:
    ids: list[str] = []
    if company_row is not None:
        ids.extend(company_row.provider_identities.keys())
    if person_row is not None:
        ids.extend(person_row.provider_identities.keys())
    return tuple(dict.fromkeys(ids))


def _build_export_result(db: Session, icp_id: str, batch_id: str | None) -> ExportResult:
    ranking_result = _compute_ranking(db, icp_id, batch_id)
    icp_record = db.get(ICPModel, icp_id)
    now = datetime.now(timezone.utc)

    exported_leads = []
    for ranked_lead in ranking_result.ranked_leads:
        company_id = ranked_lead.company_id
        person_id = ranked_lead.person_id

        company_row = db.get(CanonicalCompanyModel, company_id)
        person_row = db.get(CanonicalPersonModel, person_id) if person_id is not None else None

        identity = _identity(db, company_id, person_id)
        evidence = _evidence_summary(db, company_id, person_id)

        hard_validation = _hard_validation_row(db, icp_id, company_id, person_id)
        score_row = _score_row(db, icp_id, company_id, person_id)
        qualification_row = _qualification_row(db, icp_id, company_id, person_id)
        human_review = _human_review_row(db, icp_id, ranked_lead.lead_id)

        dedup_rows = _deduplication_rows(db, ranked_lead.lead_id)
        company_resolution_id = None
        person_resolution_id = None
        for dedup_row in dedup_rows:
            if dedup_row.company_candidate_id:
                candidate_resolution = (
                    db.query(CompanyResolutionModel)
                    .filter(CompanyResolutionModel.candidate_id == dedup_row.company_candidate_id)
                    .one_or_none()
                )
                if candidate_resolution is not None:
                    company_resolution_id = candidate_resolution.id
            if dedup_row.person_candidate_id:
                person_candidate_resolution = (
                    db.query(PersonResolutionModel)
                    .filter(PersonResolutionModel.candidate_id == dedup_row.person_candidate_id)
                    .one_or_none()
                )
                if person_candidate_resolution is not None:
                    person_resolution_id = person_candidate_resolution.id

        provenance = ExportProvenance(
            hard_validation_id=hard_validation.id if hard_validation is not None else None,
            score_id=score_row.id if score_row is not None else None,
            qualification_id=qualification_row.id if qualification_row is not None else None,
            adversarial_review_id=None,
            verification_ids=_verification_ids(db, company_id, person_id),
            human_review_id=human_review.id if human_review is not None else None,
            deduplication_ids=tuple(row.id for row in dedup_rows),
            company_resolution_id=company_resolution_id,
            person_resolution_id=person_resolution_id,
            source_provider_ids=_source_provider_ids(company_row, person_row),
        )

        # The adversarial review id is looked up via the qualification row
        # (Phase 17's own foreign key), filled in after provenance is
        # constructed so the lookup only runs when a qualification exists.
        if qualification_row is not None:
            adversarial_row = _latest(
                db.query(AdversarialReviewModel)
                .filter(AdversarialReviewModel.qualification_id == qualification_row.id)
                .order_by(AdversarialReviewModel.created_at)
                .all()
            )
            if adversarial_row is not None:
                provenance = provenance.model_copy(update={"adversarial_review_id": adversarial_row.id})

        exported_leads.append(
            build_exported_lead(
                ranked_lead=ranked_lead,
                batch_id=batch_id,
                identity=identity,
                evidence=evidence,
                qualification_summary=qualification_row.summary if qualification_row is not None else None,
                hard_rule_reason_codes=tuple(hard_validation.reason_codes) if hard_validation is not None else (),
                human_review_reason_codes=tuple(human_review.reason_codes) if human_review is not None else (),
                is_duplicate_occurrence=ranked_lead.tier.value == "DUPLICATE",
                provenance=provenance,
                exported_at=now,
            )
        )

    metadata = ExportMetadata(
        schema_version=exported_leads[0].schema_version if exported_leads else "1.0.0",
        icp_id=icp_id,
        icp_version=icp_record.version,
        batch_id=batch_id,
        lead_count=len(exported_leads),
        generated_at=now,
    )
    return ExportResult(metadata=metadata, leads=tuple(exported_leads))


@router.get("")
def export_leads(
    icp_id: str = Query(..., min_length=1),
    batch_id: str | None = Query(default=None),
    format: ExportFormat = Query(default=ExportFormat.JSON),
    db: Session = Depends(get_db),
):
    icp_record = db.get(ICPModel, icp_id)
    if icp_record is None:
        raise HTTPException(status_code=404, detail="ICP not found")

    result = _build_export_result(db, icp_id, batch_id)
    body = render_export(result, format)

    if format == ExportFormat.CSV:
        return Response(content=body, media_type="text/csv")
    return Response(content=body, media_type="application/json")
