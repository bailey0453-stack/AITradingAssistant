"""Read the private server cache without making another provider request."""
import math
import os
from datetime import datetime, timezone
from urllib.parse import urlsplit

import httpx


def fetch_shared_usdmxn(timeout=20):
    url = os.environ.get('FX_SHARED_URL', '')
    token = os.environ.get('FX_SHARED_TOKEN', '')
    parts = urlsplit(url)
    if parts.scheme != 'https' or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment or not token:
        raise RuntimeError('Shared FX service is not configured')
    try:
        response = httpx.get(url, headers={'Authorization': 'Bearer ' + token}, timeout=timeout, follow_redirects=False)
        response.raise_for_status()
        result = response.json()
        payload = result.get('payload') or {}
        if not result.get('success') or payload.get('base') != 'USD' or (result.get('metadata') or {}).get('stale'):
            raise ValueError('FX cache is stale or unavailable')
        rate = float((payload.get('rates') or {}).get('MXN', 0))
        timestamp = float(payload.get('oerTimestamp', 0))
        if not math.isfinite(rate) or rate <= 0 or not math.isfinite(timestamp) or timestamp <= 0:
            raise ValueError('FX cache is invalid')
        if timestamp > datetime.now(timezone.utc).timestamp() + 60:
            raise ValueError('FX timestamp is in the future')
        return rate, datetime.fromtimestamp(timestamp, timezone.utc).isoformat()
    except Exception:
        # HTTP errors can contain URLs, headers or credentials; expose none.
        raise RuntimeError('Shared FX rates unavailable') from None
