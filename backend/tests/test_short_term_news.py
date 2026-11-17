"""News freshness, policy context, fallback, and signal-suspension regressions."""
from datetime import timedelta
from unittest.mock import patch

import pytest

from app.config import Settings
from app.services import short_term_news as news
from app.services.news import _epoch_to_iso
from tests.test_short_term_forecast import NOW, fixture
from app.services.short_term_forecast import build


def feed(title="Dollar rises after economic data", *, minutes=5, **changes):
    item = {"headline": title, "source": "Test news", "url": "https://example.com/story",
            "published_at": (NOW-timedelta(minutes=minutes)).isoformat()}
    item.update(changes)
    return {"status": "LIVE_POLL", "provider": "finnhub", "checked_at": NOW.isoformat(), "items": [item]}


@pytest.mark.parametrize("headline,direction", [
    ("Fed cuts interest rates", "SELL_USD"), ("Banxico cuts interest rates", "BUY_USD"),
    ("Federal Reserve raises rates", "BUY_USD"), ("Banxico raises rates", "SELL_USD"),
    ("Mexican peso gains after trade agreement", "SELL_USD"),
    ("USD/MXN rises on tariff announcement", "BUY_USD"),
    ("Fed may cut rates", None), ("Banxico expected to cut rates", None),
    ("Fed does not cut rates", None), ("Fed cuts rates while Banxico raises rates", None),
    ("Dollar rises while peso gains", None),
    ("Canadian dollar rises", None), ("Argentine peso falls", None),
])
def test_does_not_confuse_us_and_mexican_policy_or_conditional_headlines(headline, direction):
    assert news._interpret(headline)[0] == direction


def test_confirmed_catalyst_keeps_price_target_and_shows_source():
    points, fix, payload = fixture(slope=.003, geometric=True)
    payload["news_watch"] = feed()
    out = build(points, fix, payload, now=NOW)
    assert out["action"] == "BUY USD / SELL MXN NOW"
    assert out["news"]["reaction"]["confirmed"]
    assert out["news"]["headlines"][0]["source"] == "Test news"
    assert "not separately calibrated" in out["news"]["target_basis"]


@pytest.mark.parametrize("change", [{"status":"fallback"}, {"status":"mock"}, {"checked_at":(NOW-timedelta(seconds=91)).isoformat()}, {"checked_at":None}])
def test_unverified_news_blocks_action_and_target(change):
    points, fix, payload = fixture()
    payload["news_watch"] = {**feed(), **change}
    out = build(points, fix, payload, now=NOW)
    assert out["action"] == "WAIT"
    assert out["expected_rate"] is None


@pytest.mark.parametrize("headline", ["US CPI beats forecast", "New tariffs announced on Mexico", "Missile attack in Middle East", "Fed may cut rates"])
def test_unclear_fresh_shock_suspends_price_only_forecast(headline):
    points, fix, payload = fixture()
    payload["news_watch"] = feed(headline)
    out = build(points, fix, payload, now=NOW)
    assert out["action"] == "WAIT"
    assert out["expected_rate"] is None
    assert "unclear USD/MXN effect" in " ".join(out["wait_reasons"])


def test_price_must_confirm_headline_and_legacy_sentiment_is_ignored():
    points, fix, payload = fixture(slope=-.0008)
    payload["news_watch"] = feed("Banxico cuts interest rates", sentiment="mxn_bullish")
    out = build(points, fix, payload, now=NOW)
    assert out["action"] == "WAIT"
    assert out["news"]["direction"] == "BUY_USD"
    assert not out["news"]["reaction"]["confirmed"]


def test_repeated_old_story_does_not_become_breaking_news():
    points, fix, _ = fixture()
    result = news.assess(feed("Banxico cuts rates", minutes=1500), points, fix, now=NOW)
    assert result["status"] == "CHECKED_NO_FRESH_CATALYST"
    assert result["headlines"] == []
    assert result["wait_reasons"]


def test_missing_or_future_publication_timestamp_cannot_be_called_fresh():
    points, fix, _ = fixture()
    for date in (None, "invalid", (NOW+timedelta(hours=1)).isoformat()):
        result = news.assess(feed(published_at=date), points, fix, now=NOW)
        assert result["wait_reasons"]
        assert result["headlines"] == []
    assert _epoch_to_iso(None) is None


def test_duplicate_headlines_are_not_counted_as_independent_confirmation():
    points, fix, _ = fixture()
    data = feed()
    data["items"] *= 5
    result = news.assess(data, points, fix, now=NOW)
    assert len(result["headlines"]) == 1


def test_conflicting_headlines_pause_signal():
    points, fix, payload = fixture()
    data = feed()
    data["items"] += feed("Mexican peso gains")['items']
    payload["news_watch"] = data
    out = build(points, fix, payload, now=NOW)
    assert out["action"] == "WAIT"
    assert any("conflicting" in r for r in out["wait_reasons"])


def test_mock_fallback_is_not_saved_or_presented_as_live():
    class Provider:
        source = "fallback"
        def get_news(self): return feed()["items"]
    news._cache = None
    try:
        with patch.object(news, "get_news_provider", return_value=Provider()):
            result = news.snapshot(settings=Settings(use_mock_data=False,news_api_key="test"))
        assert result["status"] == "UNAVAILABLE"
        assert result["items"] == []
    finally:
        news._cache = None


def test_live_provider_is_polled_only_once_inside_minute_cache():
    class Provider:
        source = "live"
        def get_news(self): return []
    news._cache = None
    try:
        with patch.object(news, "get_news_provider", return_value=Provider()) as provider:
            settings=Settings(use_mock_data=False,news_api_key="test")
            news.snapshot(settings=settings)
            news.snapshot(settings=settings)
            assert provider.call_count == 1
    finally:
        news._cache = None


def test_background_watch_saves_first_seen_once_with_original_publication_time():
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session
    from app.models import NewsItem
    class Provider:
        source = "live"
        def get_news(self): return feed()["items"]
    engine = create_engine("sqlite://")
    NewsItem.__table__.create(engine)
    news._cache = None
    try:
        with Session(engine) as db, patch.object(news, "get_news_provider", return_value=Provider()):
            settings = Settings(use_mock_data=False,news_api_key="test")
            news.snapshot(db, settings=settings)
            first = db.execute(select(NewsItem)).scalars().one()
            at = first.created_at
            news.snapshot(db, settings=settings)
            again = db.execute(select(NewsItem)).scalars().one()
            assert again.created_at == at
            assert again.published_at == feed()["items"][0]["published_at"]
            assert again.provider == "live"
    finally:
        news._cache = None
