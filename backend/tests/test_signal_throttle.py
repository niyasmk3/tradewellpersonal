"""Signal-throttle + circuit-breaker tests.

Added 2026-07-20 after the engine issued ELEVEN cards in under three hours
(design goal: 1-4/day) and the trader followed them into a large loss. A
tradeable score is necessary but not sufficient — these gates decide how often
the engine is allowed to speak at all.

Run:  python backend/tests/test_signal_throttle.py
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import time

from app.signals.models import (
    Action,
    Bias,
    Direction,
    MarketStatus,
    Regime,
    ScoreBreakdown,
    SignalCard,
    SignalResponse,
    SignalState,
    TradingMode,
)
from app.signals.store import RiskState, SignalStore, ThrottleConfig

BASE = int(time.time()) // 86400 * 86400 + 5 * 3600   # a stable mid-day epoch
CFG = ThrottleConfig(max_per_day=4, min_gap_s=900, cooldown_s=600,
                     flip_guard_s=1800, max_consecutive_losses=2, daily_loss_limit=20000)


def _card(direction=Direction.PE, at=BASE, valid=480, cid="C"):
    return SignalCard(
        id=f"{cid}-{at}", symbol="NIFTY", mode=TradingMode.INTRADAY, title="t",
        action=Action.BUY_PE if direction is Direction.PE else Action.BUY_CE,
        direction=direction, state=SignalState.ACTIVE,
        contract=f"NIFTY 24200 {direction.value}", strike=24200.0, expiry="2026-07-21",
        entry_low=90.0, entry_high=94.0, premium_sl=75.0, target1=116.0, target2=133.0,
        trailing_sl_rule="r", risk_reward=1.5, confidence=80.0,
        underlying_invalidation="u", invalidation_note="n",
        created_at=at, valid_until=at + valid,
        score=ScoreBreakdown(direction=direction, components=[], total=80.0, max=100),
    )


def _resp(card, at=BASE):
    bias = Bias.BEARISH if card and card.direction is Direction.PE else Bias.BULLISH
    return SignalResponse(
        symbol="NIFTY", mode=TradingMode.INTRADAY, evaluated_at=at,
        status=MarketStatus(
            symbol="NIFTY", mode=TradingMode.INTRADAY, regime=Regime.MODERATE_BEARISH,
            regime_label="R", bias=bias, bull_score=50, bear_score=50, headline="h",
            vix_status=None, news_label=None, news_net=None, notes=[],
        ),
        action=card.action if card else Action.AVOID,
        signal=card, no_trade_reason=None, score=None,
    )


def _store():
    return SignalStore(store_path=None)     # memory-only; never touches .signals.json


def test_daily_cap():
    s = _store()
    t = BASE
    issued = 0
    for i in range(10):
        t += 1000                            # past min_gap and cooldown each time
        r = s.reconcile(_resp(_card(at=t, cid=f"c{i}"), t), t, CFG, RiskState())
        if r.signal:
            issued += 1
            t += 500                         # let it expire before the next attempt
            s.reconcile(_resp(None, t), t, CFG, RiskState())
    assert issued == CFG.max_per_day, issued
    print(f"  CAP    -> stopped at {issued}/day (was unbounded)")


def test_min_gap_between_signals():
    s = _store()
    r1 = s.reconcile(_resp(_card(at=BASE), BASE), BASE, CFG, RiskState())
    assert r1.signal is not None
    t = BASE + 500                            # card expired (480s) but gap < 900s
    s.reconcile(_resp(None, t), t, CFG, RiskState())
    r2 = s.reconcile(_resp(_card(at=t, cid="b"), t), t, CFG, RiskState())
    assert r2.signal is None
    assert "ooling down" in r2.no_trade_reason or "hrottled" in r2.no_trade_reason, r2.no_trade_reason
    print(f"  GAP    -> second signal withheld: {r2.no_trade_reason}")


def test_direction_flip_blocked():
    """Six PE cards then five CE cards inside 25 minutes is whipsaw, not edge."""
    s = _store()
    s.reconcile(_resp(_card(Direction.PE, BASE), BASE), BASE, CFG, RiskState())
    # Retire the PE card, then step past BOTH the cooldown (600s from retire)
    # and the min-gap (900s from issue) so the FLIP guard is what bites — it
    # runs last, and only the flip should still be blocking at +1200s.
    retire_at = BASE + 500
    s.reconcile(_resp(None, retire_at), retire_at, CFG, RiskState())
    t = BASE + 1200                           # >600 since retire, >900 since issue, <1800 flip guard
    r = s.reconcile(_resp(_card(Direction.CE, t, cid="ce"), t), t, CFG, RiskState())
    assert r.signal is None and "flip" in r.no_trade_reason.lower(), r.no_trade_reason
    print(f"  FLIP   -> {r.no_trade_reason}")


def test_consecutive_loss_breaker():
    s = _store()
    r = s.reconcile(_resp(_card(at=BASE), BASE), BASE, CFG,
                    RiskState(consecutive_losses=2, realized_today=-5000))
    assert r.signal is None and "Circuit breaker" in r.no_trade_reason
    print(f"  LOSSES -> {r.no_trade_reason}")


def test_daily_loss_limit_breaker():
    s = _store()
    r = s.reconcile(_resp(_card(at=BASE), BASE), BASE, CFG,
                    RiskState(consecutive_losses=0, realized_today=-25000))
    assert r.signal is None and "loss limit" in r.no_trade_reason
    print(f"  LIMIT  -> {r.no_trade_reason}")


def test_counters_reset_next_day():
    s = _store()
    t = BASE
    for i in range(CFG.max_per_day):
        s.reconcile(_resp(_card(at=t, cid=f"d{i}"), t), t, CFG, RiskState())
        t += 500
        s.reconcile(_resp(None, t), t, CFG, RiskState())
        t += 1000
    blocked = s.reconcile(_resp(_card(at=t, cid="x"), t), t, CFG, RiskState())
    assert blocked.signal is None
    nxt = t + 86400                           # next IST day
    ok = s.reconcile(_resp(_card(at=nxt, cid="y"), nxt), nxt, CFG, RiskState())
    assert ok.signal is not None, "counters must reset with the IST day"
    print("  ROLL   -> capped today, free again tomorrow")


def test_throttle_never_blocks_an_already_active_card():
    """A live card must keep being served even once the cap is reached —
    otherwise a position's own signal vanishes from the UI."""
    s = _store()
    card = _card(at=BASE, valid=3600)
    s.reconcile(_resp(card, BASE), BASE, CFG, RiskState())
    t = BASE + 60
    r = s.reconcile(_resp(card, t), t, CFG, RiskState(consecutive_losses=9))
    assert r.signal is not None and r.signal.id == card.id
    print("  ACTIVE -> live card survives circuit breaker")


# --- open-position guards (added after 20-Jul-2026) ---------------------------
# That day the engine issued six PE signals in 66 minutes while three PE
# positions sat ~Rs 32,000 down. Nothing was REALISED until 13:13, so every
# realised-only breaker stayed silent through the whole drawdown.

OPEN_CFG = ThrottleConfig(max_per_day=99, min_gap_s=0, cooldown_s=0, flip_guard_s=0,
                          max_consecutive_losses=99, daily_loss_limit=0,
                          max_open_positions=2, max_open_drawdown=0)


def test_open_positions_cap_blocks_stacking():
    s = _store()
    r = s.reconcile(_resp(_card(at=BASE), BASE), BASE, OPEN_CFG,
                    RiskState(open_positions=2, open_pnl=-32000))
    assert r.signal is None, "3rd concurrent position must be blocked"
    assert "already open" in (r.no_trade_reason or ""), r.no_trade_reason
    print(f"  OPEN-N -> blocked: {r.no_trade_reason[:52]}...")


def test_open_positions_under_cap_still_issues():
    s = _store()
    r = s.reconcile(_resp(_card(at=BASE), BASE), BASE, OPEN_CFG,
                    RiskState(open_positions=1, open_pnl=-9000))
    assert r.signal is not None, "under the cap the engine must still speak"
    print("  OPEN-N -> 2nd position still allowed")


def test_open_drawdown_halts_signals():
    cfg = ThrottleConfig(max_per_day=99, min_gap_s=0, cooldown_s=0, flip_guard_s=0,
                         max_consecutive_losses=99, max_open_positions=0,
                         max_open_drawdown=15000)
    s = _store()
    r = s.reconcile(_resp(_card(at=BASE), BASE), BASE, cfg,
                    RiskState(open_positions=1, open_pnl=-15000))
    assert r.signal is None and "down" in (r.no_trade_reason or "")
    print(f"  OPEN-DD-> blocked at -Rs15,000 unrealised")


def test_open_profit_never_blocks():
    """A guard that fires on WINNING positions would be nonsense."""
    cfg = ThrottleConfig(max_per_day=99, min_gap_s=0, cooldown_s=0, flip_guard_s=0,
                         max_consecutive_losses=99, max_open_positions=0,
                         max_open_drawdown=15000, daily_loss_limit=20000)
    s = _store()
    r = s.reconcile(_resp(_card(at=BASE), BASE), BASE, cfg,
                    RiskState(open_positions=1, open_pnl=+40000, realized_today=0))
    assert r.signal is not None, "open PROFIT must never trip a loss guard"
    print("  OPEN-DD-> open profit does not block")


def test_daily_limit_counts_unrealised():
    """The 20-Jul hole: -Rs32,000 held open, nothing realised, engine kept talking."""
    cfg = ThrottleConfig(max_per_day=99, min_gap_s=0, cooldown_s=0, flip_guard_s=0,
                         max_consecutive_losses=99, max_open_positions=0,
                         daily_loss_limit=20000)
    s = _store()
    # Realised alone is 0 -> the old code issued happily.
    r = s.reconcile(_resp(_card(at=BASE), BASE), BASE, cfg,
                    RiskState(realized_today=0.0, open_pnl=-32000, open_positions=3))
    assert r.signal is None, "unrealised loss must count toward the daily limit"
    assert "still open" in (r.no_trade_reason or ""), r.no_trade_reason
    print(f"  DAILY  -> counts unrealised: {r.no_trade_reason[:56]}...")


def test_realised_and_unrealised_combine():
    cfg = ThrottleConfig(max_per_day=99, min_gap_s=0, cooldown_s=0, flip_guard_s=0,
                         max_consecutive_losses=99, max_open_positions=0,
                         daily_loss_limit=20000)
    s = _store()
    # Neither alone breaches 20k; together they do.
    r = s.reconcile(_resp(_card(at=BASE), BASE), BASE, cfg,
                    RiskState(realized_today=-12000, open_pnl=-9000, open_positions=1))
    assert r.signal is None, "-12k booked plus -9k open exceeds the 20k limit"
    print("  DAILY  -> -12k realised + -9k open trips the 20k limit")


def test_replay_20_jul_sequence():
    """Replay the real 20-Jul-2026 sequence against the new guard.

    Signals fired at 11:45, 11:54, 12:02, 12:15, 12:27 and 12:51; the trader
    entered on each. The only exit before 13:13 was the first trade, closed at
    11:57. Modelled exactly that way, max_open_positions=2 blocks the last
    three - the trades that lost Rs 19,988, Rs 24,505 and Rs 6,298.
    """
    SIGNALS = [0, 9, 17, 30, 42, 66]          # minutes after 11:45
    EXITS = {12: 1}                            # 11:57 -> one position closed
    open_positions = 0
    issued, blocked = [], []
    s = _store()
    for i, m in enumerate(SIGNALS):
        t = BASE + m * 60
        for at, n in EXITS.items():            # apply exits that happened by now
            if at <= m and at > (SIGNALS[i - 1] if i else -1):
                open_positions -= n
        if i:
            s.reconcile(_resp(None, t), t, OPEN_CFG, RiskState(open_positions=open_positions))
        r = s.reconcile(_resp(_card(at=t, cid=f"s{i}"), t), t, OPEN_CFG,
                        RiskState(open_positions=open_positions, open_pnl=-3000 * i))
        if r.signal is not None:
            issued.append(m)
            open_positions += 1                # trader enters and holds
        else:
            blocked.append(m)
    assert issued == [0, 9, 17], f"first three should still be issued, got {issued}"
    assert blocked == [30, 42, 66], f"the three worst should be blocked, got {blocked}"
    print(f"  REPLAY -> issued +{issued}min, blocked +{blocked}min "
          f"= the Rs19,988 / Rs24,505 / Rs6,298 trades never signalled")


def test_stale_intraday_row_does_not_mute_the_engine():
    """A day-old intraday row must not count as an open position.

    Nothing in Tradewell closes a trade by itself and Zerodha auto-squares MIS
    at ~15:20 IST, so a row the user forgot to mark exited would otherwise
    count forever — and with max_open_positions=2, two ghosts would silence the
    engine permanently. Exercises the real filter in SignalService._risk_state.
    """
    from app.signals.service import SignalService
    import app.signals.service as svc

    now = BASE
    today = (now + 19800) // 86400
    yesterday = now - 86400

    def row(mode, entered_at, status="entered"):
        return type("T", (), {
            "status": type("S", (), {"value": status})(),
            "mode": type("M", (), {"value": mode})(),
            "entered_at": entered_at, "pnl": -5000.0, "realized_pnl": 0.0,
            "exited_at": None,
        })()

    rows = [row("intraday", yesterday), row("intraday", yesterday), row("positional", yesterday)]
    orig = svc.trade_store.all
    svc.trade_store.all = lambda: rows            # type: ignore
    try:
        rs = SignalService._risk_state(object(), now)
    finally:
        svc.trade_store.all = orig                # type: ignore

    assert rs.open_positions == 1, f"only the positional row should count, got {rs.open_positions}"
    assert rs.open_pnl == -5000.0, rs.open_pnl
    print("  STALE  -> 2 day-old intraday rows ignored, positional row still counts")


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
