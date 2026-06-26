"""Application configuration.

All settings are environment-driven so the same code runs locally (SQLite +
mock data) and in production (Postgres + live data providers). Nothing here is
secret by default; real API keys are supplied via environment variables.
"""

from functools import lru_cache
from typing import Dict, Optional

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
    # Macro indicators (e.g. FRED for DXY / treasury yields)
    fred_api_key: Optional[str] = None
    # News
    news_api_key: Optional[str] = None
    # News provider selection + endpoint. Default targets NewsAPI.org; the
    # provider layer also leaves room for Finnhub / Financial Modeling Prep.
    news_provider: str = "newsapi"
    news_base_url: Optional[str] = None
    # Economic calendar
    calendar_api_key: Optional[str] = None
    # Calendar provider selection + endpoint. Default targets Trading Economics.
    calendar_provider: str = "tradingeconomics"
    calendar_base_url: Optional[str] = None
    # AI model (e.g. OpenAI) for narrative analysis
    openai_api_key: Optional[str] = None
    ai_model: str = "gpt-4o-mini"

    # --- HTTP ---
    http_timeout_seconds: float = 8.0

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
    def calendar_live_enabled(self) -> bool:
        """Live calendar is attempted only when mock mode off AND a key is set."""
        return (not self.use_mock_data) and bool(self.calendar_api_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()
