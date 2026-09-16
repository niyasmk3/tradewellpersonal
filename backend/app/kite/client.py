"""Thin wrapper around KiteConnect: auth/session + authenticated REST access.

Kite's access token is issued per login and expires each morning, so the flow is:
    1. GET  /auth/status   -> returns login_url when not authenticated
    2. user opens login_url in a browser, logs in, Kite redirects back with
       ?request_token=... on the app's redirect URL
    3. POST /auth/session {request_token} -> we exchange it for an access_token
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import time

from kiteconnect import KiteConnect
from kiteconnect.exceptions import TokenException

from app.config import get_settings

log = logging.getLogger("tradewell.kite")

# Same-day access-token cache so restarts don't force a re-login. Kite tokens
# expire ~07:30 IST next morning, so we key the cache on the IST calendar date.
# Anchored to the backend directory (not CWD) so launching uvicorn from another
# directory still finds the same cache.
_TOKEN_CACHE = Path(__file__).resolve().parents[2] / ".kite_session.json"


def _ist_today() -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).strftime("%Y-%m-%d")


class KiteService:
    def __init__(self) -> None:
        settings = get_settings()
        self._api_key = settings.kite_api_key
        self._api_secret = settings.kite_api_secret
        self.access_token: str | None = None
        self.user_id: str | None = None
        # (state, checked_at) cache for validate_token — 'unknown' until probed.
        self._token_check: tuple[str, float] = ("unknown", 0.0)
        self.kite = KiteConnect(api_key=self._api_key) if self._api_key else None

        # Reuse a previously issued token: .env wins, then today's cached token.
        if self.kite is not None:
            if settings.kite_access_token:
                self.set_access_token(settings.kite_access_token)
            else:
                self._load_cached_token()

    def _load_cached_token(self) -> None:
        try:
            if not _TOKEN_CACHE.exists():
                return
            data = json.loads(_TOKEN_CACHE.read_text())
            if data.get("date") == _ist_today() and data.get("access_token"):
                self.set_access_token(data["access_token"])
                self.user_id = data.get("user_id")
                log.info("Reused cached Kite session for user %s", self.user_id)
        except Exception as exc:  # pragma: no cover
            log.warning("Could not load cached token: %s", exc)

    def _save_cached_token(self) -> None:
        try:
            _TOKEN_CACHE.write_text(
                json.dumps(
                    {"access_token": self.access_token, "user_id": self.user_id, "date": _ist_today()}
                )
            )
            os.chmod(_TOKEN_CACHE, 0o600)  # token grants full account access — owner-only
        except Exception as exc:  # pragma: no cover
            log.warning("Could not cache token: %s", exc)

    @property
    def api_key_configured(self) -> bool:
        return bool(self._api_key and self._api_secret)

    @property
    def is_authenticated(self) -> bool:
        return bool(self.kite is not None and self.access_token)

    def validate_token(self, max_age_s: float = 300.0, force: bool = False) -> str:
        """'valid' / 'invalid' / 'unknown' — does Kite actually ACCEPT the token?

        is_authenticated only proves a token EXISTS; three stale-dashboard
        incidents (09-Aug expired-but-green, 16-Sep today-dated-but-rejected)
        came from trusting it. This probes Kite REST at most once per
        max_age_s and caches the verdict. A TokenException flips the session
        dead via invalidate() so the login gate reappears everywhere; network
        or other errors return 'unknown' and KEEP the token — a wifi blip
        must never flash the login gate.
        """
        if not self.is_authenticated:
            return "invalid"
        state, checked_at = self._token_check
        now = time.time()
        if not force and now - checked_at < max_age_s:
            return state
        # Claim the slot before probing so concurrent health polls (threadpool)
        # can't stampede Kite with parallel profile() calls.
        self._token_check = (state, now)
        try:
            self.kite.profile()
            self._token_check = ("valid", time.time())
        except TokenException:
            log.warning("Token probe: Kite REJECTED the stored token")
            self._token_check = ("invalid", time.time())
            self.invalidate()
        except Exception as exc:  # network / rate limit / Kite outage
            log.debug("Token probe inconclusive: %s", exc)
            self._token_check = ("unknown", time.time())
        return self._token_check[0]

    def login_url(self) -> str | None:
        return self.kite.login_url() if self.kite is not None else None

    def set_access_token(self, token: str) -> None:
        if self.kite is None:
            raise RuntimeError("KITE_API_KEY is not configured")
        self.access_token = token
        self.kite.set_access_token(token)
        self._token_check = ("unknown", 0.0)   # fresh token, fresh verdict

    def invalidate(self) -> None:
        """Drop a token Kite has rejected (daily ~07:30 IST flush or revocation)
        so is_authenticated flips false and the login flow reappears in the UI."""
        if self.access_token is None:
            return
        log.warning("Kite access token invalidated — daily re-login required")
        self.access_token = None
        try:
            _TOKEN_CACHE.unlink(missing_ok=True)
        except Exception:  # pragma: no cover
            pass

    def generate_session(self, request_token: str) -> str:
        """Exchange a request_token for an access_token and activate it."""
        if self.kite is None:
            raise RuntimeError("KITE_API_KEY is not configured")
        data = self.kite.generate_session(request_token, api_secret=self._api_secret)
        self.set_access_token(data["access_token"])
        self.user_id = data.get("user_id")
        self._save_cached_token()
        log.info("Kite session established for user %s", self.user_id)
        return data["access_token"]


# Module-level singleton.
kite_service = KiteService()
