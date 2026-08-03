"""Level-touch callouts (patterns/level_watch.py): the S/R ladder watched
against live spot, edge-triggered, with the tradeable contract attached.

The failure modes that matter: a level camped on all afternoon must not buzz
every cycle (edge trigger + re-arm + cooldown); the approach SIDE decides
buy-vs-ceiling; weak levels (few touch-days or coin hold rates) never alert;
and a missing chain degrades the callout, never kills it.

Run:  python backend/tests/test_level_watch.py
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import time

from app.config import Settings
from app.patterns.level_watch import (
    MIN_DAYS_TOUCHED,
    MIN_HOLD_RATE,
    LevelWatchService,
    strong_levels,
)


class _Snap:
    def __init__(self, ltp):
        self.ltp = ltp


class _Row:
    def __init__(self, strike, ce_ltp, ce_token=901):
        self.strike = strike
        self.ce_ltp = ce_ltp
        self.ce_token = ce_token


class _Chain:
    expiry = "2026-08-04"

    def __init__(self, rows):
        self.rows = rows


class _State:
    def __init__(self, spot, chain=None):
        self.spot = spot
        self.chain = chain
        # A fresh exchange-stamped tick with no last_price: premium_quote
        # falls back to the chain LTP but still gets a real age.
        self.ticks = {901: {"ts": int(time.time())}}

    def underlying_snapshot(self, symbol):
        return _Snap(self.spot)

    def get_option_chain(self, key):
        return self.chain


def _svc(spot=24600.0, chain=None, **over):
    svc = LevelWatchService(Settings(_env_file=None, **over), _State(spot, chain))
    svc._levels_at = time.time() + 10**6      # ladder injected, never re-read
    svc.pushes = []
    svc._push = lambda title, body, cfg, **kw: svc.pushes.append((title, body, kw))
    return svc


def _level(level, days=10, hold=0.7):
    return {"level": level, "days_touched": days, "hold_rate": hold,
            "total_touches": days * 2, "as_support": days, "as_resistance": 0,
            "held": int(days * hold), "broke": days - int(days * hold)}


def test_strong_level_filter():
    ladder = [
        _level(24500, days=MIN_DAYS_TOUCHED, hold=MIN_HOLD_RATE),
        _level(24550, days=MIN_DAYS_TOUCHED - 1, hold=0.9),   # too few days
        _level(24450, days=20, hold=MIN_HOLD_RATE - 0.01),    # coin hold rate
        {"level": 24400},                                     # malformed: no stats
    ]
    kept = strong_levels(ladder)
    assert [l["level"] for l in kept] == [24500]
    print("  STRONG -> only >=5 touch-days AND >=60% hold qualify")


def test_support_touch_fires_buy_with_contract():
    chain = _Chain([_Row(24600, 104.5), _Row(24550, 130.0)])
    svc = _svc(spot=24610.0, chain=chain)
    svc._levels = [_level(24600.0)]
    svc.check(now=1000)                        # prime: prev_spot set, no fire
    assert svc.pushes == []
    svc.state.spot = 24602.0                   # falls ONTO the level from above
    svc.check(now=1005)
    assert len(svc.pushes) == 1, svc.pushes
    title, body, kw = svc.pushes[0]
    assert "LEVEL BUY" in title and "24600 CE" in title and "₹104.5" in title
    assert "exp 04-Aug" in title
    assert "held 70%" in body and "NOT a scored card" in body
    assert kw.get("audience") == "private", "level context never reaches the guest"
    a = svc.recent()[0]
    assert a["side"] == "buy" and a["strike"] == 24600 and a["ce_ltp"] == 104.5
    print("  BUY    -> support touch calls out strike/expiry/premium")


def test_resistance_touch_fires_ceiling():
    svc = _svc(spot=24560.0, chain=_Chain([_Row(24550, 96.0)]))
    svc._levels = [_level(24600.0, days=19, hold=0.68)]
    svc.state.spot = 24540.0
    svc.check(now=1000)                        # prime below the level
    svc.state.spot = 24598.0                   # rises INTO the ceiling
    svc.check(now=1005)
    assert len(svc.pushes) == 1
    title, body, _ = svc.pushes[0]
    assert "CEILING" in title and "booking" in title
    assert "held 68% of 19 days" in body and "not prophecy" in body
    assert svc.recent()[0]["side"] == "sell"
    print("  SELL   -> ceiling touch calls out booking context")


def test_edge_trigger_rearm_and_cooldown():
    svc = _svc(spot=24650.0)
    svc._levels = [_level(24600.0)]
    svc.check(now=1000)                        # prime above
    svc.state.spot = 24603.0
    svc.check(now=1005)
    assert len(svc.pushes) == 1
    # Still inside the band next cycles: NO re-fire.
    svc.check(now=1010)
    svc.check(now=1015)
    assert len(svc.pushes) == 1
    # Leaves the band but under the 12-pt re-arm cap: stays disarmed.
    svc.state.spot = 24611.0
    svc.check(now=1020)
    svc.state.spot = 24602.0
    svc.check(now=1025)
    assert len(svc.pushes) == 1, "must not re-arm inside the re-arm distance"
    # Past the cap: re-arms — but the per-level cooldown still gates it.
    svc.state.spot = 24660.0
    svc.check(now=1030)
    svc.state.spot = 24601.0
    svc.check(now=1035)
    assert len(svc.pushes) == 1, "cooldown must hold the re-armed level"
    # After the cooldown, the SAME approach fires again.
    svc.state.spot = 24660.0
    svc.check(now=1000 + 1800)
    svc.state.spot = 24601.0
    svc.check(now=1005 + 1800 + 5)
    assert len(svc.pushes) == 2
    print("  EDGE   -> one callout per approach; capped re-arm; cooldown holds")


def test_adjacent_levels_never_deadlock():
    """Review catch (critical): 3x tol (~30 pts) exceeded the ladder's 25-pt
    bin spacing, so price oscillating between two adjacent strong levels
    could never re-arm either — the watch went silent for the session. The
    12-pt cap keeps re-arming possible inside the 25-pt corridor; the
    cooldown (not permanent disarm) is the spam control."""
    svc = _svc(spot=24650.0)
    svc._levels = [_level(24600.0), _level(24625.0)]
    svc.check(now=1000)                        # prime above both
    svc.state.spot = 24601.0                   # touch the lower level
    svc.check(now=1005)
    svc.state.spot = 24624.0                   # touch the upper (24 pts away)
    svc.check(now=1010)
    fired = len(svc.pushes)
    assert fired == 2, svc.pushes
    # Oscillate inside the corridor past the cooldown: the lower level was
    # RE-ARMED while price sat at the upper one (24 pts > 12-pt cap), so
    # after its cooldown it fires again instead of staying dead forever.
    svc.state.spot = 24624.0
    svc.check(now=1005 + 1800)
    svc.state.spot = 24601.0
    svc.check(now=1010 + 1800)
    assert len(svc.pushes) == 3, "adjacent levels must not deadlock the watch"
    print("  RANGE  -> two levels 25 pts apart keep alerting across cooldowns")


def test_push_titles_survive_the_latin1_header():
    """Review catch (critical): urllib encodes HTTP header values latin-1 —
    an emoji or rupee sign in the Title crashed urlopen inside _post's
    catch-all, so the push silently never left the machine (the pre-existing
    evening-positional moon marker included). Titles are sanitized centrally."""
    from app.notify import _header_safe, _text_payload

    title = "📍 LEVEL BUY setup · NIFTY 24600 CE @ ₹104.5 · exp 04-Aug"
    safe = _header_safe(title)
    safe.encode("latin-1")                     # must not raise
    assert "Rs 104.5" in safe and "[LEVEL]" in safe and "·" in safe

    cfg = Settings(_env_file=None)
    payload, headers = _text_payload("🌙 EVENING POSITIONAL · BUY CE", "body ₹100", cfg)
    headers["Title"].encode("latin-1")         # must not raise
    assert "[NIGHT]" in headers["Title"]
    assert "₹100" in payload.decode("utf-8"), "the BODY keeps real UTF-8"
    print("  TITLE  -> emoji/rupee titles sanitized; pushes actually deliver")


def test_premium_freshness_gates_the_callout():
    """Review catch: an ATM premium with no fresh timestamped tick must stay
    OUT of the callout — the 21-Jul stale-quote lesson applies to a pushed
    rupee figure too."""
    now = int(time.time())
    chain = _Chain([_Row(24600, 104.5)])
    chain.rows[0].ce_token = 901

    svc = _svc(spot=24610.0, chain=chain)
    svc.state.ticks = {901: {"last_price": 106.0, "ts": now - 10}}
    q = svc._atm_quote(24610.0, now)
    assert q["ce_ltp"] == 106.0, "fresh tick prices the callout (tick wins over chain)"

    svc.state.ticks = {901: {"last_price": 106.0, "ts": now - 3600}}
    q = svc._atm_quote(24610.0, now)
    assert q["ce_ltp"] is None, "an hour-old quote must not be pushed as live"

    svc.state.ticks = {}
    q = svc._atm_quote(24610.0, now)
    assert q["ce_ltp"] is None, "no timestamped tick = unverifiable = no premium"
    assert q["strike"] == 24600 and q["expiry"] == "2026-08-04"
    print("  FRESH  -> stale/unverifiable premiums degrade to strike-only")


def test_disabled_flag_and_missing_chain():
    svc = _svc(spot=24650.0, LEVEL_ALERTS_ENABLED=False)
    svc._levels = [_level(24600.0)]
    svc.check(now=1000)
    svc.state.spot = 24601.0
    svc.check(now=1005)
    assert svc.pushes == [], "disabled flag must silence the watch"

    svc2 = _svc(spot=24650.0, chain=None)      # no chain: degrade, still fire
    svc2._levels = [_level(24600.0)]
    svc2.check(now=1000)
    svc2.state.spot = 24601.0
    svc2.check(now=1005)
    assert len(svc2.pushes) == 1
    title, _, _ = svc2.pushes[0]
    assert "24600 CE" in title and "₹" not in title, "no premium without a chain"
    print("  GUARD  -> flag silences; missing chain degrades, never kills")


def test_watched_is_near_spot_only():
    svc = _svc(spot=24600.0)
    svc._levels = [_level(24600.0), _level(24580.0), _level(26000.0), _level(23000.0)]
    near = svc.watched(24600.0)
    assert [l["level"] for l in near] == [24600.0, 24580.0], near
    print("  NEAR   -> far levels stay off the watch and off the chart")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
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
