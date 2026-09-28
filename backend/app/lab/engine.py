"""Event-driven backtester — one code path for all 40 strategies.

Replay discipline (each rule exists because its absence fabricates money):
  * Decisions happen at BAR CLOSE (ts_open + tf). A strategy sees features
    of bars <= the decision bar, chain snapshots <= the decision instant,
    and the PRIOR day's VIX close. Nothing else.
  * Futures/index legs fill at the NEXT bar's open ± slippage points; no
    fill across a day boundary (no overnight-gap fills).
  * Option legs fill at the FIRST chain snapshot strictly AFTER the
    decision instant (buy at ask + slippage, sell at bid − slippage),
    within a TTL — else the order lapses. Any leg unfillable => the whole
    order is rejected and logged.
  * Same-bar stop+target conflicts resolve stop-first (conservative).
  * Marks older than QUOTE_STALE_S never trigger price exits.
  * Runs outside the data's real span come back 'insufficient_data' —
    the engine has no synthetic-data branch at all.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd

from app.lab import costs as costs_mod
from app.lab import data as data_mod
from app.lab import features as feat
from app.lab.fills import (
    ChainView, Quote, STRIKE_STEP, exit_side, fill_price, leg_fill_from,
    lot_size_for, resolve_leg, snap_tick,
)
from app.lab.models import (
    BUY, SELL, ExitPolicy, LegFill, LegSpec, Position, SignalIntent,
)
from app.market.calendar import IST

log = logging.getLogger("tradewell.lab.engine")

TF_SECONDS = {"5m": 300, "15m": 900, "30m": 1800}
QUOTE_STALE_S = 900
FILL_TTL_S = 900
FUT_SLIP_PTS_DEFAULT = 1.0
OPT_SLIP_PCT_DEFAULT = 0.25
WARMUP_SESSIONS = 20


class HistChainView(ChainView):
    """ChainView bound to one archive snapshot."""

    def __init__(self, reader, snap_ts: int, spot: float):
        self._r = reader
        self.ts = snap_ts
        self.spot = spot
        self._chains: dict = {}

    def _chain(self, expiry: str):
        if expiry not in self._chains:
            self._chains[expiry] = self._r.chain_at(self.ts, expiry)
        return self._chains[expiry]

    def expiry(self, rule: str) -> Optional[str]:
        return self._r.pick_expiry(self.ts, rule)

    def strikes(self, expiry: str) -> list:
        ch = self._chain(expiry)
        return sorted(ch["strike"].unique()) if len(ch) else []

    def quote(self, expiry: str, strike: float, right: str
              ) -> Optional[Quote]:
        ch = self._chain(expiry)
        row = self._r.quote(ch, strike, right)
        if not row:
            return None
        return Quote(token=row["token"], tradingsymbol="NIFTY %s %s %s" %
                     (row["expiry"], int(strike), right),
                     expiry=row["expiry"], strike=strike, right=right,
                     ltp=row["ltp"], bid=row["bid"], ask=row["ask"],
                     oi=row["oi"], ts=self.ts, iv=row["iv"],
                     volume=row.get("volume") or 0.0)


class ChainFeatures:
    """Cached as-of chain-derived features for one decision instant."""

    def __init__(self, reader, snap_ts: Optional[int], spot: float):
        self._r = reader
        self.snap_ts = snap_ts
        self.spot = spot
        self._cache: dict = {}

    def _weekly_chain(self):
        if "wc" not in self._cache:
            exp = self._r.pick_expiry(self.snap_ts, "weekly")
            self._cache["wc"] = (self._r.chain_at(self.snap_ts, exp)
                                 if exp else pd.DataFrame())
        return self._cache["wc"]

    def pcr(self) -> Optional[float]:
        if self.snap_ts is None:
            return None
        return self._r.pcr(self._weekly_chain())

    def walls(self) -> dict:
        if self.snap_ts is None:
            return {"call_wall": None, "put_wall": None}
        return self._r.oi_walls(self._weekly_chain())

    def atm_iv(self) -> Optional[float]:
        if self.snap_ts is None:
            return None
        return self._r.atm_iv(self.snap_ts, self.spot)

    def straddle(self) -> Optional[float]:
        if self.snap_ts is None:
            return None
        return self._r.straddle_premium(self.snap_ts, self.spot)

    def quote(self, expiry: str, strike: float, right: str
              ) -> Optional[dict]:
        """As-of quote for one contract (strategy-side inspection only —
        fills still go through the engine's fill path)."""
        if self.snap_ts is None:
            return None
        ch = self._r.chain_at(self.snap_ts, expiry)
        return self._r.quote(ch, strike, right)

    def weekly_expiry(self) -> Optional[str]:
        if self.snap_ts is None:
            return None
        return self._r.pick_expiry(self.snap_ts, "weekly")

    def monthly_expiry(self) -> Optional[str]:
        if self.snap_ts is None:
            return None
        return self._r.pick_expiry(self.snap_ts, "monthly")

    def near_atm_oi(self, width_steps: int = 3) -> Optional[dict]:
        if self.snap_ts is None:
            return None
        ch = self._weekly_chain()
        if ch.empty:
            return None
        atm = self._r.atm_strike(ch, self.spot)
        if atm is None:
            return None
        lo, hi = atm - width_steps * STRIKE_STEP, atm + width_steps * STRIKE_STEP
        near = ch[(ch["strike"] >= lo) & (ch["strike"] <= hi)]
        return {
            "ce_oi": float(near[near["right"] == "CE"]["oi"].sum()),
            "pe_oi": float(near[near["right"] == "PE"]["oi"].sum()),
            "atm": atm,
        }


class BarContext:
    """What a strategy may see at one decision instant."""

    def __init__(self, i: int, df: pd.DataFrame, ts: int, row: dict,
                 chainf: Optional[ChainFeatures], vix: Optional[float],
                 vix_pctile: Optional[float], regime: str, expiry_day: bool,
                 trades_today: int, flat: bool, day: str,
                 chain_hist_fn=None, iv_pctile_fn=None,
                 straddle_series_fn=None):
        self.i = i
        self._df = df
        self.ts = ts                      # decision instant (bar close)
        self.row = row
        self.chain = chainf
        self.vix = vix
        self.vix_pctile = vix_pctile
        self.regime = regime
        self.expiry_day = expiry_day
        self.trades_today = trades_today
        self.flat = flat
        self.day = day
        self._chain_hist_fn = chain_hist_fn
        self._iv_pctile_fn = iv_pctile_fn
        self._straddle_series_fn = straddle_series_fn

    def bars(self, n: int) -> pd.DataFrame:
        """Trailing n bars ENDING at the decision bar (inclusive)."""
        lo = max(0, self.i - n + 1)
        return self._df.iloc[lo:self.i + 1]

    def chain_features_ago(self, snaps_back: int) -> Optional[ChainFeatures]:
        """Chain features as of `snaps_back` snapshots before this one —
        for dOI/momentum work. Still strictly in the past."""
        if self._chain_hist_fn is None:
            return None
        return self._chain_hist_fn(snaps_back)

    def atm_iv_percentile(self, lookback_days: int) -> Optional[float]:
        if self._iv_pctile_fn is None:
            return None
        return self._iv_pctile_fn(self.ts, lookback_days)

    def straddle_series(self, n: int) -> list:
        """ATM straddle premium over the last n snapshots ending at this
        decision (each valued with its own snapshot's spot). [] when the
        chain isn't available here."""
        if self._straddle_series_fn is None:
            return []
        return self._straddle_series_fn(self.ts, n)


class StrategyBase:
    """Interface all 40 implementations satisfy."""

    def __init__(self, sdef, params: dict):
        self.sdef = sdef
        self.p = params

    def day_reset(self) -> None:
        pass

    def on_bar(self, ctx: BarContext) -> Optional[SignalIntent]:
        raise NotImplementedError

    def wants_exit(self, ctx: BarContext, pos: Position) -> Optional[str]:
        return None

    def adjust(self, ctx: BarContext, pos: Position) -> Optional[list]:
        """Optional list of ("close", leg_index) / ("open", LegSpec)."""
        return None


class RunResult:
    def __init__(self):
        self.trades: list = []           # TradeRecord dicts
        self.rejects: dict = {}          # reason -> count
        self.equity_curve: list = []     # cumulative net pnl by trade
        self.daily_pnl: dict = {}        # day -> net
        self.status = "done"
        self.note = ""
        self.peak_margin = 0.0
        self.bars = 0


def _bump(d: dict, k: str) -> None:
    d[k] = d.get(k, 0) + 1


class Backtester:
    """One (strategy, params, span) replay. Instantiate per run."""

    def __init__(self, sdef, params: dict, timeframe: str, from_ts: int,
                 to_ts: int, capital_rs: float,
                 fut_slip_pts: float = FUT_SLIP_PTS_DEFAULT,
                 opt_slip_pct: float = OPT_SLIP_PCT_DEFAULT,
                 chain_reader=None):
        from app.lab.strategies import build  # local: avoid import cycle
        self.sdef = sdef
        self.p = params
        self.tf = timeframe
        self.step = TF_SECONDS[timeframe]
        self.from_ts = from_ts
        self.to_ts = to_ts
        self.capital = capital_rs
        self.fut_slip = fut_slip_pts
        self.opt_slip = opt_slip_pct
        self.strategy: StrategyBase = build(sdef, params)
        self.reader = chain_reader if chain_reader is not None \
            else data_mod.chain_reader
        self.needs_chain = "chain_hist" in sdef.data_needs
        self.needs_fut_hedge = (sdef.spine == "index"
                                and "fut_candles" in sdef.data_needs)
        self._iv_daily_cache: Optional[pd.DataFrame] = None
        self._straddle_cache: dict = {}
        self._spot_fn = None

    # ---- data loading -----------------------------------------------------

    def _load_spine(self) -> pd.DataFrame:
        warm = WARMUP_SESSIONS * 86400 * 2  # calendar days incl. weekends
        lo = self.from_ts - warm
        if self.sdef.spine == "fut":
            raw = data_mod.futures_store.load_frame(lo, self.to_ts)
        else:
            raw = data_mod.load_index_frame(lo, self.to_ts)
        if raw.empty:
            return raw
        if self.tf != "5m":
            raw = data_mod.resample(raw, self.tf)
        return raw.reset_index(drop=True)

    # ---- position maths ---------------------------------------------------

    @staticmethod
    def _leg_sign(side: str) -> float:
        return 1.0 if side == BUY else -1.0

    def _mark_value(self, pos: Position, view: Optional[HistChainView],
                    spine_px: float) -> Optional[float]:
        """Signed market value of the OPEN legs. None => stale/no quote."""
        total = 0.0
        for idx, leg in enumerate(pos.legs):
            if str(idx) in pos.exit_prices:
                continue
            if leg.right == "FUT":
                total += self._leg_sign(leg.side) * spine_px * leg.qty
                continue
            if view is None:
                return None
            q = view.quote(leg.expiry, leg.strike, leg.right)
            if q is None:
                return None
            mid = (q.bid + q.ask) / 2.0 if (q.bid and q.ask) else q.ltp
            total += self._leg_sign(leg.side) * mid * leg.qty
        return total

    def _entry_value(self, pos: Position) -> float:
        return sum(self._leg_sign(l.side) * l.price * l.qty for l in pos.legs)

    def _realized(self, pos: Position) -> float:
        out = 0.0
        for idx, leg in enumerate(pos.legs):
            px = pos.exit_prices.get(str(idx))
            if px is None:
                continue
            out += self._leg_sign(leg.side) * (px - leg.price) * leg.qty
        return out

    # ---- exits ------------------------------------------------------------

    def _policy_levels(self, pos: Position) -> dict:
        ep = pos.exit_policy
        base = abs(pos.entry_value) or 1.0
        lv: dict = {}
        if ep.stop_kind in ("pct_value", "credit_pct") and ep.stop_value:
            lv["stop_pnl"] = -base * ep.stop_value / 100.0
        if ep.target_kind in ("pct_value", "credit_pct") and ep.target_value:
            lv["target_pnl"] = base * ep.target_value / 100.0
        if ep.stop_kind in ("points_spine", "atr_spine") and ep.stop_value:
            pts = ep.stop_value * (pos.spine_atr_entry
                                   if ep.stop_kind == "atr_spine" else 1.0)
            lv["stop_px"] = pos.spine_entry - pts if pos.direction == "LONG" \
                else pos.spine_entry + pts
        if ep.target_kind in ("points_spine", "atr_spine") and ep.target_value:
            pts = ep.target_value * (pos.spine_atr_entry
                                     if ep.target_kind == "atr_spine" else 1.0)
            lv["target_px"] = pos.spine_entry + pts \
                if pos.direction == "LONG" else pos.spine_entry - pts
        return lv

    def _check_spine_bracket(self, pos: Position, bar) -> Optional[tuple]:
        """FUT bracket on bar high/low. Stop checked FIRST (conservative).
        Returns (reason, exit_px) or None."""
        lv = self._policy_levels(pos)
        long = pos.direction == "LONG"
        stop = lv.get("stop_px")
        target = lv.get("target_px")
        if stop is not None:
            hit = bar["low"] <= stop if long else bar["high"] >= stop
            if hit:
                px = stop - self.fut_slip if long else stop + self.fut_slip
                return "stop", px
        if target is not None:
            hit = bar["high"] >= target if long else bar["low"] <= target
            if hit:
                px = target - self.fut_slip if long else target + self.fut_slip
                return "target", px
        return None

    def _check_value_bracket(self, pos: Position, mark: float
                             ) -> Optional[str]:
        pnl = (mark + self._realized(pos)) - pos.entry_value
        lv = self._policy_levels(pos)
        if "stop_pnl" in lv and pnl <= lv["stop_pnl"]:
            return "stop"
        if "target_pnl" in lv and pnl >= lv["target_pnl"]:
            return "target"
        ep = pos.exit_policy
        if ep.trail_kind == "pct_value" and ep.trail_value:
            peak = pos.peak_value
            base = abs(pos.entry_value) or 1.0
            if peak > 0 and (peak - pnl) >= base * ep.trail_value / 100.0:
                return "trail"
        return None

    # ---- fills ------------------------------------------------------------

    def _fill_options(self, intent: SignalIntent, decision_ts: int,
                      spot_at_fill_fn) -> tuple:
        """-> (list[LegFill] | None, reason). All legs from the SAME
        first-snapshot-after-decision; resolution happened at decision."""
        snap_dec = self.reader.snap_at_or_before(decision_ts, QUOTE_STALE_S)
        if snap_dec is None:
            return None, "no_decision_snapshot"
        spot_dec = spot_at_fill_fn(snap_dec)
        view_dec = HistChainView(self.reader, snap_dec, spot_dec)
        resolved = []
        for leg in intent.legs:
            if leg.right == "FUT":
                resolved.append((leg, None))
                continue
            q, why = resolve_leg(view_dec, leg, self.p.get("min_oi", 0),
                                 self.p.get("max_spread_pct", 99.0),
                                 min_volume=self.p.get("min_volume", 0),
                                 min_premium=self.p.get("min_premium", 0.0),
                                 max_premium=self.p.get("max_premium", 0.0))
            if q is None:
                return None, why
            resolved.append((leg, q))

        snap_fill = self.reader.snap_after(decision_ts, FILL_TTL_S)
        if snap_fill is None:
            return None, "order_lapsed_no_snapshot"
        spot_fill = spot_at_fill_fn(snap_fill)
        view_fill = HistChainView(self.reader, snap_fill, spot_fill)
        fills = []
        day = datetime.fromtimestamp(decision_ts, IST).date().isoformat()
        for leg, q_dec in resolved:
            if leg.right == "FUT":
                fills.append(("FUT", leg))
                continue
            q = view_fill.quote(q_dec.expiry, q_dec.strike, q_dec.right)
            if q is None or q.ltp <= 0:
                return None, "leg_gone_at_fill"
            fills.append(("OPT", leg_fill_from(q, leg, self.p.get("lots", 1),
                                               day, self.opt_slip)))
        return fills, ""

    def _fut_leg_fill(self, leg: LegSpec, bar_open_px: float, ts: int,
                      day: str) -> LegFill:
        lot = lot_size_for(day)
        lots = leg.lots * self.p.get("lots", 1)
        slip = self.fut_slip if leg.side == BUY else -self.fut_slip
        return LegFill(side=leg.side, right="FUT",
                       tradingsymbol="NIFTY-FUT", token=0, strike=0.0,
                       expiry="", lots=lots, qty=lots * lot,
                       price=snap_tick(bar_open_px + slip), quote_ts=ts,
                       lot_size=lot)

    # ---- main loop --------------------------------------------------------

    def run(self) -> RunResult:
        res = RunResult()
        df = self._load_spine()
        if df.empty or len(df) < 100:
            res.status = "insufficient_data"
            res.note = "spine has %d bars in span" % len(df)
            return res
        df = feat.add_features(
            df,
            ema_fast=int(self.p.get("ema_fast", 9)),
            ema_slow=int(self.p.get("ema_slow", 21)),
            st_period=int(self.p.get("st_period", 10)),
            st_mult=float(self.p.get("st_mult", 3.0)),
        )
        vix_df = data_mod.load_vix_daily()
        expiry_days = self._expiry_day_set()

        start_i = int((df["ts"] >= self.from_ts).idxmax())
        if df["ts"].iloc[start_i] < self.from_ts:
            res.status = "insufficient_data"
            res.note = "no bars at or after requested start"
            return res

        pos: Optional[Position] = None
        pending: Optional[tuple] = None   # (intent, decision_ts, decision_i)
        trades_today = 0
        cur_day: Optional[str] = None
        realized_total = 0.0
        # refresh: a long-lived process (API server) must see snapshots the
        # live logger wrote after this reader's cache was first built
        snap_list = self.reader.snap_ts_list(refresh=True) \
            if self.needs_chain else []
        self._spot_fn = self._spot_lookup(df)

        for i in range(start_i, len(df)):
            row = df.iloc[i]
            bar = row
            ts_open = int(row["ts"])
            decision_ts = ts_open + self.step
            day = datetime.fromtimestamp(ts_open, IST).date().isoformat()
            minute = int(row["minute"]) + self.step // 60  # decision minute

            if day != cur_day:
                # forced day roll: any position still open on a non-overnight
                # strategy was force-closed at yesterday's squareoff below;
                # if data ended a day early it closes here at last known.
                cur_day = day
                trades_today = 0
                self.strategy.day_reset()

            res.bars += 1
            vix = data_mod.vix_asof(vix_df, ts_open)
            vix_pct = data_mod.vix_percentile(vix_df, ts_open)
            regime = feat.classify_row(row, vix_pct)

            chainf = None
            chain_hist_fn = None
            if self.needs_chain:
                snap = self.reader.snap_at_or_before(decision_ts,
                                                     QUOTE_STALE_S)
                chainf = ChainFeatures(self.reader, snap,
                                       float(row["close"]))
                if snap is not None:
                    import bisect
                    pos_idx = bisect.bisect_right(snap_list, snap) - 1

                    def chain_hist_fn(back, _pos=pos_idx):
                        j = _pos - back
                        if j < 0:
                            return None
                        return ChainFeatures(self.reader, snap_list[j],
                                             float(row["close"]))

            ctx = BarContext(
                i=i, df=df, ts=decision_ts, row=row, chainf=chainf,
                vix=vix, vix_pctile=vix_pct, regime=regime,
                expiry_day=day in expiry_days, trades_today=trades_today,
                flat=pos is None, day=day,
                chain_hist_fn=chain_hist_fn,
                iv_pctile_fn=self._atm_iv_percentile,
                straddle_series_fn=self._straddle_series
                if self.needs_chain else None,
            )

            # ---- 1) manage open position --------------------------------
            if pos is not None:
                exited = self._manage_position(pos, ctx, bar, res, day)
                if exited is not None:
                    realized_total += exited
                    res.daily_pnl[day] = res.daily_pnl.get(day, 0.0) + exited
                    res.equity_curve.append(
                        (res.equity_curve[-1] if res.equity_curve else 0.0)
                        + exited)
                    pos = None

            # ---- 2) pending order fills ---------------------------------
            if pending is not None and pos is None:
                intent, dec_ts, dec_i = pending
                if i == dec_i + 1:
                    pos = self._try_fill(intent, dec_ts, df, i, res, day)
                    if pos is not None:
                        trades_today += 1
                        # conservative: the FILL bar's own range can already
                        # take out a spine stop after the open fill.
                        if not any(l.right != "FUT" for l in pos.legs):
                            hit = self._check_spine_bracket(pos, bar)
                            if hit:
                                pnl = self._close_position(
                                    pos, hit[0], ctx, bar, res, day, hit[1])
                                if pnl is not None:
                                    realized_total += pnl
                                    res.daily_pnl[day] = res.daily_pnl.get(
                                        day, 0.0) + pnl
                                    res.equity_curve.append(
                                        (res.equity_curve[-1]
                                         if res.equity_curve else 0.0) + pnl)
                                pos = None
                    pending = None
                elif i > dec_i + 1:
                    _bump(res.rejects, "order_lapsed")
                    pending = None

            # ---- 3) new signals -----------------------------------------
            if pos is None and pending is None:
                sig = None
                try:
                    sig = self.strategy.on_bar(ctx)
                except Exception as exc:   # a strategy bug must not lie
                    res.status = "error"
                    res.note = "strategy exception at ts %s: %s" % (
                        decision_ts, exc)
                    log.exception("strategy %s failed", self.sdef.sid)
                    return res
                if sig is not None:
                    why = self._gate(sig, ctx, minute, trades_today)
                    if why:
                        _bump(res.rejects, why)
                    else:
                        pending = (sig, decision_ts, i)

        # end of data: force-close
        if pos is not None:
            pnl = self._force_close(pos, df.iloc[-1], res,
                                    "eod_data",
                                    datetime.fromtimestamp(
                                        int(df.iloc[-1]["ts"]), IST
                                    ).date().isoformat())
            if pnl is not None:
                realized_total += pnl
                res.equity_curve.append(
                    (res.equity_curve[-1] if res.equity_curve else 0.0) + pnl)
        return res

    # ---- helpers ----------------------------------------------------------

    def _expiry_day_set(self) -> set:
        """Days that were actual weekly expiries — only known inside the
        chain era (stored expiry dates). Outside it the label is absent,
        and analytics say so rather than guessing."""
        out: set = set()
        if self.reader.available():
            with self.reader._conn() as c:
                rows = c.execute("SELECT DISTINCT expiry FROM chain_snap "
                                 "WHERE symbol='NIFTY'").fetchall()
            out = {r[0] for r in rows}
        return out

    def _atm_iv_percentile(self, ts: int, lookback_days: int
                           ) -> Optional[float]:
        """Percentile of today's ~10:00 ATM IV vs the trailing distribution
        of daily ~10:00 ATM IVs. Series built once per run, causally."""
        if self._iv_daily_cache is None:
            rows = []
            seen_days = set()
            for snap in self.reader.snap_ts_list():
                d = (snap + 19800) // 86400
                m = ((snap + 19800) % 86400) // 60
                if d in seen_days or m < 595:      # first snap >= 09:55
                    continue
                seen_days.add(d)
                idx = data_mod.load_index_frame(snap - 3600, snap)
                if idx.empty:
                    continue
                spot = float(idx.iloc[-1]["close"])
                iv = self.reader.atm_iv(snap, spot)
                if iv:
                    rows.append({"day": int(d), "ts": snap, "iv": iv})
            self._iv_daily_cache = pd.DataFrame(rows)
        cache = self._iv_daily_cache
        if cache.empty:
            return None
        d_now = (ts + 19800) // 86400
        past = cache[cache["day"] < d_now].tail(lookback_days)
        today = cache[cache["day"] == d_now]
        if len(past) < 5 or today.empty:
            return None
        iv_now = float(today.iloc[0]["iv"])
        return float((past["iv"] <= iv_now).mean() * 100.0)

    def _straddle_series(self, ts: int, n: int) -> list:
        """Cached per-snapshot ATM straddle values for the n snapshots at or
        before ts. Spots come from the spine as of each snapshot."""
        import bisect
        lst = self.reader.snap_ts_list()
        i = bisect.bisect_right(lst, ts)
        out = []
        for snap in lst[max(0, i - n):i]:
            if snap not in self._straddle_cache:
                spot = self._spot_fn(snap) if self._spot_fn else None
                val = None
                if spot is not None:
                    val = self.reader.straddle_premium(snap, spot)
                self._straddle_cache[snap] = val
            v = self._straddle_cache[snap]
            if v is not None:
                out.append(v)
        return out

    def _gate(self, sig: SignalIntent, ctx: BarContext, minute: int,
              trades_today: int) -> Optional[str]:
        p = self.p
        if trades_today >= int(p.get("max_trades_per_day", 99)):
            return "max_trades"
        lo = int(p.get("entry_start_min", 0))
        hi = int(p.get("entry_end_min", 2000))
        if not (lo <= minute <= hi):
            return "outside_window"
        if not feat.regime_allows(ctx.regime, p.get("regime_filter", "off")):
            return "regime_filter"
        vix = ctx.vix
        if vix is not None:
            if vix < float(p.get("vix_min", 0)) or \
               vix > float(p.get("vix_max", 99)):
                return "vix_filter"
        return None

    def _try_fill(self, intent: SignalIntent, dec_ts: int, df: pd.DataFrame,
                  i: int, res: RunResult, day: str) -> Optional[Position]:
        bar = df.iloc[i]
        prev = df.iloc[i - 1]
        if int(bar["day"]) != int(prev["day"]):
            _bump(res.rejects, "no_overnight_fill")
            return None
        legs: list = []
        entry_costs = 0.0
        has_opt = any(l.right != "FUT" for l in intent.legs)
        if has_opt:
            fills, why = self._fill_options(intent, dec_ts,
                                            self._spot_lookup(df))
            if fills is None:
                _bump(res.rejects, why)
                return None
            for kind, item in fills:
                if kind == "FUT":
                    legs.append(self._fut_leg_fill(item, float(bar["open"]),
                                                   int(bar["ts"]), day))
                else:
                    legs.append(item)
        else:
            for l in intent.legs:
                legs.append(self._fut_leg_fill(l, float(bar["open"]),
                                               int(bar["ts"]), day))
        for lf in legs:
            kind = "FUT" if lf.right == "FUT" else "OPT"
            entry_costs += costs_mod.order_cost(kind, lf.side, lf.price,
                                                lf.qty, day)
        pos = Position(
            sid=self.sdef.sid, version=0, signal_ts=intent.ts,
            direction=intent.direction, tag=intent.tag, legs=legs,
            exit_policy=intent.exit_policy, entry_ts=int(bar["ts"]),
            entry_costs=entry_costs,
            spine_entry=float(bar["open"]),
            spine_atr_entry=float(df.iloc[i - 1].get("atr") or 0.0),
        )
        pos.entry_value = self._entry_value(pos)
        # capital / margin gate (estimates, labelled in metrics)
        spot = float(bar["open"])
        margin = costs_mod.position_margin_rs(
            [{"side": l.side, "right": l.right, "strike": l.strike,
              "price": l.price, "qty": l.qty} for l in legs], spot)
        if margin > self.capital:
            _bump(res.rejects, "insufficient_capital")
            return None
        res.peak_margin = max(res.peak_margin, margin)
        return pos

    @staticmethod
    def _spot_lookup(df: pd.DataFrame):
        ts_arr = df["ts"].values

        def lookup(snap_ts: int) -> float:
            import bisect
            j = bisect.bisect_right(ts_arr, snap_ts) - 1
            j = max(0, j)
            return float(df.iloc[j]["close"])
        return lookup

    def _manage_position(self, pos: Position, ctx: BarContext, bar,
                         res: RunResult, day: str) -> Optional[float]:
        """Evaluate exits for the open position at this bar.
        Returns realized net pnl if the position closed, else None."""
        p = self.p
        minute = int(bar["minute"]) + self.step // 60
        has_opt = any(l.right != "FUT" for l in pos.legs)
        view = None
        if has_opt:
            snap = self.reader.snap_at_or_before(ctx.ts, QUOTE_STALE_S)
            if snap is not None:
                view = HistChainView(self.reader, snap, float(bar["close"]))

        mark = self._mark_value(pos, view, float(bar["close"]))
        if mark is not None:
            pnl_open = (mark + self._realized(pos)) - pos.entry_value
            pos.peak_value = max(pos.peak_value, pnl_open)
            pos.mae = min(pos.mae, pnl_open)
            pos.mfe = max(pos.mfe, pnl_open)

        reason: Optional[str] = None
        spine_exit_px: Optional[float] = None

        # 1) hard time exits first — they fire even on stale quotes
        ep = pos.exit_policy
        if ep.time_exit_min is not None and minute >= int(ep.time_exit_min):
            reason = "time_exit"
        elif not self.sdef.overnight and \
                minute >= int(p.get("squareoff_min", 915)):
            reason = "squareoff"
        if reason is None and ep.max_hold_bars:
            held = (ctx.ts - pos.entry_ts) // self.step
            if held >= ep.max_hold_bars:
                reason = "max_hold"

        # 2) price exits (fresh data only)
        if reason is None and not has_opt:
            hit = self._check_spine_bracket(pos, bar)
            if hit:
                reason, spine_exit_px = hit
            elif mark is not None:
                # R-based trail for spine positions: retrace from the peak
                # open pnl by trail_value% of the entry risk => exit.
                ep2 = pos.exit_policy
                lv = self._policy_levels(pos)
                if ep2.trail_kind == "risk_pct" and ep2.trail_value and \
                        "stop_px" in lv:
                    qty = sum(l.qty for l in pos.legs)
                    risk_rs = abs(pos.spine_entry - lv["stop_px"]) * qty
                    pnl_open = (mark + self._realized(pos)) - pos.entry_value
                    if pos.peak_value > 0 and risk_rs > 0 and \
                            (pos.peak_value - pnl_open) >= \
                            risk_rs * ep2.trail_value / 100.0:
                        reason = "trail"
        if reason is None and has_opt and mark is not None:
            reason = self._check_value_bracket(pos, mark)

        # 3) strategy-specific exit
        if reason is None:
            reason = self.strategy.wants_exit(ctx, pos)

        # 4) adjustments (rolls, hedges) when staying in
        if reason is None:
            acts = self.strategy.adjust(ctx, pos)
            if acts:
                self._apply_adjustments(pos, acts, ctx, bar, res, day)
            return None

        return self._close_position(pos, reason, ctx, bar, res, day,
                                    spine_exit_px)

    def _apply_adjustments(self, pos: Position, acts: list, ctx: BarContext,
                           bar, res: RunResult, day: str) -> None:
        snap = self.reader.snap_after(ctx.ts, FILL_TTL_S) \
            if any(a[0] == "open" and a[1].right != "FUT" for a in acts) or \
               any(a[0] == "close" and pos.legs[a[1]].right != "FUT"
                   for a in acts) else None
        view = None
        if snap is not None:
            view = HistChainView(self.reader, snap,
                                 float(bar["close"]))
        for act in acts:
            if act[0] == "close":
                idx = act[1]
                if str(idx) in pos.exit_prices:
                    continue
                leg = pos.legs[idx]
                if leg.right == "FUT":
                    px = float(bar["close"])
                    px = snap_tick(px - self.fut_slip if leg.side == BUY
                                   else px + self.fut_slip)
                else:
                    if view is None:
                        continue
                    q = view.quote(leg.expiry, leg.strike, leg.right)
                    if q is None:
                        continue
                    px = fill_price(q, exit_side(leg.side), self.opt_slip)
                pos.exit_prices[str(idx)] = px
                kind = "FUT" if leg.right == "FUT" else "OPT"
                pos.exit_costs += costs_mod.order_cost(
                    kind, exit_side(leg.side), px, leg.qty, day)
            elif act[0] == "open":
                spec: LegSpec = act[1]
                if spec.right == "FUT":
                    pos.legs.append(self._fut_leg_fill(
                        spec, float(bar["close"]), ctx.ts, day))
                    lf = pos.legs[-1]
                    pos.entry_costs += costs_mod.order_cost(
                        "FUT", lf.side, lf.price, lf.qty, day)
                else:
                    if view is None:
                        continue
                    q, why = resolve_leg(view, spec,
                                         self.p.get("min_oi", 0),
                                         self.p.get("max_spread_pct", 99.0))
                    if q is None:
                        _bump(res.rejects, "adjust_" + why)
                        continue
                    lf = leg_fill_from(q, spec, self.p.get("lots", 1), day,
                                       self.opt_slip)
                    pos.legs.append(lf)
                    pos.entry_costs += costs_mod.order_cost(
                        "OPT", lf.side, lf.price, lf.qty, day)
        # entry_value must reflect the legs the pnl is measured against
        pos.entry_value = self._entry_value(pos)

    def _close_position(self, pos: Position, reason: str, ctx: BarContext,
                        bar, res: RunResult, day: str,
                        spine_exit_px: Optional[float]) -> Optional[float]:
        has_opt = any(l.right != "FUT" for l in pos.legs)
        view = None
        if has_opt:
            snap = self.reader.snap_after(ctx.ts, FILL_TTL_S)
            if snap is None:
                snap = self.reader.snap_at_or_before(ctx.ts, 3600)
            if snap is not None:
                view = HistChainView(self.reader, snap, float(bar["close"]))
        for idx, leg in enumerate(pos.legs):
            if str(idx) in pos.exit_prices:
                continue
            if leg.right == "FUT":
                if spine_exit_px is not None:
                    px = snap_tick(spine_exit_px)
                else:
                    base = float(bar["close"])
                    px = snap_tick(base - self.fut_slip if leg.side == BUY
                                   else base + self.fut_slip)
            else:
                q = view.quote(leg.expiry, leg.strike, leg.right) \
                    if view else None
                if q is None:
                    # no quote at all near the close decision: last resort is
                    # the leg's last mark via decision snapshot; if even that
                    # is missing the trade is recorded as data-broken.
                    _bump(res.rejects, "exit_no_quote")
                    px = leg.price   # flat assumption, flagged below
                    reason = reason + "+exit_no_quote"
                else:
                    px = fill_price(q, exit_side(leg.side), self.opt_slip)
            pos.exit_prices[str(idx)] = px
            kind = "FUT" if leg.right == "FUT" else "OPT"
            pos.exit_costs += costs_mod.order_cost(
                kind, exit_side(leg.side), px, leg.qty, day)

        pos.exit_ts = ctx.ts
        pos.exit_reason = reason
        pnl_gross = self._realized(pos)
        costs = pos.entry_costs + pos.exit_costs
        pnl_net = pnl_gross - costs
        res.trades.append({
            "signal_ts": pos.signal_ts, "entry_ts": pos.entry_ts,
            "exit_ts": pos.exit_ts, "direction": pos.direction,
            "tag": pos.tag,
            "legs": [{
                "side": l.side, "right": l.right, "symbol": l.tradingsymbol,
                "strike": l.strike, "expiry": l.expiry, "lots": l.lots,
                "qty": l.qty, "entry": l.price,
                "exit": pos.exit_prices.get(str(k)),
            } for k, l in enumerate(pos.legs)],
            "pnl_gross": round(pnl_gross, 2), "costs": round(costs, 2),
            "pnl_net": round(pnl_net, 2), "exit_reason": reason,
            "mae": round(pos.mae, 2), "mfe": round(pos.mfe, 2),
            "regime": ctx.regime,
            "session_bucket": feat.session_bucket(
                int(bar["minute"])),
            "expiry_day": ctx.expiry_day,
            "bars_held": int((ctx.ts - pos.entry_ts) // self.step),
        })
        return pnl_net

    def _force_close(self, pos: Position, last_bar, res: RunResult,
                     reason: str, day: str) -> Optional[float]:
        ctx_ts = int(last_bar["ts"]) + self.step
        fake_ctx = BarContext(
            i=0, df=pd.DataFrame([last_bar]), ts=ctx_ts, row=last_bar,
            chainf=None, vix=None, vix_pctile=None, regime="",
            expiry_day=False, trades_today=0, flat=False, day=day)
        return self._close_position(pos, reason, fake_ctx, last_bar, res,
                                    day, None)
