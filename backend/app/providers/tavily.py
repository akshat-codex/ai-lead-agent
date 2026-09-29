"""Tavily web-search COMPANY_DISCOVERY provider.

Explorium's discovery is a fixed-taxonomy database lookup (linkedin_category/
naics_category), which structurally cannot resolve industry terms outside
its own taxonomy — confirmed live and documented in app/providers/
explorium.py's own module docstring: "FMCG, D2C Brands, OTT Platforms, and
Microdrama Companies all return zero results from Explorium's own live
autocomplete endpoint." This module exists specifically to cover that gap.

Live-test finding that shaped this module's design (2026-09-03): the
naive, obvious query — "<industry term> companies" — reliably surfaces
roundup/listicle articles ("76 Top SaaS Companies to Know in 2026") and
glossary pages, almost never a real company's own site, because that
exact phrase is what content-marketing article titles are written to
rank for. Filtering results after the fact (by title shape) caught the
most blatant cases but could never fully solve this — it was fighting
the query's own structure, not the actual problem.

The fix (confirmed via live test queries before being implemented, not
guessed): route around the ambiguity entirely by querying content that is
STRUCTURALLY single-company by construction — see
TavilyCompanyDiscoveryProvider's own docstring for the two query types
(DIRECTORY: individual Crunchbase/YC company-profile pages; HIRING:
individual Lever/Greenhouse job postings, keyed off the ICP's own
allowed_titles when it names a genuinely job-postable role) — rather than
asking the search engine to distinguish "a company's homepage" from "an
article about companies" the way the old generic query did.

Tavily's API (https://tavily.com) is a search endpoint purpose-built for
LLM/agent consumption — it returns a title, url, and content snippet per
result, not raw HTML, which is what makes reliable company-name
extraction possible without a scraping/parsing layer. Neither query type's
OWN result ever carries a candidate's real homepage domain
(Crunchbase/YC/Lever/Greenhouse never echo it in the SERP title/URL) — see
the "Homepage resolution" section below for why that mattered enough to
fix, and how: a deliberate SECOND search per candidate, verified against
the candidate's own already-known description before a domain is ever
accepted (never on name similarity alone — company names collide,
confirmed live). Audit finding (2026-09-03) that motivated this: without
it, a Tavily/Serper candidate for the SAME real company an Explorium
sighting already found could never be recognized as the same company
(app/services/company_resolution.py's only cross-provider merge path is
an exact domain match), silently defeating Hard mode's entire premise of
corroborating across all 3 providers.

Trust discipline: exactly like app/providers/hermes.py, this is an honest,
UNVERIFIED web-research sighting, never a structured taxonomy match. This
provider's id is deliberately never added to app/services/evidence_engine.py's
_TRUSTED_STRUCTURED_PROVIDERS — a lone Tavily sighting for industry/country
never reaches SUPPORTED_STRUCTURED, only SUPPORTED once corroborated by a
second independent record (from any provider), mirroring Hermes exactly.
Never invents a taxonomy relationship, never guesses a company's industry
beyond what the search result's own text says.

The API key is supplied at construction (read once, from Settings, by
app/providers/default_registry.py) and never read from the environment
inside execute(), never included in any ProviderResponse field, never
logged.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

import httpx

from app.providers.base import ProviderAdapter
from app.providers.contracts import (
    NormalizedRecord,
    ProviderCapability,
    ProviderError,
    ProviderRequest,
    ProviderResponse,
    SourceMetadata,
)
from app.services.company_identity import normalize_domain

logger = logging.getLogger(__name__)

TAVILY_AUTH_FAILED = "TAVILY_AUTH_FAILED"
TAVILY_RATE_LIMITED = "TAVILY_RATE_LIMITED"

# Splits a SERP title on its first separator — "Company Name | Tagline",
# "Company Name - What we do" — used only by the Lever hiring-result
# extractor (see _extract_hiring_result), whose confirmed-live title
# format is "<Company> - <Role Title>".
_TITLE_SEPARATOR_RE = re.compile(r"\s*[|–—-]\s*")

# A hard ceiling on how much of an arbitrary ICP term's text is ever
# interpolated into a query string — an ICP field is user-authored (or
# AI-expanded) free text with no length contract, and query APIs
# generally have their own undocumented limits; this bounds our own side
# of that regardless, rather than relying on the vendor to reject
# gracefully. Comfortably longer than any real industry/role phrase.
_MAX_QUERY_TERM_LENGTH = 100


def _sanitize_query_term(term: str) -> str:
    """Makes an arbitrary ICP term safe to interpolate inside a
    double-quoted phrase in a search query string. A literal `"` in the
    term (plausible from pasted listing text or an AI-expanded phrase)
    would otherwise prematurely close the quoted phrase and silently
    change what the query actually searches for — e.g. a term like
    `AI "Agents" Startups` would un-scope the phrase match entirely
    rather than erroring, a correctness bug that would never surface as
    a visible failure. Quotes are stripped (not escaped — Tavily/Serper's
    query syntax has no documented escape sequence), and the term is
    truncated to _MAX_QUERY_TERM_LENGTH. Never raises — arbitrary input
    always produces SOME safe, boundable query text."""
    return term.replace('"', "").strip()[:_MAX_QUERY_TERM_LENGTH]


def _host_of(url: str) -> str | None:
    match = re.match(r"^[a-z][a-z0-9+.-]*://([^/]+)", url.strip().lower())
    if not match:
        return None
    host = match.group(1).split(":")[0]
    return host[4:] if host.startswith("www.") else host


# allowed_titles (see app/schemas/candidate_company.py's own docstring on
# why this field reaches here at all) is the ICP's DECISION-MAKER contact
# title filter — "Founder", "CTO", "VP Marketing" — never a job-posting
# search term a company would actually publish on an ATS board (no
# company posts a job listing titled "Founder"). Only a genuinely
# job-postable, individual-contributor-shaped role (e.g. "Software
# Engineer", "AI/ML Researcher", "DevOps Engineer") is used for hiring-
# signal queries — executive/founder-level titles are excluded here,
# never sent to Lever/Greenhouse as a search term, since doing so would
# either return zero results or (worse) misleadingly return unrelated
# postings that happen to share a word.
_EXECUTIVE_TITLE_RE = re.compile(
    r"\b(?:founder|co-founder|chief|c[a-z]o|vp|vice president|president|owner|principal|partner|director)\b",
    re.IGNORECASE,
)


def _is_job_role_title(title: str) -> bool:
    return not _EXECUTIVE_TITLE_RE.search(title)


# ycombinator.com/companies/industry/<slug>[/<location>] is a CATEGORY
# listing page (confirmed live: "Deep Learning Startups funded by Y
# Combinator (YC) 2026" — a roundup, not one company), never an individual
# company profile — those are ycombinator.com/companies/<slug> with no
# "/industry/" segment. crunchbase.com/organization/<slug> profile paths
# have no equivalent category-listing collision confirmed live, so this
# check is YC-specific by construction, not a generic path pattern.
_YC_CATEGORY_LISTING_RE = re.compile(r"^/companies/industry(?:/|$)", re.IGNORECASE)

# "<Company Name> - Crunchbase Company Profile & Funding" — confirmed live,
# the exact, consistent Crunchbase SERP title format.
_CRUNCHBASE_TITLE_SUFFIX_RE = re.compile(r"\s*-\s*Crunchbase Company Profile(?:\s*&\s*Funding)?\s*$", re.IGNORECASE)
# "<Company>: <tagline> | Y Combinator" or "<Company> - Y Combinator" —
# confirmed live (e.g. "Flux Auto: Physical AI for Warehouses and
# Factories | Y Combinator").
_YC_TITLE_SUFFIX_RE = re.compile(r"\s*[|\-]\s*Y Combinator\s*$", re.IGNORECASE)


def _url_path(url: str) -> str:
    match = re.match(r"^[a-z][a-z0-9+.-]*://[^/]+(/.*)?$", url.strip(), re.IGNORECASE)
    return (match.group(1) or "") if match else ""


# Confirmed live (2026-09-03): Tavily's crawler is sometimes rate-limited/
# blocked by Crunchbase itself (a real anti-bot layer — see e.g.
# scrapfly.io's own documentation of Crunchbase's Cloudflare protection),
# and when that happens the search result's "content" field is
# Crunchbase's OWN block/CAPTCHA page text, not the company's real
# profile content — confirmed live examples: "Sorry, you have been
# blocked. You are unable to access crunchbase.com", "Warning: Target
# URL returned error 403: Forbidden", "Warning: This page maybe
# requiring CAPTCHA". This is a mechanical, vendor-error signal, never a
# business-relevance judgment — a result whose title/URL still parses as
# a valid company profile is discarded ONLY when its own content field
# is unmistakably Crunchbase's block boilerplate, not a real description.
_CRUNCHBASE_BLOCK_CONTENT_RE = re.compile(
    r"you have been blocked|unable to access crunchbase\.com|error 403[:\s]|forbidden|requiring captcha",
    re.IGNORECASE,
)


def _is_crunchbase_block_page(content: str | None) -> bool:
    return bool(content) and bool(_CRUNCHBASE_BLOCK_CONTENT_RE.search(content))


def _extract_directory_result(url: str, title: str) -> tuple[str, str] | None:
    """Returns (external_id_suffix, company_name) for a directory-profile
    result, or None when the URL is a category/listing page rather than an
    individual company profile — see _YC_CATEGORY_LISTING_RE's own
    docstring. The company's real homepage domain is NOT knowable from
    THIS result alone (Crunchbase/YC never echo it in the SERP title/URL)
    — this function itself never returns one, only a directory-scoped
    external_id; a caller wanting a domain must separately call
    _resolve_homepage_domain (see this module's "Homepage resolution"
    section) with the name this function returns."""
    host = _host_of(url)
    if not host:
        return None
    path = _url_path(url)
    if host == "ycombinator.com" or host.endswith(".ycombinator.com"):
        if _YC_CATEGORY_LISTING_RE.match(path):
            return None
        slug_match = re.match(r"^/companies/([^/?#]+)/?$", path)
        if not slug_match:
            return None
        name = _YC_TITLE_SUFFIX_RE.sub("", title).strip()
        # A title like "Flux Auto: Physical AI for Warehouses and
        # Factories" still has a tagline after the company name — take
        # only the part before the first ":" or "|", same discipline as
        # _company_name_from_title, never inventing a shorter name than
        # what a clean separator actually indicates.
        name = re.split(r"[:|]", name, maxsplit=1)[0].strip() or slug_match.group(1)
        return f"yc:{slug_match.group(1)}", name
    if host == "crunchbase.com" or host.endswith(".crunchbase.com"):
        slug_match = re.match(r"^/organization/([^/?#]+)/?$", path)
        if not slug_match:
            return None
        name = _CRUNCHBASE_TITLE_SUFFIX_RE.sub("", title).strip() or slug_match.group(1)
        return f"crunchbase:{slug_match.group(1)}", name
    return None


def _extract_hiring_result(url: str, title: str) -> tuple[str, str] | None:
    """Returns (external_id_suffix, company_name) for a hiring-signal
    (Lever/Greenhouse) job-posting result. Same "no real homepage domain
    knowable from this result" constraint as directory results — see
    _extract_directory_result's own docstring."""
    host = _host_of(url)
    if not host:
        return None
    path = _url_path(url)
    if host == "jobs.lever.co" or host.endswith(".jobs.lever.co"):
        slug_match = re.match(r"^/([^/?#]+)/", path)
        if not slug_match:
            return None
        company_slug = slug_match.group(1)
        # "OSARO - Machine Learning Engineer I" — confirmed live Lever
        # title format: "<Company> - <Role Title>".
        name = _TITLE_SEPARATOR_RE.split(title.strip(), maxsplit=1)[0].strip() or company_slug
        return f"lever:{company_slug}", name
    if host == "boards.greenhouse.io" or host.endswith(".boards.greenhouse.io"):
        slug_match = re.match(r"^/([^/?#]+)/", path)
        if not slug_match:
            return None
        company_slug = slug_match.group(1)
        # Greenhouse titles are less consistent than Lever's (confirmed
        # live: "Job Application for Senior Machine Learning Engineer I
        # ... at Signifyd") — the company name is reliably the URL slug
        # itself (Greenhouse board slugs are the company's own chosen
        # identifier), a firmer signal here than parsing the title.
        name = company_slug.replace("-", " ").replace("_", " ").strip().title() or company_slug
        return f"greenhouse:{company_slug}", name
    return None


# --- Homepage resolution ------------------------------------------------
#
# Audit finding (2026-09-03): DIRECTORY/HIRING candidates never carry a
# real homepage domain (Crunchbase/YC/Lever/Greenhouse never echo it —
# see this module's own top docstring). Since app/services/
# company_resolution.py::resolve_candidate's ONLY cross-provider identity
# merge path is an exact domain match (name-alone match is explicitly
# "never sufficient to merge," by design), a Tavily/Serper candidate for
# the SAME real company an Explorium sighting already found could never
# be recognized as the same company — silently defeating Hard mode's
# entire premise of corroborating across all 3 providers, for every ICP.
#
# The fix: a lightweight second search ("<name> official site") to find
# the company's real domain — but company names collide (confirmed live:
# searching "Deep Labs" surfaces three unrelated real companies; "Flux
# Auto" surfaces both a plausible match AND an unrelated same-name
# business) — so a resolved domain is NEVER accepted on name alone. It
# is accepted ONLY when the resolution result's own title/content shares
# genuine word overlap with the candidate's OWN already-known description
# (from its Crunchbase/YC profile text) — see _verify_homepage_match's
# own docstring. No match found -> domain stays unset, exactly today's
# existing (safe) behavior; this only ever ADDS a domain when there is
# real corroborating evidence it's the right one, never guesses.

# Hosts a resolution result can never itself BE the "real homepage" —
# the same directory/social/aggregator problem _NON_COMPANY_HOST_SUFFIXES
# solved for the original generic-query design, reused here for the same
# reason: a LinkedIn or Crunchbase URL turning up in the resolution
# search is a mention of the company, never its own site.
_NON_HOMEPAGE_HOST_SUFFIXES = frozenset(
    {
        "linkedin.com",
        "crunchbase.com",
        "ycombinator.com",
        "facebook.com",
        "twitter.com",
        "x.com",
        "instagram.com",
        "youtube.com",
        "wikipedia.org",
        "bloomberg.com",
        "g2.com",
        "glassdoor.com",
        "indeed.com",
        "owler.com",
        "zoominfo.com",
        "pitchbook.com",
        "jobs.lever.co",
        "boards.greenhouse.io",
    }
)

_WORD_RE = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset(
    {
        "the", "a", "an", "and", "or", "for", "of", "in", "on", "to", "is", "are", "we",
        "our", "with", "at", "by", "from", "your", "that", "this", "it", "as", "be",
    }
)


def _significant_words(text: str) -> set[str]:
    return {w for w in _WORD_RE.findall(text.lower()) if len(w) > 2 and w not in _STOPWORDS}


def _verify_homepage_match(candidate_name: str, candidate_description: str, result_title: str, result_content: str) -> bool:
    """True only when a homepage-resolution search result shares genuine
    word overlap with what we ALREADY independently know about the
    candidate's BUSINESS (its own Crunchbase/YC description) — never
    accepted on name similarity alone, since company names collide (see
    this section's own top comment for confirmed-live examples: two
    unrelated real "Deep Labs" companies). The company NAME's own words
    are deliberately excluded from both sides of the comparison before
    counting overlap — two same-named-but-unrelated companies will
    always share the name's words (confirmed by an earlier version of
    this function actually failing on exactly that case: "Deep Labs" vs.
    "Deep Labs" shared {"deep", "labs"} regardless of business), which
    would make the check trivially always pass for a name collision,
    defeating its entire purpose. Deliberately a simple, deterministic
    shared-vocabulary check — no fuzzy/semantic matching, no LLM
    judgment call. Requires at least 2 shared significant NON-NAME words
    — 1 is too easy to hit by chance on an unrelated business in the
    same general space (e.g. both mentioning "software")."""
    name_words = _significant_words(candidate_name)
    candidate_words = _significant_words(candidate_description) - name_words
    if not candidate_words:
        return False
    result_words = _significant_words(f"{result_title} {result_content}") - name_words
    return len(candidate_words & result_words) >= 2


def _is_homepage_host(host: str) -> bool:
    return not any(host == suffix or host.endswith("." + suffix) for suffix in _NON_HOMEPAGE_HOST_SUFFIXES)


class TavilyCompanyDiscoveryProvider(ProviderAdapter):
    """Calls Tavily's real POST /search endpoint with THREE distinct,
    LIVE-VERIFIED query types, never the single generic "<term> companies"
    query this module started with — that phrase is exactly what listicle/
    roundup article titles are written to rank for (confirmed live: 5/5
    results for "Healthcare SaaS companies" were roundup articles from
    datamation.com, f6s.com, blog.hubspot.com, etc., never a real company).
    Each type below was individually confirmed live before being written:

    1. DIRECTORY ("site:crunchbase.com/organization <term>",
       "site:ycombinator.com/companies <term>") — targets individual
       company-PROFILE pages, confirmed live to return clean, real,
       single-company titles ("1001 AI - Crunchbase Company Profile &
       Funding", "Flux Auto: Physical AI for Warehouses and Factories |
       Y Combinator"). Category/listing pages (ycombinator.com/companies/
       industry/<slug>) are excluded — see _extract_directory_result.

    2. HIRING ("site:jobs.lever.co <role>", "site:boards.greenhouse.io
       <role>") — only built when the ICP itself specifies allowed_titles
       (Founder/CTO/etc. are person-level titles, not roles to search job
       boards for — only genuinely role-shaped titles, see
       _is_job_role_title, are used). Confirmed live: real, named
       companies (OSARO, Quincus, Veo, Signifyd) actively hiring for that
       exact role. A direct match for "actively hiring for X" ICPs (e.g.
       Deep Tech startups hiring AI/ML engineers).

    Neither DIRECTORY nor HIRING results carry the company's real
    homepage domain in their OWN search result (Crunchbase/YC/Lever/
    Greenhouse never echo it) — but every candidate that reaches
    NormalizedRecord construction gets a SECOND, dedicated resolution
    attempt (_resolve_homepage_domain) before that, so
    NormalizedRecord.attributes["domain"] IS populated whenever that
    second search finds a real homepage AND its own content genuinely
    corroborates the candidate's already-known description (see the
    "Homepage resolution" section above this class for the full
    rationale and why a domain is never accepted on name alone). A
    verified match is common but not guaranteed — an unverifiable or
    ambiguous name still safely omits "domain", identical to this
    provider's behavior before this method existed.

    Geography is appended to query text only when the ICP names exactly
    one unrecognized country/region term (e.g. "GCC") — a compound OR-of-
    many-countries query would dilute a single search beyond usefulness,
    so multi-country ICPs omit it from the text and rely on the existing
    geography hard rule (app/services/hard_rule_engine.py::
    _evaluate_geography) to filter candidates afterward, unchanged."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.tavily.com",
        timeout_seconds: float = 30.0,
        max_results_per_query: int = 10,
    ) -> None:
        super().__init__(
            provider_id="tavily-company-discovery-v1",
            provider_name="Tavily",
            capabilities={ProviderCapability.COMPANY_DISCOVERY},
        )
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._max_results_per_query = max_results_per_query

    def _geography_suffix(self, query: dict) -> str:
        geography_unrecognized = query.get("geography_unrecognized") or ()
        return f" in {geography_unrecognized[0]}" if len(geography_unrecognized) == 1 else ""

    def _build_directory_queries(self, query: dict) -> list[tuple[str, str]]:
        # industries AND company_types both describe "what kind of company
        # this is" (the same distinction Explorium itself draws — see
        # app/providers/explorium.py's own _plan_industry_branches vs.
        # _plan_company_type_branches, both structurally planned there).
        # An ICP defined mainly by company_types (e.g. "D2C Brand",
        # "PE-backed") with few or no industries previously produced ZERO
        # directory queries here — this module's only real gap-filling
        # role (covering terms Explorium's structured taxonomy can't
        # resolve) was silently absent for exactly the ICP shape most
        # likely to need it. Deduplicated case-insensitively so a term
        # appearing in both fields (or repeated) is never queried twice.
        industries = query.get("industries") or ()
        company_types = query.get("company_types") or ()
        seen_terms: set[str] = set()
        terms: list[str] = []
        for raw_term in (*industries, *company_types):
            term = _sanitize_query_term(raw_term)
            key = term.lower()
            if not term or key in seen_terms:
                continue
            seen_terms.add(key)
            terms.append(term)

        geography_suffix = self._geography_suffix(query)
        pairs: list[tuple[str, str]] = []
        for term in terms:
            pairs.append((f'site:crunchbase.com/organization "{term}"{geography_suffix}', "directory"))
            pairs.append((f'site:ycombinator.com/companies "{term}"{geography_suffix}', "directory"))
        return pairs

    def _build_hiring_queries(self, query: dict) -> list[tuple[str, str]]:
        allowed_titles = query.get("allowed_titles") or ()
        job_roles = [title for title in allowed_titles if _is_job_role_title(title)]
        if not job_roles:
            return []
        geography_suffix = self._geography_suffix(query)
        pairs: list[tuple[str, str]] = []
        for role in job_roles:
            term = _sanitize_query_term(role)
            if not term:
                continue
            pairs.append((f'site:jobs.lever.co OR site:boards.greenhouse.io "{term}"{geography_suffix}', "hiring"))
        return pairs

    def _resolve_homepage_domain(self, company_name: str, candidate_description: str) -> str | None:
        """See this module's "Homepage resolution" section (above the
        class definition) for the full rationale. One extra search call
        per candidate — a real, deliberate cost (see that section's own
        comment) in exchange for making cross-provider corroboration with
        Explorium possible at all. Never raises: any failure here is
        exactly as safe as never having attempted resolution — the
        candidate is still returned, just without a domain, identical to
        this module's behavior before this method existed."""
        try:
            response = httpx.post(
                f"{self._base_url}/search",
                json={
                    "api_key": self._api_key,
                    "query": f'"{_sanitize_query_term(company_name)}" official site',
                    "search_depth": "basic",
                    "max_results": 5,
                },
                timeout=self._timeout_seconds,
            )
            if response.status_code != 200:
                return None
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("tavily_homepage_resolution_failed company=%r error=%s", company_name, exc)
            return None

        for result in body.get("results", []) or []:
            url = result.get("url")
            title = result.get("title") or ""
            content = result.get("content") or ""
            if not url:
                continue
            host = _host_of(url)
            if not host or not _is_homepage_host(host):
                continue
            if not _verify_homepage_match(company_name, candidate_description, title, content):
                continue
            domain = normalize_domain(host)
            if domain:
                return domain
        return None

    def execute(self, request: ProviderRequest) -> ProviderResponse:
        query_pairs = self._build_directory_queries(request.query) + self._build_hiring_queries(request.query)
        if not query_pairs:
            return ProviderResponse(
                provider_id=self.provider_id,
                capability=request.capability,
                success=True,
                data=(),
                source=SourceMetadata(
                    provider_id=self.provider_id, provider_name=self.provider_name, retrieved_at=datetime.now(timezone.utc), is_mock=False
                ),
                exhausted=True,
            )

        limit = request.query.get("limit") or 20
        records: list[NormalizedRecord] = []
        seen_external_ids: set[str] = set()

        for search_query, query_kind in query_pairs:
            response = httpx.post(
                f"{self._base_url}/search",
                json={
                    "api_key": self._api_key,
                    "query": search_query,
                    "search_depth": "basic",
                    "max_results": self._max_results_per_query,
                },
                timeout=self._timeout_seconds,
            )

            # Auth/rate-limit failures apply to the WHOLE API key, not one
            # query — every remaining query would fail identically, so
            # these still abort the call immediately, exactly as before.
            if response.status_code == 401:
                return self._error_response(request, TAVILY_AUTH_FAILED, "Tavily rejected the configured API key.")
            if response.status_code == 432 or response.status_code == 429:
                return self._error_response(request, TAVILY_RATE_LIMITED, "Tavily usage limit reached.", retryable=True)
            if response.status_code != 200:
                # A per-QUERY failure (e.g. a malformed/rejected query
                # string) must never abort every OTHER query in the same
                # call — an ICP with several industry terms, one of which
                # happens to trip a vendor-side rejection, still gets
                # results for its other, well-formed terms. Logged, not
                # silently dropped, and never raised (raising here would
                # propagate out of execute() and, via ProviderAdapter.run(),
                # report the ENTIRE call as failed even though other
                # queries in this same loop may have already succeeded).
                logger.warning(
                    "tavily_query_failed query=%r status=%d body=%r", search_query, response.status_code, response.text[:200]
                )
                continue

            body = response.json()
            for result in body.get("results", []) or []:
                url = result.get("url")
                title = result.get("title")
                content = result.get("content")
                if not url or not title:
                    continue
                if _is_crunchbase_block_page(content):
                    continue

                extracted = _extract_directory_result(url, title) if query_kind == "directory" else _extract_hiring_result(url, title)
                if extracted is None:
                    continue
                external_id_suffix, name = extracted
                external_id = f"tavily:{external_id_suffix}"
                if external_id in seen_external_ids:
                    continue
                seen_external_ids.add(external_id)

                attributes: dict = {}
                description = content[:2000] if content else ""
                if description:
                    attributes["description"] = description

                domain = self._resolve_homepage_domain(name, description)
                if domain:
                    attributes["domain"] = domain

                records.append(NormalizedRecord(external_id=external_id, name=name, attributes=attributes))
                if len(records) >= limit:
                    break
            if len(records) >= limit:
                break

        return ProviderResponse(
            provider_id=self.provider_id,
            capability=request.capability,
            success=True,
            data=tuple(records),
            source=SourceMetadata(
                provider_id=self.provider_id, provider_name=self.provider_name, retrieved_at=datetime.now(timezone.utc), is_mock=False
            ),
            # Tavily's /search endpoint is not a paginated, resumable
            # cursor-based API the way Explorium's is — each call is a
            # fresh, complete query. Reporting exhausted=True means
            # run_company_discovery's caller never wastes a repeat "Find
            # More" round asking this exact query again with no new
            # cursor to advance it; a genuinely different candidate pool
            # would require different ICP terms, not a continuation token.
            exhausted=True,
        )

    def _error_response(self, request: ProviderRequest, code: str, message: str, retryable: bool = False) -> ProviderResponse:
        return ProviderResponse(
            provider_id=self.provider_id,
            capability=request.capability,
            success=False,
            error=ProviderError(code=code, message=message, retryable=retryable),
            source=SourceMetadata(
                provider_id=self.provider_id, provider_name=self.provider_name, retrieved_at=datetime.now(timezone.utc), is_mock=False
            ),
        )

    def get_usage(self) -> dict:
        """GET /usage — Tavily's real, documented account-usage endpoint
        (https://docs.tavily.com/documentation/api-reference/endpoint/usage),
        confirmed to exist via direct research before this was written; the
        ONLY one of the four discovery-mode providers (Explorium/Gemini/
        Tavily/Serper) that exposes a genuine remaining-balance check —
        the other three are dashboard-only (their own docs say so; no
        endpoint exists to poll). Returns Tavily's raw response body
        unmodified — {"key": {...}, "account": {...}}, see the docs link
        above for the exact shape — this method does no interpretation of
        it, so a future Tavily API change is never silently misread here.
        Raises on any HTTP failure; the caller (app/api/provider_usage.py)
        converts that into an honest "unavailable" response rather than
        ever fabricating a usage number."""
        response = httpx.get(f"{self._base_url}/usage", headers={"Authorization": f"Bearer {self._api_key}"}, timeout=self._timeout_seconds)
        response.raise_for_status()
        return response.json()
