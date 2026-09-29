"""Phase 17 — deterministic mock adversarial response helper.

Mirrors app/services/llm_providers/mock.py's build_good_fit_response(), but
for the adversarial schema (AdversarialContext / RawAdversarialOutput)
instead of the Phase 16 qualification schema. Kept in its own module rather
than added to mock.py so Phase 16's file is untouched by Phase 17.
"""
from __future__ import annotations

import json

from app.schemas.adversarial_review import AdversarialContext


def build_survives_response(context: AdversarialContext) -> str:
    """A well-formed SURVIVES response that cites only real evidence ids
    drawn from the given context, never inventing any."""
    supporting = [e.id for e in context.base.evidence[:2]]
    return json.dumps(
        {
            "adversarial_result": "SURVIVES",
            "confidence": 78,
            "contradictions": [],
            "risk_codes": [],
            "supporting_evidence_ids": supporting,
            "contradicting_evidence_ids": [],
            "unsupported_claims": [],
            "missing_evidence": [],
            "reasoning_summary": "No material contradiction found; supplied evidence remains consistent with the first-pass decision.",
            "recommendation": "No further action required.",
        }
    )
