"""Live monitoring of manually-journaled condor positions.

Sign-aware from scratch: profit is the combined premium FALLING, the stop is
it RISING — the exact inversion of trades/monitor.py, which is why this file
exists instead of a retrofit. Advisory only: every output is a recommendation
with the rule and its numbers attached; Tradewell places and exits nothing.

Review-hardened invariants (adversarial review 11-Aug, all confirmed then
fixed here):
  * Each position is priced against the universe matching ITS OWN symbol AND
    expiry — never "whichever universe the route had handy" (wrong-week
    quotes after the weekly rolls were silently plausible numbers).
  * Quotes are fail-closed: a leg the ChainView refuses (stale, no depth)
    withholds premium-driven advice with a DATA QUALITY status instead of
    driving the stop-loss off a frozen print.
  * Live P&L subtracts `charges_accrued` (entry + journaled adjustments) so
    it can never disagree with close_position's realized figure by the sunk
    friction.
  * Excursions are latched through the store under its lock (a GET must not
    read-modify-write a deep copy over a concurrent close).

Exit ladder priority (spec §11): STOP LOSS → BREAKOUT → ADJUST → EVENT →
EXPIRY RISK → BOOK PROFIT → PARTIAL → HOLD. First match wins and the UI shows
which rule fired.
"""
from __future__ import annotations

import logging
import math
import time
import uuid
from datetime import datetime, timedelta, timezone

from app.condor import charges as ccharges
from app.condor.chain_view import ChainView
from app.condor.engine import DELTA_BANDS
from app.condor.models import (
    AdjustCondorRequest,
    CondorLeg,
    CondorPosition,
    EnterCondorRequest,
    LegSide,
    PositionView,
)
from app.market import calendar as mcal
from app.market.events import upcoming as event_upcoming

log = logging.getLogger("tradewell.condor")

_IST = timezone(timedelta(hours=5, minutes=30))


def build_position(req: EnterCondorRequest, lot_size: int,
                   expiry_iso: str | None) -> CondorPosition:
    def leg(side: LegSide, right: str, strike: float, fill: float) -> CondorLeg:
        return CondorLeg(side=side, right=right, strike=strike, ltp=fill)

    credit = round((req.short_ce_fill + req.short_pe_fill)
                   - (req.wing_ce_fill + req.wing_pe_fill), 2)
    qty = lot_size * req.lots
    return CondorPosition(
        id=f"ICP-{uuid.uuid4().hex[:8]}",
        card_id=req.card_id,
        symbol=req.symbol.upper(),
        expiry=req.expiry or expiry_iso,
        legs=[
            leg(LegSide.SELL, "CE", req.short_ce_strike, req.short_ce_fill),
            leg(LegSide.BUY, "CE", req.wing_ce_strike, req.wing_ce_fill),
            leg(LegSide.SELL, "PE", req.short_pe_strike, req.short_pe_fill),
            leg(LegSide.BUY, "PE", req.wing_pe_strike, req.wing_pe_fill),
        ],
        width=round(req.wing_ce_strike - req.short_ce_strike, 1),
        lot_size=lot_size, lots=req.lots,
        credit_fill=credit,
        charges_accrued=ccharges.entry_charges(
            req.short_ce_fill, req.short_pe_fill,
            req.wing_ce_fill, req.wing_pe_fill, qty),
        status="open",
        entered_at=int(time.time()), notes=req.notes,
    )


class CondorMonitor:
    def __init__(self, cfg, state, store) -> None:
        self.cfg = cfg
        self.state = state
        self.store = store

    # ------------------------------------------------------------------
    def views(self, universes: dict | None, breakout_band: str | None,
              now_ts: float | None = None) -> list[PositionView]:
        now = time.time() if now_ts is None else now_ts
        out = []
        for pos in self.store.open_positions():
            try:
                uni = self._universe_for(pos, universes)
                out.append(self._view(pos, uni, breakout_band, now))
            except Exception as exc:
                log.warning("condor monitor failed for %s: %s", pos.id, exc)
        return out

    @staticmethod
    def _universe_for(pos: CondorPosition, universes: dict | None):
        """The universe matching this position's OWN symbol and expiry, or
        None. A near-miss (right symbol, wrong week) must NOT be used — its
        quotes are plausible numbers for the wrong contracts."""
        for uni in (universes or {}).values():
            if (uni.symbol.upper() == pos.symbol.upper() and uni.expiry
                    and pos.expiry and uni.expiry.isoformat() == pos.expiry):
                return uni
        return None

    def _view(self, pos: CondorPosition, universe, breakout_band, now) -> PositionView:
        cfg = self.cfg
        pv = PositionView(position=pos, breakout_band=breakout_band)
        dte = self._dte(pos)

        if universe is None:
            pv.data_problems.append(
                f"expiry {pos.expiry} not in subscribed universes — quotes unavailable")
            pv.status_advice, pv.status_detail = self._no_quote_advice(pos, pv, dte, now)
            return pv

        snap = self.state.underlying_snapshot(pos.symbol)
        spot = snap.ltp if snap else None
        view = ChainView(self.state, universe, cfg.signal_max_premium_age_s)
        t_rem = view.t_years(now)

        s_ce = next(l for l in pos.legs if l.side == LegSide.SELL and l.right == "CE")
        s_pe = next(l for l in pos.legs if l.side == LegSide.SELL and l.right == "PE")
        w_ce = next(l for l in pos.legs if l.side == LegSide.BUY and l.right == "CE")
        w_pe = next(l for l in pos.legs if l.side == LegSide.BUY and l.right == "PE")

        quotes = {id(l): view.leg(l.strike, l.right, spot, now) for l in pos.legs}
        qty = pos.lot_size * pos.lots
        for l in pos.legs:
            q = quotes[id(l)]
            if not q.ok:
                pv.data_problems.append(
                    f"{l.side.value} {l.strike:.0f} {l.right}: {'; '.join(q.problems)}")

        # ---- combined premium: FAIL CLOSED on any refused leg ---------
        combined = None
        if not pv.data_problems:
            combined = round((quotes[id(s_ce)].mid + quotes[id(s_pe)].mid)
                             - (quotes[id(w_ce)].mid + quotes[id(w_pe)].mid), 2)
        pv.combined_mid = combined
        if combined is not None and qty:
            exit_est = ccharges.exit_charges(
                quotes[id(s_ce)].mid, quotes[id(s_pe)].mid,
                quotes[id(w_ce)].mid, quotes[id(w_pe)].mid, qty)
            pv.pnl = round((pos.credit_fill - combined) * qty
                           - pos.charges_accrued - exit_est, 0)
            max_profit = pos.credit_fill * qty - pos.charges_accrued
            pv.pnl_pct_of_max = round(pv.pnl / max_profit * 100, 1) if max_profit > 0 else None
            pv.captured_pct = round((1 - combined / pos.credit_fill) * 100, 1) \
                if pos.credit_fill else None
            # Excursion latch goes through the store's lock: a GET reading a
            # deep copy must never write that copy back over a concurrent
            # close (review: closed positions could be resurrected).
            self.store.note_excursion(pos.id, combined)

        # Distances and remaining EM
        em_rem = None
        ivs = [q.iv for q in (quotes[id(s_ce)], quotes[id(s_pe)]) if q.iv]
        if spot and ivs and t_rem > 0:
            em_rem = spot * (sum(ivs) / len(ivs)) * math.sqrt(t_rem)
        if spot:
            pv.dist_short_ce = round(s_ce.strike - spot, 1)
            pv.dist_short_pe = round(spot - s_pe.strike, 1)
            if em_rem:
                pv.dist_short_ce_em = round(pv.dist_short_ce / em_rem, 2)
                pv.dist_short_pe_em = round(pv.dist_short_pe / em_rem, 2)

        greeks_ok = all(quotes[id(l)].greeks for l in pos.legs)
        if greeks_ok:
            sgn = {id(s_ce): -1, id(s_pe): -1, id(w_ce): 1, id(w_pe): 1}
            pv.net_delta = round(sum(sgn[id(l)] * quotes[id(l)].greeks["delta"]
                                     for l in pos.legs) * pos.lot_size, 3)
            pv.net_theta = round(sum(sgn[id(l)] * quotes[id(l)].greeks["theta"]
                                     for l in pos.legs) * pos.lot_size, 1)

        # ---- health (spec §11) ---------------------------------------
        dists = [x for x in (pv.dist_short_ce_em, pv.dist_short_pe_em) if x is not None]
        min_dist_em = min(dists) if dists else None
        band_score = {"LOW": 10, "MEDIUM": 35, "HIGH": 55, "EXTREME": 80}.get(breakout_band or "", 20)
        health = 100.0
        if min_dist_em is not None:
            health -= 35.0 * max(0.0, min(1.0, 1.0 - min_dist_em))
        if combined is not None and pos.credit_fill:
            health -= 25.0 * max(0.0, min(1.0, combined / pos.credit_fill - 1.0))
        health -= 20.0 * band_score / 100.0
        if pv.net_delta is not None and pos.lot_size:
            health -= 10.0 * min(1.0, abs(pv.net_delta / pos.lot_size) / 0.25)
        if dte is not None and dte <= 1:
            health -= 10.0
        pv.health = int(max(0, round(health)))
        pv.health_band = ("Healthy" if pv.health >= 70 else
                          "Warning" if pv.health >= 40 else "Critical")

        # ---- exit ladder ---------------------------------------------
        if pv.data_problems:
            # Premium-driven rules must not fire off refused quotes; the two
            # premium-free deadlines (expiry, event) still apply.
            pv.status_advice, pv.status_detail = self._no_quote_advice(pos, pv, dte, now)
            return pv
        pv.status_advice, pv.status_detail = self._advice(
            pos, pv, combined, breakout_band, min_dist_em, quotes,
            (s_ce, s_pe), dte, now)
        if pv.status_advice == "ADJUST":
            pv.adjustment = self._adjustment(pos, pv, view, spot, em_rem,
                                             quotes, s_ce, s_pe, w_ce, w_pe,
                                             breakout_band, now)
            if pv.adjustment is None:
                pv.status_advice = "EXIT – ADJUSTMENT UNAVAILABLE"
                pv.status_detail += " · no adjustment clears the floors — exit is the correct move"
        return pv

    # ------------------------------------------------------------------
    def _no_quote_advice(self, pos, pv, dte, now) -> tuple[str, str]:
        deadline = self._past_expiry_deadline(dte, now)
        if deadline:
            return deadline
        ev = event_upcoming(int(now), window_h=self.cfg.condor_event_window_h)
        if ev:
            return ("EXIT – EVENT RISK", ev)
        return ("DATA QUALITY WARNING",
                "; ".join(pv.data_problems)[:300] or "quotes unavailable")

    def _past_expiry_deadline(self, dte, now) -> tuple[str, str] | None:
        if dte is None or dte > 0:
            return None
        now_ist = datetime.fromtimestamp(now, _IST)
        m = now_ist.hour * 60 + now_ist.minute
        hh, mm = self.cfg.condor_expiry_exit_ist.split(":")
        if m >= int(hh) * 60 + int(mm):
            return ("EXIT – EXPIRY RISK",
                    f"expiry day past {self.cfg.condor_expiry_exit_ist} IST "
                    "(CAS freezes NIFTY 15:15–15:35 — exit while liquid)")
        return None

    def _dte(self, pos: CondorPosition) -> int | None:
        if not pos.expiry:
            return None
        try:
            y, m, d = (int(x) for x in pos.expiry.split("-"))
            from datetime import date
            return (date(y, m, d) - mcal.now_ist().date()).days
        except Exception:
            return None

    def _advice(self, pos, pv, combined, breakout_band, min_dist_em,
                quotes, shorts, dte, now) -> tuple[str, str]:
        cfg = self.cfg
        if combined is not None and pos.credit_fill and \
                combined >= cfg.condor_sl_mult * pos.credit_fill:
            return ("EXIT – STOP LOSS",
                    f"combined {combined:.1f} ≥ {cfg.condor_sl_mult}x credit {pos.credit_fill:.1f}")
        if breakout_band == "EXTREME":
            return ("EXIT – BREAKOUT RISK", "breakout risk EXTREME")
        if breakout_band == "HIGH" and min_dist_em is not None and min_dist_em <= 0.3:
            return ("EXIT – BREAKOUT RISK",
                    f"breakout HIGH with nearer short at {min_dist_em:.2f}x remaining EM")
        s_ce, s_pe = shorts
        for leg in (s_ce, s_pe):
            g = quotes[id(leg)].greeks
            if g and abs(g["delta"]) >= 0.30:
                return ("ADJUST", f"short {leg.right} {leg.strike:.0f} delta "
                                  f"{abs(g['delta']):.2f} ≥ 0.30")
        if min_dist_em is not None and min_dist_em <= 0.4:
            return ("ADJUST", f"nearer short at {min_dist_em:.2f}x remaining EM")
        ev = event_upcoming(int(now), window_h=cfg.condor_event_window_h)
        if ev:
            return ("EXIT – EVENT RISK", ev)
        deadline = self._past_expiry_deadline(dte, now)
        if deadline:
            return deadline
        if pv.captured_pct is not None and pv.captured_pct >= cfg.condor_profit_target_pct:
            return ("BOOK PROFIT",
                    f"{pv.captured_pct:.0f}% of credit captured "
                    f"(target {cfg.condor_profit_target_pct:.0f}%)")
        if pv.captured_pct is not None and pv.captured_pct >= 35 and (dte or 0) >= 3:
            return ("PARTIAL PROFIT", f"{pv.captured_pct:.0f}% captured with {dte} DTE left")
        return ("HOLD", "no exit rule triggered")

    # ------------------------------------------------------------------
    def _adjustment(self, pos, pv, view: ChainView, spot, em_rem, quotes,
                    s_ce, s_pe, w_ce, w_pe, breakout_band, now) -> dict | None:
        """Deterministic candidates, repriced live (spec §12). Returns the
        best qualifying suggestion or None (=> exit is the honest advice)."""
        cfg = self.cfg
        if pos.adjustments >= cfg.condor_max_adjustments:
            return None
        # Spec §12: no roll is suggested INTO a breakout — adding credit to a
        # structure the tape is leaving is how condors die twice.
        if breakout_band in ("HIGH", "EXTREME"):
            return None
        if not spot or em_rem is None:
            return None
        threatened_ce = (pv.dist_short_ce_em or 9) <= (pv.dist_short_pe_em or 9)
        # Untested side is the one we roll IN toward spot.
        old_s, old_w = (s_pe, w_pe) if threatened_ce else (s_ce, w_ce)
        right = old_s.right
        lo_d, hi_d = DELTA_BANDS.get(cfg.condor_profile, DELTA_BANDS["balanced"])
        old_sq, old_wq = quotes[id(old_s)], quotes[id(old_w)]
        if not (old_sq.mid and old_wq.mid):
            return None
        close_debit = round(old_sq.mid - old_wq.mid, 2)

        best = None
        for k in view.strikes():
            sign = 1 if right == "CE" else -1
            if sign * (k - spot) <= 0:
                continue
            q = view.leg(k, right, spot, now)
            if not q.ok or q.greeks is None:
                continue
            if not lo_d <= abs(q.greeks["delta"]) <= hi_d:
                continue
            if abs(k - spot) < 0.75 * em_rem:
                continue
            wq = view.leg(k + sign * pos.width, right, spot, now)
            if not wq.ok or not wq.mid:
                continue
            new_credit = round(q.mid - wq.mid, 2)
            added = round(new_credit - close_debit, 2)
            if added < cfg.condor_adj_min_credit:
                continue
            new_total_credit = round(pos.credit_fill + added, 2)
            new_max_loss = (pos.width - new_total_credit) * pos.lot_size * pos.lots
            orig_max_loss = (pos.width - pos.credit_fill) * pos.lot_size * pos.lots
            if new_max_loss > 1.15 * orig_max_loss:
                continue
            cand = {
                "action": f"ROLL {'PUT' if right == 'PE' else 'CALL'} SIDE "
                          f"{'UP' if right == 'PE' else 'DOWN'}",
                "side": right,
                "close": f"{old_s.strike:.0f}/{old_w.strike:.0f} {right} spread "
                         f"(debit {close_debit:.1f})",
                "close_debit": close_debit,
                "open": f"{k:.0f}/{k + sign * pos.width:.0f} {right} spread "
                        f"(credit {new_credit:.1f})",
                "new_short_strike": k,
                "new_wing_strike": k + sign * pos.width,
                "added_credit": added,
                "new_total_credit": new_total_credit,
                "new_max_loss": round(new_max_loss, 0),
                "new_breakevens": (
                    f"{(s_pe.strike if right == 'CE' else k) - new_total_credit:.0f} – "
                    f"{(k if right == 'CE' else s_ce.strike) + new_total_credit:.0f}"),
            }
            if best is None or added > best["added_credit"]:
                best = cand
        return best

    # ------------------------------------------------------------------
    def record_adjustment(self, pid: str, req: AdjustCondorRequest) -> CondorPosition | None:
        """Journal a roll the user executed manually: replace the side's legs,
        fold the added credit in, accrue the 4 orders' charges, and count it
        against the adjustment cap (which until this existed could never trip)."""
        pos = self.store.get_position(pid)
        if pos is None or pos.status != "open":
            return None
        old_s = next((l for l in pos.legs
                      if l.side == LegSide.SELL and l.right == req.side), None)
        old_w = next((l for l in pos.legs
                      if l.side == LegSide.BUY and l.right == req.side), None)
        if old_s is None or old_w is None:
            return None
        qty = pos.lot_size * pos.lots
        new_credit = round(req.new_short_fill - req.new_wing_fill, 2)
        added = round(new_credit - req.close_debit, 2)
        # Close = buy short back + sell wing; open = sell new short + buy wing.
        # Split the recorded close_debit across the two closing orders in the
        # entry-fill proportion (estimate; the totals are what matter).
        tot = (old_s.ltp or 0) + (old_w.ltp or 0)
        s_part = req.close_debit * ((old_s.ltp or 0) / tot) if tot else req.close_debit
        w_part = max(0.0, req.close_debit - s_part)
        roll_charges = (
            ccharges.exit_charges(s_part if req.side == "CE" else 0.0,
                                  s_part if req.side == "PE" else 0.0,
                                  w_part if req.side == "CE" else 0.0,
                                  w_part if req.side == "PE" else 0.0, qty)
            + ccharges.entry_charges(
                req.new_short_fill if req.side == "CE" else 0.0,
                req.new_short_fill if req.side == "PE" else 0.0,
                req.new_wing_fill if req.side == "CE" else 0.0,
                req.new_wing_fill if req.side == "PE" else 0.0, qty))
        pos.legs = [l for l in pos.legs if l.right != req.side] + [
            CondorLeg(side=LegSide.SELL, right=req.side,
                      strike=req.new_short_strike, ltp=req.new_short_fill),
            CondorLeg(side=LegSide.BUY, right=req.side,
                      strike=req.new_wing_strike, ltp=req.new_wing_fill),
        ]
        pos.credit_fill = round(pos.credit_fill + added, 2)
        pos.charges_accrued = round(pos.charges_accrued + roll_charges, 2)
        pos.adjustments += 1
        pos.notes = ((pos.notes + " · ") if pos.notes else "") + \
            f"rolled {req.side} to {req.new_short_strike:.0f}/{req.new_wing_strike:.0f} " \
            f"(+{added:.1f} credit)"
        self.store.update_position(pos)
        return pos

    # ------------------------------------------------------------------
    def close_position(self, pid: str, exit_debit: float, reason: str) -> CondorPosition | None:
        pos = self.store.get_position(pid)
        if pos is None or pos.status != "open":
            return None
        qty = pos.lot_size * pos.lots
        # Exit charge estimate splits the debit across legs in entry proportion.
        exit_ch = ccharges.exit_charges(exit_debit * 0.35, exit_debit * 0.35,
                                        exit_debit * 0.15, exit_debit * 0.15, qty)
        pos.status = "closed"
        pos.exited_at = int(time.time())
        pos.exit_debit = exit_debit
        pos.exit_reason = reason
        pos.realized_pnl = round((pos.credit_fill - exit_debit) * qty
                                 - pos.charges_accrued - exit_ch, 0)
        self.store.update_position(pos)
        return pos
