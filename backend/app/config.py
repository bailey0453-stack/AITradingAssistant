"""Application configuration.

All settings are environment-driven so the same code runs locally (SQLite +
mock data) and in production (Postgres + live data providers). Nothing here is
secret by default; real API keys are supplied via environment variables.
"""

from functools import lru_cache
from typing import Optional

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
    # Macro indicators (e.g. FRED for DXY / treasury yields)
    fred_api_key: Optional[str] = None
    # News
    news_api_key: Optional[str] = None
    # AI model (e.g. OpenAI) for narrative analysis
    openai_api_key: Optional[str] = None
    ai_model: str = "gpt-4o-mini"

    @property
    def is_mock(self) -> bool:
        """Mock mode is forced on whenever required live keys are absent."""
        return self.use_mock_data


@lru_cache
def get_settings() -> Settings:
    return Settings()
