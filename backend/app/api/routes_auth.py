"""Kite authentication endpoints (daily login flow)."""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from app.kite.client import kite_service
from app.models.schemas import AuthStatus, SessionRequest
from app.services import feed
from app.state import market_state

log = logging.getLogger("tradewell.auth")
router = APIRouter(prefix="/auth", tags=["auth"])


def _status(extra: str | None = None) -> AuthStatus:
    authed = kite_service.is_authenticated
    if extra:
        message = extra
    elif feed.running and market_state.ticker_dead:
        message = "Feed disconnected — re-login (or restart the backend) to resume live data"
    elif feed.running:
        message = "Live feed running"
    elif authed:
        message = "Authenticated; feed starting"
    else:
        message = "Open login_url, then POST the request_token to /auth/session"
    return AuthStatus(
        authenticated=authed,
        api_key_configured=kite_service.api_key_configured,
        login_url=None if authed else kite_service.login_url(),
        user_id=kite_service.user_id,
        ticker_connected=market_state.ticker_connected,
        instruments_loaded=market_state.instruments_loaded,
        message=message,
    )


@router.get("/status", response_model=AuthStatus)
def auth_status() -> AuthStatus:
    return _status()


@router.post("/feed/restart", response_model=AuthStatus)
async def restart_feed() -> AuthStatus:
    """Operator recovery: rebuild the ticker + universe on the current token.
    Cheaper than a Kite re-login (which burns a request_token) or a process
    bounce when the feed wedges."""
    if not kite_service.is_authenticated:
        raise HTTPException(status_code=409, detail="Not authenticated — log in first")
    try:
        await feed.restart()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Feed restart failed: {exc}") from exc
    return _status()


@router.post("/session", response_model=AuthStatus)
async def create_session(body: SessionRequest) -> AuthStatus:
    if not kite_service.api_key_configured:
        raise HTTPException(status_code=400, detail="KITE_API_KEY / KITE_API_SECRET not configured")
    try:
        kite_service.generate_session(body.request_token)
    except Exception as exc:
        raise HTTPException(status_code=401, detail=f"Session exchange failed: {exc}") from exc

    # Start the feed — or RESTART it if it's running on the previous token
    # (daily re-login while the server stayed up overnight).
    try:
        await feed.ensure_current_token()
    except Exception as exc:
        # The login itself succeeded and the request_token is single-use —
        # never 500 here or the UI would ask the user to burn another login.
        log.exception("Feed start failed after login")
        return _status(extra=f"Login OK, but the feed failed to start: {exc}")

    return _status()
