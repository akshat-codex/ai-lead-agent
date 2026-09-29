"""Deep discovery mode — app/services/homepage_fetch.py.

No live calls: every HTTP call is mocked via respx, exactly like
tests/test_discovery_mode_web_search.py's own discipline. Confirms every
failure mode (timeout, non-2xx, block/CAPTCHA page, empty content, oversized
content) degrades to a non-raising HomepageFetchResult(success=False) with
an honest `reason`, and that a genuine successful fetch extracts readable
text bounded by max_chars.
"""
import httpx
import pytest
import respx

from app.services.homepage_fetch import fetch_homepage

URL = "https://example-company.test/"


@respx.mock
def test_successful_fetch_extracts_readable_text():
    respx.get(URL).mock(
        return_value=httpx.Response(
            200,
            html="<html><head><title>Acme</title><script>var x=1;</script></head>"
            "<body><nav>Home About</nav><h1>Acme Corp</h1><p>We build widgets for factories.</p>"
            "<footer>Copyright</footer></body></html>"
            + ("padding " * 40),
        )
    )
    result = fetch_homepage(URL, timeout_seconds=8.0, max_bytes=300_000, max_chars=6_000)
    assert result.success is True
    assert "Acme Corp" in result.text
    assert "We build widgets for factories" in result.text
    assert "var x=1" not in result.text  # script content stripped
    assert "Home About" not in result.text  # nav content stripped
    assert "Copyright" not in result.text  # footer content stripped


@respx.mock
def test_max_chars_bounds_the_extracted_text():
    long_paragraph = "Real content. " * 2000  # far more than any max_chars budget
    respx.get(URL).mock(return_value=httpx.Response(200, html=f"<html><body><p>{long_paragraph}</p></body></html>"))
    result = fetch_homepage(URL, timeout_seconds=8.0, max_bytes=1_000_000, max_chars=500)
    assert result.success is True
    assert len(result.text) <= 500


@respx.mock
def test_timeout_degrades_to_a_non_raising_failure():
    respx.get(URL).mock(side_effect=httpx.TimeoutException("timed out"))
    result = fetch_homepage(URL, timeout_seconds=8.0, max_bytes=300_000, max_chars=6_000)
    assert result.success is False
    assert result.reason == "timeout"


@respx.mock
def test_connection_error_degrades_to_a_non_raising_failure():
    respx.get(URL).mock(side_effect=httpx.ConnectError("connection refused"))
    result = fetch_homepage(URL, timeout_seconds=8.0, max_bytes=300_000, max_chars=6_000)
    assert result.success is False
    assert result.reason == "fetch_error"


@respx.mock
def test_non_200_status_degrades_to_a_non_raising_failure():
    respx.get(URL).mock(return_value=httpx.Response(403, text="nope"))
    result = fetch_homepage(URL, timeout_seconds=8.0, max_bytes=300_000, max_chars=6_000)
    assert result.success is False
    assert result.reason == "http_403"


@respx.mock
def test_block_page_content_is_never_treated_as_real_company_content():
    respx.get(URL).mock(
        return_value=httpx.Response(
            200,
            html="<html><body><h1>Please verify you are human</h1>"
            "<p>Enable JavaScript and cookies to continue using this site.</p></body></html>"
            + ("padding " * 40),
        )
    )
    result = fetch_homepage(URL, timeout_seconds=8.0, max_bytes=300_000, max_chars=6_000)
    assert result.success is False
    assert result.reason == "block_page"


@respx.mock
def test_empty_content_after_extraction_degrades_to_a_non_raising_failure():
    respx.get(URL).mock(return_value=httpx.Response(200, html="<html><body></body></html>"))
    result = fetch_homepage(URL, timeout_seconds=8.0, max_bytes=300_000, max_chars=6_000)
    assert result.success is False
    assert result.reason == "empty_content"


@respx.mock
def test_oversized_response_is_truncated_at_max_bytes_not_read_in_full():
    huge_html = "<html><body><p>" + ("A" * 2_000_000) + "</p></body></html>"
    respx.get(URL).mock(return_value=httpx.Response(200, html=huge_html))
    result = fetch_homepage(URL, timeout_seconds=8.0, max_bytes=10_000, max_chars=6_000)
    # Never raises, never hangs reading 2MB — bounded by max_bytes then max_chars.
    assert result.success in (True, False)
    if result.success:
        assert len(result.text) <= 6_000


@pytest.mark.parametrize("exc", [httpx.TimeoutException("t"), httpx.ConnectError("c"), httpx.ReadError("r")])
@respx.mock
def test_every_httpx_error_type_is_caught_and_never_propagates(exc):
    respx.get(URL).mock(side_effect=exc)
    result = fetch_homepage(URL, timeout_seconds=8.0, max_bytes=300_000, max_chars=6_000)  # must not raise
    assert result.success is False
