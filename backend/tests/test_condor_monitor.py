"""Condor position monitoring: sign-aware P&L, health, and the exit ladder."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.condor.models import EnterCondorRequest, PositionView
from app.condor.monitor import CondorMonitor, build_position
from app.condor.store import CondorStore
from app.config import Settings

IST = timezone(timedelta(hours=5, minutes=30))


def _settings(**over) -> Settings:
    base = dict(KITE_API_KEY="t", KITE_API_SECRET="t")
    base.update(over)
    return Settings(_env_file=None, **base)


def _req(**over) -> EnterCondorRequest:
    base = dict(symbol="NIFTY", expiry="2026-08-13", lots=1,
                short_ce_strike=25300, short_ce_fill=72.0,
                short_pe_strike=24700, short_pe_fill=65.0,
                wing_ce_strike=25500, wing_ce_fill=28.0,
                wing_pe_strike=24500, wing_pe_fill=25.0)
    base.update(over)
    return EnterCondorRequest(**base)


class _StubQuote:
    def __init__(self, delta):
        self.greeks = {"delta": delta, "theta": -5.0, "vega": 3.0, "gamma": 1e-4}


def _monitor(**cfg_over) -> tuple[CondorMonitor, CondorStore]:
    store = CondorStore(None, None)          # memory-only, house test pattern
    mon = CondorMonitor(_settings(**cfg_over), None, store)
    return mon, store


def _pv(pos) -> PositionView:
    return PositionView(position=pos)


def test_build_position_credit_and_width():
    pos = build_position(_req(), lot_size=75, expiry_iso="2026-08-13")
    assert pos.credit_fill == 84.0            # 72+65-28-25
    assert pos.width == 200.0
    assert len(pos.legs) == 4
    assert pos.status == "open"


def test_close_position_pnl_sign_aware():
    mon, store = _monitor()
    pos = build_position(_req(), lot_size=75, expiry_iso="2026-08-13")
    store.add_position(pos)
    closed = mon.close_position(pos.id, exit_debit=40.0, reason="book_profit")
    assert closed is not None
    # Credit 84 collected, 40 paid back: gross +44 x 75 = 3300, minus charges.
    assert 2800 < closed.realized_pnl < 3300
    assert closed.status == "closed"
    # Idempotent: a second close returns None (not a double-book).
    assert mon.close_position(pos.id, 40.0, "again") is None


def test_close_losing_position_negative():
    mon, store = _monitor()
    pos = build_position(_req(), lot_size=75, expiry_iso="2026-08-13")
    store.add_position(pos)
    closed = mon.close_position(pos.id, exit_debit=150.0, reason="stop")
    assert closed.realized_pnl < -4500        # (84-150) x 75 = -4950 - charges


def _advice(mon, pos, pv, combined, band, min_dist_em, deltas=(0.15, -0.15),
            dte=3, now=None):
    s_ce = next(l for l in pos.legs if l.side.value == "SELL" and l.right == "CE")
    s_pe = next(l for l in pos.legs if l.side.value == "SELL" and l.right == "PE")
    quotes = {id(s_ce): _StubQuote(deltas[0]), id(s_pe): _StubQuote(deltas[1])}
    if now is None:
        now = int(datetime(2026, 8, 11, 11, 0, tzinfo=IST).timestamp())
    return mon._advice(pos, pv, combined, band, min_dist_em, quotes,
                       (s_ce, s_pe), dte, now)


def test_stop_loss_outranks_everything():
    mon, _ = _monitor()
    pos = build_position(_req(), lot_size=75, expiry_iso="2026-08-13")
    pv = _pv(pos)
    pv.captured_pct = 60.0                    # profit target ALSO met
    advice, detail = _advice(mon, pos, pv, combined=84.0 * 1.8, band="LOW",
                             min_dist_em=1.0)
    assert advice == "EXIT – STOP LOSS"
    assert "1.75x" in detail


def test_breakout_extreme_exits():
    mon, _ = _monitor()
    pos = build_position(_req(), lot_size=75, expiry_iso="2026-08-13")
    advice, _ = _advice(mon, pos, _pv(pos), combined=80.0, band="EXTREME",
                        min_dist_em=1.2)
    assert advice == "EXIT – BREAKOUT RISK"


def test_threatened_delta_triggers_adjust():
    mon, _ = _monitor()
    pos = build_position(_req(), lot_size=75, expiry_iso="2026-08-13")
    advice, detail = _advice(mon, pos, _pv(pos), combined=90.0, band="LOW",
                             min_dist_em=0.8, deltas=(0.34, -0.12))
    assert advice == "ADJUST"
    assert "0.34" in detail


def test_book_profit_at_target():
    mon, _ = _monitor()
    pos = build_position(_req(), lot_size=75, expiry_iso="2026-08-13")
    pv = _pv(pos)
    pv.captured_pct = 52.0
    advice, detail = _advice(mon, pos, pv, combined=40.0, band="LOW",
                             min_dist_em=1.1)
    assert advice == "BOOK PROFIT"
    assert "52" in detail


def test_expiry_day_exit_after_deadline():
    mon, _ = _monitor()
    pos = build_position(_req(expiry="2026-08-11"), lot_size=75,
                         expiry_iso="2026-08-11")
    pv = _pv(pos)
    at_1445 = int(datetime(2026, 8, 11, 14, 45, tzinfo=IST).timestamp())
    advice, detail = _advice(mon, pos, pv, combined=80.0, band="LOW",
                             min_dist_em=1.0, dte=0, now=at_1445)
    assert advice == "EXIT – EXPIRY RISK"
    assert "CAS" in detail


def test_hold_when_nothing_triggers():
    mon, _ = _monitor()
    pos = build_position(_req(), lot_size=75, expiry_iso="2026-08-13")
    pv = _pv(pos)
    pv.captured_pct = 10.0
    advice, _ = _advice(mon, pos, pv, combined=76.0, band="LOW", min_dist_em=1.3)
    assert advice == "HOLD"
