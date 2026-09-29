def _icp_payload(name: str = "D2C Skincare") -> dict:
    return {
        "name": name,
        "hard_rules": {
            "industry": ["Skincare"],
            "geography": ["United States"],
            "min_employees": 10,
            "max_employees": 200,
            "allowed_titles": ["CMO"],
            "company_type": ["D2C"],
            "exclusions": [],
            "custom_rules": [],
        },
        "soft_preferences": {
            "business_model_preferences": ["Subscription"],
            "commercial_signals": [],
            "growth_signals": [],
            "marketing_signals": [],
            "other_preferences": [],
        },
    }


def _feedback_payload(**overrides) -> dict:
    base = {
        "lead_ref": "lead-123",
        "icp_id": "placeholder",
        "decision": "GOOD_FIT",
        "reason_codes": [],
        "reviewer_note": None,
        "reviewer_id": "manager@example.com",
    }
    base.update(overrides)
    return base


def _create_icp(client, name: str = "D2C Skincare") -> dict:
    return client.post("/api/v1/icps", json=_icp_payload(name)).json()


# --- all four decisions -------------------------------------------------


def test_good_fit_accepted_without_reason_codes(client):
    icp = _create_icp(client)
    payload = _feedback_payload(icp_id=icp["id"], decision="GOOD_FIT", reason_codes=[])
    response = client.post("/api/v1/feedback", json=payload)
    assert response.status_code == 201
    assert response.json()["decision"] == "GOOD_FIT"


def test_weak_fit_accepted_with_reason_code(client):
    icp = _create_icp(client)
    payload = _feedback_payload(icp_id=icp["id"], decision="WEAK_FIT", reason_codes=["UNCLEAR_BUSINESS_MODEL"])
    response = client.post("/api/v1/feedback", json=payload)
    assert response.status_code == 201
    assert response.json()["decision"] == "WEAK_FIT"


def test_not_fit_accepted_with_reason_code(client):
    icp = _create_icp(client)
    payload = _feedback_payload(icp_id=icp["id"], decision="NOT_FIT", reason_codes=["AGENCY"])
    response = client.post("/api/v1/feedback", json=payload)
    assert response.status_code == 201
    assert response.json()["decision"] == "NOT_FIT"


def test_hold_accepted_with_reason_code(client):
    icp = _create_icp(client)
    payload = _feedback_payload(icp_id=icp["id"], decision="HOLD", reason_codes=["UNCLEAR_BUSINESS_MODEL"])
    response = client.post("/api/v1/feedback", json=payload)
    assert response.status_code == 201
    assert response.json()["decision"] == "HOLD"


def test_unsupported_decision_rejected(client):
    icp = _create_icp(client)
    payload = _feedback_payload(icp_id=icp["id"], decision="MAYBE_FIT", reason_codes=["FOUNDER_LED"])
    response = client.post("/api/v1/feedback", json=payload)
    assert response.status_code == 422


# --- reason codes ------------------------------------------------------


def test_novel_well_formed_reason_code_is_accepted():
    """The reason-code system is extensible: a brand-new SCREAMING_SNAKE_CASE
    code not in the illustrative examples must still be accepted."""
    from app.schemas.feedback import FeedbackCreate

    feedback = FeedbackCreate(
        lead_ref="lead-1",
        icp_id="icp-1",
        decision="NOT_FIT",
        reason_codes=["BRAND_NEW_REASON_NOBODY_HAS_SEEN"],
        reviewer_id="manager@example.com",
    )
    assert feedback.reason_codes == ["BRAND_NEW_REASON_NOBODY_HAS_SEEN"]


def test_malformed_reason_code_rejected(client):
    icp = _create_icp(client)
    payload = _feedback_payload(icp_id=icp["id"], decision="NOT_FIT", reason_codes=["not a code"])
    response = client.post("/api/v1/feedback", json=payload)
    assert response.status_code == 422


def test_lowercase_reason_code_rejected(client):
    icp = _create_icp(client)
    payload = _feedback_payload(icp_id=icp["id"], decision="NOT_FIT", reason_codes=["founder_led"])
    response = client.post("/api/v1/feedback", json=payload)
    assert response.status_code == 422


def test_duplicate_reason_codes_rejected(client):
    icp = _create_icp(client)
    payload = _feedback_payload(icp_id=icp["id"], decision="NOT_FIT", reason_codes=["AGENCY", "AGENCY"])
    response = client.post("/api/v1/feedback", json=payload)
    assert response.status_code == 422


def test_non_good_fit_without_reason_code_rejected(client):
    icp = _create_icp(client)
    payload = _feedback_payload(icp_id=icp["id"], decision="NOT_FIT", reason_codes=[])
    response = client.post("/api/v1/feedback", json=payload)
    assert response.status_code == 422


# --- required fields -----------------------------------------------------


def test_missing_lead_ref_rejected(client):
    icp = _create_icp(client)
    payload = _feedback_payload(icp_id=icp["id"])
    del payload["lead_ref"]
    response = client.post("/api/v1/feedback", json=payload)
    assert response.status_code == 422


def test_missing_icp_id_rejected(client):
    payload = _feedback_payload(icp_id="whatever")
    del payload["icp_id"]
    response = client.post("/api/v1/feedback", json=payload)
    assert response.status_code == 422


def test_missing_reviewer_id_rejected(client):
    icp = _create_icp(client)
    payload = _feedback_payload(icp_id=icp["id"])
    del payload["reviewer_id"]
    response = client.post("/api/v1/feedback", json=payload)
    assert response.status_code == 422


def test_blank_lead_ref_rejected(client):
    icp = _create_icp(client)
    payload = _feedback_payload(icp_id=icp["id"], lead_ref="   ")
    response = client.post("/api/v1/feedback", json=payload)
    assert response.status_code == 422


# --- existence checks ----------------------------------------------------


def test_feedback_referencing_unknown_icp_rejected(client):
    payload = _feedback_payload(icp_id="does-not-exist")
    response = client.post("/api/v1/feedback", json=payload)
    assert response.status_code == 404


def test_get_unknown_feedback_returns_404(client):
    response = client.get("/api/v1/feedback/does-not-exist")
    assert response.status_code == 404


# --- ICP/version preservation ---------------------------------------------


def test_icp_version_is_captured_from_the_referenced_icp(client):
    icp_v1 = _create_icp(client, "D2C Skincare")
    icp_v2 = _create_icp(client, "D2C Skincare")  # same name -> version 2
    assert icp_v1["version"] == 1
    assert icp_v2["version"] == 2

    feedback_v1 = client.post(
        "/api/v1/feedback", json=_feedback_payload(icp_id=icp_v1["id"])
    ).json()
    feedback_v2 = client.post(
        "/api/v1/feedback", json=_feedback_payload(icp_id=icp_v2["id"])
    ).json()

    assert feedback_v1["icp_version"] == 1
    assert feedback_v2["icp_version"] == 2
    # submitting feedback against v2 must not retroactively change the v1 record
    refetched_v1 = client.get(f"/api/v1/feedback/{feedback_v1['id']}").json()
    assert refetched_v1["icp_version"] == 1


# --- historical records are not overwritten --------------------------------


def test_multiple_feedback_records_for_same_lead_are_all_preserved(client):
    icp = _create_icp(client)
    first = client.post(
        "/api/v1/feedback", json=_feedback_payload(icp_id=icp["id"], decision="HOLD", reason_codes=["UNCLEAR_BUSINESS_MODEL"])
    ).json()
    second = client.post(
        "/api/v1/feedback", json=_feedback_payload(icp_id=icp["id"], decision="GOOD_FIT", reason_codes=[])
    ).json()

    assert first["id"] != second["id"]

    records = client.get("/api/v1/feedback", params={"lead_ref": "lead-123"}).json()
    ids = {r["id"] for r in records}
    assert {first["id"], second["id"]} <= ids
    # the earlier HOLD decision must still be present, unchanged
    stored_first = next(r for r in records if r["id"] == first["id"])
    assert stored_first["decision"] == "HOLD"


# --- retrieval -------------------------------------------------------------


def test_retrieval_by_lead_ref_only_returns_that_leads_feedback(client):
    icp = _create_icp(client)
    client.post("/api/v1/feedback", json=_feedback_payload(icp_id=icp["id"], lead_ref="lead-A"))
    client.post("/api/v1/feedback", json=_feedback_payload(icp_id=icp["id"], lead_ref="lead-B"))

    records = client.get("/api/v1/feedback", params={"lead_ref": "lead-A"}).json()
    assert len(records) == 1
    assert records[0]["lead_ref"] == "lead-A"


def test_retrieval_by_icp_id_returns_all_its_feedback(client):
    icp_a = _create_icp(client, "ICP A")
    icp_b = _create_icp(client, "ICP B")
    client.post("/api/v1/feedback", json=_feedback_payload(icp_id=icp_a["id"], lead_ref="lead-1"))
    client.post("/api/v1/feedback", json=_feedback_payload(icp_id=icp_a["id"], lead_ref="lead-2"))
    client.post("/api/v1/feedback", json=_feedback_payload(icp_id=icp_b["id"], lead_ref="lead-3"))

    records = client.get("/api/v1/feedback", params={"icp_id": icp_a["id"]}).json()
    assert len(records) == 2
    assert all(r["icp_id"] == icp_a["id"] for r in records)


def test_retrieval_by_icp_id_and_version(client):
    icp_v1 = _create_icp(client, "D2C Skincare")
    icp_v2 = _create_icp(client, "D2C Skincare")
    client.post("/api/v1/feedback", json=_feedback_payload(icp_id=icp_v1["id"], lead_ref="lead-1"))
    client.post("/api/v1/feedback", json=_feedback_payload(icp_id=icp_v2["id"], lead_ref="lead-1"))

    records = client.get(
        "/api/v1/feedback", params={"icp_id": icp_v2["id"], "icp_version": 2}
    ).json()
    assert len(records) == 1
    assert records[0]["icp_version"] == 2


def test_retrieval_requires_at_least_one_filter(client):
    response = client.get("/api/v1/feedback")
    assert response.status_code == 400


def test_retrieval_icp_version_without_icp_id_rejected(client):
    response = client.get("/api/v1/feedback", params={"icp_version": 1})
    assert response.status_code == 400


# --- reviewer/timestamp persistence -----------------------------------


def test_reviewer_id_and_note_and_timestamp_persist(client):
    icp = _create_icp(client)
    payload = _feedback_payload(
        icp_id=icp["id"], reviewer_id="ksrivastava@gravityer.com", reviewer_note="Great fit, fast follow-up."
    )
    created = client.post("/api/v1/feedback", json=payload).json()

    assert created["reviewer_id"] == "ksrivastava@gravityer.com"
    assert created["reviewer_note"] == "Great fit, fast follow-up."
    assert created["created_at"]

    fetched = client.get(f"/api/v1/feedback/{created['id']}").json()
    assert fetched == created


def test_created_at_ordering_across_submissions(client):
    icp = _create_icp(client)
    first = client.post("/api/v1/feedback", json=_feedback_payload(icp_id=icp["id"])).json()
    second = client.post("/api/v1/feedback", json=_feedback_payload(icp_id=icp["id"])).json()

    records = client.get("/api/v1/feedback", params={"lead_ref": "lead-123"}).json()
    assert [r["id"] for r in records] == [first["id"], second["id"]]


# --- feedback cannot modify hard ICP rules ---------------------------------


def test_submitting_feedback_never_changes_the_icp_or_its_canonical_form(client):
    icp = _create_icp(client)
    canonical_before = client.get(f"/api/v1/icps/{icp['id']}/canonical").json()

    for decision, reason_codes in [
        ("NOT_FIT", ["WRONG_GEOGRAPHY_APPARENTLY"]),
        ("WEAK_FIT", ["ENTERPRISE"]),
        ("HOLD", ["UNCLEAR_BUSINESS_MODEL"]),
        ("GOOD_FIT", []),
    ]:
        client.post(
            "/api/v1/feedback",
            json=_feedback_payload(icp_id=icp["id"], decision=decision, reason_codes=reason_codes),
        )

    icp_after = client.get(f"/api/v1/icps/{icp['id']}").json()
    canonical_after = client.get(f"/api/v1/icps/{icp['id']}/canonical").json()

    assert icp_after == icp
    assert canonical_after == canonical_before
