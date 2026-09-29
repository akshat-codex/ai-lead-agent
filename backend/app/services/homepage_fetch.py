"""Deep discovery mode — homepage content fetch.

A single, synchronous httpx.get per candidate — the SAME call shape every
other outbound HTTP call in this codebase already uses (app/providers/
tavily.py, app/providers/serper.py, app/services/llm_providers/
openai_provider.py), never a new HTTP client dependency and never async.

Never raises. Every failure mode (timeout, connection error, non-2xx,
bot-block/CAPTCHA page, empty content after extraction) is represented in
the returned HomepageFetchResult and degrades the caller
(app/services/deep_prescreen.py) to its existing search-snippet fallback —
see that module's own docstring for the full failure discipline. This
mirrors app/providers/tavily.py::_is_crunchbase_block_page's own
"never treat a vendor's own error page as real company content" discipline,
generalized here to homepages instead of one specific vendor.
"""
from __future__ import annotations

import html
import logging
import re

import httpx

logger = logging.getLogger(__name__)

_SCRIPT_STYLE_RE = re.compile(r"(?is)<(script|style|noscript|svg|nav|footer|header)[^>]*>.*?</\1>")
_COMMENT_RE = re.compile(r"(?is)<!--.*?-->")
_TAG_RE = re.compile(r"(?s)<[^>]+>")
_WHITESPACE_RUN_RE = re.compile(r"[ \t\r\f\v]+")
_BLANK_LINES_RE = re.compile(r"\n\s*\n+")
_LINE_PADDING_RE = re.compile(r" *\n *")

_BLOCK_PAGE_RE = re.compile(
    r"(?i)you have been blocked|access denied|are you a human|enable javascript and cookies|"
    r"verify you are human|unusual traffic|complete the security check|attention required|"
    r"error 403|forbidden|requiring captcha|please enable cookies",
)

_MIN_CONTENT_CHARS = 200  # below this, extracted text is treated as effectively empty


def _strip_html_to_text(raw_html: str) -> str:
    """A minimal, dependency-light tag-strip — no HTML parser dependency
    exists anywhere else in this codebase, so this stays intentionally
    small rather than pulling in a new library for one feature. Confirmed
    via a manual spike against real modern SPA-style sites (Stripe,
    Notion, Figma, Salesforce) before this module was written: server-
    rendered title/tagline/product copy survives even on heavily
    client-rendered pages, which is exactly the signal the pre-screen LLM
    needs — this is never expected to extract EVERY word of a page, only
    enough real, on-topic text to judge relevance from."""
    text = _SCRIPT_STYLE_RE.sub(" ", raw_html)
    text = _COMMENT_RE.sub(" ", text)
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = _WHITESPACE_RUN_RE.sub(" ", text)
    text = _LINE_PADDING_RE.sub("\n", text)
    text = _BLANK_LINES_RE.sub("\n", text)
    return text.strip()


def _looks_like_block_page(text: str) -> bool:
    return bool(_BLOCK_PAGE_RE.search(text))


class HomepageFetchResult:
    """Plain result object (not a pydantic model — this never crosses an
    API boundary or gets persisted directly, only consumed in-process by
    app/services/deep_prescreen.py, mirroring HardRuleEvaluation's own
    "internal computation result" shape)."""

    __slots__ = ("success", "text", "reason")

    def __init__(self, success: bool, text: str = "", reason: str | None = None) -> None:
        self.success = success
        self.text = text
        self.reason = reason


def fetch_homepage(url: str, timeout_seconds: float, max_bytes: int, max_chars: int) -> HomepageFetchResult:
    """Never raises. `max_bytes` bounds the RAW response read (protects
    against a pathologically large page consuming memory/time);
    `max_chars` further bounds the EXTRACTED text handed to the LLM prompt
    (protects prompt/token cost regardless of how much raw HTML survived
    the byte cap) — the two limits are independent and both always
    enforced."""
    try:
        with httpx.stream("GET", url, timeout=timeout_seconds, follow_redirects=True) as response:
            if response.status_code != 200:
                return HomepageFetchResult(success=False, reason=f"http_{response.status_code}")
            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_bytes():
                chunks.append(chunk)
                total += len(chunk)
                if total >= max_bytes:
                    break
            raw_bytes = b"".join(chunks)
    except httpx.TimeoutException as exc:
        logger.warning("homepage_fetch_timeout url=%r error=%s", url, exc)
        return HomepageFetchResult(success=False, reason="timeout")
    except httpx.HTTPError as exc:
        logger.warning("homepage_fetch_error url=%r error=%s", url, exc)
        return HomepageFetchResult(success=False, reason="fetch_error")

    raw_html = raw_bytes.decode("utf-8", errors="replace")
    text = _strip_html_to_text(raw_html)

    if _looks_like_block_page(text[:2000]):
        return HomepageFetchResult(success=False, reason="block_page")
    if len(text) < _MIN_CONTENT_CHARS:
        return HomepageFetchResult(success=False, reason="empty_content")

    return HomepageFetchResult(success=True, text=text[:max_chars])
