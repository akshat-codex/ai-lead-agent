"""Discovery-mode (fast/safe/hard) + Tavily/Serper COMPANY_DISCOVERY
provider tests.

No live API calls anywhere in this file — every HTTP call is mocked via
respx, using response shapes CONFIRMED via a real, one-off live probe
(2026-09-03) before being written here, never guessed. See
app/providers/tavily.py's own module docstring for the full rationale:
the original "<industry> companies" query reliably surfaced listicle
articles, never real companies, so it was replaced with two query types
that are structurally single-company by construction — DIRECTORY
(Crunchbase/YC profile pages) and HIRING (Lever/Greenhouse job postings,
only when the ICP's allowed_titles names a genuinely job-postable role).

Covers: (1) TavilyCompanyDiscoveryProvider/SerperCompanyDiscoveryProvider
build the right query text per type, extract company name from each
vendor's real confirmed title format, exclude YC category/listing pages,
exclude executive-title-driven hiring queries, and handle auth/rate-limit
errors honestly; (2) both are registered in the provider registry only
when their key is configured, and never mixed with the mock; (3)
app/services/batch_orchestration.py::_providers_allowed_for_mode correctly
filters which providers run per mode, including for provider ids it
doesn't recognize by name (mirroring how a real future provider, or any
of this test suite's own custom test-double providers, must behave
identically to Explorium under Fast mode).
"""
import json

import httpx
import respx

from app.core.config import Settings
from app.providers.contracts import ProviderCapability, ProviderRequest
from app.providers.default_registry import build_default_registry
from app.providers.serper import SerperCompanyDiscoveryProvider
from app.providers.tavily import TavilyCompanyDiscoveryProvider, _is_job_role_title
from app.schemas.batch import DiscoveryMode
from app.services.batch_orchestration import _providers_allowed_for_mode

TAVILY_SEARCH_URL = "https://api.tavily.com/search"
SERPER_SEARCH_URL = "https://google.serper.dev/search"


def _tavily_request(
    industries=("Deep Tech AI",), company_types=(), allowed_titles=(), geography_unrecognized=(), limit=20
) -> ProviderRequest:
    return ProviderRequest(
        capability=ProviderCapability.COMPANY_DISCOVERY,
        query={
            "industries": industries,
            "company_types": company_types,
            "allowed_titles": allowed_titles,
            "geography_unrecognized": geography_unrecognized,
            "limit": limit,
        },
    )


# --- Tavily: DIRECTORY query type (Crunchbase / YC) -------------------------


def _tavily_handler_by_query(canned: dict) -> "callable":
    """Builds a respx side_effect that dispatches on the outgoing
    request's own JSON "query" field rather than call order/count — the
    directory query type always issues TWO primary queries per industry
    term (Crunchbase + YC, see _build_directory_queries) plus one
    homepage-resolution follow-up EACH (see _resolve_homepage_domain), so
    a fixed-order response list is fragile; `canned` maps a substring of
    the query text to the JSON body to return for it, with an empty
    "results"/"organic" fallback for any query not explicitly listed
    (e.g. the YC-branch queries this test isn't exercising)."""

    def _handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        query_text = body.get("query") or body.get("q") or ""
        for needle, response_json in canned.items():
            if needle in query_text:
                return httpx.Response(200, json=response_json)
        return httpx.Response(200, json={"results": [], "organic": []})

    return _handler


@respx.mock
def test_tavily_extracts_a_real_crunchbase_profile_result():
    """Title format confirmed via a real, one-off live probe against
    site:crunchbase.com/organization before this test was written:
    "<Company Name> - Crunchbase Company Profile & Funding"."""
    respx.post(TAVILY_SEARCH_URL).mock(
        side_effect=_tavily_handler_by_query(
            {
                "site:crunchbase.com/organization": {
                    "results": [
                        {
                            "title": "1001 AI - Crunchbase Company Profile & Funding",
                            "url": "https://www.crunchbase.com/organization/1001-ai",
                            "content": "1001 AI builds autonomous coding agents.",
                        }
                    ]
                },
                # No usable homepage-resolution result — this test is
                # about extraction, not resolution (see
                # test_tavily_resolves_and_verifies_a_real_homepage below
                # for the positive resolution case).
            }
        )
    )
    provider = TavilyCompanyDiscoveryProvider(api_key="test-key")
    response = provider.run(_tavily_request())

    assert response.success is True
    assert len(response.data) == 1
    record = response.data[0]
    assert record.name == "1001 AI"
    assert record.external_id == "tavily:crunchbase:1001-ai"
    assert "domain" not in record.attributes
    assert "autonomous coding agents" in record.attributes["description"].lower()


@respx.mock
def test_tavily_extracts_a_real_yc_profile_result():
    """Title format confirmed via a real, one-off live probe against
    site:ycombinator.com/companies before this test was written:
    "<Company>: <tagline> | Y Combinator"."""
    respx.post(TAVILY_SEARCH_URL).mock(
        side_effect=_tavily_handler_by_query(
            {
                "site:ycombinator.com/companies": {
                    "results": [
                        {
                            "title": "Flux Auto: Physical AI for Warehouses and Factories | Y Combinator",
                            "url": "https://www.ycombinator.com/companies/flux-auto",
                            "content": "Flux Auto builds physical AI robots for logistics.",
                        }
                    ]
                },
            }
        )
    )
    provider = TavilyCompanyDiscoveryProvider(api_key="test-key")
    response = provider.run(_tavily_request())

    assert len(response.data) == 1
    record = response.data[0]
    assert record.name == "Flux Auto"
    assert record.external_id == "tavily:yc:flux-auto"
    assert "domain" not in record.attributes


# --- Homepage resolution (cross-provider corroboration fix) -----------------


@respx.mock
def test_tavily_resolves_and_verifies_a_real_homepage():
    """The core audit fix (2026-09-03): a directory candidate's real
    homepage domain IS populated when a follow-up search finds a result
    whose own content genuinely corroborates the candidate's already-
    known description — never accepted on name similarity alone. This
    is what makes cross-provider corroboration with Explorium
    (app/services/company_resolution.py's domain-match merge path)
    possible for a Tavily-sourced candidate at all."""
    respx.post(TAVILY_SEARCH_URL).mock(
        side_effect=_tavily_handler_by_query(
            {
                "site:crunchbase.com/organization": {
                    "results": [
                        {
                            "title": "Soteri Skin - Crunchbase Company Profile & Funding",
                            "url": "https://www.crunchbase.com/organization/soteri-skin",
                            "content": "Soteri Skin is D2C skincare for chronic skin conditions like eczema.",
                        }
                    ]
                },
                '"Soteri Skin" official site': {
                    "results": [
                        {"title": "Soteri Skin - Eczema Free in 4 Weeks", "url": "https://www.linkedin.com/company/soteriskin", "content": "..."},
                        {
                            "title": "Soteri Skin",
                            "url": "https://soteriskin.com",
                            "content": "Soteri Skin makes non-prescription skincare for eczema and chronic skin conditions.",
                        },
                    ]
                },
            }
        )
    )
    provider = TavilyCompanyDiscoveryProvider(api_key="test-key")
    response = provider.run(_tavily_request())

    assert len(response.data) == 1
    record = response.data[0]
    # LinkedIn (a non-homepage host, see _NON_HOMEPAGE_HOST_SUFFIXES) is
    # correctly skipped even though it appears first; the real homepage
    # domain is the one actually accepted.
    assert record.attributes["domain"] == "soteriskin.com"


@respx.mock
def test_tavily_never_accepts_a_homepage_match_on_name_alone_without_content_overlap():
    """The core safety property: company names collide (confirmed live —
    see this module's own "Homepage resolution" section for real
    examples: "Deep Labs" matched 3 unrelated companies). A resolution
    result must share genuine content overlap with the candidate's own
    description, never accepted just because the name matched."""
    respx.post(TAVILY_SEARCH_URL).mock(
        side_effect=_tavily_handler_by_query(
            {
                "site:crunchbase.com/organization": {
                    "results": [
                        {
                            "title": "Deep Labs - Crunchbase Company Profile & Funding",
                            "url": "https://www.crunchbase.com/organization/deep-labs",
                            "content": "Deep Labs is the leader in Large Signal Model technology for enterprise AI.",
                        }
                    ]
                },
                # A completely different, unrelated real business that
                # happens to share the exact same name — no shared
                # vocabulary with the candidate's own description above.
                '"Deep Labs" official site': {
                    "results": [
                        {
                            "title": "Deep Labs | AI-Powered Threat Intelligence & Cyber Defense",
                            "url": "https://www.deepdecision.ai",
                            "content": "Deep Labs provides cyber threat intelligence and defense platforms for security teams.",
                        }
                    ]
                },
            }
        )
    )
    provider = TavilyCompanyDiscoveryProvider(api_key="test-key")
    response = provider.run(_tavily_request())

    assert len(response.data) == 1
    assert "domain" not in response.data[0].attributes


@respx.mock
def test_tavily_homepage_resolution_never_accepts_a_directory_or_social_host_as_the_homepage():
    respx.post(TAVILY_SEARCH_URL).mock(
        side_effect=_tavily_handler_by_query(
            {
                "site:crunchbase.com/organization": {
                    "results": [
                        {
                            "title": "1001 AI - Crunchbase Company Profile & Funding",
                            "url": "https://www.crunchbase.com/organization/1001-ai",
                            "content": "1001 AI builds autonomous coding agents.",
                        }
                    ]
                },
                # Every result in the resolution search is itself a
                # directory/social host — even with strong content
                # overlap, none may ever be accepted AS the homepage.
                '"1001 AI" official site': {
                    "results": [
                        {
                            "title": "1001 AI - Crunchbase Company Profile & Funding",
                            "url": "https://www.crunchbase.com/organization/1001-ai",
                            "content": "1001 AI builds autonomous coding agents.",
                        },
                        {
                            "title": "1001 AI",
                            "url": "https://www.linkedin.com/company/1001-ai",
                            "content": "1001 AI builds autonomous coding agents.",
                        },
                    ]
                },
            }
        )
    )
    provider = TavilyCompanyDiscoveryProvider(api_key="test-key")
    response = provider.run(_tavily_request())

    assert len(response.data) == 1
    assert "domain" not in response.data[0].attributes


def test_verify_homepage_match_requires_at_least_two_shared_significant_words():
    from app.providers.tavily import _verify_homepage_match

    assert _verify_homepage_match(
        "Soteri Skin",
        "Soteri Skin is D2C skincare for chronic skin conditions like eczema.",
        "Soteri Skin",
        "Soteri Skin makes non-prescription skincare for eczema and chronic conditions.",
    ) is True
    # Two SAME-NAMED but unrelated real companies (confirmed live) —
    # their shared name words ("deep", "labs") are excluded from the
    # comparison, and the actual business descriptions share nothing.
    assert _verify_homepage_match(
        "Deep Labs",
        "Deep Labs is the leader in Large Signal Model technology for enterprise AI.",
        "Deep Labs",
        "Deep Labs provides cyber threat intelligence and defense platforms for security teams.",
    ) is False
    assert _verify_homepage_match("Some Co", "", "Some Title", "Some content") is False


# --- live-test regression (2026-10-01): bad domains leaking through ---------
#
# A real Safe-mode batch against a Healthcare ICP (see app/providers/
# tavily.py's own _NON_HOMEPAGE_HOST_SUFFIXES comment) surfaced several
# directory/data-broker sites that were never on the denylist, each wrongly
# accepted as a candidate's own homepage domain. These tests cover both the
# expanded denylist AND the new path-shape guard that catches an UNLISTED
# future data-broker site the same way.


def test_newly_confirmed_data_broker_hosts_are_rejected_as_homepages():
    from app.providers.tavily import _is_homepage_host

    for host in ("leadiq.com", "rocketreach.co", "cbinsights.com", "tracxn.com", "squarepeg.vc", "mapquest.com", "samplefocus.com", "extruct.ai"):
        assert _is_homepage_host(host) is False, host


def test_directory_profile_path_shape_is_rejected_even_for_an_unlisted_host():
    """The path-shape guard must catch the SAME class of bad domain even
    for a hostname that was never explicitly denylisted — this is what
    makes the fix robust against the next unlisted data-broker site,
    rather than only today's confirmed examples."""
    from app.providers.tavily import _looks_like_directory_profile_path

    assert _looks_like_directory_profile_path("/company/some-totally-new-directory-site") is True
    assert _looks_like_directory_profile_path("/companies/acme-inc") is True
    assert _looks_like_directory_profile_path("/organization/acme-inc") is True
    assert _looks_like_directory_profile_path("/profile/acme-inc") is True
    assert _looks_like_directory_profile_path("/people/jane-doe") is True


def test_directory_profile_path_shape_never_rejects_a_real_marketing_path():
    """A legitimate homepage path that merely CONTAINS one of the
    directory-shaped words must still pass — the guard only rejects a
    path that STARTS with that segment."""
    from app.providers.tavily import _looks_like_directory_profile_path

    assert _looks_like_directory_profile_path("/") is False
    assert _looks_like_directory_profile_path("/about-our-company") is False
    assert _looks_like_directory_profile_path("/pricing") is False
    assert _looks_like_directory_profile_path("/en/home") is False


@respx.mock
def test_homepage_resolution_rejects_a_directory_site_even_with_strong_word_overlap():
    """End-to-end reproduction of the live bug: a directory/data-broker
    result that genuinely republishes the company's own description (so
    it WOULD pass _verify_homepage_match's word-overlap check) must still
    be rejected by the path-shape guard, and resolution must fall through
    to no-domain rather than accepting the wrong one."""
    respx.post(TAVILY_SEARCH_URL).mock(
        side_effect=_tavily_handler_by_query(
            {
                "site:crunchbase.com/organization": {
                    "results": [
                        {
                            "title": "1001 AI - Crunchbase Company Profile & Funding",
                            "url": "https://www.crunchbase.com/organization/1001-ai",
                            "content": "1001 AI builds autonomous agents for enterprise workflows.",
                        }
                    ]
                },
                # Homepage-resolution follow-up: a directory site
                # republishing the SAME description (high word overlap) at
                # a profile-shaped path, never 1001 AI's own real homepage.
                "official site": {
                    "results": [
                        {
                            "title": "1001 AI - Company Profile",
                            "url": "https://www.somenewdatabroker.com/company/1001-ai",
                            "content": "1001 AI builds autonomous agents for enterprise workflows, serving enterprise customers.",
                        },
                    ]
                },
            }
        )
    )
    provider = TavilyCompanyDiscoveryProvider(api_key="test-key")
    response = provider.run(_tavily_request())

    assert len(response.data) == 1
    assert "domain" not in response.data[0].attributes


def test_crunchbase_title_suffix_strips_a_truncated_or_varied_trailing_phrase():
    """Live-test regression: real Crunchbase SERP titles sometimes end
    with a truncated "..." or a phrase other than the exact "& Funding"
    the original regex required (confirmed live: "NextGen Healthcare -
    Crunchbase Company Profile & ..." leaked the raw suffix straight into
    the stored company name). The fix matches from "- Crunchbase Company
    Profile" onward regardless of what follows."""
    from app.providers.tavily import _extract_directory_result

    extracted = _extract_directory_result(
        "https://www.crunchbase.com/organization/nextgen-healthcare",
        "NextGen Healthcare - Crunchbase Company Profile & ...",
    )
    assert extracted is not None
    _, name = extracted
    assert name == "NextGen Healthcare"

    extracted_overview = _extract_directory_result(
        "https://www.crunchbase.com/organization/acme-inc",
        "Acme Inc - Crunchbase Company Profile & Overview",
    )
    assert extracted_overview is not None
    _, name_overview = extracted_overview
    assert name_overview == "Acme Inc"


@respx.mock
def test_tavily_excludes_yc_category_listing_pages_never_individual_companies():
    """Live-verified distinction: ycombinator.com/companies/industry/<slug>
    is a CATEGORY roundup ("Deep Learning Startups funded by Y Combinator
    (YC) 2026"), never one company — must never become a candidate,
    exactly the same class of bug the original listicle-article problem
    was."""
    respx.post(TAVILY_SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "Deep Learning Startups funded by Y Combinator (YC) 2026 | Y Combinator",
                        "url": "https://www.ycombinator.com/companies/industry/deep-learning",
                        "content": "...",
                    },
                    {
                        "title": "Flux Auto: Physical AI for Warehouses and Factories | Y Combinator",
                        "url": "https://www.ycombinator.com/companies/flux-auto",
                        "content": "...",
                    },
                ]
            },
        )
    )
    provider = TavilyCompanyDiscoveryProvider(api_key="test-key")
    response = provider.run(_tavily_request())

    assert len(response.data) == 1
    assert response.data[0].name == "Flux Auto"


@respx.mock
def test_tavily_discards_crunchbase_block_pages_confirmed_live_regression():
    """Live-test regression (2026-09-03, D2C ICP): Tavily's crawler is
    sometimes rate-limited/blocked by Crunchbase's own anti-bot layer —
    confirmed live examples of the resulting "content" text: "Sorry, you
    have been blocked. You are unable to access crunchbase.com", "Warning:
    Target URL returned error 403: Forbidden", "Warning: This page maybe
    requiring CAPTCHA". Before this fix, that block-page boilerplate was
    stored as if it were the company's real description — a real result
    quality problem even though hard-rule validation still correctly HELD
    every affected candidate (never a false PASS)."""
    respx.post(TAVILY_SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "Bankr Brand - Crunchbase Company Profile & Funding",
                        "url": "https://www.crunchbase.com/organization/bankr-brand",
                        "content": "Sorry, you have been blocked. You are unable to access crunchbase.com. Why have I been blocked? This website is using a security service to protect itself",
                    },
                    {
                        "title": "Brand Castle - Crunchbase Company Profile & Funding",
                        "url": "https://www.crunchbase.com/organization/brand-castle",
                        "content": "Warning: Target URL returned error 403: Forbidden. Warning: This page maybe requiring CAPTCHA, please make sure you are authorized to access this page",
                    },
                    {
                        "title": "Deep Labs - Crunchbase Company Profile & Funding",
                        "url": "https://www.crunchbase.com/organization/deep-labs",
                        "content": "Deep Labs is an AI infrastructure company.",
                    },
                ]
            },
        )
    )
    provider = TavilyCompanyDiscoveryProvider(api_key="test-key")
    response = provider.run(_tavily_request())

    assert len(response.data) == 1
    assert response.data[0].name == "Deep Labs"


@respx.mock
def test_tavily_directory_queries_use_site_restricted_search_syntax():
    calls = []

    def _handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.content.decode())
        return httpx.Response(200, json={"results": []})

    respx.post(TAVILY_SEARCH_URL).mock(side_effect=_handler)
    provider = TavilyCompanyDiscoveryProvider(api_key="test-key")
    provider.run(_tavily_request(industries=("Deep Tech AI",)))

    assert any("site:crunchbase.com/organization" in c and "Deep Tech AI" in c for c in calls)
    assert any("site:ycombinator.com/companies" in c and "Deep Tech AI" in c for c in calls)
    # One Crunchbase + one YC query per industry term, never the old
    # "<industry> companies" generic phrasing.
    assert len(calls) == 2


def test_tavily_returns_empty_success_when_icp_has_no_industries_or_hiring_signal():
    provider = TavilyCompanyDiscoveryProvider(api_key="test-key")
    response = provider.run(_tavily_request(industries=(), allowed_titles=()))
    assert response.success is True
    assert response.data == ()


@respx.mock
def test_tavily_builds_directory_queries_from_company_types_when_industries_is_empty():
    """Audit finding (2026-09-03): an ICP defined mainly by company_types
    (e.g. "D2C Brand", "PE-backed") with NO industries previously
    produced zero directory queries — the only real gap-filling role this
    provider has (covering terms Explorium's structured taxonomy can't
    resolve) was silently absent for exactly this ICP shape."""
    calls = []

    def _handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.content.decode())
        return httpx.Response(200, json={"results": []})

    respx.post(TAVILY_SEARCH_URL).mock(side_effect=_handler)
    provider = TavilyCompanyDiscoveryProvider(api_key="test-key")
    provider.run(_tavily_request(industries=(), company_types=("D2C Brand",)))

    assert len(calls) == 2
    assert any("site:crunchbase.com/organization" in c and "D2C Brand" in c for c in calls)
    assert any("site:ycombinator.com/companies" in c and "D2C Brand" in c for c in calls)


@respx.mock
def test_tavily_deduplicates_a_term_present_in_both_industries_and_company_types():
    calls = []

    def _handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.content.decode())
        return httpx.Response(200, json={"results": []})

    respx.post(TAVILY_SEARCH_URL).mock(side_effect=_handler)
    provider = TavilyCompanyDiscoveryProvider(api_key="test-key")
    provider.run(_tavily_request(industries=("D2C",), company_types=("D2C",)))

    # One Crunchbase + one YC query for the single distinct term, never
    # duplicated because it appears in both fields.
    assert len(calls) == 2


def test_tavily_query_terms_are_sanitized_against_embedded_quotes_and_length():
    from app.providers.tavily import _sanitize_query_term

    assert _sanitize_query_term('AI "Agents" Startups') == "AI Agents Startups"
    assert _sanitize_query_term("  Deep Tech  ") == "Deep Tech"
    assert len(_sanitize_query_term("x" * 500)) == 100
    assert _sanitize_query_term('"""') == ""


@respx.mock
def test_tavily_a_single_bad_query_never_aborts_the_others_in_the_same_call():
    """Audit finding (2026-09-03): a non-200 response for ONE query
    (e.g. a vendor-side rejection of a malformed/oversized query string)
    previously raised, aborting EVERY other query in the same execute()
    call — a single bad industry term could zero out results for every
    other, well-formed term in a multi-industry ICP."""
    call_count = {"n": 0}

    def _handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        if call_count["n"] == 1:
            return httpx.Response(400, text="bad request")
        return httpx.Response(
            200,
            json={"results": [{"title": "Deep Labs - Crunchbase Company Profile & Funding", "url": "https://www.crunchbase.com/organization/deep-labs", "content": "..."}]},
        )

    respx.post(TAVILY_SEARCH_URL).mock(side_effect=_handler)
    provider = TavilyCompanyDiscoveryProvider(api_key="test-key")
    response = provider.run(_tavily_request(industries=("Bad Term",)))

    assert response.success is True
    assert call_count["n"] >= 2
    assert len(response.data) >= 1


@respx.mock
def test_tavily_auth_failure_reported_honestly_never_fabricates_candidates():
    respx.post(TAVILY_SEARCH_URL).mock(return_value=httpx.Response(401, json={"error": "invalid key"}))
    provider = TavilyCompanyDiscoveryProvider(api_key="bad-key")
    response = provider.run(_tavily_request())
    assert response.success is False
    assert response.error.code == "TAVILY_AUTH_FAILED"
    assert response.data == ()


@respx.mock
def test_tavily_rate_limit_reported_as_retryable():
    respx.post(TAVILY_SEARCH_URL).mock(return_value=httpx.Response(432, json={"error": "limit exceeded"}))
    provider = TavilyCompanyDiscoveryProvider(api_key="test-key")
    response = provider.run(_tavily_request())
    assert response.success is False
    assert response.error.code == "TAVILY_RATE_LIMITED"
    assert response.error.retryable is True


# --- Tavily: HIRING query type (Lever / Greenhouse) --------------------------


@respx.mock
def test_tavily_extracts_a_real_lever_hiring_result():
    """Title format confirmed via a real, one-off live probe against
    site:jobs.lever.co before this test was written: "<Company> - <Role
    Title>", with the company slug also present in the URL path."""
    respx.post(TAVILY_SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "OSARO - Machine Learning Engineer I",
                        "url": "https://jobs.lever.co/osaro/c21a5113-1643-44b4-a72a-bfb795f30f66",
                        "content": "We are looking for a Machine Learning Engineer...",
                    }
                ]
            },
        )
    )
    provider = TavilyCompanyDiscoveryProvider(api_key="test-key")
    response = provider.run(_tavily_request(industries=(), allowed_titles=("Machine Learning Engineer",)))

    assert len(response.data) == 1
    record = response.data[0]
    assert record.name == "OSARO"
    assert record.external_id == "tavily:lever:osaro"
    # The mock reflects the same jobs.lever.co URL back for the homepage-
    # resolution follow-up too (respx.mock(return_value=...) repeats one
    # response) — correctly rejected by _is_homepage_host (Lever is a
    # non-homepage host), so no domain is fabricated from a job posting.
    assert "domain" not in record.attributes


@respx.mock
def test_tavily_extracts_a_real_greenhouse_hiring_result_from_the_url_slug():
    """Confirmed live: Greenhouse titles are less consistent than Lever's
    ("Job Application for Senior Machine Learning Engineer I ... at
    Signifyd") — the company name is reliably extracted from the URL's
    own board slug instead, a firmer signal than the title text."""
    respx.post(TAVILY_SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "Job Application for Senior Machine Learning Engineer I // Senior Machine Learning Engineer II at Signifyd",
                        "url": "https://boards.greenhouse.io/signifyd95/jobs/7733596",
                        "content": "...",
                    }
                ]
            },
        )
    )
    provider = TavilyCompanyDiscoveryProvider(api_key="test-key")
    response = provider.run(_tavily_request(industries=(), allowed_titles=("Machine Learning Engineer",)))

    assert len(response.data) == 1
    assert response.data[0].name == "Signifyd95"
    assert response.data[0].external_id == "tavily:greenhouse:signifyd95"


def test_hiring_queries_never_fire_for_executive_or_decision_maker_titles():
    """The core distinction this design depends on: allowed_titles is the
    ICP's DECISION-MAKER contact filter (Founder, CTO, VP Marketing) —
    none of these are job-postable roles a company would list on an ATS
    board, so none may ever become a Lever/Greenhouse search term."""
    for title in ("Founder", "Co-Founder", "CTO", "VP Marketing", "Chief Marketing Officer", "Director of Marketing", "President"):
        assert _is_job_role_title(title) is False, title
    for title in ("Software Engineer", "AI/ML Researcher", "Machine Learning Engineer", "DevOps Engineer", "Founding Engineer"):
        assert _is_job_role_title(title) is True, title


@respx.mock
def test_tavily_builds_no_hiring_query_when_icp_only_has_executive_titles():
    calls = []

    def _handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.content.decode())
        return httpx.Response(200, json={"results": []})

    respx.post(TAVILY_SEARCH_URL).mock(side_effect=_handler)
    provider = TavilyCompanyDiscoveryProvider(api_key="test-key")
    provider.run(_tavily_request(industries=(), allowed_titles=("Founder", "CTO")))

    assert calls == []


@respx.mock
def test_tavily_hiring_query_uses_site_restricted_lever_and_greenhouse_syntax():
    calls = []

    def _handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.content.decode())
        return httpx.Response(200, json={"results": []})

    respx.post(TAVILY_SEARCH_URL).mock(side_effect=_handler)
    provider = TavilyCompanyDiscoveryProvider(api_key="test-key")
    provider.run(_tavily_request(industries=(), allowed_titles=("AI/ML Researcher",)))

    assert len(calls) == 1
    assert "site:jobs.lever.co" in calls[0]
    assert "site:boards.greenhouse.io" in calls[0]
    assert "AI/ML Researcher" in calls[0]


# --- Serper: mirrors Tavily exactly, different vendor field names -----------


@respx.mock
def test_serper_extracts_a_real_crunchbase_profile_result():
    respx.post(SERPER_SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "organic": [
                    {
                        "title": "Deep Labs - Crunchbase Company Profile & Funding",
                        "link": "https://www.crunchbase.com/organization/deep-labs",
                        "snippet": "Deep Labs is an AI infrastructure company.",
                    }
                ]
            },
        )
    )
    provider = SerperCompanyDiscoveryProvider(api_key="test-key")
    response = provider.run(_tavily_request())

    assert response.success is True
    assert len(response.data) == 1
    record = response.data[0]
    assert record.name == "Deep Labs"
    assert record.external_id == "serper:crunchbase:deep-labs"
    # The mock reflects the same Crunchbase result back for the homepage-
    # resolution follow-up too — correctly rejected by _is_homepage_host,
    # so no domain is fabricated. See tavily.py's equivalent positive/
    # negative resolution tests for the real corroboration behavior.
    assert "domain" not in record.attributes


@respx.mock
def test_serper_resolves_and_verifies_a_real_homepage():
    """Serper's own analogue of test_tavily_resolves_and_verifies_a_real_
    homepage above — same shared verification logic, confirming the fix
    also works through Serper's field-name shape (organic/link/snippet)."""

    def _handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        query_text = body.get("q") or ""
        if "site:crunchbase.com/organization" in query_text:
            return httpx.Response(
                200,
                json={
                    "organic": [
                        {
                            "title": "Soteri Skin - Crunchbase Company Profile & Funding",
                            "link": "https://www.crunchbase.com/organization/soteri-skin",
                            "snippet": "Soteri Skin is D2C skincare for chronic skin conditions like eczema.",
                        }
                    ]
                },
            )
        if '"Soteri Skin" official site' in query_text:
            return httpx.Response(
                200,
                json={
                    "organic": [
                        {
                            "title": "Soteri Skin",
                            "link": "https://soteriskin.com",
                            "snippet": "Soteri Skin makes non-prescription skincare for eczema and chronic skin conditions.",
                        }
                    ]
                },
            )
        return httpx.Response(200, json={"organic": []})

    respx.post(SERPER_SEARCH_URL).mock(side_effect=_handler)
    provider = SerperCompanyDiscoveryProvider(api_key="test-key")
    response = provider.run(_tavily_request())

    assert len(response.data) == 1
    assert response.data[0].attributes["domain"] == "soteriskin.com"


@respx.mock
def test_serper_builds_directory_queries_from_company_types_when_industries_is_empty():
    calls = []

    def _handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.content.decode())
        return httpx.Response(200, json={"organic": []})

    respx.post(SERPER_SEARCH_URL).mock(side_effect=_handler)
    provider = SerperCompanyDiscoveryProvider(api_key="test-key")
    provider.run(_tavily_request(industries=(), company_types=("D2C Brand",)))

    assert len(calls) == 2
    assert any("D2C Brand" in c for c in calls)


@respx.mock
def test_serper_extracts_a_real_lever_hiring_result():
    respx.post(SERPER_SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "organic": [
                    {
                        "title": "Quincus - Machine Learning Engineer",
                        "link": "https://jobs.lever.co/quincus/51f562da-f4ed-45f9-a8e2-4c56b02bae13",
                        "snippet": "...",
                    }
                ]
            },
        )
    )
    provider = SerperCompanyDiscoveryProvider(api_key="test-key")
    response = provider.run(_tavily_request(industries=(), allowed_titles=("Machine Learning Engineer",)))

    assert len(response.data) == 1
    assert response.data[0].name == "Quincus"
    assert response.data[0].external_id == "serper:lever:quincus"


@respx.mock
def test_serper_excludes_yc_category_listing_pages():
    respx.post(SERPER_SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "organic": [
                    {
                        "title": "Hard Tech Startups funded by Y Combinator (YC) 2026 | Y Combinator",
                        "link": "https://www.ycombinator.com/companies/industry/hard-tech",
                        "snippet": "...",
                    }
                ]
            },
        )
    )
    provider = SerperCompanyDiscoveryProvider(api_key="test-key")
    response = provider.run(_tavily_request())
    assert response.data == ()


@respx.mock
def test_serper_discards_crunchbase_block_pages():
    respx.post(SERPER_SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "organic": [
                    {
                        "title": "Bankr Brand - Crunchbase Company Profile & Funding",
                        "link": "https://www.crunchbase.com/organization/bankr-brand",
                        "snippet": "Sorry, you have been blocked. You are unable to access crunchbase.com",
                    },
                    {
                        "title": "Deep Labs - Crunchbase Company Profile & Funding",
                        "link": "https://www.crunchbase.com/organization/deep-labs",
                        "snippet": "Deep Labs is an AI infrastructure company.",
                    },
                ]
            },
        )
    )
    provider = SerperCompanyDiscoveryProvider(api_key="test-key")
    response = provider.run(_tavily_request())
    assert len(response.data) == 1
    assert response.data[0].name == "Deep Labs"


@respx.mock
def test_serper_auth_failure_reported_honestly():
    respx.post(SERPER_SEARCH_URL).mock(return_value=httpx.Response(403, json={"error": "invalid key"}))
    provider = SerperCompanyDiscoveryProvider(api_key="bad-key")
    response = provider.run(_tavily_request())
    assert response.success is False
    assert response.error.code == "SERPER_AUTH_FAILED"
    assert response.data == ()


# --- registry wiring ---------------------------------------------------


def _settings(tavily_api_key=None, serper_api_key=None, explorium_api_key=None) -> Settings:
    return Settings(
        tavily_api_key=tavily_api_key,
        serper_api_key=serper_api_key,
        explorium_api_key=explorium_api_key,
        apollo_api_key=None,
        unipile_api_key=None,
        unipile_dsn=None,
        unipile_account_id=None,
        database_url="sqlite:///:memory:",
    )


def test_neither_key_configured_registers_neither_web_search_provider():
    registry = build_default_registry(_settings())
    provider_ids = {p.provider_id for p in registry.find_by_capability(ProviderCapability.COMPANY_DISCOVERY)}
    assert "tavily-company-discovery-v1" not in provider_ids
    assert "serper-company-discovery-v1" not in provider_ids
    # No real COMPANY_DISCOVERY provider at all -> the mock is still the
    # only one registered, exactly like today for an unconfigured Explorium.
    assert provider_ids == {"mock-company-data-v1"}


def test_tavily_key_alone_registers_only_tavily():
    registry = build_default_registry(_settings(tavily_api_key="t-key"))
    provider_ids = {p.provider_id for p in registry.find_by_capability(ProviderCapability.COMPANY_DISCOVERY)}
    assert provider_ids == {"tavily-company-discovery-v1"}


def test_both_keys_configured_registers_both_and_drops_the_mock():
    registry = build_default_registry(_settings(tavily_api_key="t-key", serper_api_key="s-key"))
    provider_ids = {p.provider_id for p in registry.find_by_capability(ProviderCapability.COMPANY_DISCOVERY)}
    assert provider_ids == {"tavily-company-discovery-v1", "serper-company-discovery-v1"}


def test_tavily_only_also_drops_mock_company_enrichment():
    """Live-test regression (2026-09-03 Safe-mode E2E test): a Safe-mode
    batch with Tavily/Serper configured but NO Explorium key still left
    both mock enrichment providers active, silently stamping fixed fake
    data (industry="Skincare", company_type="D2C", ...) onto real Tavily-
    discovered companies — confirmed live on Xbox Cloud Gaming, iCloud,
    and OneDrive results. COMPANY_ENRICHMENT must be dropped from BOTH
    mocks whenever ANY real COMPANY_DISCOVERY provider is active, not
    only when Explorium specifically is configured."""
    registry = build_default_registry(_settings(tavily_api_key="t-key"))
    enrichment_ids = {p.provider_id for p in registry.find_by_capability(ProviderCapability.COMPANY_ENRICHMENT)}
    assert "mock-company-data-v1" not in enrichment_ids
    assert "mock-company-registry-v1" not in enrichment_ids


def test_serper_only_also_drops_mock_company_enrichment():
    registry = build_default_registry(_settings(serper_api_key="s-key"))
    enrichment_ids = {p.provider_id for p in registry.find_by_capability(ProviderCapability.COMPANY_ENRICHMENT)}
    assert "mock-company-data-v1" not in enrichment_ids
    assert "mock-company-registry-v1" not in enrichment_ids


def test_no_real_discovery_provider_configured_keeps_mock_enrichment_active():
    """The inverse case: with no real COMPANY_DISCOVERY provider at all
    (fully unconfigured / test environment), the mock enrichment
    providers legitimately stay active — this is today's pre-existing,
    correct behavior for an all-mock environment, unaffected by this fix."""
    registry = build_default_registry(_settings())
    enrichment_ids = {p.provider_id for p in registry.find_by_capability(ProviderCapability.COMPANY_ENRICHMENT)}
    assert "mock-company-data-v1" in enrichment_ids
    assert "mock-company-registry-v1" in enrichment_ids


# --- free (no-API-key) SEC EDGAR / Wikidata COMPANY_ENRICHMENT providers ---


def _settings_with_free_enrichment(enabled: bool) -> Settings:
    return Settings(
        apollo_api_key=None,
        unipile_api_key=None,
        unipile_dsn=None,
        unipile_account_id=None,
        enable_free_company_enrichment_providers=enabled,
        database_url="sqlite:///:memory:",
    )


def test_free_enrichment_providers_are_off_by_default():
    """Confirms zero behavior change for every existing environment/test
    that doesn't explicitly opt in — see app/core/config.py::Settings.
    enable_free_company_enrichment_providers's own docstring for why this
    must default to False (tests must never make real network calls)."""
    registry = build_default_registry(_settings_with_free_enrichment(enabled=False))
    enrichment_ids = {p.provider_id for p in registry.find_by_capability(ProviderCapability.COMPANY_ENRICHMENT)}
    assert "sec-edgar-company-enrichment-v1" not in enrichment_ids
    assert "wikidata-company-enrichment-v1" not in enrichment_ids
    # The inverse of the fix below: disabled means the mocks are UNAFFECTED
    # by this feature's existence, exactly like before it was added.
    assert "mock-company-data-v1" in enrichment_ids
    assert "mock-company-registry-v1" in enrichment_ids


def test_enabling_free_enrichment_providers_registers_both_and_drops_the_mocks():
    registry = build_default_registry(_settings_with_free_enrichment(enabled=True))
    enrichment_ids = {p.provider_id for p in registry.find_by_capability(ProviderCapability.COMPANY_ENRICHMENT)}
    assert "sec-edgar-company-enrichment-v1" in enrichment_ids
    assert "wikidata-company-enrichment-v1" in enrichment_ids
    # Same CONFLICT-avoidance reasoning as the Tavily/Serper mock-drop
    # fixes above: a real (even if narrow-coverage) COMPANY_ENRICHMENT
    # source must never run alongside the mocks' fixed fake payload.
    assert "mock-company-data-v1" not in enrichment_ids


# --- Signal Check (Tavily-backed COMPANY_ENRICHMENT) provider gating ---


def _settings_with_signal_check(*, tavily_api_key: str | None, enabled: bool) -> Settings:
    return Settings(
        apollo_api_key=None,
        unipile_api_key=None,
        unipile_dsn=None,
        unipile_account_id=None,
        tavily_api_key=tavily_api_key,
        enable_signal_check_provider=enabled,
        database_url="sqlite:///:memory:",
    )


def test_signal_check_provider_is_off_by_default_even_with_tavily_configured():
    """Confirms zero behavior change for every existing environment/test
    that doesn't explicitly opt in — see app/core/config.py::Settings.
    enable_signal_check_provider's own docstring for why this must default
    to False even when TAVILY_API_KEY is already set."""
    registry = build_default_registry(_settings_with_signal_check(tavily_api_key="t-key", enabled=False))
    enrichment_ids = {p.provider_id for p in registry.find_by_capability(ProviderCapability.COMPANY_ENRICHMENT)}
    assert "signal-check-v1" not in enrichment_ids


def test_signal_check_provider_requires_tavily_key_even_if_flag_is_enabled():
    """The opt-in flag alone is not enough — there is no separate credential
    for this provider, so it must never be registered without Tavily's own
    key configured (it would otherwise crash on every real call)."""
    registry = build_default_registry(_settings_with_signal_check(tavily_api_key=None, enabled=True))
    enrichment_ids = {p.provider_id for p in registry.find_by_capability(ProviderCapability.COMPANY_ENRICHMENT)}
    assert "signal-check-v1" not in enrichment_ids


def test_enabling_signal_check_provider_with_tavily_key_registers_it_and_drops_the_mocks():
    registry = build_default_registry(_settings_with_signal_check(tavily_api_key="t-key", enabled=True))
    enrichment_ids = {p.provider_id for p in registry.find_by_capability(ProviderCapability.COMPANY_ENRICHMENT)}
    assert "signal-check-v1" in enrichment_ids
    # Same CONFLICT-avoidance reasoning as every other real COMPANY_ENRICHMENT
    # source in this file: must never run alongside the mocks' fixed fake payload.
    assert "mock-company-data-v1" not in enrichment_ids
    assert "mock-company-registry-v1" not in enrichment_ids


# --- Tech Stack Detector (free, no-API-key) COMPANY_ENRICHMENT provider ---


def _settings_with_tech_stack_detector(enabled: bool) -> Settings:
    return Settings(
        apollo_api_key=None,
        unipile_api_key=None,
        unipile_dsn=None,
        unipile_account_id=None,
        enable_tech_stack_detector=enabled,
        database_url="sqlite:///:memory:",
    )


def test_tech_stack_detector_is_off_by_default():
    registry = build_default_registry(_settings_with_tech_stack_detector(enabled=False))
    enrichment_ids = {p.provider_id for p in registry.find_by_capability(ProviderCapability.COMPANY_ENRICHMENT)}
    assert "tech-stack-detector-v1" not in enrichment_ids
    assert "mock-company-data-v1" in enrichment_ids
    assert "mock-company-registry-v1" in enrichment_ids


def test_enabling_tech_stack_detector_registers_it_and_drops_the_mocks():
    registry = build_default_registry(_settings_with_tech_stack_detector(enabled=True))
    enrichment_ids = {p.provider_id for p in registry.find_by_capability(ProviderCapability.COMPANY_ENRICHMENT)}
    assert "tech-stack-detector-v1" in enrichment_ids
    assert "mock-company-data-v1" not in enrichment_ids
    assert "mock-company-registry-v1" not in enrichment_ids


def test_explorium_and_both_web_search_providers_can_coexist_for_hard_mode():
    registry = build_default_registry(_settings(explorium_api_key="e-key", tavily_api_key="t-key", serper_api_key="s-key"))
    provider_ids = {p.provider_id for p in registry.find_by_capability(ProviderCapability.COMPANY_DISCOVERY)}
    assert provider_ids == {"explorium-company-discovery-v1", "tavily-company-discovery-v1", "serper-company-discovery-v1"}


# --- discovery_mode provider filtering --------------------------------------


def test_fast_mode_excludes_web_search_providers_but_keeps_everything_else():
    active = ("explorium-company-discovery-v1", "tavily-company-discovery-v1", "serper-company-discovery-v1", "some-future-provider-v1")
    result = _providers_allowed_for_mode(DiscoveryMode.FAST.value, active)
    assert result == ("explorium-company-discovery-v1", "some-future-provider-v1")


def test_fast_mode_keeps_a_provider_it_does_not_recognize_by_name():
    """The exact regression this design fixes: a test double (or a future
    real provider) with an arbitrary provider_id must behave exactly like
    Explorium under Fast mode — Fast mode is defined by EXCLUDING known
    web-search ids, never by an allowlist of recognized ones."""
    active = ("paged-disc", "mock-company-data-v1")
    result = _providers_allowed_for_mode(DiscoveryMode.FAST.value, active)
    assert result == ("paged-disc", "mock-company-data-v1")


def test_safe_mode_keeps_only_web_search_providers():
    active = ("explorium-company-discovery-v1", "tavily-company-discovery-v1", "serper-company-discovery-v1")
    result = _providers_allowed_for_mode(DiscoveryMode.SAFE.value, active)
    assert result == ("tavily-company-discovery-v1", "serper-company-discovery-v1")


def test_safe_mode_with_no_web_search_provider_configured_discovers_nothing():
    active = ("explorium-company-discovery-v1",)
    result = _providers_allowed_for_mode(DiscoveryMode.SAFE.value, active)
    assert result == ()


def test_hard_mode_keeps_every_active_provider_unfiltered():
    active = ("explorium-company-discovery-v1", "tavily-company-discovery-v1", "serper-company-discovery-v1", "paged-disc")
    result = _providers_allowed_for_mode(DiscoveryMode.HARD.value, active)
    assert result == active


def test_unrecognized_mode_value_degrades_to_unfiltered_never_silently_empty():
    active = ("explorium-company-discovery-v1", "tavily-company-discovery-v1")
    result = _providers_allowed_for_mode("not-a-real-mode", active)
    assert result == active


def test_mode_filter_never_reintroduces_an_already_exhausted_provider():
    """_providers_allowed_for_mode only ever narrows an already-computed
    active-provider tuple (see its call site in _run_one_discovery_round,
    applied AFTER the exhaustion filter) — it has no way to add a provider
    back, but this documents the invariant explicitly: an empty input
    always produces an empty output regardless of mode."""
    for mode in (DiscoveryMode.FAST.value, DiscoveryMode.SAFE.value, DiscoveryMode.HARD.value):
        assert _providers_allowed_for_mode(mode, ()) == ()
