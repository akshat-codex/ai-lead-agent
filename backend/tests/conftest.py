"""Phase 34 — test-session provider/credential isolation.

MUST run before ANYTHING under app/ is imported: app/providers/
default_registry.py and app/services/llm_providers/default_registry.py
each build a module-level singleton (`default_registry`,
`default_llm_provider`) exactly once, at import time, from
app.core.config.get_settings() — which reads real credentials straight out
of this repo's own .env (EXPLORIUM_API_KEY, GEMINI_API_KEY,
OPENAI_API_KEY, APOLLO_API_KEY, UNIPILE_*). A developer's local .env is
expected to carry real keys for manual/live use of the app — but a test
run must never inherit them: whichever provider a test doesn't explicitly
mock (the large majority of this suite mocks per-test via
app.dependency_overrides[get_provider_registry], but a handful of tests —
see tests/test_provider_routing_api.py's
test_provider_with_no_signal_linkage_still_included_and_called — call an
endpoint with no override active at all, deliberately exercising the real
default singleton) would otherwise silently pick up whatever real,
credentialed provider happens to be configured in .env at the time,
exactly as production does. That is what caused this suite's 8
EXPLORIUM_API_KEY-dependent failures (real Explorium registered instead
of the expected mock-only shape) and a separate, more serious live-call
incident during manual debugging outside pytest.

The fix: force every provider/LLM credential to empty BEFORE app.core.
config.Settings is ever constructed, via real OS environment variables
(pydantic-settings' own precedence is env var > .env file — confirmed:
an empty-string OS env var wins over a real .env value, verified against
this exact Settings class), then clear get_settings()'s lru_cache so the
next call re-reads the now-empty values. Because this happens at import
time, both module-level singletons are built with a fully mock/degraded
configuration from the start — there is no later window where a real
provider could already be baked in. Every test that already explicitly
builds its own registry/provider (respx-mocked or Mock*Provider-based) or
overrides get_settings locally is completely unaffected: this only
changes what a test gets by NOT overriding anything, from "whatever this
machine's .env happens to contain" to "always mock, always safe."

respx-mocked tests are unaffected by construction — this file never
touches httpx, never patches httpx.post/get, and never wraps or replaces
any transport. Nothing here can shadow or interfere with respx's own
transport-level patching (a documented reason a prior attempt at network
blocking here broke respx: it monkeypatched httpx.post directly, which
sits ABOVE respx's own patch layer, defeating it. This approach never
touches that layer at all).

A test that genuinely needs a real, credentialed provider (a deliberate,
opt-in live integration test) should set the specific env var(s) it needs
itself, via `monkeypatch.setenv(...)` + `get_settings.cache_clear()`, and
must be clearly marked as a live test in its own name/docstring — no such
test exists in this suite today.

Phase 5 addition — settings.live_test_mode (app/api/batch.py's own
_clamp_for_live_test): defaults to True in the real app precisely so a
first live provider run is safe by default with zero .env changes
required. But this ENTIRE test suite already runs exclusively against
mocks/fakes and deliberately exercises the full [1,100]/[1,1000]/[1,10]
schema ranges of discovery_limit/target_count/max_discovery_rounds to
test THOSE bounds (and max_discovery_rounds_per_batch) correctly — a
test suite is not a "live provider test" in the sense that setting
exists to protect, so it is forced off here, the same way real provider
credentials are forced off above, for the same reason: a test run must
never silently inherit a safety behavior meant for real spend and have
it change what the test suite itself is verifying.
"""
import os

_BLOCKED_PROVIDER_ENV_VARS = (
    "EXPLORIUM_API_KEY",
    "GEMINI_API_KEY",
    "OPENAI_API_KEY",
    "APOLLO_API_KEY",
    "UNIPILE_API_KEY",
    "UNIPILE_DSN",
    "UNIPILE_ACCOUNT_ID",
    # Discovery-mode web-search providers (app/providers/tavily.py /
    # serper.py) — same reasoning as every other key above: a real key in
    # this machine's .env for manual live testing must never leak into
    # the test suite's default (unconfigured) registry state.
    "TAVILY_API_KEY",
    "SERPER_API_KEY",
    # Not a credential itself, but selects GeminiProvider for discovery-
    # strategy interpretation (app/services/llm_providers/default_registry.py
    # ::get_discovery_strategy_llm_provider) when combined with a real
    # GEMINI_API_KEY — forced back to the safe default so an explicit
    # "gemini" selection left in .env can never combine with a real key
    # some other test or a future .env edit introduces.
    "DISCOVERY_STRATEGY_LLM_PROVIDER",
)
for _var in _BLOCKED_PROVIDER_ENV_VARS:
    os.environ[_var] = ""
os.environ["LIVE_TEST_MODE"] = "false"

from app.core.config import get_settings  # noqa: E402  (see module docstring: must follow the env-var patch above)

get_settings.cache_clear()

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.db.session import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture()
def client():
    """A TestClient wired to an isolated in-memory SQLite database.

    Keeps ICP tests independent of whether a real PostgreSQL instance is
    running locally.
    """
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    testing_session_local = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)

    def override_get_db():
        db = testing_session_local()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()
