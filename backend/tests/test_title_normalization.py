"""P3 fix — title-comparison normalization regression tests.

Root cause: app/services/hard_rule_engine.py's allowed_titles rule used
plain exact-string matching (clean+lowercase only), so a real title variant
like "VP Marketing" never matched an ICP's "Vice President, Marketing" —
not because the person genuinely lacked the role, but because the SAME
role was spelled differently. normalize_title_for_comparison closes this
with a small, fixed abbreviation-expansion table plus punctuation/word-order
normalization — never fuzzy or semantic matching, and never turning an
uncertain/unknown title into a match.
"""
from app.services.icp_normalization import normalize_title_for_comparison


# --- abbreviation expansion -----------------------------------------------


def test_vp_expands_to_vice_president():
    assert normalize_title_for_comparison("VP Marketing") == normalize_title_for_comparison("Vice President Marketing")


def test_cmo_expands_to_chief_marketing_officer():
    assert normalize_title_for_comparison("CMO") == normalize_title_for_comparison("Chief Marketing Officer")


def test_all_c_suite_abbreviations_expand():
    pairs = [
        ("CEO", "Chief Executive Officer"),
        ("CFO", "Chief Financial Officer"),
        ("CTO", "Chief Technology Officer"),
        ("COO", "Chief Operating Officer"),
        ("CPO", "Chief Product Officer"),
        ("CRO", "Chief Revenue Officer"),
        ("CIO", "Chief Information Officer"),
    ]
    for abbrev, full in pairs:
        assert normalize_title_for_comparison(abbrev) == normalize_title_for_comparison(full), f"{abbrev} vs {full}"


def test_svp_and_evp_are_distinct_from_plain_vp():
    """Different real seniority levels must never be merged."""
    vp = normalize_title_for_comparison("VP Sales")
    svp = normalize_title_for_comparison("SVP Sales")
    evp = normalize_title_for_comparison("EVP Sales")
    assert vp != svp
    assert vp != evp
    assert svp != evp
    assert svp == normalize_title_for_comparison("Senior Vice President Sales")
    assert evp == normalize_title_for_comparison("Executive Vice President Sales")


def test_director_and_manager_abbreviations():
    assert normalize_title_for_comparison("Dir of Marketing") == normalize_title_for_comparison("Director of Marketing")
    assert normalize_title_for_comparison("Mgr, Sales") == normalize_title_for_comparison("Manager Sales")


# --- punctuation / word order --------------------------------------------


def test_comma_and_word_order_do_not_matter():
    assert normalize_title_for_comparison("VP Marketing") == normalize_title_for_comparison("Vice President, Marketing")
    assert normalize_title_for_comparison("Marketing VP") == normalize_title_for_comparison("VP Marketing")


def test_of_connective_is_stripped():
    assert normalize_title_for_comparison("VP of Marketing") == normalize_title_for_comparison("VP Marketing")


def test_trailing_period_on_abbreviation_is_handled():
    assert normalize_title_for_comparison("Sr. Manager") == normalize_title_for_comparison("Senior Manager")


# --- what must NOT match (still exact-content matching, never fuzzy) -----


def test_a_genuinely_different_role_never_matches():
    assert normalize_title_for_comparison("VP Marketing") != normalize_title_for_comparison("VP Sales")


def test_extra_distinguishing_word_never_matches():
    """A real content difference (not an abbreviation/punctuation/order
    artifact) must still produce a different key — this is not fuzzy or
    substring matching."""
    assert normalize_title_for_comparison("VP Marketing") != normalize_title_for_comparison("VP Marketing Operations")


def test_semantically_similar_but_different_titles_never_match():
    """Head of Growth is NOT folded into Growth Director — that would be
    an organizational-equivalence guess, not a pure abbreviation
    expansion, and is deliberately out of scope."""
    assert normalize_title_for_comparison("Head of Growth") != normalize_title_for_comparison("Growth Director")


def test_empty_string_normalizes_to_empty_tuple():
    assert normalize_title_for_comparison("") == ()
    assert normalize_title_for_comparison("   ") == ()


def test_result_is_a_sorted_tuple_of_strings():
    result = normalize_title_for_comparison("VP Marketing")
    assert result == tuple(sorted(result))
    assert all(isinstance(t, str) for t in result)
