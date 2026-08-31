"""Customer SSO / session tests for AI Trading Assistant.

Run from the backend directory:

    ./.venv/bin/python -m tests.test_customer_sso_auth
"""

from __future__ import annotations

import os
import time

os.environ["USE_MOCK_DATA"] = "true"
os.environ["AITA_REQUIRE_CUSTOMER_AUTH"] = "true"
os.environ["AI_TRADING_ASSISTANT_SSO_SECRET"] = "test-aita-sso-secret"
os.environ["AITA_SESSION_SECRET"] = "test-aita-session-secret"
os.environ["ADMIN_SECRET"] = "test-aita-admin-secret"
os.environ["CRON_SECRET"] = "test-aita-cron-secret"

from app.config import get_settings

get_settings.cache_clear()

from fastapi.testclient import TestClient

from app.main import app
from app.services.border_handoff_token import encode_handoff_token, verify_handoff_token, HandoffTokenError

SSO_SECRET = os.environ["AI_TRADING_ASSISTANT_SSO_SECRET"]
_passed = 0
_failed = 0


def check(name, fn):
    global _passed, _failed
    try:
        fn()
        _passed += 1
        print(f"  PASS  {name}")
    except Exception as exc:  # noqa: BLE001
        _failed += 1
        print(f"  FAIL  {name}: {exc}")


def handoff(now: int, **overrides) -> str:
    claims = {
        "userId": "cust-1",
        "email": "customer@example.com",
        "name": "Ada Customer",
        "role": "customer_ai_trading",
    }
    claims.update(overrides.pop("claims", {}))
    return encode_handoff_token(
        claims,
        secret=overrides.get("secret", SSO_SECRET),
        now_sec=now,
        ttl_sec=overrides.get("ttl_sec", 300),
        audience=overrides.get("audience", "ai-trading-assistant"),
        issuer=overrides.get("issuer", "border-currency-shipments"),
    )


def main() -> int:
    now = int(time.time())
    with TestClient(app) as client:
        def health_public():
            r = client.get("/health")
            assert r.status_code == 200, r.status_code
            assert r.json()["customer_auth_required"] is True

        def market_requires_auth():
            r = client.get("/market/usdmxn")
            assert r.status_code == 401, r.status_code

        def expired_token_rejected():
            token = handoff(now - 400, ttl_sec=60)
            try:
                verify_handoff_token(token, secret=SSO_SECRET, now_sec=now)
                raise AssertionError("expired token verified")
            except HandoffTokenError as exc:
                assert exc.code == "token_expired"

            r = client.post("/auth/sso/redeem", json={"token": token})
            assert r.status_code == 401, r.status_code

        def invalid_signature_rejected():
            token = handoff(now)
            bad = token[:-2] + ("A" if token[-2] != "A" else "B") + token[-1:]
            r = client.post("/auth/sso/redeem", json={"token": bad})
            assert r.status_code == 401, r.status_code

        def wrong_audience_rejected():
            token = handoff(now, audience="someone-else")
            r = client.post("/auth/sso/redeem", json={"token": token})
            assert r.status_code == 401, r.status_code

        def valid_redeem_creates_session():
            token = handoff(now)
            r = client.post("/auth/sso/redeem", json={"token": token})
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["email"] == "customer@example.com"
            assert body["role"] == "customer_ai_trading"
            assert body["customer"] is True
            assert "aita_session" in r.cookies

            session = client.get("/auth/session")
            assert session.status_code == 200, session.text
            assert session.json()["email"] == "customer@example.com"

            market = client.get("/market/usdmxn")
            assert market.status_code == 200, market.status_code

        def customer_cannot_use_admin():
            token = handoff(now + 1)
            client.post("/auth/sso/redeem", json={"token": token})
            r = client.post("/admin/research/rebuild-snapshots")
            assert r.status_code in (401, 403, 405, 422), r.status_code

        def admin_secret_still_works():
            client.cookies.clear()
            r = client.get(
                "/market/usdmxn",
                headers={"Authorization": "Bearer test-aita-admin-secret"},
            )
            assert r.status_code == 200, r.status_code

        def logout_clears_session():
            token = handoff(now + 2)
            client.post("/auth/sso/redeem", json={"token": token})
            out = client.post("/auth/logout")
            assert out.status_code == 200, out.status_code
            session = client.get("/auth/session")
            assert session.status_code == 401, session.status_code

        check("health remains public and reports customer_auth_required", health_public)
        check("intelligence API requires auth", market_requires_auth)
        check("expired handoff token is rejected", expired_token_rejected)
        check("invalid signature is rejected", invalid_signature_rejected)
        check("wrong audience is rejected", wrong_audience_rejected)
        check("valid Border token creates a session", valid_redeem_creates_session)
        check("customer session cannot use admin endpoints", customer_cannot_use_admin)
        check("admin secret still unlocks intelligence APIs", admin_secret_still_works)
        check("logout clears the session", logout_clears_session)

    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
