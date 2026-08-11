"""Iron Condor evaluation engine — deterministic, floors-first, auditable.

Pipeline (spec §7): data-quality gate → regime gate → volatility gate →
strike selection → wing economics → hard gates (credit, POP, risk, liquidity,
entry window, events, DTE) → display score. Every refusal is a listed reason;
the default output is NO TRADE and that is a feature, not a failure.

The score is a GATE with a breakdown, never a ranking — the 10-Aug methodology
audit showed the directional engine's score is anti-calibrated above 80, so
this one refuses to pretend magnitude means quality. No LLM anywhere in this
path.
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta, timezone

from app.condor import charges as ccharges
from app.condor import expected_move as em_mod
from app.condor import regime as cregime
from app.condor.chain_view import ChainView, LegQuote
from app.condor.greeks import condor_pop
from app.condor.models import (
    CondorCard,
    CondorLeg,
    CondorResponse,
    LegSide,
    ScorePart,
)
from app.kite.instruments import OptionUniverse
from app.market import calendar as mcal
from app.market.events import upcoming as event_upcoming
from app.models.schemas import OptionChain
from app.signals.features import oi_analysis
from app.state import MarketState

log = logging.getLogger("tradewell.condor")

_IST = timezone(timedelta(hours=5, minutes=30))

DELTA_BANDS = {
    "conservative": (0.10, 0.15),
    "balanced": (0.15, 0.20),
    "aggressive": (0.20, 0.25),
}


def _hhmm_minutes(s: str) -> int:
    hh, mm = s.split(":")
    return int(hh) * 60 + int(mm)


class CondorEngine:
    def __init__(self, cfg, state: MarketState) -> None:
        self.cfg = cfg
        self.state = state
        # (ts, put_writing, call_writing) trail for the breakout OI component.
        self._writing_trail: list[tuple[float, float, float]] = []

    # ------------------------------------------------------------------
    def evaluate(self, symbol: str, universe: OptionUniverse,
                 chain: OptionChain | None,
                 now_ts: float | None = None) -> CondorResponse:
        now = time.time() if now_ts is None else now_ts
        resp = CondorResponse(symbol=symbol, evaluated_at=int(now))
        cfg = self.cfg

        # ---- G0: feed & frame integrity ------------------------------
        problems: list[str] = []
        if not mcal.is_market_open(datetime.fromtimestamp(now, _IST)):
            problems.append("market closed")
        age = self.state.last_tick_age()
        if age is None or age > 15:
            problems.append(f"feed stale (tick age {age}s)")
        snap = self.state.underlying_snapshot(symbol)
        spot = snap.ltp if snap else None
        if not spot:
            problems.append("no spot LTP")
        # All session-time reads derive from the evaluation clock `now`, never
        # the wall clock — a replay/test evaluating a past timestamp must see
        # that timestamp's session state (the assistant.py 19800-offset bug
        # class; caught again here by the entry-window test).
        now_ist = datetime.fromtimestamp(now, _IST)
        expiry = universe.expiry
        resp.expiry = expiry.isoformat() if expiry else None
        dte = (expiry - now_ist.date()).days if expiry else None
        resp.dte = dte
        resp.spot = spot
        if dte is None:
            problems.append("no expiry resolved")
        elif not (cfg.condor_min_dte <= dte <= cfg.condor_max_dte):
            problems.append(
                f"DTE {dte} outside [{cfg.condor_min_dte},{cfg.condor_max_dte}]"
                + (" — gamma risk EXTREME near expiry" if dte < cfg.condor_min_dte else ""))
        eng = self.state.engine_for_symbol(symbol)
        df5 = eng.dataframe("5m") if eng else None
        df15 = eng.dataframe("15m") if eng else None
        # Spec G0.3: 36 closed 5m bars (~12:15 IST from a 09:15 open). That is
        # deliberately conservative — BB-width percentiles and the compression
        # veto are noise on a thinner session — and it means condor entries
        # effectively start after noon even though the window opens at 10:00.
        if df5 is None or len(df5) < 36 or df15 is None or len(df15) < 12:
            problems.append("indicator frames warming up (need 36x5m + 12x15m closed bars)")
        # Spec G0.7: VIX is a hard input to the vol gate and the regime vetoes;
        # without its tick the evaluation must fail CLOSED, not label vol
        # "UNKNOWN" and sail on (review catch).
        vix_probe = self.state.vix_snapshot()
        if vix_probe is None or vix_probe.ltp is None:
            problems.append("no India VIX tick")
        if problems:
            resp.data_ok = False
            resp.data_problems = problems
            resp.no_trade_reasons = problems
            return resp

        # ---- context: VIX, pulse, walls, levels ----------------------
        vix = self.state.vix_snapshot()
        resp.vix = vix.ltp if vix else None
        vix_pctile = self.state.vix_percentile()
        resp.vix_percentile = vix_pctile
        range_vs_typical = None
        try:
            from app.market.pulse import compute_pulse
            range_vs_typical = compute_pulse(self.state, symbol).get("range_vs_typical_pct")
        except Exception:
            pass
        oi = oi_analysis(chain, spot)
        self._note_writing(now, oi.put_writing, oi.call_writing)
        levels_pair = self._nearest_strong_levels(spot)

        basis = (snap.fut_ltp - spot) if (snap and snap.fut_ltp and spot) else 0.0
        prev_close_fut = (snap.prev_close + basis) if (snap and snap.prev_close) else None

        reg = cregime.classify(
            df5, df15, prev_close_fut,
            vix.status if vix else None,
            vix.change_pct if vix else None,
            range_vs_typical, self.state.gap_ended_at,
            oi_walls=(oi.support_strike, oi.resistance_strike),
            levels=levels_pair, now_ts=now)
        resp.regime = reg.label
        resp.regime_confidence = reg.confidence
        resp.regime_votes = {k: bool(v) for k, v in reg.votes.items()}
        resp.regime_vetoes = reg.vetoes

        brisk = cregime.breakout_risk(
            df5, df15, vix.change_pct if vix else None,
            oi_shift_against=self._writing_unwinding(now, reg.net15))
        resp.breakout_band = brisk.band
        resp.breakout_score = brisk.score
        resp.breakout_parts = brisk.parts

        # ---- expected move + vol regime ------------------------------
        view = ChainView(self.state, universe, cfg.signal_max_premium_age_s)
        t = view.t_years(now)
        em = em_mod.compute(chain, spot, t, expiry)
        resp.em_straddle, resp.em_iv = em.straddle, em.iv_based
        resp.em_atr, resp.em_rv = em.atr_based, em.rv_based
        resp.em_primary = em.primary
        resp.iv_over_rv = em.iv_over_rv
        if em.primary and spot:
            resp.expected_low = round(spot - em.primary, 1)
            resp.expected_high = round(spot + em.primary, 1)
        vol_regime = ("UNKNOWN" if vix_pctile is None else
                      "LOW" if vix_pctile < 25 else
                      "NORMAL" if vix_pctile <= 75 else
                      "ELEVATED" if vix_pctile <= 90 else "EXTREME")
        resp.vol_regime = vol_regime

        # ---- hard gates that need no strikes -------------------------
        reasons: list[str] = []
        if not reg.condor_allowed:
            why = f"regime {reg.label}"
            if reg.vetoes:
                why += f" ({'; '.join(reg.vetoes)})"
            elif reg.notes:
                why += f" ({reg.notes[0]})"
            reasons.append(why)
        if brisk.band in ("HIGH", "EXTREME"):
            reasons.append(f"breakout risk {brisk.band} ({brisk.score})")
        if vol_regime == "EXTREME":
            reasons.append(f"VIX percentile {vix_pctile} — abnormal vol, not selling into it")
        minutes = now_ist.hour * 60 + now_ist.minute
        if not (_hhmm_minutes(cfg.condor_entry_from) <= minutes <= _hhmm_minutes(cfg.condor_entry_to)):
            reasons.append(f"outside entry window {cfg.condor_entry_from}-{cfg.condor_entry_to} IST")
        ev = event_upcoming(int(now), window_h=cfg.condor_event_window_h)
        if ev:
            reasons.append(f"event risk: {ev} — new entries disabled")
        if em.primary is None:
            reasons.append("expected move unavailable (no ATM straddle/IV)")
        if reasons:
            resp.no_trade_reasons = reasons
            return resp

        # ---- strike selection ----------------------------------------
        card, sel_reasons = self._build_card(
            symbol, universe, view, chain, spot, em, reg, brisk,
            vol_regime, vix, vix_pctile, oi, levels_pair, t, dte, now)
        if card is None:
            resp.no_trade_reasons = sel_reasons
            return resp
        resp.card = card
        return resp

    # ------------------------------------------------------------------
    def _build_card(self, symbol, universe, view: ChainView, chain, spot,
                    em, reg, brisk, vol_regime, vix, vix_pctile, oi,
                    levels_pair, t, dte, now):
        cfg = self.cfg
        lo_d, hi_d = DELTA_BANDS.get(cfg.condor_profile, DELTA_BANDS["balanced"])
        reasons: list[str] = []
        em_ref = em.iv_based or em.primary

        def pick_short(right: str) -> tuple[LegQuote | None, list[str]]:
            sign = 1 if right == "CE" else -1
            wall = oi.resistance_strike if right == "CE" else oi.support_strike
            lvl = (levels_pair[1] if right == "CE" else levels_pair[0]) if levels_pair else None
            # MILD regimes: the drift side needs the full EM of room.
            drift_side = ((reg.label == cregime.MILD_BULLISH and right == "CE")
                          or (reg.label == cregime.MILD_BEARISH and right == "PE"))
            min_dist = (1.0 if drift_side else cfg.condor_min_em_dist) * (em_ref or 0)
            cands: list[LegQuote] = []
            why: list[str] = []
            for k in view.strikes():
                if sign * (k - spot) <= 0:
                    continue
                if abs(k - spot) > 6 * (em_ref or spot):     # don't quote the moon
                    continue
                q = view.leg(k, right, spot, now)
                if not q.ok or q.greeks is None:
                    continue
                d = abs(q.greeks["delta"])
                if lo_d <= d <= hi_d:
                    cands.append(q)
            if not cands:
                why.append(f"no liquid {right} short in Δ {lo_d:.2f}-{hi_d:.2f}")
                return None, why
            cands.sort(key=lambda q: q.strike, reverse=(right == "PE"))
            # cands now ordered nearest-the-money first -> outermost last.
            passing = [q for q in cands
                       if abs(q.strike - spot) >= min_dist
                       and self._liquid(q, short=True)]
            if not passing:
                why.append(f"{right} shorts in band fail EM floor "
                           f"({min_dist:.0f} pts) or liquidity")
                return None, why
            barrier = max(filter(None, [wall, lvl]), default=None) if right == "CE" \
                else min(filter(None, [wall, lvl]), default=None)
            # Snap to the barrier only when it lies INSIDE the candidate band
            # (spec §6.3). A wall already behind the innermost candidate makes
            # every candidate "beyond" it — snapping then selects the
            # innermost, i.e. the RISKIEST short, exactly inverted (review
            # catch, confirmed by two lenses).
            if barrier is not None:
                inner = passing[0].strike
                barrier_inside = (barrier > inner) if right == "CE" else (barrier < inner)
                if barrier_inside:
                    beyond = [q for q in passing
                              if (q.strike >= barrier if right == "CE" else q.strike <= barrier)]
                    if beyond:
                        return beyond[0], why       # first at/beyond the barrier
            return passing[-1], why                  # outermost passing: safety first

        s_ce, why_ce = pick_short("CE")
        s_pe, why_pe = pick_short("PE")
        reasons.extend(why_ce + why_pe)
        if s_ce is None or s_pe is None:
            return None, reasons or ["no qualifying short strikes"]

        # ---- wings: smallest width that clears the economics ----------
        best = None
        for w in cfg.condor_wing_width_list:
            w_ce = view.leg(s_ce.strike + w, "CE", spot, now)
            w_pe = view.leg(s_pe.strike - w, "PE", spot, now)
            if not (w_ce.ok and w_pe.ok):
                continue
            if not (self._liquid(w_ce, short=False) and self._liquid(w_pe, short=False)):
                continue
            credit = round((s_ce.mid + s_pe.mid) - (w_ce.mid + w_pe.mid), 2)
            if credit <= 0 or credit / w < cfg.condor_min_credit_pct:
                continue
            lot = s_ce.lot_size or s_pe.lot_size or universe_lot(universe)
            qty = lot * cfg.condor_lots
            est_charges = ccharges.entry_charges(s_ce.mid, s_pe.mid, w_ce.mid, w_pe.mid, qty) \
                + ccharges.exit_charges(s_ce.mid / 2, s_pe.mid / 2, w_ce.mid / 2, w_pe.mid / 2, qty)
            max_loss = (w - credit) * qty + est_charges
            if max_loss > cfg.condor_max_loss_per_trade:
                continue
            if credit * qty < 2 * est_charges:
                continue
            best = (w, w_ce, w_pe, credit, lot, qty, est_charges, max_loss)
            break
        if best is None:
            reasons.append("no wing width clears credit/max-loss/liquidity floors")
            return None, reasons
        w, w_ce, w_pe, credit, lot, qty, est_charges, max_loss = best

        be_low = round(s_pe.strike - credit, 1)
        be_high = round(s_ce.strike + credit, 1)
        pop = condor_pop(spot, be_low, be_high, t, s_pe.iv, s_ce.iv)
        if pop is None:
            reasons.append("POP unavailable (short-leg IV unsolvable)")
            return None, reasons
        if pop < cfg.condor_min_pop:
            reasons.append(f"POP {round(pop * 100)}% below floor "
                           f"{round(cfg.condor_min_pop * 100)}%")
            return None, reasons

        max_profit = credit * qty - est_charges
        margin_est = w * qty * cfg.condor_margin_factor - credit * qty
        rr = round(max_profit / max_loss, 2) if max_loss > 0 else None

        def net(field: str) -> float | None:
            vals = []
            for q, sgn in ((s_ce, -1), (s_pe, -1), (w_ce, 1), (w_pe, 1)):
                if q.greeks is None:
                    return None
                vals.append(sgn * q.greeks[field])
            return round(sum(vals) * lot, 3)

        # ---- score (display, floors already passed) -------------------
        parts: list[ScorePart] = []

        def part(name: str, pts: float, mx: float, detail: str) -> None:
            parts.append(ScorePart(name=name, points=round(pts, 1), max=mx, detail=detail))

        part("Market regime", 20.0 * (reg.votes_sum / 7.0) * min(1.0, reg.bars_held / 6.0),
             20, f"{reg.votes_sum}/7 votes, held {reg.bars_held} bars")
        vol_pts = {"NORMAL": 12.0, "ELEVATED": 15.0, "LOW": 6.0, "UNKNOWN": 8.0}.get(vol_regime, 8.0)
        if em.iv_over_rv and em.iv_over_rv >= 1.0:
            vol_pts = min(15.0, vol_pts + min(3.0, (em.iv_over_rv - 1.0) * 15.0))
        part("Volatility suitability", vol_pts, 15,
             f"{vol_regime}, IV/RV {em.iv_over_rv or 'n/a'}")
        min_dist = min(s_ce.strike - spot, spot - s_pe.strike)
        em_ratio = min_dist / em.primary if em.primary else 0.0
        part("Expected-move safety", 15.0 * max(0.0, min(0.6, em_ratio - 0.6)) / 0.6,
             15, f"nearer short at {em_ratio:.2f}x EM")
        wall_pts = 0.0
        wall_notes = []
        if oi.resistance_strike and s_ce.strike >= oi.resistance_strike:
            wall_pts += 5.0
            wall_notes.append(f"CE beyond wall {oi.resistance_strike:.0f}")
        if oi.support_strike and s_pe.strike <= oi.support_strike:
            wall_pts += 5.0
            wall_notes.append(f"PE beyond wall {oi.support_strike:.0f}")
        if oi.bias == "neutral":
            wall_pts += 3.0
            wall_notes.append("balanced OI flows")
        if oi.pcr is not None and 0.8 <= oi.pcr <= 1.2:
            wall_pts += 2.0
            wall_notes.append(f"PCR {oi.pcr}")
        part("Option-chain structure", min(15.0, wall_pts), 15, "; ".join(wall_notes) or "—")
        sr_pts = 0.0
        if levels_pair:
            lvl_low, lvl_high = levels_pair
            if lvl_high and s_ce.strike >= lvl_high:
                sr_pts += 5.0
            if lvl_low and s_pe.strike <= lvl_low:
                sr_pts += 5.0
        part("Support/resistance", sr_pts, 10, "shorts vs 3y level ladder")
        part("Premium attractiveness",
             10.0 * max(0.0, min(1.0, (credit / w - 0.20) / 0.15)), 10,
             f"credit {credit:.1f} = {credit / w * 100:.0f}% of width")
        part("Probability of profit",
             10.0 * max(0.0, min(1.0, (pop - 0.60) / 0.20)), 10, f"POP {pop * 100:.0f}%")
        liq_pts = 5.0
        for q in (s_ce, s_pe, w_ce, w_pe):
            cap = cfg.condor_max_spread_pct_short if q in (s_ce, s_pe) else cfg.condor_max_spread_pct_wing
            if q.spread_pct is not None and q.spread_pct > cap / 2:
                liq_pts -= 1.0
        part("Liquidity", max(0.0, liq_pts), 5, "spread tightness across legs")

        score = round(sum(p.points for p in parts), 1)
        if score < cfg.condor_watch_min:
            reasons.append(f"score {score} below watchlist floor {cfg.condor_watch_min}")
            return None, reasons
        quality = "HIGH QUALITY" if score >= cfg.condor_score_min else "WATCHLIST"

        def leg(q: LegQuote, side: LegSide) -> CondorLeg:
            g = q.greeks or {}
            return CondorLeg(
                side=side, right=q.right, strike=q.strike, token=q.token,
                tradingsymbol=q.tradingsymbol, ltp=q.ltp, bid=q.bid, ask=q.ask,
                mid=q.mid, spread_pct=q.spread_pct, oi=q.oi, iv=q.iv,
                delta=g.get("delta"), theta=g.get("theta"), vega=g.get("vega"),
                gamma=g.get("gamma"), quote_age_s=q.age_s)

        ist_day = int((now + 19800) // 86400)
        card = CondorCard(
            id=(f"IC-{symbol}-{universe.expiry}-{s_pe.strike:.0f}x{w_pe.strike:.0f}"
                f"-{s_ce.strike:.0f}x{w_ce.strike:.0f}-{ist_day}"),
            symbol=symbol,
            expiry=universe.expiry.isoformat() if universe.expiry else None,
            dte=dte or 0, dte_trading=em.dte_trading, spot=spot,
            legs=[leg(s_ce, LegSide.SELL), leg(w_ce, LegSide.BUY),
                  leg(s_pe, LegSide.SELL), leg(w_pe, LegSide.BUY)],
            width=float(w), lot_size=lot, lots=cfg.condor_lots,
            credit_mid=credit,
            credit_ideal_low=round(credit * 0.95, 1),
            credit_ideal_high=round(credit * 1.05, 1),
            credit_min_acceptable=round(credit * 0.90, 1),
            credit_avoid_below=round(credit * 0.85, 1),
            max_profit=round(max_profit, 0), max_loss=round(max_loss, 0),
            be_low=be_low, be_high=be_high, pop=round(pop, 3),
            risk_reward=rr, margin_estimate=round(margin_est, 0),
            charges_estimate=round(est_charges, 0),
            net_delta=net("delta"), net_theta=net("theta"),
            net_vega=net("vega"), net_gamma=net("gamma"),
            regime=reg.label, regime_confidence=reg.confidence,
            breakout_band=brisk.band, breakout_score=brisk.score,
            vol_regime=vol_regime, vix=vix.ltp if vix else None,
            vix_percentile=vix_pctile, iv_over_rv=em.iv_over_rv,
            em_primary=em.primary, em_straddle=em.straddle, em_iv=em.iv_based,
            em_atr=em.atr_based, em_rv=em.rv_based,
            range_low=reg.range_low, range_high=reg.range_high,
            # 300s validity = 5 eval cycles of grace for a data hiccup, but a
            # real regime flip retires the card within 5 minutes (a fresh
            # qualifying eval refreshes it every 60s, so a live card never
            # actually ages past ~1 minute).
            score=score, score_parts=parts, quality=quality,
            created_at=int(now), valid_until=int(now) + 300, updated_at=int(now),
        )
        card.reasons = self._positives(card, reg, em, oi, em_ratio)
        card.risks = self._negatives(card, reg, brisk, vix, em)
        return card, reasons

    # ------------------------------------------------------------------
    def _liquid(self, q: LegQuote, short: bool) -> bool:
        cfg = self.cfg
        floor_oi = cfg.condor_min_oi_short if short else cfg.condor_min_oi_wing
        if q.oi is None or q.oi < floor_oi:
            return False
        if q.mid is None or q.spread is None:
            return False
        cap_pct = cfg.condor_max_spread_pct_short if short else cfg.condor_max_spread_pct_wing
        return q.spread <= max(cap_pct * q.mid, cfg.condor_spread_abs_floor)

    def _positives(self, card: CondorCard, reg, em, oi, em_ratio: float) -> list[str]:
        out = [
            f"Regime {reg.label} ({reg.votes_sum}/7 votes, held {reg.bars_held} bars)",
            f"Shorts at {em_ratio:.2f}x expected move (EM ±{em.primary:.0f} pts)",
            f"Credit {card.credit_mid:.1f} pts = {card.credit_mid / card.width * 100:.0f}% of {card.width:.0f}-pt wings",
            f"POP {card.pop * 100:.0f}% · breakevens {card.be_low:.0f} / {card.be_high:.0f}",
        ]
        if oi.resistance_strike:
            out.append(f"Call wall {oi.resistance_strike:.0f} · Put wall {oi.support_strike:.0f}")
        if card.net_theta and card.net_theta > 0:
            out.append(f"Theta +₹{card.net_theta:.0f}/day per lot")
        return out

    def _negatives(self, card: CondorCard, reg, brisk, vix, em) -> list[str]:
        out = []
        if brisk.band == "MEDIUM":
            out.append(f"Breakout risk MEDIUM ({brisk.score}): " + "; ".join(brisk.parts[:2]))
        if vix and vix.change_pct and vix.change_pct > 2:
            out.append(f"VIX +{vix.change_pct:.1f}% today")
        if em.iv_over_rv and em.iv_over_rv < 1.0:
            out.append(f"IV below realized vol ({em.iv_over_rv}) — premium is cheap")
        if card.dte <= 2:
            out.append(f"{card.dte} DTE — gamma builds fast from here")
        if reg.label != cregime.RANGE_BOUND:
            out.append(f"Regime is {reg.label}, not fully range-bound — skewed placement applied")
        return out

    # ------------------------------------------------------------------
    def _note_writing(self, now: float, put_w: float, call_w: float) -> None:
        self._writing_trail.append((now, put_w, call_w))
        cutoff = now - 3600
        self._writing_trail = [x for x in self._writing_trail if x[0] >= cutoff]

    def _writing_unwinding(self, now: float, net15: int) -> bool:
        """True when the dominant side's writing has SHRUNK over ~30 min —
        writers covering is how breakouts start in the chain."""
        old = [x for x in self._writing_trail if x[0] <= now - 1500]
        if not old:
            return False
        _, put_old, call_old = old[-1]
        _, put_now, call_now = self._writing_trail[-1]
        dominant_put = put_old >= call_old
        if dominant_put:
            return put_now < put_old * 0.8 and put_old > 0
        return call_now < call_old * 0.8 and call_old > 0

    def _nearest_strong_levels(self, spot: float | None) -> tuple | None:
        if not spot:
            return None
        try:
            from app.patterns import store as pstore
            from app.patterns.level_watch import strong_levels
            results = pstore.load_results()
            ladder = (results or {}).get("levels", {}).get("levels", [])
            strong = strong_levels(ladder)
            below = [lv["price"] for lv in strong if lv["price"] < spot]
            above = [lv["price"] for lv in strong if lv["price"] > spot]
            return (max(below) if below else None, min(above) if above else None)
        except Exception:
            return None


def universe_lot(universe: OptionUniverse) -> int:
    for sp in universe.strikes.values():
        if sp.ce_lot_size:
            return sp.ce_lot_size
        if sp.pe_lot_size:
            return sp.pe_lot_size
    return 0
