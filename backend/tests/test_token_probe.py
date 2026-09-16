"""validate_token: the health badge stops lying (16-Sep, third stale morning).

Pins: Kite-accepted token -> 'valid'; TokenException -> 'invalid' AND the
session invalidates (login gate returns); network errors -> 'unknown' and the
token SURVIVES (a wifi blip must not flash the gate); the verdict is cached;
a fresh token resets the cache. _TOKEN_CACHE is redirected to a temp dir so
invalidate() can never touch the real .kite_session.json from a test run.

Run:  python backend/tests/test_token_probe.py
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from kiteconnect.exceptions import TokenException

import app.kite.client as client_mod
from app.kite.client import KiteService

_TMP = tempfile.mkdtemp()
client_mod._TOKEN_CACHE = Path(_TMP) / "session.json"   # NEVER the real file


class _FakeKite:
    def __init__(self, behavior):
        self.behavior = behavior
        self.calls = 0

    def profile(self):
        self.calls += 1
        if self.behavior == "ok":
            return {"user_id": "TEST"}
        if self.behavior == "token":
            raise TokenException("Incorrect `api_key` or `access_token`.")
        raise ConnectionError("network down")

    def set_access_token(self, tok):
        pass


def _svc(behavior):
    ks = KiteService.__new__(KiteService)
    ks._api_key, ks._api_secret = "k", "s"
    ks.access_token, ks.user_id = "tok", "TEST"
    ks._token_check = ("unknown", 0.0)
    ks.kite = _FakeKite(behavior)
    return ks


def test_valid_token_probes_and_caches():
    ks = _svc("ok")
    assert ks.validate_token(max_age_s=300) == "valid"
    assert ks.validate_token(max_age_s=300) == "valid"
    assert ks.kite.calls == 1, "second call must come from cache"
    assert ks.validate_token(max_age_s=300, force=True) == "valid"
    assert ks.kite.calls == 2, "force bypasses the cache"
    print("  VALID  -> probed once, cached, force re-probes")


def test_rejected_token_invalidates_session():
    ks = _svc("token")
    assert ks.validate_token(max_age_s=0) == "invalid"
    assert ks.access_token is None, "invalidate() must clear the token"
    assert ks.is_authenticated is False
    # Subsequent calls short-circuit without probing a dead session.
    calls = ks.kite.calls
    assert ks.validate_token() == "invalid" and ks.kite.calls == calls
    print("  DEAD   -> TokenException clears the session; gate reappears")


def test_network_error_is_unknown_and_token_survives():
    ks = _svc("net")
    assert ks.validate_token(max_age_s=0) == "unknown"
    assert ks.access_token == "tok", "a wifi blip must NOT log the user out"
    assert ks.is_authenticated is True
    print("  BLIP   -> network error = 'unknown', token kept")


def test_fresh_token_resets_verdict():
    ks = _svc("ok")
    assert ks.validate_token(max_age_s=300) == "valid"
    ks.set_access_token("tok2")
    assert ks._token_check == ("unknown", 0.0)
    print("  FRESH  -> new token resets the cached verdict")


def test_no_kite_configured_is_invalid():
    ks = KiteService.__new__(KiteService)
    ks._api_key = ks._api_secret = ""
    ks.access_token = None
    ks.kite = None
    ks._token_check = ("unknown", 0.0)
    assert ks.validate_token() == "invalid"
    print("  NOKEY  -> unconfigured service reads invalid, never probes")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
    print("\n" + ("ALL PASSED" if failed == 0 else f"{failed} FAILED"))
    sys.exit(1 if failed else 0)
