from app.services.person_identity import (
    extract_linkedin_identifier,
    normalize_linkedin_identifier,
    normalize_person_name,
)


def test_name_lowercases_and_cleans_whitespace():
    assert normalize_person_name("  Jane   Testperson ") == "jane testperson"


def test_name_none_for_blank_input():
    assert normalize_person_name(None) is None
    assert normalize_person_name("") is None


def test_linkedin_strips_scheme_and_www_and_trailing_slash():
    assert normalize_linkedin_identifier("https://www.linkedin.com/in/johnsmith/") == "in/johnsmith"


def test_linkedin_bare_handle_unchanged():
    assert normalize_linkedin_identifier("in/johnsmith") == "in/johnsmith"


def test_linkedin_none_for_blank_input():
    assert normalize_linkedin_identifier(None) is None
    assert normalize_linkedin_identifier("") is None


def test_extract_linkedin_identifier_checks_both_conventional_keys():
    assert extract_linkedin_identifier({"linkedin_id": "in/janedoe"}) == "in/janedoe"
    assert extract_linkedin_identifier({"linkedin_url": "https://linkedin.com/in/janedoe"}) == "in/janedoe"
    assert extract_linkedin_identifier({}) is None
