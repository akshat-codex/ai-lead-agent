def _valid_payload(name: str = "D2C Skincare") -> dict:
    return {
        "name": name,
        "hard_rules": {
            "industry": ["Skincare", "Beauty"],
            "geography": ["United States", "Canada"],
            "min_employees": 10,
            "max_employees": 200,
            "allowed_titles": ["Head of Growth", "CMO"],
            "company_type": ["D2C"],
            "exclusions": ["Wholesale-only brands"],
            "custom_rules": [{"label": "Founded after 2015", "description": "Company founded after 2015."}],
        },
        "soft_preferences": {
            "business_model_preferences": ["Subscription"],
            "commercial_signals": ["Recent funding round"],
            "growth_signals": ["Hiring for growth roles"],
            "marketing_signals": ["Active on Instagram/TikTok"],
            "other_preferences": [],
        },
    }


def test_create_icp_returns_version_one(client):
    response = client.post("/api/v1/icps", json=_valid_payload())
    assert response.status_code == 201

    body = response.json()
    assert body["name"] == "D2C Skincare"
    assert body["version"] == 1
    assert "id" in body and body["id"]


def test_hard_and_soft_data_stay_separated(client):
    response = client.post("/api/v1/icps", json=_valid_payload())
    body = response.json()

    assert "hard_rules" in body and "soft_preferences" in body
    assert "business_model_preferences" not in body["hard_rules"]
    assert "industry" not in body["soft_preferences"]
    assert body["hard_rules"]["industry"] == ["Skincare", "Beauty"]
    assert body["soft_preferences"]["business_model_preferences"] == ["Subscription"]


def test_save_then_load_round_trips(client):
    created = client.post("/api/v1/icps", json=_valid_payload()).json()

    fetched = client.get(f"/api/v1/icps/{created['id']}")
    assert fetched.status_code == 200
    assert fetched.json() == created


def test_get_unknown_icp_returns_404(client):
    response = client.get("/api/v1/icps/does-not-exist")
    assert response.status_code == 404


def test_saving_same_name_creates_next_version(client):
    first = client.post("/api/v1/icps", json=_valid_payload("D2C Skincare")).json()
    second = client.post("/api/v1/icps", json=_valid_payload("D2C Skincare")).json()
    third = client.post("/api/v1/icps", json=_valid_payload("d2c skincare")).json()  # case-insensitive match

    assert first["version"] == 1
    assert second["version"] == 2
    assert third["version"] == 3
    assert first["id"] != second["id"] != third["id"]


def test_list_icps_groups_versions_by_name(client):
    client.post("/api/v1/icps", json=_valid_payload("D2C Skincare"))
    client.post("/api/v1/icps", json=_valid_payload("D2C Skincare"))
    client.post("/api/v1/icps", json=_valid_payload("B2B SaaS"))

    response = client.get("/api/v1/icps")
    assert response.status_code == 200

    body = response.json()
    assert len(body) == 3
    skincare_versions = sorted(item["version"] for item in body if item["name"] == "D2C Skincare")
    assert skincare_versions == [1, 2]
    assert any(item["name"] == "B2B SaaS" for item in body)


def test_rejects_blank_name(client):
    payload = _valid_payload()
    payload["name"] = "   "
    response = client.post("/api/v1/icps", json=payload)
    assert response.status_code == 422


def test_rejects_min_employees_greater_than_max(client):
    payload = _valid_payload()
    payload["hard_rules"]["min_employees"] = 500
    payload["hard_rules"]["max_employees"] = 50
    response = client.post("/api/v1/icps", json=payload)
    assert response.status_code == 422


def test_rejects_completely_empty_icp(client):
    payload = {
        "name": "Empty ICP",
        "hard_rules": {},
        "soft_preferences": {},
    }
    response = client.post("/api/v1/icps", json=payload)
    assert response.status_code == 422


def test_rejects_duplicate_geography_entries(client):
    payload = _valid_payload()
    payload["hard_rules"]["geography"] = ["United States", "united states"]
    response = client.post("/api/v1/icps", json=payload)
    assert response.status_code == 422


def test_rejects_contradictory_exclusion(client):
    payload = _valid_payload()
    payload["hard_rules"]["industry"] = ["Healthcare"]
    payload["hard_rules"]["exclusions"] = ["healthcare"]
    response = client.post("/api/v1/icps", json=payload)
    assert response.status_code == 422


def test_rejects_custom_rule_with_empty_label(client):
    payload = _valid_payload()
    payload["hard_rules"]["custom_rules"] = [{"label": "", "description": "Something"}]
    response = client.post("/api/v1/icps", json=payload)
    assert response.status_code == 422


def test_rejects_custom_rule_with_empty_description(client):
    payload = _valid_payload()
    payload["hard_rules"]["custom_rules"] = [{"label": "Some rule", "description": ""}]
    response = client.post("/api/v1/icps", json=payload)
    assert response.status_code == 422


def test_canonical_endpoint_returns_normalized_icp_tied_to_source_version(client):
    payload = _valid_payload()
    payload["hard_rules"]["geography"] = ["US", "United States", "UK"]
    created = client.post("/api/v1/icps", json=payload).json()

    response = client.get(f"/api/v1/icps/{created['id']}/canonical")
    assert response.status_code == 200

    body = response.json()
    assert body["icp_id"] == created["id"]
    assert body["version"] == created["version"]
    codes = {entry["code"] for entry in body["hard_rules"]["geography"]["countries"]}
    assert codes == {"US", "GB"}
    assert body["hard_rules"]["geography"]["unrecognized"] == []
    assert body["soft_preferences"]["business_models"] == ["Subscription"]


def test_canonical_endpoint_404_for_unknown_icp(client):
    response = client.get("/api/v1/icps/does-not-exist/canonical")
    assert response.status_code == 404


def test_evaluate_endpoint_returns_pass_for_matching_candidate(client):
    created = client.post("/api/v1/icps", json=_valid_payload()).json()

    candidate = {
        "industry": "Skincare",
        "geography": "United States",
        "employee_count": 50,
        "title": "CMO",
        "company_type": "D2C",
        "custom_rule_results": {"Founded after 2015": True},
    }
    response = client.post(f"/api/v1/icps/{created['id']}/evaluate", json=candidate)
    assert response.status_code == 200

    body = response.json()
    assert body["overall_result"] == "PASS"
    assert body["icp_id"] == created["id"]
    assert body["failed_rules"] == []
    assert body["unresolved_rules"] == []


def test_evaluate_endpoint_returns_fail_for_undersized_candidate(client):
    created = client.post("/api/v1/icps", json=_valid_payload()).json()

    candidate = {
        "industry": "Skincare",
        "geography": "United States",
        "employee_count": 2,
        "title": "CMO",
        "company_type": "D2C",
        "custom_rule_results": {"Founded after 2015": True},
    }
    response = client.post(f"/api/v1/icps/{created['id']}/evaluate", json=candidate)
    assert response.status_code == 200

    body = response.json()
    assert body["overall_result"] == "FAIL"
    assert "EMPLOYEE_TOO_SMALL" in body["reason_codes"]


def test_evaluate_endpoint_returns_hold_for_unknown_candidate_fields(client):
    created = client.post("/api/v1/icps", json=_valid_payload()).json()

    response = client.post(f"/api/v1/icps/{created['id']}/evaluate", json={})
    assert response.status_code == 200

    body = response.json()
    assert body["overall_result"] == "HOLD"
    assert body["failed_rules"] == []
    assert len(body["unresolved_rules"]) > 0


def test_evaluate_endpoint_404_for_unknown_icp(client):
    response = client.post("/api/v1/icps/does-not-exist/evaluate", json={})
    assert response.status_code == 404


def _filters_payload(name: str = "D2C via filters") -> dict:
    return {
        "name": name,
        "filters": [
            {"key": "industry", "operator": "in", "value": ["Skincare", "Beauty"], "label": None},
            {"key": "geography", "operator": "in", "value": ["United States", "Canada"], "label": None},
            {"key": "employee_range", "operator": "range", "value": {"min": 10, "max": 200}, "label": None},
            {"key": "allowed_titles", "operator": "in", "value": ["Head of Growth", "CMO"], "label": None},
            {"key": "company_type", "operator": "in", "value": ["D2C"], "label": None},
        ],
    }


def test_create_icp_with_filters_only_returns_201(client):
    response = client.post("/api/v1/icps", json=_filters_payload())
    assert response.status_code == 201

    body = response.json()
    assert body["hard_rules"]["industry"] == ["Skincare", "Beauty"]
    assert body["hard_rules"]["geography"] == ["United States", "Canada"]
    assert body["hard_rules"]["min_employees"] == 10
    assert body["hard_rules"]["max_employees"] == 200
    assert len(body["filters"]) == 5


def test_filters_and_direct_hard_rules_produce_equivalent_canonical_output(client):
    via_filters = client.post("/api/v1/icps", json=_filters_payload("Parity via filters")).json()

    direct_payload = _valid_payload("Parity via hard_rules")
    direct_payload["hard_rules"]["industry"] = ["Skincare", "Beauty"]
    direct_payload["hard_rules"]["geography"] = ["United States", "Canada"]
    direct_payload["hard_rules"]["min_employees"] = 10
    direct_payload["hard_rules"]["max_employees"] = 200
    direct_payload["hard_rules"]["allowed_titles"] = ["Head of Growth", "CMO"]
    direct_payload["hard_rules"]["company_type"] = ["D2C"]
    direct_payload["hard_rules"]["exclusions"] = []
    direct_payload["hard_rules"]["custom_rules"] = []
    direct_payload["soft_preferences"] = {
        "business_model_preferences": [],
        "commercial_signals": [],
        "growth_signals": [],
        "marketing_signals": [],
        "other_preferences": [],
    }
    via_hard_rules = client.post("/api/v1/icps", json=direct_payload).json()

    via_filters_canonical = client.get(f"/api/v1/icps/{via_filters['id']}/canonical").json()
    via_hard_rules_canonical = client.get(f"/api/v1/icps/{via_hard_rules['id']}/canonical").json()

    assert via_filters_canonical["hard_rules"] == via_hard_rules_canonical["hard_rules"]


def test_filters_with_unknown_key_returns_422(client):
    payload = _filters_payload()
    payload["filters"].append({"key": "not_a_real_filter", "operator": "in", "value": ["x"], "label": None})
    response = client.post("/api/v1/icps", json=payload)
    assert response.status_code == 422


def test_filters_with_disallowed_operator_returns_422(client):
    payload = _filters_payload()
    payload["filters"] = [{"key": "geography", "operator": "equals", "value": "US", "label": None}]
    response = client.post("/api/v1/icps", json=payload)
    assert response.status_code == 422


def test_filters_and_hard_rules_both_supplied_returns_422(client):
    payload = _filters_payload()
    payload["hard_rules"] = _valid_payload()["hard_rules"]
    payload["soft_preferences"] = _valid_payload()["soft_preferences"]
    response = client.post("/api/v1/icps", json=payload)
    assert response.status_code == 422


def test_custom_filter_without_label_returns_422(client):
    payload = _filters_payload()
    payload["filters"] = [{"key": "custom", "operator": "contains", "value": "some requirement", "label": None}]
    response = client.post("/api/v1/icps", json=payload)
    assert response.status_code == 422


def test_custom_filter_with_label_roundtrips_into_custom_rules(client):
    payload = _filters_payload()
    payload["filters"] = [
        {"key": "custom", "operator": "contains", "value": "Must use Shopify", "label": "Uses Shopify"}
    ]
    response = client.post("/api/v1/icps", json=payload)
    assert response.status_code == 201

    body = response.json()
    assert len(body["hard_rules"]["custom_rules"]) == 1
    assert body["hard_rules"]["custom_rules"][0]["label"] == "Uses Shopify"


def test_soft_category_non_field_backed_filter_roundtrips_into_other_preferences(client):
    payload = _filters_payload()
    payload["filters"] = [
        {"key": "industry", "operator": "in", "value": ["D2C"], "label": None},
        {"key": "funding", "operator": "in", "value": ["Recently raised"], "label": None},
    ]
    response = client.post("/api/v1/icps", json=payload)
    assert response.status_code == 201

    body = response.json()
    assert len(body["soft_preferences"]["other_preferences"]) == 1


def test_get_filter_catalog_returns_known_and_extended_filters(client):
    response = client.get("/api/v1/icps/filter-catalog")
    assert response.status_code == 200

    body = response.json()
    keys = {d["key"] for d in body}
    for known in ("industry", "geography", "employee_range", "allowed_titles", "company_type", "exclusions"):
        assert known in keys
        definition = next(d for d in body if d["key"] == known)
        assert definition["has_specialized_ui"] is True
    for extra in ("funding", "hiring", "revenue", "technology", "website", "custom"):
        assert extra in keys


def test_filter_catalog_route_does_not_misroute_into_icp_id_handler(client):
    """Route-ordering regression: /filter-catalog must resolve to the catalog
    endpoint, not be swallowed by GET /{icp_id}."""
    response = client.get("/api/v1/icps/filter-catalog")
    assert response.status_code == 200
    assert isinstance(response.json(), list)
