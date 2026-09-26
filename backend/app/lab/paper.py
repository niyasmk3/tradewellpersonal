"""Automated daily paper trading for the Strategy Lab.

Design: paper trading IS the backtest engine advanced in real time.
  * Bars come from the same source backtests replay (Kite 5-min candles,
    fetched at each closed boundary), so a later backtest over today
    reproduces today's paper session.
  * Option quotes come from the SAME chain-snapshot archive the condor
    logger is writing live (.condor_chain.db) — an entry decided at bar
    close fills at the first snapshot written AFTER the decision, exactly
    like the backtester's rule. No quote, no fill.
  * The same Strategy objects, gates, exit policies and cost model run in
    both places (a Backtester instance provides them here).

State lives in .lab.db (positions, orders, signals, sessions), so a
restart rehydrates open positions and never re-cuts a day ledger.

Known, disclosed limitation: in-memory per-day strategy flags (e.g. a
"once per day" latch) reset if the process restarts mid-session; the
trades/day cap still holds because it is counted from the store.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd

from app.config import get_settings
from app.lab import costs as costs_mod
from app.lab import data as data_mod
from app.lab import features as feat
from app.lab import risk as risk_mod
from app.lab.catalog import BY_SID
from app.lab.engine import (
    Backtester, BarContext, ChainFeatures, FILL_TTL_S, HistChainView,
    QUOTE_STALE_S, TF_SECONDS,
)
from app.lab.fills import exit_side, fill_price, leg_fill_from, resolve_leg, snap_tick
from app.lab.models import BUY, LegFill, Position
from app.lab.store import lab_store
from app.market.calendar import IST, is_trading_day, now_ist
from app.services import kite_service
from app.state import market_state

log = logging.getLogger("tradewell.lab.paper")

_INDEX_TOKEN = 256265
_PREMARKET_MIN = 545      # 09:05
_SESSION_OPEN = 555       # 09:15
_SESSION_CLOSE = 930      # 15:30
_POSTCLOSE_MIN = 940      # 15:40


def _ist_minute(dt: datetime) -> int:
    return dt.hour * 60 + dt.minute


class _SpineFeed:
    """Today's 5-min bars for one token, appended to the stored tail so
    features get real warm-up. Fetched from Kite at closed boundaries."""

    def __init__(self, kind: str):
        self.kind = kind              # "index" | "fut"
        self.tail: Optional[pd.DataFrame] = None
        self.today: list = []
        self.day: Optional[str] = None
        self.last_fetch = 0.0

    def _token(self) -> Optional[int]:
        if self.kind == "index":
            return _INDEX_TOKEN
        meta = market_state.underlyings.get("NIFTY")
        return meta.fut_token if meta else None

    def _load_tail(self, now_ts: int) -> None:
        lo = now_ts - 45 * 86400
        day_start = now_ts - (now_ts + 19800) % 86400
        if self.kind == "index":
            self.tail = data_mod.load_index_frame(lo, day_start - 1)
        else:
            self.tail = data_mod.futures_store.load_frame(lo, day_start - 1)

    def refresh(self, now: datetime) -> Optional[str]:
        """Fetch today's bars. Returns an error string or None."""
        day = now.date().isoformat()
        if day != self.day:
            self.day = day
            self.today = []
            self._load_tail(int(now.timestamp()))
        if not kite_service.is_authenticated:
            return "kite_unauthenticated"
        token = self._token()
        if not token:
            return "no_token"
        try:
            start = now.replace(hour=9, minute=0, second=0, microsecond=0)
            rows = kite_service.kite.historical_data(
                token, start, now, "5minute",
                oi=(self.kind == "fut"))
        except Exception as exc:
            return "fetch_failed: %s" % exc
        self.today = []
        for d in rows:
            dt = d["date"]
            if not ((9, 15) <= (dt.hour, dt.minute) <= (15, 25)):
                continue
            row = {"ts": int(dt.timestamp()), "open": d["open"],
                   "high": d["high"], "low": d["low"], "close": d["close"],
                   "volume": float(d.get("volume", 0) or 0)}
            if self.kind == "fut":
                row["oi"] = float(d.get("oi", 0) or 0)
            self.today.append(row)
        self.last_fetch = time.time()
        return None

    def frame(self) -> pd.DataFrame:
        parts = []
        if self.tail is not None and not self.tail.empty:
            parts.append(self.tail)
        if self.today:
            parts.append(pd.DataFrame(self.today))
        if not parts:
            return pd.DataFrame(columns=["ts", "open", "high", "low",
                                         "close", "volume"])
        df = pd.concat(parts, ignore_index=True)
        return df.drop_duplicates("ts").sort_values("ts"
                                                    ).reset_index(drop=True)


def _rehydrate(row: dict) -> Position:
    legs = [LegFill(**l) for l in row["legs"]]
    from app.lab.models import ExitPolicy
    pos = Position(
        sid=row["sid"], version=row["version"], signal_ts=row["opened_ts"],
        direction=row["direction"], tag=row["tag"], legs=legs,
        exit_policy=ExitPolicy(**row["exit_policy"]),
        entry_ts=row["opened_ts"])
    st = row.get("state") or {}
    pos.entry_value = st.get("entry_value", 0.0)
    pos.peak_value = st.get("peak_value", 0.0)
    pos.mae = st.get("mae", 0.0)
    pos.mfe = st.get("mfe", 0.0)
    pos.spine_entry = st.get("spine_entry", 0.0)
    pos.spine_atr_entry = st.get("spine_atr_entry", 0.0)
    pos.exit_prices = {k: v for k, v in (st.get("exit_prices") or {}).items()}
    pos.entry_costs = st.get("entry_costs", 0.0)
    pos.exit_costs = st.get("exit_costs", 0.0)
    return pos


def _pos_state(pos: Position) -> dict:
    return {"entry_value": pos.entry_value, "peak_value": pos.peak_value,
            "mae": pos.mae, "mfe": pos.mfe, "spine_entry": pos.spine_entry,
            "spine_atr_entry": pos.spine_atr_entry,
            "exit_prices": pos.exit_prices, "entry_costs": pos.entry_costs,
            "exit_costs": pos.exit_costs,
            "pending_exit": getattr(pos, "pending_exit", None)}


class _Runner:
    """One armed (sid, version) — champion or experimental."""

    def __init__(self, sid: str, version: int, params: dict,
                 experimental: bool):
        self.sid = sid
        self.version = version
        self.experimental = experimental
        self.sdef = BY_SID[sid]
        self.bt = Backtester(self.sdef, params, self.sdef.timeframe,
                             0, 0, capital_rs=risk_mod.effective()
                             ["max_capital_rs"])
        self.last_bar_ts = 0
        self.day: Optional[str] = None

    def strategy(self):
        return self.bt.strategy


class PaperEngine:
    def __init__(self):
        self.feeds = {"index": _SpineFeed("index"), "fut": _SpineFeed("fut")}
        self.runners: dict = {}
        self.session_day: Optional[str] = None
        self._premarket_done: Optional[str] = None
        self._postclose_done: Optional[str] = None

    # ---- runner management ------------------------------------------------

    def _sync_runners(self) -> None:
        states = lab_store.all_states()
        want: dict = {}
        for sid, st in states.items():
            if sid not in BY_SID or st.get("suspended_reason"):
                continue
            if st.get("paper_enabled"):
                want[(sid, False)] = int(st["champion_version"])
            if st.get("experimental_version"):
                want[(sid, True)] = int(st["experimental_version"])
        for key in list(self.runners):
            if key not in want or self.runners[key].version != want[key]:
                del self.runners[key]
        for key, ver in want.items():
            if key not in self.runners:
                sid, exp = key
                params = lab_store.get_params(sid, ver)
                if params is None:
                    continue
                try:
                    self.runners[key] = _Runner(sid, ver, params, exp)
                except Exception as exc:
                    log.warning("runner %s v%s failed to build: %s",
                                sid, ver, exc)

    # ---- session jobs -----------------------------------------------------

    def premarket(self, now: datetime) -> None:
        day = now.date().isoformat()
        checks = {"day": day, "trading_day": is_trading_day(now.date()),
                  "kite_authenticated": kite_service.is_authenticated}
        ready = data_mod.readiness()
        checks["chain_rows"] = ready["chain_hist"]["rows"]
        checks["fut_status"] = ready["fut_candles"]["status"]
        # incremental futures sync so today's backfills exist tomorrow
        if kite_service.is_authenticated:
            meta = market_state.underlyings.get("NIFTY")
            if meta and meta.fut_token:
                try:
                    checks["fut_sync"] = data_mod.sync_futures(
                        kite_service.kite, meta.fut_token)
                except Exception as exc:
                    checks["fut_sync_error"] = str(exc)
        checks["risk"] = risk_mod.effective()
        enabled = [k[0] for k in self.runners]
        checks["strategies"] = enabled
        lab_store.paper_session_start(day, checks)
        lab_store.log_data_event(int(now.timestamp()), "premarket", checks)
        log.info("Lab premarket: %s", json.dumps(checks, default=str))

    def postclose(self, now: datetime) -> None:
        from app.lab import learning, report
        day = now.date().isoformat()
        # force-close leftovers (shouldn't exist: squareoff is 15:15)
        for row in lab_store.open_positions():
            self._emergency_close(row, "postclose_forced", now)
        # cut immutable per-(sid,version) day ledgers
        by_key: dict = {}
        for row in lab_store.positions_for_day(day):
            if row.get("closed_ts") is None:
                continue
            k = (row["sid"], row["version"])
            by_key.setdefault(k, []).append(row)
        for (sid, ver), rows in by_key.items():
            pnl = sum(r["pnl_net"] or 0 for r in rows)
            summary = {
                "trades": len(rows), "pnl_net": round(pnl, 2),
                "by_exit": {},
            }
            for r in rows:
                er = r.get("exit_reason") or "?"
                summary["by_exit"][er] = summary["by_exit"].get(er, 0) + 1
            lab_store.save_paper_day(day, sid, ver, len(rows), round(pnl, 2),
                                     summary)
        try:
            report.build_daily(day)
        except Exception:
            log.exception("daily report failed")
        try:
            learning.daily_cycle(day)
        except Exception:
            log.exception("learning cycle failed")
        lab_store.paper_session_end(day, "done")
        log.info("Lab postclose done for %s", day)

    def _emergency_close(self, row: dict, reason: str,
                         now: datetime) -> None:
        """Close a stored position at the best available marks."""
        pos = _rehydrate(row)
        bt = None
        for r in self.runners.values():
            if r.sid == row["sid"]:
                bt = r.bt
                break
        day = now.date().isoformat()
        snap = data_mod.chain_reader.snap_at_or_before(
            int(now.timestamp()), 3600)
        spine_px = self._fut_ltp() or pos.spine_entry
        view = None
        if snap:
            view = HistChainView(data_mod.chain_reader, snap,
                                 self._index_spot() or spine_px)
        for idx, leg in enumerate(pos.legs):
            if str(idx) in pos.exit_prices:
                continue
            if leg.right == "FUT":
                px = snap_tick(spine_px)
            else:
                q = view.quote(leg.expiry, leg.strike, leg.right) \
                    if view else None
                px = fill_price(q, exit_side(leg.side), 0.25) if q \
                    else leg.price
            pos.exit_prices[str(idx)] = px
            kind = "FUT" if leg.right == "FUT" else "OPT"
            pos.exit_costs += costs_mod.order_cost(kind, exit_side(leg.side),
                                                   px, leg.qty, day)
        pnl_gross = sum(
            (1.0 if l.side == BUY else -1.0)
            * (pos.exit_prices[str(i)] - l.price) * l.qty
            for i, l in enumerate(pos.legs))
        costs = pos.entry_costs + pos.exit_costs
        lab_store.close_position(row["id"], int(now.timestamp()), reason,
                                 pos.exit_prices, round(pnl_gross, 2),
                                 round(costs, 2),
                                 round(pnl_gross - costs, 2))
        lab_store.log_risk_event(int(now.timestamp()), day, row["sid"],
                                 reason, {"position_id": row["id"]})

    # ---- live helpers -----------------------------------------------------

    @staticmethod
    def _fut_ltp() -> Optional[float]:
        meta = market_state.underlyings.get("NIFTY")
        if not meta or not meta.fut_token:
            return None
        tick = market_state.ticks.get(meta.fut_token)
        if not tick:
            return None
        return float(tick.get("last_price") or 0) or None

    @staticmethod
    def _index_spot() -> Optional[float]:
        meta = market_state.underlyings.get("NIFTY")
        if not meta:
            return None
        tick = market_state.ticks.get(meta.spot_token)
        if not tick:
            return None
        return float(tick.get("last_price") or 0) or None

    # ---- per-poll processing ----------------------------------------------

    def process(self, now: datetime) -> None:
        self._sync_runners()
        if not self.runners:
            return
        day = now.date().isoformat()
        data_mod.chain_reader.snap_ts_list(refresh=True)

        needed_kinds = {r.sdef.spine for r in self.runners.values()}
        errors = {}
        for kind in needed_kinds:
            feed = self.feeds[kind]
            # refetch shortly after each 5m boundary
            if time.time() - feed.last_fetch >= 60:
                err = feed.refresh(now)
                if err:
                    errors[kind] = err
        for key, runner in list(self.runners.items()):
            feed = self.feeds[runner.sdef.spine]
            if runner.sdef.spine in errors and not feed.today:
                self._suspend_if_persistent(runner, errors[runner.sdef.spine])
                continue
            try:
                self._process_runner(runner, feed, now, day)
            except Exception:
                log.exception("runner %s failed", runner.sid)

    def _suspend_if_persistent(self, runner: "_Runner", err: str) -> None:
        # after 10 consecutive failed polls, suspend with the reason
        n = getattr(runner, "_feed_fails", 0) + 1
        runner._feed_fails = n
        if n >= 10:
            lab_store.set_state(runner.sid,
                                suspended_reason="feed: %s" % err)
            lab_store.log_risk_event(int(time.time()),
                                     now_ist().date().isoformat(),
                                     runner.sid, "suspended_feed",
                                     {"error": err})

    def _process_runner(self, runner: "_Runner", feed: _SpineFeed,
                        now: datetime, day: str) -> None:
        runner._feed_fails = 0
        df = feed.frame()
        if df.empty or len(df) < 100:
            return
        step = TF_SECONDS[runner.sdef.timeframe]
        if runner.sdef.timeframe != "5m":
            df = data_mod.resample(df, runner.sdef.timeframe)
        p = runner.bt.p
        df = feat.add_features(
            df, ema_fast=int(p.get("ema_fast", 9)),
            ema_slow=int(p.get("ema_slow", 21)),
            st_period=int(p.get("st_period", 10)),
            st_mult=float(p.get("st_mult", 3.0)))
        runner.bt._spot_fn = runner.bt._spot_lookup(df)

        if runner.day != day:
            runner.day = day
            runner.strategy().day_reset()
            runner.last_bar_ts = 0

        now_ts = int(now.timestamp())
        # the newest CLOSED bar: open + step <= now
        closed = df[df["ts"] + step <= now_ts]
        if closed.empty:
            return
        i = int(closed.index[-1])
        bar = df.iloc[i]
        bar_ts = int(bar["ts"])
        if int(bar["day"]) != (now_ts + 19800) // 86400:
            return                      # today's bars not arrived yet

        # 1) settle pending orders (waiting for their fill observation)
        self._settle_pending(runner, df, i, day, now_ts)

        # 2) manage open position at every poll (marks move between bars)
        open_rows = [r for r in lab_store.open_positions(runner.sid)
                     if bool(r["experimental"]) == runner.experimental
                     and r["version"] == runner.version]
        pos_open = bool(open_rows)
        for row in open_rows:
            self._manage_live(runner, row, df, i, now, day)

        if bar_ts <= runner.last_bar_ts:
            return                      # this closed bar is already done
        runner.last_bar_ts = bar_ts

        # 3) new signal on the just-closed bar
        open_rows = [r for r in lab_store.open_positions(runner.sid)
                     if bool(r["experimental"]) == runner.experimental]
        if open_rows or self._has_pending(runner, day):
            return
        ctx = self._ctx(runner, df, i, now_ts, day,
                        flat=True)
        sig = None
        try:
            sig = runner.strategy().on_bar(ctx)
        except Exception as exc:
            log.exception("strategy %s on_bar failed", runner.sid)
            lab_store.log_risk_event(now_ts, day, runner.sid,
                                     "strategy_error", {"err": str(exc)})
            return
        if sig is None:
            return
        minute = int(bar["minute"]) + step // 60
        why = runner.bt._gate(sig, ctx, minute, ctx.trades_today)
        payload = {
            "direction": sig.direction, "tag": sig.tag, "note": sig.note,
            "legs": [l.__dict__ for l in sig.legs],
            "exit_policy": sig.exit_policy.__dict__,
        }
        if why:
            lab_store.log_signal(now_ts, day, runner.sid, runner.version,
                                 runner.experimental, payload, "blocked",
                                 why)
            return
        # risk engine gate (margin refined again at fill time)
        rd = risk_mod.check_order(runner.sid, day,
                                  [l.__dict__ for l in sig.legs],
                                  margin_est=0.0,
                                  expiry_day=ctx.expiry_day,
                                  lots=int(p.get("lots", 1)),
                                  version=runner.version,
                                  experimental=runner.experimental)
        if not rd.allowed:
            lab_store.log_signal(now_ts, day, runner.sid, runner.version,
                                 runner.experimental, payload, "blocked",
                                 "risk:%s" % rd.code)
            lab_store.log_risk_event(now_ts, day, runner.sid, rd.code,
                                     {"detail": rd.detail})
            return
        sig_id = lab_store.log_signal(now_ts, day, runner.sid,
                                      runner.version, runner.experimental,
                                      payload, "ordered", "")
        lab_store.create_order(sig_id, now_ts, day, runner.sid,
                               runner.version, payload["legs"])

    def _has_pending(self, runner: "_Runner", day: str) -> bool:
        return any(o["sid"] == runner.sid and o["version"] == runner.version
                   for o in lab_store.pending_orders(day))

    def _ctx(self, runner: "_Runner", df: pd.DataFrame, i: int, now_ts: int,
             day: str, flat: bool) -> BarContext:
        bar = df.iloc[i]
        step = TF_SECONDS[runner.sdef.timeframe]
        decision_ts = int(bar["ts"]) + step
        vix_df = data_mod.load_vix_daily()
        vix = data_mod.vix_asof(vix_df, int(bar["ts"]))
        vix_pct = data_mod.vix_percentile(vix_df, int(bar["ts"]))
        regime = feat.classify_row(bar, vix_pct)
        chainf = None
        if runner.bt.needs_chain:
            snap = data_mod.chain_reader.snap_at_or_before(decision_ts,
                                                           QUOTE_STALE_S)
            chainf = ChainFeatures(data_mod.chain_reader, snap,
                                   float(bar["close"]))
        trades_today = len([s for s in lab_store.signals_for_day(day)
                            if s["sid"] == runner.sid
                            and s["outcome"] == "ordered"])
        expiry_days = runner.bt._expiry_day_set()
        return BarContext(
            i=i, df=df, ts=decision_ts, row=bar, chainf=chainf, vix=vix,
            vix_pctile=vix_pct, regime=regime, expiry_day=day in expiry_days,
            trades_today=trades_today, flat=flat, day=day,
            iv_pctile_fn=runner.bt._atm_iv_percentile,
            straddle_series_fn=runner.bt._straddle_series
            if runner.bt.needs_chain else None)

    def _settle_pending(self, runner: "_Runner", df: pd.DataFrame, i: int,
                        day: str, now_ts: int) -> None:
        for order in lab_store.pending_orders(day):
            if order["sid"] != runner.sid or \
                    order["version"] != runner.version:
                continue
            dec_ts = int(order["ts"])
            if now_ts - dec_ts > FILL_TTL_S:
                lab_store.set_order(order["id"], "lapsed", [],
                                    "no fill observation within TTL")
                continue
            from app.lab.models import ExitPolicy, LegSpec, SignalIntent
            sig_rows = [s for s in lab_store.signals_for_day(day)
                        if s["id"] == order["signal_id"]]
            if not sig_rows:
                lab_store.set_order(order["id"], "rejected", [],
                                    "signal row missing")
                continue
            payload = sig_rows[0]["payload"]
            legs = tuple(LegSpec(**l) for l in payload["legs"])
            intent = SignalIntent(
                ts=dec_ts, direction=payload["direction"], legs=legs,
                exit_policy=ExitPolicy(**payload["exit_policy"]),
                tag=payload.get("tag", ""))
            has_opt = any(l.right != "FUT" for l in legs)
            if has_opt:
                snap = data_mod.chain_reader.snap_after(dec_ts, FILL_TTL_S)
                if snap is None:
                    continue            # keep waiting inside TTL
                fills, why = runner.bt._fill_options(
                    intent, dec_ts, runner.bt._spot_fn)
                if fills is None:
                    lab_store.set_order(order["id"], "rejected", [], why)
                    continue
                leg_fills = []
                for kind, item in fills:
                    if kind == "FUT":
                        px = self._fut_ltp()
                        if px is None:
                            leg_fills = None
                            break
                        leg_fills.append(runner.bt._fut_leg_fill(
                            item, px, now_ts, day))
                    else:
                        leg_fills.append(item)
                if leg_fills is None:
                    lab_store.set_order(order["id"], "rejected", [],
                                        "no futures quote for hedge leg")
                    continue
            else:
                px = self._fut_ltp()
                if px is None:
                    continue            # wait for a futures tick
                leg_fills = [runner.bt._fut_leg_fill(l, px, now_ts, day)
                             for l in legs]

            entry_costs = 0.0
            for lf in leg_fills:
                kind = "FUT" if lf.right == "FUT" else "OPT"
                entry_costs += costs_mod.order_cost(kind, lf.side, lf.price,
                                                    lf.qty, day)
            pos = Position(
                sid=runner.sid, version=runner.version, signal_ts=dec_ts,
                direction=intent.direction, tag=intent.tag, legs=leg_fills,
                exit_policy=intent.exit_policy, entry_ts=now_ts,
                entry_costs=entry_costs)
            pos.entry_value = runner.bt._entry_value(pos)
            bar = df.iloc[i]
            pos.spine_entry = float(bar["close"])
            pos.spine_atr_entry = float(bar.get("atr") or 0.0)
            spot = self._index_spot() or float(bar["close"])
            margin = costs_mod.position_margin_rs(
                [{"side": l.side, "right": l.right, "strike": l.strike,
                  "price": l.price, "qty": l.qty} for l in leg_fills], spot)
            rd = risk_mod.check_order(
                runner.sid, day,
                [{"side": l.side, "right": l.right} for l in leg_fills],
                margin_est=margin, expiry_day=False,
                lots=int(runner.bt.p.get("lots", 1)),
                version=runner.version, experimental=runner.experimental)
            if not rd.allowed:
                lab_store.set_order(order["id"], "rejected", [],
                                    "risk:%s" % rd.code)
                continue
            fills_json = [l.__dict__ for l in leg_fills]
            lab_store.set_order(order["id"], "filled", fills_json, "")
            lab_store.open_position_create(
                order["id"], day, runner.sid, runner.version,
                runner.experimental, now_ts, intent.direction, intent.tag,
                fills_json, intent.exit_policy.__dict__, _pos_state(pos))

    def _manage_live(self, runner: "_Runner", row: dict, df: pd.DataFrame,
                     i: int, now: datetime, day: str) -> None:
        pos = _rehydrate(row)
        bar = df.iloc[i]
        now_ts = int(now.timestamp())
        minute = _ist_minute(now)
        has_opt = any(l.right != "FUT" for l in pos.legs)
        view = None
        spine_px = self._fut_ltp() or float(bar["close"])
        if has_opt:
            snap = data_mod.chain_reader.snap_at_or_before(now_ts,
                                                           QUOTE_STALE_S)
            if snap:
                view = HistChainView(data_mod.chain_reader, snap,
                                     self._index_spot()
                                     or float(bar["close"]))
        mark = runner.bt._mark_value(pos, view, spine_px)
        if mark is not None:
            pnl_open = (mark + runner.bt._realized(pos)) - pos.entry_value
            pos.peak_value = max(pos.peak_value, pnl_open)
            pos.mae = min(pos.mae, pnl_open)
            pos.mfe = max(pos.mfe, pnl_open)

        reason = None
        ep = pos.exit_policy
        p = runner.bt.p
        if ep.time_exit_min is not None and minute >= int(ep.time_exit_min):
            reason = "time_exit"
        elif not runner.sdef.overnight and \
                minute >= int(p.get("squareoff_min", 915)):
            reason = "squareoff"
        if reason is None and risk_mod.effective().get("emergency_stop") \
                and risk_mod.effective().get("emergency_flatten"):
            reason = "emergency_flatten"
        if reason is None and not has_opt:
            hit = runner.bt._check_spine_bracket(pos, bar)
            if hit:
                reason = hit[0]
        if reason is None and has_opt and mark is not None:
            reason = runner.bt._check_value_bracket(pos, mark)
        if reason is None:
            ctx = self._ctx(runner, df, i, now_ts, day, flat=False)
            reason = runner.strategy().wants_exit(ctx, pos)
            if reason is None:
                acts = runner.strategy().adjust(ctx, pos)
                if acts:
                    self._adjust_live(runner, row, pos, acts, view, bar, day)
                    return
        if reason is None:
            lab_store.update_position_state(row["id"], _pos_state(pos))
            return

        # exit fills: options need an observation AFTER the trigger; use
        # the freshest snapshot (live: the trigger IS the fresh snapshot)
        for idx, leg in enumerate(pos.legs):
            if str(idx) in pos.exit_prices:
                continue
            if leg.right == "FUT":
                px = snap_tick(spine_px)
            else:
                q = view.quote(leg.expiry, leg.strike, leg.right) \
                    if view else None
                if q is None:
                    lab_store.update_position_state(row["id"],
                                                    _pos_state(pos))
                    return              # wait for a quote to exit honestly
                px = fill_price(q, exit_side(leg.side),
                                runner.bt.opt_slip)
            pos.exit_prices[str(idx)] = px
            kind = "FUT" if leg.right == "FUT" else "OPT"
            pos.exit_costs += costs_mod.order_cost(kind, exit_side(leg.side),
                                                   px, leg.qty, day)
        pnl_gross = runner.bt._realized(pos)
        costs = pos.entry_costs + pos.exit_costs
        lab_store.close_position(row["id"], now_ts, reason, pos.exit_prices,
                                 round(pnl_gross, 2), round(costs, 2),
                                 round(pnl_gross - costs, 2))

    def _adjust_live(self, runner: "_Runner", row: dict, pos: Position,
                     acts: list, view, bar, day: str) -> None:
        now_ts = int(time.time())
        for act in acts:
            if act[0] == "close":
                idx = act[1]
                if str(idx) in pos.exit_prices:
                    continue
                leg = pos.legs[idx]
                if leg.right == "FUT":
                    px = snap_tick(self._fut_ltp()
                                   or float(bar["close"]))
                else:
                    q = view.quote(leg.expiry, leg.strike, leg.right) \
                        if view else None
                    if q is None:
                        continue
                    px = fill_price(q, exit_side(leg.side),
                                    runner.bt.opt_slip)
                pos.exit_prices[str(idx)] = px
                kind = "FUT" if leg.right == "FUT" else "OPT"
                pos.exit_costs += costs_mod.order_cost(
                    kind, exit_side(leg.side), px, leg.qty, day)
            elif act[0] == "open":
                spec = act[1]
                if spec.right == "FUT":
                    px = self._fut_ltp()
                    if px is None:
                        continue
                    pos.legs.append(runner.bt._fut_leg_fill(spec, px,
                                                            now_ts, day))
                else:
                    if view is None:
                        continue
                    q, why = resolve_leg(view, spec,
                                         runner.bt.p.get("min_oi", 0),
                                         runner.bt.p.get("max_spread_pct",
                                                         99.0))
                    if q is None:
                        continue
                    pos.legs.append(leg_fill_from(
                        q, spec, runner.bt.p.get("lots", 1), day,
                        runner.bt.opt_slip))
                lf = pos.legs[-1]
                kind = "FUT" if lf.right == "FUT" else "OPT"
                pos.entry_costs += costs_mod.order_cost(kind, lf.side,
                                                        lf.price, lf.qty,
                                                        day)
        pos.entry_value = runner.bt._entry_value(pos)
        lab_store.update_position_state(row["id"], _pos_state(pos),
                                        legs=[l.__dict__ for l in pos.legs])


paper_engine = PaperEngine()


async def lab_loop() -> None:
    """Started from main.py lifespan. Idles unless LAB_PAPER_ENABLED."""
    log.info("Lab paper loop up")
    while True:
        cfg = get_settings()
        await asyncio.sleep(max(5.0, cfg.lab_poll_s))
        try:
            if not cfg.lab_paper_enabled:
                continue
            now = now_ist()
            if not is_trading_day(now.date()):
                continue
            day = now.date().isoformat()
            minute = _ist_minute(now)
            eng = paper_engine
            if minute >= _PREMARKET_MIN and eng._premarket_done != day:
                eng._sync_runners()
                await asyncio.to_thread(eng.premarket, now)
                eng._premarket_done = day
            if _SESSION_OPEN <= minute <= _SESSION_CLOSE + 5:
                await asyncio.to_thread(eng.process, now)
            sess = lab_store.paper_session(day)
            if minute >= _POSTCLOSE_MIN and eng._postclose_done != day and \
                    sess and sess.get("status") == "live":
                await asyncio.to_thread(eng.postclose, now)
                eng._postclose_done = day
        except Exception:
            log.exception("lab loop iteration failed")
