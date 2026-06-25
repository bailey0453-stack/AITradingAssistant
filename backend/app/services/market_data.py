"""Market data providers for USD/MXN and macro drivers.

The router depends on `get_market_provider()`, which returns a mock provider by
default. To add live data, implement a new provider class (e.g. backed by an FX
API + FRED for DXY/yields + an oil feed) and return it from the factory when the
relevant API keys are configured.
"""

from __future__ import annotations

import random
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field

from app.config import Settings, get_settings


@dataclass
class MarketData:
    pair: str = "USDMXN"
    usdmxn: float | None = None
    dxy: float | None = None  # US Dollar Index (placeholder)
    treasury_yield: float | None = None  # US 10Y yield % (placeholder)
    oil: float | None = None  # WTI crude USD/bbl (placeholder)
    source: str = "mock"
    drivers: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


class MarketDataProvider(ABC):
    source = "base"

    @abstractmethod
    def get_usdmxn(self) -> MarketData:
        """Return a current USD/MXN snapshot with macro drivers."""
        raise NotImplementedError


class MockMarketDataProvider(MarketDataProvider):
    """Deterministic-ish but lively mocked data, good enough to build against."""

    source = "mock"

    # Rough, realistic baselines around which we jitter.
    BASE_USDMXN = 17.85
    BASE_DXY = 104.2
    BASE_YIELD = 4.32
    BASE_OIL = 76.5

    def get_usdmxn(self) -> MarketData:
        usdmxn = round(self.BASE_USDMXN + random.uniform(-0.25, 0.25), 4)
        dxy = round(self.BASE_DXY + random.uniform(-0.6, 0.6), 2)
        treasury_yield = round(self.BASE_YIELD + random.uniform(-0.08, 0.08), 3)
        oil = round(self.BASE_OIL + random.uniform(-2.5, 2.5), 2)

        return MarketData(
            pair="USDMXN",
            usdmxn=usdmxn,
            dxy=dxy,
            treasury_yield=treasury_yield,
            oil=oil,
            source=self.source,
            drivers={
                # Deltas vs baseline give the analysis engine something to chew on.
                "dxy_delta": round(dxy - self.BASE_DXY, 3),
                "yield_delta": round(treasury_yield - self.BASE_YIELD, 3),
                "oil_delta": round(oil - self.BASE_OIL, 3),
                "usdmxn_delta": round(usdmxn - self.BASE_USDMXN, 4),
            },
        )


# --- Placeholder for a future live provider -------------------------------
class LiveMarketDataProvider(MarketDataProvider):  # pragma: no cover - stub
    """Skeleton for real integrations. Not wired until keys are configured."""

    source = "live"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def get_usdmxn(self) -> MarketData:
        # TODO: call FX API for USD/MXN, FRED for DXY + 10Y yield, oil feed.
        raise NotImplementedError(
            "LiveMarketDataProvider is not implemented yet. "
            "Set USE_MOCK_DATA=true or implement live fetches."
        )


def get_market_provider(settings: Settings | None = None) -> MarketDataProvider:
    settings = settings or get_settings()
    if settings.is_mock:
        return MockMarketDataProvider()
    return LiveMarketDataProvider(settings)
