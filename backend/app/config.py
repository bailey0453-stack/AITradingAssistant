"""Application configuration.

All settings are environment-driven so the same code runs locally (SQLite +
mock data) and in production (Postgres + live data providers). Nothing here is
secret by default; real API keys are supplied via environment variables.
"""

from functools import lru_cache
from typing import Dict, List, Optional

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")
    app_name: str = "AI Trading Assistant"
    environment: str = "development"
    database_url: str = "sqlite:///./aitrading.db"
    use_mock_data: bool = True
    fx_api_key: Optional[str] = None
    market_data_api_key: Optional[str] = None
    fx_provider: str = "openexchangerates"
    fx_base_url: Optional[str] = None
    fred_api_key: Optional[str] = None
    alpha_vantage_api_key: Optional[str] = None
    macro_cache_seconds: int = 600
    news_api_key: Optional[str] = None
    news_provider: str = "newsapi"
    news_base_url: Optional[str] = None
    calendar_api_key: Optional[str] = None
    calendar_provider: str = "auto"
    calendar_base_url: Optional[str] = None
    calendar_csv_path: Optional[str] = None
    openai_api_key: Optional[str] = None
    ai_model: str = "gpt-4o-mini"
    http_timeout_seconds: float = 8.0
    cron_secret: Optional[str] = None
    signal_weights: Optional[Dict[str, float]] = None
    similarity_weights: Optional[Dict[str, float]] = None
    confidence_weights: Optional[Dict[str, float]] = None
    history_importer: str = "mock"
    history_lookback_days: int = 3650
    market_max_age_minutes: int = 180
    refresh_policies: Optional[Dict[str, float]] = None
    market_holidays: Optional[List[str]] = None

    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None

    centroid_md_host: Optional[str] = None
    centroid_md_port: Optional[int] = None
    centroid_md_username: Optional[str] = None
    centroid_md_password: Optional[str] = None
    centroid_md_sender_comp_id: Optional[str] = None
    centroid_md_target_comp_id: Optional[str] = None
    centroid_md_ssl: bool = False
    centroid_md_reset_on_logon: bool = True
    centroid_md_symbol_usdmxn: str = Field(default="USD/MXN", validation_alias=AliasChoices("centroid_symbol_usdmxn", "centroid_md_symbol_usdmxn"))
    centroid_md_enabled: bool = False
    centroid_md_subscription_request_type: int = 1
    centroid_md_market_depth: int = 1
    centroid_md_include_md_update_type: bool = True
    fix_worker_base_url: Optional[str] = None

    centroid_td_host: Optional[str] = None
    centroid_td_port: Optional[int] = None
    centroid_td_username: Optional[str] = None
    centroid_td_password: Optional[str] = None
    centroid_td_sender_comp_id: Optional[str] = None
    centroid_td_target_comp_id: Optional[str] = None
    centroid_td_account: Optional[str] = None
    centroid_td_ssl: bool = False
    centroid_td_reset_on_logon: bool = True

    @property
    def centroid_md_configured(self) -> bool:
        return bool(self.centroid_md_host and self.centroid_md_port and self.centroid_md_sender_comp_id and self.centroid_md_target_comp_id)

    @property
    def telegram_configured(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    @property
    def is_mock(self) -> bool:
        return self.use_mock_data

    @property
    def fx_live_enabled(self) -> bool:
        return (not self.use_mock_data) and bool(self.fx_api_key)

    @property
    def news_live_enabled(self) -> bool:
        return (not self.use_mock_data) and bool(self.news_api_key)

    @property
    def macro_live_enabled(self) -> bool:
        return (not self.use_mock_data) and bool(self.fred_api_key or self.alpha_vantage_api_key)

    @property
    def calendar_live_enabled(self) -> bool:
        return (not self.use_mock_data) and bool(self.calendar_api_key or self.fred_api_key)

    @property
    def calendar_official_enabled(self) -> bool:
        if not self.fred_api_key or self.use_mock_data:
            return False
        name = (self.calendar_provider or "auto").lower()
        if name in ("tradingeconomics", "finnhub", "csv", "mock"):
            return False
        return name in ("official", "fred", "composite", "auto", "")

    @property
    def calendar_csv_enabled(self) -> bool:
        return (self.calendar_provider or "").lower() == "csv" and bool(self.calendar_csv_path)


@lru_cache
def get_settings() -> Settings:
    return Settings()
