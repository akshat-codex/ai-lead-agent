from app.services.company_identity import normalize_company_name, normalize_domain


def test_domain_strips_scheme_and_www():
    assert normalize_domain("https://www.abc.com/") == "abc.com"


def test_domain_bare_form_unchanged():
    assert normalize_domain("abc.com") == "abc.com"


def test_domain_www_without_scheme():
    assert normalize_domain("www.abc.com") == "abc.com"


def test_domain_is_case_insensitive():
    assert normalize_domain("ABC.COM") == "abc.com"


def test_domain_strips_path_and_query():
    assert normalize_domain("https://abc.com/pricing?ref=home") == "abc.com"


def test_domain_none_for_blank_input():
    assert normalize_domain(None) is None
    assert normalize_domain("") is None
    assert normalize_domain("   ") is None


def test_name_strips_inc_suffix():
    assert normalize_company_name("ABC Inc.") == "abc"


def test_name_strips_comma_inc_suffix():
    assert normalize_company_name("ABC, Inc.") == "abc"


def test_name_strips_corporation_suffix():
    assert normalize_company_name("ABC Corporation") == "abc"


def test_name_is_case_insensitive():
    assert normalize_company_name("abc corporation") == normalize_company_name("ABC CORPORATION")


def test_name_none_for_blank_input():
    assert normalize_company_name(None) is None
    assert normalize_company_name("") is None
