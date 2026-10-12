"""Dependency-light integration tests for shared cache ingestion."""
import importlib.util
import os
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch
from datetime import datetime, timezone

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
try:
    import httpx
except ModuleNotFoundError:
    httpx = types.ModuleType('httpx')
    sys.modules['httpx'] = httpx
from app.services.shared_fx import fetch_shared_usdmxn
try:
    from app.services.market_data import get_market_data
except ModuleNotFoundError:
    # This standalone check can also run in a scratch runtime without Pydantic.
    config = types.ModuleType('app.config')
    config.Settings = object
    config.get_settings = lambda: None
    sys.modules['app.config'] = config
    from app.services.market_data import get_market_data


class SharedFxTest(unittest.TestCase):
    def setUp(self):
        self.timestamp = int(datetime.now(timezone.utc).timestamp()) - 120
        self.result = {'success': True, 'payload': {'base': 'USD', 'rates': {'MXN': 18}, 'oerTimestamp': self.timestamp}, 'metadata': {'stale': False}}
        self.env = patch.dict(os.environ, {'FX_SHARED_URL': 'https://central.test/api/shared-fx-rates', 'FX_SHARED_TOKEN': 'private-test-token'})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.calls = []
        def get(url, **kwargs):
            self.calls.append((url, kwargs))
            return types.SimpleNamespace(raise_for_status=lambda: None, json=lambda: self.result)
        self.http = patch.object(httpx, 'get', get, create=True)
        self.http.start(); self.addCleanup(self.http.stop)

    def test_shared_rate_keeps_provider_timestamp_and_avoids_provider_fetch(self):
        settings = types.SimpleNamespace(is_mock=False, fx_api_key=None, fx_base_url=None, http_timeout_seconds=10, fx_provider='openexchangerates', macro_live_enabled=False)
        result = get_market_data(settings)
        self.assertEqual(result.usdmxn, 18)
        self.assertEqual(result.source, 'cached')
        self.assertEqual(result.provider, 'shared_open_exchange_rates')
        self.assertEqual(int(datetime.fromisoformat(result.timestamp).timestamp()), self.timestamp)
        self.assertGreaterEqual(result.field_meta['usdmxn']['age_seconds'], 120)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][0], 'https://central.test/api/shared-fx-rates')
        self.assertEqual(self.calls[0][1]['headers']['Authorization'], 'Bearer private-test-token')

    def test_stale_invalid_and_future_snapshots_are_rejected(self):
        for field, value in [('stale', True), ('rate', -1), ('rate', float('nan')), ('timestamp', self.timestamp + 10000), ('base', 'EUR')]:
            with self.subTest(field=field):
                self.result = {'success': True, 'payload': {'base': 'USD', 'rates': {'MXN': 18}, 'oerTimestamp': self.timestamp}, 'metadata': {'stale': False}}
                if field == 'stale': self.result['metadata']['stale'] = value
                elif field == 'rate': self.result['payload']['rates']['MXN'] = value
                elif field == 'base': self.result['payload']['base'] = value
                else: self.result['payload']['oerTimestamp'] = value
                with self.assertRaisesRegex(RuntimeError, '^Shared FX rates unavailable$'):
                    fetch_shared_usdmxn()

    def test_no_direct_provider_fallback_when_server_unavailable(self):
        self.result['metadata']['stale'] = True
        settings = types.SimpleNamespace(is_mock=False, fx_api_key='provider-test-key', fx_base_url=None, http_timeout_seconds=10, fx_provider='openexchangerates', macro_live_enabled=False)
        result = get_market_data(settings)
        self.assertIsNone(result.usdmxn)
        self.assertEqual(result.field_sources['usdmxn'], 'unavailable')
        self.assertEqual(len(self.calls), 1)


if __name__ == '__main__':
    unittest.main()
