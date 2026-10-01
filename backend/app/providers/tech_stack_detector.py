"""Passive HTTP/HTML tech-stack detection — a free, no-API-key
COMPANY_ENRICHMENT provider.

Root problem this closes: this codebase had zero technographic capability
before this module — a real, commonly-expected 2026 GTM-data category
(BuiltWith/Wappalyzer-style "what platform does this company run on") that
every major competitor (Clearbit/Breeze Intelligence, ZoomInfo, Cognism)
offers natively. This is deliberately a NARROW, passive-fingerprinting
implementation, not a BuiltWith/Wappalyzer integration — neither vendor has
a confirmed free-tier API (unlike the email/phone verification research
that found Abstract API's genuine free tier), so this closes the gap at
zero cost via the same "one HTTP GET + honest pattern match" mechanism
app/services/homepage_fetch.py already uses for a different purpose.

Mechanism: ONE httpx GET of the company's own homepage — same timeout/
byte-cap/never-raise safety shape as app/services/homepage_fetch.py's own
fetch_homepage, but deliberately NOT a reuse of that function itself: its
own text-extraction strips every <script src=...>/<link href=...> tag
entirely (by design, for its own LLM-prose-extraction purpose — see its own
_SCRIPT_STYLE_RE/_TAG_RE), which would destroy exactly the markup signals
(script src, link href, CDN hostnames) this module's fingerprints depend
on. This module fetches and scans the RAW HTML instead, reusing only the
same block-page phrase list (a local copy, not an import, to avoid
coupling two modules with genuinely different text-processing needs) so a
CAPTCHA/anti-bot page is never mistaken for real site content here either.

Signatures are a small, curated table of UNAMBIGUOUS strings — a literal,
vendor-specific path/hostname that could only appear if that exact
platform is actually in use (e.g. "cdn.shopify.com" only ever appears on a
site actually served by Shopify), never a bare product-name word that could
appear in unrelated prose. This mirrors app/services/
commercial_signal_extractor.py's own "no keyword-only false positives"
discipline (see that module's docstring) applied to markup instead of
evidence text.

Honest scope: this detects COMMON, MARKET-VISIBLE platforms whose presence
leaves an unambiguous fingerprint in a homepage's own HTML/headers
(e-commerce platform, CMS, marketing/CRM widget, analytics tag) — it is NOT
a comprehensive stack audit (no backend language, no cloud provider, no
database detection: none of those leave a reliable client-visible
fingerprint without executing JavaScript or probing infrastructure this
codebase has no business doing). A homepage with no matched signature
returns an honest "no match," never a guessed platform — identical
degradation discipline to every other provider in this codebase (e.g.
app/providers/sec_edgar.py's SEC_EDGAR_NO_MATCH).
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
    ProviderReliabilityProfile,
    ProviderRequest,
    ProviderResponse,
    SourceMetadata,
)

logger = logging.getLogger(__name__)

TECH_STACK_NO_DOMAIN_SUPPLIED = "TECH_STACK_NO_DOMAIN_SUPPLIED"
TECH_STACK_FETCH_FAILED = "TECH_STACK_FETCH_FAILED"
TECH_STACK_NO_MATCH = "TECH_STACK_NO_MATCH"

# A local copy of app/services/homepage_fetch.py's own block-page phrase
# list (not an import — that module's own regex is tuned for its
# stripped-text consumer; this one always runs against raw HTML, a
# genuinely different input shape, so keeping them independently editable
# is more honest than a shared import that happens to work today by luck).
_BLOCK_PAGE_RE = re.compile(
    r"(?i)you have been blocked|access denied|are you a human|enable javascript and cookies|"
    r"verify you are human|unusual traffic|complete the security check|attention required|"
    r"error 403|forbidden|requiring captcha|please enable cookies",
)

# Each signature is a literal substring that can ONLY appear if that exact
# platform/tool is genuinely in use — never a bare product name (e.g. never
# just "shopify", which could appear in unrelated marketing copy about
# e-commerce). Grouped by category so a caller/reviewer can see what KIND
# of signal each one is; category is not itself exposed as a separate
# evidence field, only used to dedupe (a homepage that matches two
# signatures in the same category reports only the first, avoiding
# "Shopify AND WooCommerce" contradictions from an unrelated false hit).
_ECOMMERCE_SIGNATURES: tuple[tuple[str, str], ...] = (
    ("cdn.shopify.com", "Shopify"),
    ("cdn.shopifycdn.net", "Shopify"),
    ("/wp-content/plugins/woocommerce", "WooCommerce"),
    ("bigcommerce.com/s-", "BigCommerce"),
    ("cdn.shopware.com", "Shopware"),
)
_CMS_SIGNATURES: tuple[tuple[str, str], ...] = (
    ("/wp-content/", "WordPress"),
    ("/wp-includes/", "WordPress"),
    ("webflow.js", "Webflow"),
    ("assets.squarespace.com", "Squarespace"),
    ("wixstatic.com", "Wix"),
    ("cdn.contentful.com", "Contentful"),
)
_MARKETING_CRM_SIGNATURES: tuple[tuple[str, str], ...] = (
    ("js.hs-scripts.com", "HubSpot"),
    ("js.hsforms.net", "HubSpot"),
    ("js.hsleadflows.net", "HubSpot"),
    ("cdn.marketo.com", "Marketo"),
    ("munchkin.marketo.net", "Marketo"),
    ("pardot.com", "Pardot"),
    ("d.la4-c1-was.salesforceliveagent.com", "Salesforce Live Agent"),
)
_ANALYTICS_SIGNATURES: tuple[tuple[str, str], ...] = (
    ("googletagmanager.com/gtm.js", "Google Tag Manager"),
    ("cdn.segment.com/analytics.js", "Segment"),
    ("cdn.amplitude.com", "Amplitude"),
    ("static.hotjar.com", "Hotjar"),
)

_SIGNATURE_CATEGORIES: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    ("ecommerce_platform", _ECOMMERCE_SIGNATURES),
    ("cms_platform", _CMS_SIGNATURES),
    ("marketing_crm_platform", _MARKETING_CRM_SIGNATURES),
    ("analytics_platform", _ANALYTICS_SIGNATURES),
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _detect_signatures(homepage_text: str) -> dict[str, str]:
    """Returns {evidence_field: matched_platform_name}, at most one match
    per category (the FIRST signature in that category's own tuple order
    that matches) — never multiple contradictory platforms for the same
    category from one homepage fetch."""
    lowered = homepage_text.lower()
    detected: dict[str, str] = {}
    for field_name, signatures in _SIGNATURE_CATEGORIES:
        for needle, platform_name in signatures:
            if needle.lower() in lowered:
                detected[field_name] = platform_name
                break
    return detected


class TechStackDetectorProvider(ProviderAdapter):
    """Free, no-key, best-effort COMPANY_ENRICHMENT — see module docstring
    for the honest "passive fingerprint, not a stack audit" scope."""

    def __init__(
        self,
        provider_id: str = "tech-stack-detector-v1",
        timeout_seconds: float = 10.0,
        max_bytes: int = 2_000_000,
        max_chars: int = 200_000,
    ) -> None:
        super().__init__(
            provider_id=provider_id,
            provider_name="Tech Stack Detector",
            capabilities={ProviderCapability.COMPANY_ENRICHMENT},
            reliability_profile=ProviderReliabilityProfile(),
        )
        self._timeout_seconds = timeout_seconds
        self._max_bytes = max_bytes
        self._max_chars = max_chars

    def _fetch_raw_html(self, url: str) -> tuple[str | None, str | None]:
        """Returns (html, None) on success or (None, reason) on any
        failure — never raises. Same byte-cap-streaming shape as
        app/services/homepage_fetch.py::fetch_homepage, but returns the
        RAW html (truncated to max_chars) instead of stripped prose, since
        stripping would destroy the exact script-src/link-href signals
        this provider depends on (see module docstring)."""
        try:
            with httpx.stream("GET", url, timeout=self._timeout_seconds, follow_redirects=True) as response:
                if response.status_code != 200:
                    return None, f"http_{response.status_code}"
                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_bytes():
                    chunks.append(chunk)
                    total += len(chunk)
                    if total >= self._max_bytes:
                        break
                raw_bytes = b"".join(chunks)
        except httpx.TimeoutException as exc:
            logger.warning("tech_stack_fetch_timeout url=%r error=%s", url, exc)
            return None, "timeout"
        except httpx.HTTPError as exc:
            logger.warning("tech_stack_fetch_error url=%r error=%s", url, exc)
            return None, "fetch_error"

        html = raw_bytes.decode("utf-8", errors="replace")
        if _BLOCK_PAGE_RE.search(html[:4000]):
            return None, "block_page"
        return html[: self._max_chars], None

    def execute(self, request: ProviderRequest) -> ProviderResponse:
        domain = request.query.get("domain")
        if not domain:
            return ProviderResponse(
                provider_id=self.provider_id,
                capability=request.capability,
                success=False,
                error=ProviderError(
                    code=TECH_STACK_NO_DOMAIN_SUPPLIED,
                    message="No domain supplied; tech-stack detection requires a homepage to fetch.",
                    retryable=False,
                ),
                source=SourceMetadata(provider_id=self.provider_id, provider_name=self.provider_name, retrieved_at=_now(), is_mock=False),
            )

        url = domain if domain.startswith(("http://", "https://")) else f"https://{domain}"
        html, fetch_failure_reason = self._fetch_raw_html(url)

        if html is None:
            return ProviderResponse(
                provider_id=self.provider_id,
                capability=request.capability,
                success=False,
                error=ProviderError(
                    code=TECH_STACK_FETCH_FAILED,
                    message=f"Homepage fetch failed ({fetch_failure_reason}); no tech-stack signature could be checked.",
                    retryable=False,
                ),
                source=SourceMetadata(provider_id=self.provider_id, provider_name=self.provider_name, retrieved_at=_now(), is_mock=False),
            )

        attributes = _detect_signatures(html)
        if not attributes:
            return ProviderResponse(
                provider_id=self.provider_id,
                capability=request.capability,
                success=False,
                error=ProviderError(
                    code=TECH_STACK_NO_MATCH,
                    message="No known platform signature found in this homepage's content.",
                    retryable=False,
                ),
                source=SourceMetadata(provider_id=self.provider_id, provider_name=self.provider_name, retrieved_at=_now(), is_mock=False),
            )

        record = NormalizedRecord(external_id=domain, name=domain, attributes=attributes)

        return ProviderResponse(
            provider_id=self.provider_id,
            capability=request.capability,
            success=True,
            data=(record,),
            source=SourceMetadata(provider_id=self.provider_id, provider_name=self.provider_name, retrieved_at=_now(), is_mock=False),
        )
