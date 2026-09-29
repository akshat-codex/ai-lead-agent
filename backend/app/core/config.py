"""Application configuration loaded from environment variables."""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # General
    app_name: str = "Leads Agent"
    environment: str = "development"
    debug: bool = True

    # API
    api_v1_prefix: str = "/api/v1"
    cors_origins: list[str] = ["http://localhost:3000"]

    # Database
    database_url: str = "postgresql+psycopg://leads_agent:leads_agent@localhost:5432/leads_agent"

    # Redis / Celery
    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str = "redis://localhost:6379/0"
    celery_result_backend: str = "redis://localhost:6379/1"

    # Providers
    apollo_api_key: str | None = None
    apollo_base_url: str = "https://api.apollo.io/api/v1"

    explorium_api_key: str | None = None
    explorium_base_url: str = "https://api.explorium.ai/v2"

    # Unipile requires three values together (unlike Apollo/Explorium's
    # single API key): an API key, a per-tenant DSN (base URL — Unipile
    # issues a dedicated subdomain per account, not a shared hostname), and
    # a connected LinkedIn account_id to search from. All three must be set
    # for the real provider to register; see app/providers/default_registry.py.
    unipile_api_key: str | None = None
    unipile_dsn: str | None = None
    unipile_account_id: str | None = None

    # Phase 9: real OpenAI provider for LLM qualification (Phase 16) /
    # adversarial review (Phase 17). Registered only when openai_api_key is
    # set — see app/services/llm_providers/default_registry.py. Leave unset
    # to keep running on the deterministic mock (no cost, no network call).
    openai_api_key: str | None = None
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-5-nano"

    # Phase 23: temporary A/B test provider, scoped ONLY to Phase 10
    # discovery-strategy interpretation (app/services/discovery_strategy.py
    # ::interpret_icp) — never the shared get_llm_provider() used by Phase
    # 16 qualification / Phase 17 adversarial review. See
    # app/services/llm_providers/default_registry.py::
    # get_discovery_strategy_llm_provider() for the one call site this
    # setting affects. Leave GEMINI_API_KEY unset (or
    # discovery_strategy_llm_provider="openai") to keep discovery-strategy
    # interpretation on OpenAIProvider exactly as before this phase.
    gemini_api_key: str | None = None
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta"
    # Phase 25: gemini-2.0-flash was retired by Google (confirmed live,
    # 2026-09-02 Phase 24 E2E test: HTTP 404 "This model
    # models/gemini-2.0-flash is no longer available... use
    # models/gemini-3.6-flash") — updated to the model Google's own error
    # response named as the current replacement. Not verified via a new
    # live call (Explorium/Gemini credits are exhausted at time of this
    # change); if this ID is also wrong, the same clean PROVIDER_UNAVAILABLE
    # degradation observed in Phase 24 will occur again, never a crash.
    gemini_model: str = "gemini-3.6-flash"
    # "openai" (default) | "gemini" — explicit override; if unset, presence
    # of gemini_api_key alone does NOT switch anything (opt-in, not
    # opt-out-by-side-effect), matching this codebase's existing "a second
    # configured key never silently changes behavior without an explicit
    # switch" discipline elsewhere (e.g. provider routing).
    discovery_strategy_llm_provider: str = "openai"

    # Phase 25: hard ceiling on how many discovery rounds ONE BATCH may
    # ever run in its lifetime, across create + every resume call combined
    # — see app/services/batch_orchestration.py::_should_run_another_discovery_round.
    # Root-caused live (Phase 24 E2E test): a batch with 0 ACCEPTED results
    # kept satisfying "accepted_so_far < target_count" forever, so the
    # frontend's own auto-continue loop (up to 20 resume calls) kept
    # seeding new discovery rounds — 6 rounds ran and fully exhausted a
    # real Explorium credit balance before target_count=1 was ever
    # satisfied. This cap is a backend-enforced, un-bypassable floor
    # under that: no matter how many times any client calls resume, or
    # what max_discovery_rounds it requests, a single batch can never
    # exceed this many discovery rounds total.
    max_discovery_rounds_per_batch: int = 5

    # Phase 34 — Hermes ICP Search: a second, complementary COMPANY_DISCOVERY
    # source, fundamentally different from Explorium's structured-database
    # lookups — an async, browser-driven research agent (see
    # app/providers/hermes.py's own module docstring for the full API
    # contract and why it cannot be a synchronous ProviderAdapter). Leave
    # hermes_api_token unset to keep discovery Explorium-only, exactly as
    # today — see app/services/batch_orchestration.py::_maybe_advance_hermes_job
    # for the one call site this affects.
    hermes_base_url: str = "http://198.244.141.137"
    hermes_api_token: str | None = None
    # The real service's documented single-slot behavior (MAX_CONCURRENT_JOBS=1,
    # a job can take several minutes) — this codebase never submits a second
    # job while one is still pending for a batch, and never blocks an HTTP
    # request waiting for a job to finish; see _maybe_advance_hermes_job.
    hermes_max_records_per_job: int = 10

    # Phase 5 (benchmark + live-readiness audit) — a hard safety limit
    # specifically for a FIRST live provider test, independent of and
    # tighter than every existing per-batch bound
    # (max_discovery_rounds_per_batch, discovery_limit's own [1,100]
    # schema range, target_count's own [1,1000] schema range — all
    # UNCHANGED by this setting). Those existing bounds were sized for
    # normal production use once a provider's real behavior/cost is
    # already understood; this setting exists for the window BEFORE that
    # is true, when a config mistake or an unexpectedly high discovery
    # yield could still spend far more than intended on the very first
    # real call.
    #
    # Defaults to ON (fails closed): a batch created/resumed while
    # live_test_mode=True has discovery_limit and target_count silently
    # clamped DOWN (never up) to live_test_max_discovery_limit/
    # live_test_max_target_count — see app/api/batch.py's own
    # _apply_live_test_clamp. A caller requesting MORE than the clamp
    # gets the clamp, not an error — the same "never force a caller's
    # request past a safety bound" discipline
    # max_discovery_rounds_per_batch already uses, just applied one level
    # earlier (at request time, not mid-round). Set live_test_mode=false
    # once a real provider's actual per-round yield/cost is understood
    # from a deliberate, small, monitored first run — this setting is a
    # training-wheel for that first run, not a permanent production cap.
    live_test_mode: bool = True
    live_test_max_discovery_limit: int = 5
    live_test_max_target_count: int = 5

    # Discovery-mode web-search COMPANY_DISCOVERY sources — see
    # app/providers/tavily.py / app/providers/serper.py's own module
    # docstrings. Both cover the confirmed gap in Explorium's fixed
    # taxonomy (industry terms like "OTT Platforms"/"Microdrama Companies"
    # that resolve to zero real Explorium categories). Leave either unset
    # to keep that provider out of the registry entirely — see
    # app/providers/default_registry.py. Neither key's presence changes
    # Fast mode (Explorium-only) behavior at all; they are only ever
    # called for Safe/Hard mode batches (see BatchModel.discovery_mode).
    tavily_api_key: str | None = None
    tavily_base_url: str = "https://api.tavily.com"

    serper_api_key: str | None = None
    serper_base_url: str = "https://google.serper.dev"

    # Explorium exposes NO programmatic credit-balance/usage endpoint
    # (confirmed against Explorium's own API docs — credits are visible
    # only via their web dashboard). This is therefore a user-supplied,
    # OBSERVED estimate — "roughly 1 credit per company Explorium
    # returns" — not a vendor-confirmed rate, and is surfaced to the
    # frontend clearly labeled as an estimate (see
    # app/schemas/batch.py::BatchDetailRead.estimated_explorium_credits).
    # Change this if your own observed rate differs; it is never treated
    # as authoritative anywhere in validation/billing logic.
    explorium_estimated_credits_per_company: float = 1.0

    # Deep discovery mode — opt-in, OFF by default (see
    # app/schemas/batch.py::DiscoveryMode.DEEP). After a candidate's company
    # is resolved and before it enters evidence/hard-rule validation, Deep
    # mode fetches the candidate's real homepage (see
    # app/services/homepage_fetch.py) and runs one bounded-concurrency LLM
    # relevance pre-screen (see app/services/deep_prescreen.py) — a
    # discovery-stage filter/rank signal only, never a hard-rule/
    # qualification decision. Every limit below is a genuinely NEW bound
    # (deep mode adds no new spend CEILING primitive beyond these — it
    # still rides entirely on discovery_limit/live_test_mode/
    # max_discovery_rounds_per_batch above, which already bound how many
    # candidates ever reach the pre-screen step in the first place).
    deep_prescreen_max_concurrency: int = 4
    deep_prescreen_homepage_timeout_seconds: float = 8.0
    deep_prescreen_homepage_max_bytes: int = 300_000
    deep_prescreen_homepage_max_chars: int = 6_000
    deep_prescreen_llm_timeout_seconds: float = 20.0
    # "openai" (default) | "gemini" — same explicit-override discipline as
    # discovery_strategy_llm_provider above (an unset/"openai" value never
    # silently changes anything). Independent of discovery_strategy_llm_provider
    # and of get_llm_provider()'s own qualification/adversarial-review
    # provider — a change to either of those never affects pre-screen, and
    # vice versa.
    deep_prescreen_llm_provider: str = "openai"
    # A separate, optionally CHEAPER/FASTER OpenAI model than
    # openai_model (used for qualification/adversarial review) — pre-screen
    # runs on every raw candidate discovery returns (bounded by
    # discovery_limit), a materially larger volume than qualification's own
    # (only the subset that already structurally passed hard-rule
    # validation), so this is the one place in the codebase where model
    # cost-tier choice matters most. Defaults to openai_model itself (which
    # is already gpt-5-nano, OpenAI's own cheap/fast tier — see
    # openai_model's own comment) so leaving this unset never requires a
    # second OpenAI model to exist; set it explicitly only if a cheaper
    # model becomes available/desired later. Never a Gemini/other-vendor
    # model unless deep_prescreen_llm_provider="gemini" is also set — this
    # field only ever feeds OpenAIProvider's own model_id parameter.
    deep_prescreen_openai_model: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
