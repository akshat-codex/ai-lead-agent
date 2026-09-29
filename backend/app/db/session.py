"""SQLAlchemy engine/session setup.

Models so far: the Phase 1 ICP draft (app/models/icp.py), the Phase 4
manager feedback record (app/models/feedback.py), the Phase 6 company
discovery run/candidate records (app/models/discovery.py), the Phase 7
canonical company / resolution audit records (app/models/company.py), the
Phase 8 enrichment run/fact records (app/models/enrichment.py), the Phase 9
people discovery run/candidate records (app/models/people_discovery.py),
the Phase 10 canonical person / resolution audit records
(app/models/person.py), the Phase 11 evidence records
(app/models/evidence.py), the Phase 12 evidence-backed hard ICP validation
audit records (app/models/hard_icp_validation.py), the Phase 13 business
model classification history (app/models/business_model.py), the
Phase 14 commercial signal history (app/models/commercial_signal.py), the
Phase 15 lead score history (app/models/scoring.py), the Phase 16 LLM
qualification history (app/models/llm_qualification.py), the Phase 17
adversarial review history (app/models/adversarial_review.py), the
Phase 18 multi-source verification history (app/models/field_verification.py),
the Phase 19 canonical lead / deduplication history (app/models/lead.py),
the Phase 20 human review history (app/models/human_review.py), the
Phase 21 batch orchestration coordination records (app/models/batch.py),
the Phase 22 ranking snapshot history (app/models/ranking.py), the
Phase 24 feedback learning snapshot history
(app/models/feedback_learning.py), the Phase 25 optimization
recommendation snapshot history (app/models/optimization.py), and the
Phase 26 optimization approval/application audit history
(app/models/optimization_application.py), the Phase 28 lead
confidence/readiness snapshot history (app/models/lead_confidence.py),
and the Phase 29 end-to-end pipeline run coordination record
(app/models/pipeline.py). Part B Phase 5 (Contact Enrichment) adds the
person enrichment run record (app/models/person_enrichment.py) — individual
facts from that phase are stored as ordinary evidence rows
(app/models/evidence.py), not a new fact table.
Phase 23's export and Phase 27's provider-routing layers are stateless
and add no model. The final lead-state machine belongs to a later phase
of the roadmap.
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.core.config import get_settings

settings = get_settings()

engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    from app.models import (  # noqa: F401  (register model metadata before create_all)
        adversarial_review,
        batch,
        business_model,
        commercial_signal,
        company,
        company_quality,
        discovery,
        enrichment,
        evidence,
        feedback,
        feedback_learning,
        field_verification,
        hard_icp_validation,
        human_review,
        icp,
        lead,
        lead_confidence,
        llm_qualification,
        optimization,
        optimization_application,
        people_discovery,
        person,
        person_enrichment,
        pipeline,
        ranking,
        scoring,
    )

    Base.metadata.create_all(bind=engine)
