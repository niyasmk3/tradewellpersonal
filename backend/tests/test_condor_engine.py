"""End-to-end condor engine test on a fabricated MarketState.

Builds a full synthetic universe (strikes, FULL-mode ticks with depth, BS-fair
premiums at a known sigma), a range-bound tape, and a balanced chain, then
asserts the pipeline produces a coherent card — and that each hard gate
refuses for the right reason when its precondition is broken.
"""
from __future__ import annotations

import math
import os
import sys
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.condor.engine import CondorEngine
from app.condor.regime import RANGE_BOUND
from app.config import Settings
from app.kite.instruments import OptionUniverse, StrikePair
from app.models.schemas import OptionChain, OptionRow
from app.options.iv import bs_price, years_to_expiry
from app.state import MarketState, UnderlyingMeta

IST = timezone(timedelta(hours=5, minutes=30))
# 13:15 IST: past the spec's 36-closed-5m-bar warm-up (~12:15 from a 09:15
# open), inside the 10:00-14:30 entry window.
NOW = int(datetime(2026, 8, 11, 13, 15, tzinfo=IST).timestamp())
EXPIRY = date(2026, 8, 13)     # Thursday, 2 days out
SPOT = 25000.0
SIGMA = 0.14
LOT = 75


def _settings(**over) -> Settings:
    base = dict(KITE_API_KEY="t", KITE_API_SECRET="t", CONDOR_ENABLED="true")
    base.update(over)
    return Settings(_env_file=None, **base)


def _universe() -> OptionUniverse:
    uni = OptionUniverse(symbol="NIFTY", expiry=EXPIRY, step=50)
    token = 1000
    for k in range(23500, 26550, 50):
        token += 2
        uni.strikes[float(k)] = StrikePair(
            strike=float(k), ce_token=token, pe_token=token + 1,
            ce_symbol=f"NIFTY{k}CE", pe_symbol=f"NIFTY{k}PE",
            ce_lot_size=LOT, pe_lot_size=LOT)
    return uni


def _tick(px: float, oi: float = 300_000.0) -> dict:
    bid = round(px * 0.995, 2)
    ask = round(px * 1.005 + 0.05, 2)
    return {
        "last_price": round(px, 2), "oi": oi, "volume_traded": 50_000,
        "ts": NOW,
        "depth": {"buy": [{"price": bid}], "sell": [{"price": ask}]},
    }


def _state(uni: OptionUniverse, spot: float = SPOT) -> MarketState:
    st = MarketState()
    meta = UnderlyingMeta("NIFTY", spot_token=1, spot_tradingsymbol="NIFTY 50",
                          strike_step=50, fut_token=2, fut_tradingsymbol="FUT",
                          lot_size=LOT)
    st.register_underlying(meta)
    st.ticks[1] = {"last_price": spot, "ts": NOW,
                   "ohlc": {"close": spot - 10, "open": spot - 5,
                            "high": spot + 60, "low": spot - 60}}
    st.ticks[2] = {"last_price": spot + 20, "ts": NOW}
    t = years_to_expiry(EXPIRY, NOW)
    for k, pair in uni.strikes.items():
        ce = bs_price(spot, k, t, SIGMA, True)
        pe = bs_price(spot, k, t, SIGMA, False)
        # OI walls exactly at the balanced-band strikes (25250 CE / 24750 PE):
        # the fixture describes a genuinely good setup — shorts AT the walls —
        # because a mediocre one (walls beyond reach) scores ~67 and correctly
        # refuses, which is its own test below.
        ce_oi = 900_000 if k == 25250 else 300_000
        pe_oi = 900_000 if k == 24750 else 300_000
        if ce > 0.5:
            st.ticks[pair.ce_token] = _tick(ce, ce_oi)
        if pe > 0.5:
            st.ticks[pair.pe_token] = _tick(pe, pe_oi)
    st.vix_token = 99
    st.ticks[99] = {"last_price": 14.0, "ts": NOW, "ohlc": {"close": 14.0}}
    st.vix_daily_closes = [11.0 + (i % 80) / 10.0 for i in range(200)]  # mid pctile

    # Range-bound tape seeded into the futures candle engine.
    eng = st.engine(2)
    open_ts = int(datetime(2026, 8, 11, 9, 15, tzinfo=IST).timestamp())
    prev_open = int(datetime(2026, 8, 10, 9, 15, tzinfo=IST).timestamp())

    def bars(t0, n, secs, fn):
        return [{"ts": t0 + i * secs, "open": fn(i), "high": fn(i) + 6,
                 "low": fn(i) - 6, "close": fn(i), "volume": 100_000}
                for i in range(n)]

    fut = spot + 20
    # 5m: two early spike bars set the session extremes, then a tight
    # mean-reverting cycle. Sine tapes are phase-sensitive — V6 (near value)
    # flips with wherever the persistence slice happens to land on the wave;
    # a +/-10 cycle keeps every offset near VWAP, which is what a genuine
    # range day looks like.
    cyc5 = [10.0, 2.0, -10.0, -2.0]
    rows5 = bars(open_ts, 4, 300,
                 lambda i: fut + (80 if i == 1 else -80 if i == 2 else 0))
    rows5 += bars(open_ts + 1200, 44, 300, lambda i: fut + cyc5[i % 4])
    eng.seed("5m", rows5)
    # 3m frame: pulse derives the day range (and thus V5's range-vs-typical)
    # from it; live it is always tick-built, so the fixture must supply it.
    # Amplitude chosen so today's range ~= the prior day's typical (~48 pts).
    cyc3 = [16.0, 2.0, -16.0, -2.0]
    rows3 = bars(open_ts, 80, 180, lambda i: fut + cyc3[i % 4])
    eng.seed("3m", rows3)
    # Four-bar cycle [+18, +2, -18, -2]: mean-reverting with two near-mid
    # bars per cycle, so the directional vote stays inside the balanced band
    # at EVERY persistence offset. A pure 2-bar alternation leaks the last
    # bar's sign into ~4 votes (VWAP side, prev-close side, EMA9) and reads
    # MILD/TRENDING depending on the ending phase — correct classifier
    # behavior, wrong fixture (probed empirically, see review-fix session).
    cyc = [18.0, 2.0, -18.0, -2.0]
    rows15 = bars(prev_open, 25, 900, lambda i: fut + cyc[i % 4])
    rows15 += bars(open_ts, 15, 900, lambda i: fut + cyc[(i + 25) % 4])
    eng.seed("15m", rows15)
    return st


def _chain(uni: OptionUniverse, st: MarketState) -> OptionChain:
    t = years_to_expiry(EXPIRY, NOW)
    rows = []
    for k in sorted(uni.strikes):
        if abs(k - SPOT) > 500:
            continue
        pair = uni.strikes[k]
        ce = st.ticks.get(pair.ce_token, {})
        pe = st.ticks.get(pair.pe_token, {})
        rows.append(OptionRow(
            strike=k, ce_token=pair.ce_token, ce_ltp=ce.get("last_price"),
            ce_oi=ce.get("oi"), ce_oi_change=1000.0, ce_volume=50_000,
            ce_iv=SIGMA * 100 if abs(k - SPOT) <= 250 else None,
            pe_token=pair.pe_token, pe_ltp=pe.get("last_price"),
            pe_oi=pe.get("oi"), pe_oi_change=1000.0, pe_volume=50_000,
            pe_iv=SIGMA * 100 if abs(k - SPOT) <= 250 else None))
    return OptionChain(symbol="NIFTY", expiry=EXPIRY.isoformat(),
                       atm_strike=25000.0, pcr=1.0, rows=rows, updated_at=NOW)


def _fresh(monkeypatch):
    monkeypatch.setattr(MarketState, "last_tick_age", lambda self: 1)
    # Deterministic environment: don't let the REAL patterns DB / level ladder
    # on this machine leak into test outcomes.
    from app.condor import expected_move as em_mod
    monkeypatch.setattr(em_mod, "hv20", lambda: 0.11)
    monkeypatch.setattr(em_mod, "daily_atr14", lambda: 180.0)
    monkeypatch.setattr(CondorEngine, "_nearest_strong_levels",
                        lambda self, spot: (24750.0, 25250.0))


def test_happy_path_produces_coherent_card(monkeypatch):
    _fresh(monkeypatch)
    uni = _universe()
    st = _state(uni)
    engine = CondorEngine(_settings(), st)
    resp = engine.evaluate("NIFTY", uni, _chain(uni, st), NOW)

    assert resp.data_ok, resp.data_problems
    assert resp.regime == RANGE_BOUND, (resp.regime, resp.regime_votes, resp.no_trade_reasons)
    assert resp.em_primary and resp.em_primary > 0
    card = resp.card
    assert card is not None, resp.no_trade_reasons
    assert len(card.legs) == 4
    s_ce = next(l for l in card.legs if l.side.value == "SELL" and l.right == "CE")
    s_pe = next(l for l in card.legs if l.side.value == "SELL" and l.right == "PE")
    w_ce = next(l for l in card.legs if l.side.value == "BUY" and l.right == "CE")
    w_pe = next(l for l in card.legs if l.side.value == "BUY" and l.right == "PE")
    assert s_pe.strike < SPOT < s_ce.strike
    assert w_ce.strike - s_ce.strike == card.width
    assert s_pe.strike - w_pe.strike == card.width
    # Short deltas inside the balanced band.
    assert 0.15 <= abs(s_ce.delta) <= 0.20
    assert 0.15 <= abs(s_pe.delta) <= 0.20
    assert card.credit_mid > 0
    assert abs(card.be_low - (s_pe.strike - card.credit_mid)) < 0.11   # 1dp rounding
    assert abs(card.be_high - (s_ce.strike + card.credit_mid)) < 0.11
    assert 0.60 <= card.pop <= 0.99
    assert card.max_profit > 0 and card.max_loss > 0
    assert card.max_loss <= engine.cfg.condor_max_loss_per_trade
    # Score arithmetic: parts sum to the total, all within their maxima.
    assert abs(sum(p.points for p in card.score_parts) - card.score) < 0.1
    assert all(0 <= p.points <= p.max for p in card.score_parts)
    assert card.quality in ("HIGH QUALITY", "WATCHLIST")
    assert card.reasons and card.trial
    # Net delta near zero for a symmetric-ish condor (per 1 lot).
    assert abs(card.net_delta) < 0.20 * LOT
    # Net theta positive: a credit structure earns decay.
    assert card.net_theta > 0


def test_market_closed_blocks(monkeypatch):
    _fresh(monkeypatch)
    uni = _universe()
    st = _state(uni)
    engine = CondorEngine(_settings(), st)
    sunday = int(datetime(2026, 8, 9, 11, 0, tzinfo=IST).timestamp())
    resp = engine.evaluate("NIFTY", uni, _chain(uni, st), sunday)
    assert not resp.data_ok
    assert any("market closed" in p for p in resp.data_problems)


def test_stale_feed_blocks(monkeypatch):
    monkeypatch.setattr(MarketState, "last_tick_age", lambda self: 300)
    uni = _universe()
    st = _state(uni)
    engine = CondorEngine(_settings(), st)
    resp = engine.evaluate("NIFTY", uni, _chain(uni, st), NOW)
    assert not resp.data_ok
    assert any("feed stale" in p for p in resp.data_problems)


def test_dte_gate_blocks_zero_dte(monkeypatch):
    _fresh(monkeypatch)
    uni = _universe()
    uni.expiry = date(2026, 8, 11)          # expiry today = 0 DTE
    st = _state(uni)
    engine = CondorEngine(_settings(), st)
    resp = engine.evaluate("NIFTY", uni, _chain(uni, st), NOW)
    assert not resp.data_ok
    assert any("DTE" in p for p in resp.data_problems)


def test_entry_window_blocks_open_and_late(monkeypatch):
    _fresh(monkeypatch)
    uni = _universe()
    st = _state(uni)
    engine = CondorEngine(_settings(), st)
    at_0930 = int(datetime(2026, 8, 11, 9, 30, tzinfo=IST).timestamp())
    # Re-stamp ticks so freshness holds at the simulated clock.
    for tk in st.ticks.values():
        tk["ts"] = at_0930
    resp = engine.evaluate("NIFTY", uni, _chain(uni, st), at_0930)
    assert resp.card is None
    assert any("entry window" in r for r in resp.no_trade_reasons), resp.no_trade_reasons


def test_trending_tape_yields_no_trade(monkeypatch):
    _fresh(monkeypatch)
    uni = _universe()
    st = _state(uni)
    # Overwrite the futures tape with an accelerating one-way move.
    eng = st.engine(2)
    open_ts = int(datetime(2026, 8, 11, 9, 15, tzinfo=IST).timestamp())
    rows5 = [{"ts": open_ts + i * 300, "open": 24800 + 0.55 * i * i,
              "high": 24810 + 0.55 * i * i, "low": 24790 + 0.55 * i * i,
              "close": 24800 + 0.55 * i * i, "volume": 100_000 + 6_000 * i}
             for i in range(44)]
    # seed() refuses to shrink a live buffer; swap in a fresh engine instead.
    from app.market.candles import CandleEngine
    st.candle_engines[2] = CandleEngine(2)
    st.candle_engines[2].seed("5m", rows5)
    rows15 = [{"ts": open_ts + i * 900, "open": 24500 + 2.2 * i * i,
               "high": 24510 + 2.2 * i * i, "low": 24490 + 2.2 * i * i,
               "close": 24500 + 2.2 * i * i, "volume": 100_000}
              for i in range(30)]
    st.candle_engines[2].seed("15m", rows15)
    engine = CondorEngine(_settings(), st)
    resp = engine.evaluate("NIFTY", uni, _chain(uni, st), NOW)
    assert resp.card is None
    assert resp.no_trade_reasons


def test_max_loss_cap_blocks(monkeypatch):
    _fresh(monkeypatch)
    uni = _universe()
    st = _state(uni)
    engine = CondorEngine(_settings(CONDOR_MAX_LOSS_PER_TRADE="500"), st)
    resp = engine.evaluate("NIFTY", uni, _chain(uni, st), NOW)
    assert resp.card is None
    assert any("wing width" in r or "credit" in r for r in resp.no_trade_reasons), \
        resp.no_trade_reasons
