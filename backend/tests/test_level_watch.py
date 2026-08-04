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
    # alerts_path=None: a test instance must never read or WRITE the live
    # callout ledger (the persistence upgrade made the default path live).
    svc = LevelWatchService(Settings(_env_file=None, **over), _State(spot, chain),
                            alerts_path=None)
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
    a = list(svc.alerts)[0]      # raw buffer: recent() date-scopes (own test)
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
    assert list(svc.alerts)[0]["side"] == "sell"
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


def test_recent_is_scoped_to_today():
    """Review catch: the ring buffer outlives sessions on a long-running
    process — yesterday's callout must never be served to today's chart."""
    svc = _svc(spot=24650.0)
    now = int(time.time())
    svc.alerts.append({"ts": now - 86400, "side": "buy", "level": 24500.0,
                       "title": "yesterday"})
    svc.alerts.append({"ts": now, "side": "sell", "level": 24600.0,
                       "title": "today"})
    got = svc.recent()
    assert [a["title"] for a in got] == ["today"], got
    print("  TODAY  -> prior-session callouts never reach the chart")


def test_callout_outcomes_graded_and_summarised():
    """The scoreboard: a BUY callout followed for an hour — horizons filled,
    break detection, and the 30m premium verdict; summary() aggregates it."""
    chain = _Chain([_Row(24600, 100.0)])
    chain.rows[0].ce_token = 901
    svc = _svc(spot=24610.0, chain=chain)
    svc._levels = [_level(24600.0)]
    base = int(time.time()) - 5000          # backdated so 'today' scoping holds
    svc.state.ticks = {901: {"last_price": 100.0, "ts": base}}
    svc.check(now=base)                      # prime above
    svc.state.spot = 24602.0                 # touch -> BUY callout
    svc.check(now=base + 5)
    assert len(svc.pushes) == 1
    a = list(svc.alerts)[0]
    assert a["token"] == 901 and a["ce_ltp"] == 100.0

    # 16 minutes on: spot recovered, premium up — 15m horizon fills.
    svc.state.spot = 24630.0
    svc.state.ticks[901] = {"last_price": 112.0, "ts": base + 960}
    svc.check(now=base + 5 + 960)
    o = a["outcomes"]
    assert o["15m"]["spot"] == 24630.0 and o["15m"]["prem"] == 112.0
    assert not o["broke"] and o["win"] is None

    # 31 minutes: the win verdict lands on the premium (112 > 100).
    svc.check(now=base + 5 + 1860)
    assert o["30m"]["prem"] == 112.0 and o["win"] is True
    # 66 minutes: finalized; extremes recorded the round trip.
    svc.state.spot = 24660.0
    svc.check(now=base + 5 + 3960)
    assert o["final"] is True and o["spot_max"] == 24660.0

    s = svc.summary()
    assert s["buy"]["n"] == 1 and s["buy"]["win_rate"] == 100.0
    assert s["buy"]["held_rate"] == 100.0
    assert s["buy"]["avg_prem_move_30m_pct"] == 12.0
    print("  GRADE  -> horizons, break check, 30m verdict, scoreboard")


def test_callout_break_marks_the_level_failed():
    """Spot travelling 15+ pts through a BUY level = level broke; with no
    premium tick at the 30m mark the verdict falls back to spot direction."""
    svc = _svc(spot=24650.0, chain=None)      # no chain: strike-only callout
    svc._levels = [_level(24600.0)]
    base = int(time.time()) - 5000
    svc.check(now=base)
    svc.state.spot = 24602.0
    svc.check(now=base + 5)
    a = list(svc.alerts)[0]
    assert a["ce_ltp"] is None
    svc.state.spot = 24580.0                  # 20 pts through the level
    svc.check(now=base + 400)
    assert a["outcomes"]["broke"] is True
    svc.check(now=base + 5 + 1860)            # 30m: spot below fire spot
    assert a["outcomes"]["win"] is False, "spot fallback must grade the miss"
    s = svc.summary()
    assert s["buy"]["held_rate"] == 0.0 and s["buy"]["win_rate"] == 0.0
    assert s["buy"]["avg_prem_move_30m_pct"] is None
    print("  BREAK  -> failed levels and premium-less callouts grade honestly")


def test_buy_callout_names_both_exits():
    """04-Aug user questions: the BUY push must name the failure line (the
    stop, = the grader's own break threshold) AND the first strong ceiling
    above (the sell side) at fire time — no waiting for a 30m verdict."""
    chain = _Chain([_Row(24600, 104.5)])
    svc = _svc(spot=24610.0, chain=chain)
    svc._levels = [_level(24600.0), _level(24675.0, days=35, hold=0.88)]
    base = int(time.time()) - 5000
    svc.check(now=base)
    svc.state.spot = 24602.0
    svc.check(now=base + 5)
    assert len(svc.pushes) == 1
    _, body, _ = svc.pushes[0]
    assert "FAILS below 24,585" in body, body
    assert "first strong ceiling above is 24,675" in body
    assert "held 88% of 35d" in body
    a = list(svc.alerts)[0]
    assert a["fails_below"] == 24585.0 and a["next_ceiling"] == 24675.0
    print("  EXITS  -> buy callout carries its stop AND its sell side")


def test_level_broke_fires_the_protective_alert_once():
    """The moment a BUY level breaks, say so — minutes, not the report card.
    Once per callout; a persisted broke flag cannot re-buzz after restart."""
    svc = _svc(spot=24650.0, chain=None)
    svc._levels = [_level(24600.0)]
    base = int(time.time()) - 5000
    svc.check(now=base)
    svc.state.spot = 24602.0
    svc.check(now=base + 5)                   # BUY callout
    assert len(svc.pushes) == 1
    svc.state.spot = 24580.0                  # 20 pts through: broke
    svc.check(now=base + 300)
    titles = [t for t, _, _ in svc.pushes]
    assert any("LEVEL BROKE" in t for t in titles), titles
    assert len(svc.pushes) == 2
    svc.state.spot = 24575.0                  # still broken: no re-buzz
    svc.check(now=base + 400)
    assert len(svc.pushes) == 2, "broke alert must fire exactly once"
    print("  ALARM  -> level break alerts within minutes, exactly once")


def test_ceiling_books_the_earlier_buy():
    """04-Aug user catch: the ceiling quoted a fresh ATM strike, which read
    as a DIFFERENT trade. With a BUY earlier today, the ceiling must talk
    about that bought strike and its live value; a broken buy is excluded."""
    chain = _Chain([_Row(24600, 104.5)])
    svc = _svc(spot=24610.0, chain=chain)
    svc._levels = [_level(24600.0), _level(24775.0, days=25, hold=0.82)]
    base = int(time.time()) - 5000
    svc.check(now=base)                        # prime above 24600
    svc.state.spot = 24602.0
    svc.check(now=base + 5)                    # BUY @ 24600, quotes ₹104.5
    assert len(svc.pushes) == 1

    # Price rallies to the ceiling; the bought contract now trades ₹142.3.
    svc.state.ticks[901] = {"last_price": 142.3, "ts": base + 3000}
    svc.state.spot = 24700.0                   # travel up (re-arms 24600 too)
    svc.check(now=base + 2400)
    svc.state.spot = 24772.0                   # into the ceiling from below
    svc.check(now=base + 3000)
    ceil = [p for p in svc.pushes if "CEILING" in p[0]]
    assert ceil, [t for t, _, _ in svc.pushes]
    _, body, _ = ceil[0]
    assert "NIFTY 24600 CE @ ₹104.5" in body and "now ₹142.3" in body, body
    assert "+36%" in body
    a = next(x for x in svc.alerts if x["side"] == "sell")
    assert a["ref_buy"]["strike"] == 24600 and a["ref_buy"]["pct"] == 36.2

    # A BROKEN buy must not be "booked" by a later ceiling.
    svc2 = _svc(spot=24610.0, chain=_Chain([_Row(24600, 104.5)]))
    svc2._levels = [_level(24600.0), _level(24775.0)]
    svc2.check(now=base)
    svc2.state.spot = 24602.0
    svc2.check(now=base + 5)
    list(svc2.alerts)[0]["outcomes"]["broke"] = True
    svc2.state.spot = 24700.0
    svc2.check(now=base + 2400)
    svc2.state.spot = 24772.0
    svc2.check(now=base + 3000)
    ceil2 = [p for p in svc2.pushes if "CEILING" in p[0]]
    assert ceil2 and "Your" not in ceil2[0][1], "a broken buy is not bookable"
    print("  LINK   -> the ceiling books the strike the BUY named")


def test_trade_codes_correlate_buy_book_and_exit():
    """User suggestion (04-Aug): every BUY gets a per-day code (#B1, #B2…)
    and the booking ceiling AND the broke exit quote it — one glance says
    WHICH bought strike a later alert refers to."""
    chain = _Chain([_Row(24600, 104.5)])
    svc = _svc(spot=24610.0, chain=chain)
    svc._levels = [_level(24600.0), _level(24775.0, days=25, hold=0.82)]
    base = int(time.time()) - 5000
    svc.check(now=base)
    svc.state.spot = 24602.0
    svc.check(now=base + 5)                    # -> #B1
    t1, b1, _ = svc.pushes[0]
    assert "#B1" in t1 and "Trade code #B1" in b1
    assert list(svc.alerts)[0]["code"] == "#B1"

    # The ceiling books it BY CODE.
    svc.state.ticks[901] = {"last_price": 142.3, "ts": base + 3000}
    svc.state.spot = 24700.0
    svc.check(now=base + 2400)
    svc.state.spot = 24772.0
    svc.check(now=base + 3000)
    ceil = next(p for p in svc.pushes if "CEILING" in p[0])
    assert "book #B1" in ceil[0] and "#B1 BUY" in ceil[1], ceil[0]
    # The chart marker payload carries the code too (BOOK #B1 @ level).
    sell_alert = next(x for x in svc.alerts if x["side"] == "sell")
    assert sell_alert["ref_buy"]["code"] == "#B1"

    # A second buy the same day gets #B2; the broke alert quotes ITS code.
    svc2 = _svc(spot=24650.0, chain=None)
    svc2._levels = [_level(24600.0)]
    svc2.alerts.append({"ts": base, "side": "buy", "code": "#B1",
                        "level": 24500.0, "spot": 24510.0, "strike": 24500,
                        "ce_ltp": 90.0, "token": None,
                        "outcomes": {"spot_max": 0, "spot_min": 0,
                                     "broke": False, "win": None, "final": True}})
    svc2.check(now=base + 100)
    svc2.state.spot = 24602.0
    svc2.check(now=base + 105)
    assert list(svc2.alerts)[-1]["code"] == "#B2"
    svc2.state.spot = 24580.0
    svc2.check(now=base + 400)
    broke = next(p for p in svc2.pushes if "LEVEL BROKE" in p[0])
    assert "#B2" in broke[0] and "NIFTY 24600 CE" in broke[1], broke[0]
    print("  CODE   -> #B1 buys, 'book #B1' ceilings, '#B1 BROKE' exits")


def test_callout_history_persists_across_restart():
    """Yesterday's lesson (the 14:21 callout vanished in a restart): alerts
    and their grades round-trip through the store file."""
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "alerts.json"
        svc = _svc(spot=24650.0)
        svc._alerts_path = path
        svc._levels = [_level(24600.0)]
        base = int(time.time()) - 5000
        svc.check(now=base)
        svc.state.spot = 24602.0
        svc.check(now=base + 5)
        assert path.exists(), "fire must persist the ledger"

        from app.patterns.level_watch import LevelWatchService

        svc2 = LevelWatchService(svc.cfg, svc.state, alerts_path=path)
        assert len(svc2.alerts) == 1
        assert list(svc2.alerts)[0]["level"] == 24600.0
    print("  STORE  -> callouts and grades survive a restart")


def test_frozen_tape_fires_nothing():
    """04-Aug: the dead ticker parked spot 9pts from a level and every fresh
    instance 'touched' it. With no ticks flowing the watcher must not prime,
    fire or grade."""
    svc = _svc(spot=24650.0)
    svc.state.last_tick_age = lambda: 400          # frozen tape
    svc._levels = [_level(24600.0)]
    base = int(time.time()) - 5000
    svc.check(now=base)
    svc.state.spot = 24602.0
    svc.check(now=base + 5)
    assert svc.pushes == [], "a frozen tape must be silent"
    svc.state.last_tick_age = lambda: 3            # tape alive again
    svc.check(now=base + 10)                       # prime
    svc.check(now=base + 15)
    assert len(svc.pushes) == 1, "fresh ticks resume normal behaviour"
    print("  FROZEN -> no priming, no firing, no grading on a dead tape")


def test_cooldown_survives_instance_rebuild():
    """THE 84-PUSH BUG: every feed restart rebuilt the watcher with empty
    cooldowns and re-fired the same level within seconds. The persisted
    ledger is now the cooldown memory."""
    import tempfile
    from pathlib import Path

    from app.patterns.level_watch import LevelWatchService

    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "alerts.json"
        svc = _svc(spot=24650.0)
        svc._alerts_path = path
        svc._levels = [_level(24600.0)]
        base = int(time.time()) - 3000
        svc.check(now=base)
        svc.state.spot = 24602.0
        svc.check(now=base + 5)                    # fires, persists
        assert len(svc.pushes) == 1

        # "Feed restart": a brand-new instance, same frozen-ish situation.
        svc2 = LevelWatchService(svc.cfg, svc.state, alerts_path=path)
        svc2._levels_at = time.time() + 10**6
        svc2._levels = [_level(24600.0)]
        svc2.pushes = []
        svc2._push = lambda t, b, c, **k: svc2.pushes.append((t, b, k))
        svc2.check(now=base + 60)                  # prime
        svc2.check(now=base + 65)                  # touch again, 1min later
        assert svc2.pushes == [], "the rebuilt instance must honour the cooldown"
    print("  MEMORY -> a rebuilt watcher cannot re-fire inside the cooldown")


def test_knife_guard_mutes_buys_after_a_break():
    """A broken BUY level = falling tape: new BUY callouts stay muted for
    KNIFE_MUTE_S; ceilings are unaffected; the mute expires."""
    from app.patterns.level_watch import KNIFE_MUTE_S

    svc = _svc(spot=24700.0, chain=None)
    svc._levels = [_level(24650.0), _level(24600.0), _level(24775.0)]
    base = int(time.time()) - 20000
    svc.check(now=base)
    svc.state.spot = 24652.0
    svc.check(now=base + 5)                        # BUY @ 24650
    assert len(svc.pushes) == 1
    svc.state.spot = 24630.0                       # breaks 24650 (20 pts)
    svc.check(now=base + 120)                      # -> BROKE alert + mute
    assert any("LEVEL BROKE" in t for t, _, _ in svc.pushes)
    n = len(svc.pushes)
    svc.state.spot = 24602.0                       # next support "touch"
    svc.check(now=base + 300)
    assert len(svc.pushes) == n, "BUY must stay muted while the knife falls"
    # A ceiling during the mute still speaks (it books, not buys).
    svc.state.spot = 24700.0
    svc.check(now=base + 400)
    svc.state.spot = 24773.0
    svc.check(now=base + 500)
    assert any("CEILING" in t for t, _, _ in svc.pushes)
    # After the mute, a fresh support touch fires again.
    n2 = len(svc.pushes)
    svc.state.spot = 24640.0
    svc.check(now=base + 120 + KNIFE_MUTE_S + 60)
    svc.state.spot = 24602.0
    svc.check(now=base + 120 + KNIFE_MUTE_S + 65)
    assert len(svc.pushes) == n2 + 1, "the mute must expire"
    print("  KNIFE  -> broken support mutes new BUYs 20m; ceilings unaffected")


def test_summary_dedupes_restart_storms():
    """84 duplicates of one frozen event must grade as ONE."""
    svc = _svc(spot=24650.0)
    base = int(time.time()) - 7200
    for i in range(10):                            # a storm, 1 min apart
        svc.alerts.append({
            "ts": base + i * 60, "side": "buy", "level": 24625.0,
            "spot": 24634.0, "ce_ltp": None, "strike": 24650,
            "outcomes": {"broke": True, "win": False, "final": True,
                         "spot_max": 0, "spot_min": 0},
        })
    svc.alerts.append({                            # a real, separate event
        "ts": base + 4000, "side": "buy", "level": 24625.0,
        "spot": 24630.0, "ce_ltp": 50.0, "strike": 24650,
        "outcomes": {"broke": False, "win": True, "final": True,
                     "spot_max": 0, "spot_min": 0,
                     "30m": {"spot": 24660.0, "prem": 60.0}},
    })
    s = svc.summary()
    assert s["buy"]["n"] == 2, s
    assert s["buy"]["win_rate"] == 50.0
    print("  DEDUP  -> a restart storm grades as one event, not eighty-four")


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
