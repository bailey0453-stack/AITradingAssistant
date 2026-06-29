"""Application configuration.

All settings are environment-driven so the same code runs locally (SQLite +
mock data) and in production (Postgres + live data providers). Nothing here is
secret by default; real API keys are supplied via environment variables.
"""

from functools import lru_cache
from typing import Dict, List, Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- General ---
    app_name: str = "AI Trading Assistant"
    environment: str = "development"

    # --- Database ---
    # SQLite by default; set to a postgresql+psycopg:// URL in production.
    database_url: str = "sqlite:///./aitrading.db"

    # --- Data provider mode ---
    # When True (default), services return realistic mocked data so the API is
    # fully functional without any external API keys configured.
    use_mock_data: bool = True

    # --- External provider API keys (placeholders; optional) ---
    # FX / market data
    fx_api_key: Optional[str] = None
    market_data_api_key: Optional[str] = None
    # FX provider selection + endpoint. Default targets Open Exchange Rates
    # (https://openexchangerates.org), which returns USD-based rates.
    fx_provider: str = "openexchangerates"
    fx_base_url: Optional[str] = None
    # Macro indicators
    # FRED (St. Louis Fed) — US 2Y / 10Y treasury yields + macro series.
    fred_api_key: Optional[str] = None
    # Alpha Vantage — DXY / gold / oil / VIX / S&P where available.
    alpha_vantage_api_key: Optional[str] = None
    # Cache macro fetches this many seconds to respect provider rate limits
    # (Alpha Vantage free tier is ~25 requests/day). 0 disables caching.
    macro_cache_seconds: int = 600
    # News
    news_api_key: Optional[str] = None
    # News provider selection + endpoint. Default targets NewsAPI.org; the
    # provider layer also leaves room for Finnhub / Financial Modeling Prep.
    news_provider: str = "newsapi"
    news_base_url: Optional[str] = None
    # Economic calendar
    calendar_api_key: Optional[str] = None
    # Calendar provider selection + endpoint. Default targets Trading Economics.
    # Set CALENDAR_PROVIDER=csv (with CALENDAR_CSV_PATH) to import a calendar from
    # a local CSV with no API key.
    calendar_provider: str = "tradingeconomics"
    calendar_base_url: Optional[str] = None
    # Path to an importable calendar CSV (used when CALENDAR_PROVIDER=csv).
    calendar_csv_path: Optional[str] = None
    # AI model (e.g. OpenAI) for narrative analysis
    openai_api_key: Optional[str] = None
    ai_model: str = "gpt-4o-mini"

    # --- HTTP ---
    http_timeout_seconds: float = 8.0

    # --- Scheduled jobs (cron) ---
    # Shared secret protecting the scheduled job endpoints. Vercel Cron sends it
    # as `Authorization: Bearer <CRON_SECRET>`. When unset, job endpoints are
    # rejected in production and allowed only in mock/dev mode (for local runs).
    cron_secret: Optional[str] = None

    # --- Signal weighting engine ---
    # Optional override of the default signal weights, as a JSON object in the
    # SIGNAL_WEIGHTS env var, e.g. SIGNAL_WEIGHTS='{"dxy": 9, "oil": 6}'. Unknown
    # keys are ignored. Defaults live in services/signal_weights.py.
    signal_weights: Optional[Dict[str, float]] = None

    # --- Historical intelligence engine (Phase 4) ---
    # Optional JSON overrides; unknown keys ignored. Defaults live in the history
    # services. SIMILARITY_WEIGHTS tunes feature importance for "find events like
    # this"; CONFIDENCE_WEIGHTS tunes how signal/historical/regime/volatility/
    # data-quality combine into blended confidence.
    similarity_weights: Optional[Dict[str, float]] = None
    confidence_weights: Optional[Dict[str, float]] = None
    # Which importer seeds historical backfill (mock | csv | yahoo | fred | ...).
    history_importer: str = "mock"

    # --- Market data freshness (stale-fallback safety) ---
    # Max age (MINUTES) a cached real FX quote may be while the market is OPEN
    # before it is considered stale and the dashboard shows "Market data
    # unavailable" instead of an outdated number. When the market is closed the
    # most recent real session quote is served (it is not moving), but a quote
    # older than the last market close by more than this is still treated as
    # stale. Production never substitutes hardcoded mock rates for live data.
    market_max_age_minutes: int = 180

    # --- Market hours + refresh policies (Phase 5.1) ---
    # Per-provider refresh cadence override, in MINUTES, as a JSON object, e.g.
    # REFRESH_POLICIES='{"usdmxn": 30, "news": 10}'. Unknown keys ignored.
    # Defaults live in services/cache_manager.py.
    refresh_policies: Optional[Dict[str, float]] = None
    # FX market holidays as a JSON list of ISO dates, e.g.
    # MARKET_HOLIDAYS='["2026-01-01", "2026-12-25"]'. Empty by default; the
    # weekend schedule always applies regardless.
    market_holidays: Optional[List[str]] = None

    @property
    def is_mock(self) -> bool:
        """Global mock toggle. Mock mode is on whenever USE_MOCK_DATA is true."""
        return self.use_mock_data

    @property
    def fx_live_enabled(self) -> bool:
        """Live FX is attempted only when mock mode is off AND a key is set."""
        return (not self.use_mock_data) and bool(self.fx_api_key)

    @property
    def news_live_enabled(self) -> bool:
        """Live news is attempted only when mock mode is off AND a key is set."""
        return (not self.use_mock_data) and bool(self.news_api_key)

    @property
    def macro_live_enabled(self) -> bool:
        """Live macro (FRED / Alpha Vantage) attempted when off-mock + any key."""
        return (not self.use_mock_data) and bool(
            self.fred_api_key or self.alpha_vantage_api_key
        )

    @property
    def calendar_live_enabled(self) -> bool:
        """Live calendar is attempted only when mock mode off AND a key is set."""
        return (not self.use_mock_data) and bool(self.calendar_api_key)

    @property
    def calendar_csv_enabled(self) -> bool:
        """Importable CSV calendar is used when selected + a path is configured.

        Unlike the live API providers, this needs no key and works in mock mode,
        so an operator can import a real calendar export without a paid feed.
        """
        return (self.calendar_provider or "").lower() == "csv" and bool(
            self.calendar_csv_path
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
