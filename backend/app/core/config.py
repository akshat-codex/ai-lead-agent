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

    # Abstract API's Email Validation & Verification endpoint — a second,
    # independent PERSON_ENRICHMENT provider (see app/providers/
    # abstract_email_verification.py's own module docstring) that performs
    # a real, live SMTP/MX/catch-all/disposable deliverability check,
    # rather than trusting Apollo's own self-reported email_status guess
    # alone. Free tier confirmed live (2026-09-30, abstractapi.com/pricing):
    # 100 requests/month, a genuinely recurring monthly allowance, not an
    # expiring trial credit. Leave ABSTRACT_EMAIL_API_KEY unset to skip it
    # entirely — PERSON_ENRICHMENT then runs on whatever other providers
    # (Apollo) are configured, exactly as before this was added.
    abstract_email_api_key: str | None = None
    abstract_email_base_url: str = "https://emailvalidation.abstractapi.com/v1"

    # Abstract API's Phone Validation endpoint — a THIRD, independent
    # PERSON_ENRICHMENT provider (see app/providers/
    # abstract_phone_verification.py's own module docstring). Same account
    # family/free-tier shape as the email sibling above (100 requests/month,
    # recurring, no card). Honest scope: the documented response has no
    # real-time reachability field, only format/numbering-plan validity and
    # line-type/carrier — this closes this codebase's previous ZERO phone
    # capability with a real, narrowly-scoped signal, not a claim of parity
    # with a dedicated phone-data vendor. Leave ABSTRACT_PHONE_API_KEY unset
    # to skip it entirely — PERSON_ENRICHMENT then runs on whatever other
    # providers are configured, exactly as before this was added.
    abstract_phone_api_key: str | None = None
    abstract_phone_base_url: str = "https://phonevalidation.abstractapi.com/v1"

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
    #
    # Raised from an original 5 to 25 (still fails closed by default, still
    # meaningfully tighter than discovery_limit/target_count's own schema
    # ranges of [1,100]/[1,1000] — this is not a removal of the rail, only
    # a less cramped one): with items 1-2's real COMPANY_ENRICHMENT/signal
    # providers now landed, a 5-company first run was too small a sample to
    # meaningfully judge real provider yield/quality/cost before a user
    # would need to raise this setting anyway — 25 is enough to see a
    # representative spread of PASS/HOLD/FAIL outcomes and real per-round
    # cost while still bounding a config mistake's blast radius on the very
    # first live call.
    live_test_mode: bool = True
    live_test_max_discovery_limit: int = 25
    live_test_max_target_count: int = 25

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

    # SEC EDGAR + Wikidata COMPANY_ENRICHMENT — free, public, no-API-key
    # data sources (see app/providers/sec_edgar.py / app/providers/
    # wikidata.py's own module docstrings for their honest, narrow coverage
    # scope: US SEC-registered public companies only / Wikidata-notable
    # companies only). OFF by default, unlike every genuinely optional
    # provider elsewhere in this file being "off" because no key is
    # configured — these have no key to configure at all, so they need
    # their own explicit opt-in flag instead, for two reasons: (1) tests
    # must never make real network calls by default (see tests/conftest.py's
    # own "respx-mocked, no global httpx blocking" design — an
    # unconditionally-registered real provider would silently hit the real
    # internet on every test that builds the default registry), and (2) a
    # user should be able to choose not to enrich with public-registry data
    # even though it costs nothing, e.g. to keep discovery fully offline-
    # testable or to avoid the (small, honest) latency of two extra HTTP
    # calls per company. Set to true to enable both providers together;
    # see app/providers/default_registry.py for the one place this is read.
    enable_free_company_enrichment_providers: bool = False

    # Signal Check — a Tavily-backed COMPANY_ENRICHMENT provider that makes
    # two targeted, real web searches per company (hiring-for-marketing,
    # funding announcements) so app/services/commercial_signal_extractor.py's
    # existing MARKETING_HIRING/FUNDING keyword-matching engine has real
    # evidence text to actually detect, instead of only whatever text
    # happened to already be collected by an unrelated provider (see
    # app/providers/signal_check.py's own module docstring for the full
    # root-cause rationale). Reuses settings.tavily_api_key — no new
    # credential is required — so this flag exists purely to gate the
    # EXTRA search cost/latency (two more Tavily calls per company) behind
    # an explicit, conscious opt-in, exactly like
    # enable_free_company_enrichment_providers above gates its own extra
    # calls; it is never registered at all when tavily_api_key is unset,
    # matching every other Tavily-dependent provider in this codebase (see
    # app/providers/default_registry.py for the one place this is read).
    enable_signal_check_provider: bool = False

    # Tech Stack Detector — a free, no-API-key COMPANY_ENRICHMENT provider
    # (see app/providers/tech_stack_detector.py's own module docstring) that
    # fetches a company's own homepage once and checks it against a small,
    # curated table of unambiguous platform signatures (Shopify/WordPress/
    # HubSpot/etc.). Same two reasons as
    # enable_free_company_enrichment_providers above for needing its own
    # explicit opt-in flag despite costing nothing: tests must never make a
    # real network call by default, and a user should be able to opt out of
    # the extra per-company homepage-fetch latency even though it's free.
    enable_tech_stack_detector: bool = False

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

    # Export webhook — a generic, file-free delivery mechanism (see
    # app/services/export_webhook.py's own module docstring). When a
    # pipeline run completes, its export JSON (the same shape GET
    # /api/v1/pipeline-runs/{id}/export?format=JSON already returns) is
    # POSTed to this URL, HMAC-signed so the receiver can verify it really
    # came from this app. This is deliberately NOT a specific CRM
    # integration — it is what Zapier/Make/n8n themselves consume to bridge
    # to ANY downstream tool, so one webhook unlocks the whole live-
    # integration ecosystem rather than one vendor. No OAuth, no stored
    # third-party credential, no outbound call of any kind when unset
    # (the default) — a pipeline run behaves exactly as before this was
    # added. Never blocks or fails a pipeline run: see that module's own
    # "never raise" discipline.
    export_webhook_url: str | None = None
    # A per-deployment secret used to HMAC-sign the webhook payload (header
    # X-Lead-Agent-Signature) — required whenever export_webhook_url is
    # set, so a receiver can always verify authenticity; generate any
    # random string (e.g. `openssl rand -hex 32`). Not sent to the
    # receiving URL's own operator by any other channel — this app only
    # ever uses it locally to compute the signature.
    export_webhook_secret: str | None = None
    export_webhook_timeout_seconds: float = 10.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
