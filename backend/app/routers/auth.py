"""Border Currency SSO redeem / session / logout."""

from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from app.config import get_settings
from app.services.border_handoff_token import HandoffTokenError, verify_handoff_token
from app.services.customer_session import COOKIE_NAME, encode_session_token, verify_session_token
from app.services.customer_auth import extract_session_token

router = APIRouter(prefix="/auth", tags=["auth"])


class RedeemBody(BaseModel):
    token: str


def _public_session(claims: dict) -> dict:
    return {
        "authenticated": True,
        "userId": claims.get("userId"),
        "email": claims.get("email"),
        "name": claims.get("name") or "",
        "role": claims.get("role") or "",
        "customer": str(claims.get("role") or "") == "customer_ai_trading",
    }


@router.post("/sso/redeem")
def redeem_sso(body: RedeemBody, response: Response) -> dict:
    settings = get_settings()
    secret = settings.border_sso_signing_secret
    session_secret = settings.session_signing_secret
    if not secret or not session_secret:
        raise HTTPException(status_code=503, detail="SSO is not configured")

    try:
        claims = verify_handoff_token(
            body.token,
            secret=secret,
            now_sec=int(time.time()),
            audience=settings.border_sso_audience,
            issuer=settings.border_sso_issuer,
        )
    except HandoffTokenError as exc:
        status = 401
        raise HTTPException(status_code=status, detail={"code": exc.code, "message": exc.message}) from exc

    session_token = encode_session_token(
        claims,
        secret=session_secret,
        ttl_sec=settings.aita_session_ttl_seconds,
    )
    response.set_cookie(
        COOKIE_NAME,
        session_token,
        httponly=True,
        secure=settings.environment != "development",
        samesite="lax",
        max_age=settings.aita_session_ttl_seconds,
        path="/",
    )
    return {**_public_session(claims), "token": session_token}


@router.get("/session")
def session_info(request: Request) -> dict:
    settings = get_settings()
    token = extract_session_token(request)
    claims = verify_session_token(token or "", secret=settings.session_signing_secret or "")
    if not claims:
        raise HTTPException(status_code=401, detail="Authentication required")
    return _public_session(claims)


@router.post("/logout")
def logout(response: Response) -> dict:
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}
