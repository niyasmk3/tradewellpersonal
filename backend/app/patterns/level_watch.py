"""Level-touch callouts: the S/R ladder made audible.

WHAT THIS IS: the Patterns Module's fractal-pivot levels carry 3 years of
touch history — how many distinct days each level was tested and how often it
HELD. This watches the live index against the STRONGEST of those levels (the
user's ask: "only the highest-frequency trends") and calls out, with the
tradeable contract attached:

  * BUY setup  — price falls onto a level that historically held as support:
    the callout names the ATM call, its expiry and live premium.
  * CEILING    — price climbs into the strongest overhead resistance: the
    callout says "if long, consider booking" with the level's rejection rate.

WHAT THIS IS NOT: a signal card. Level touches bypass none of the engine's
gates because they never enter the engine — they are CONTEXT alerts, each
carrying its own historical hold rate and n so the phone reads a frequency,
not a promise. No sizing, primary topic only.

Mechanics: edge-triggered per level (one callout per approach; re-arms only
after price leaves the band by 3x tolerance) with a per-level cooldown, so a
tape oscillating on a level cannot buzz the phone every five seconds.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from pathlib import Path

log = logging.getLogger("tradewell.patterns")

# Persisted callout history + outcomes: the 03-Aug 14:21 CEILING callout
# vanished in a 15:31 restart because the ring buffer was memory-only — a
# ledger that forgets its own calls cannot grade them.
_ALERTS_PATH = Path(__file__).resolve().parents[2] / ".level_alerts.json"
_KEEP_DAYS = 14

# "Highest-frequency trends" filter: a level qualifies only with this many
# DISTINCT touch-days and this hold rate — below either, it is a coin drawn
# on a chart. Same floors the tendencies layer uses for its verdicts.
MIN_DAYS_TOUCHED = 5
MIN_HOLD_RATE = 0.60

# Outcome grading (the "are the callouts making money?" ledger): each callout
# is followed for an hour after it fires. A level "broke" when spot travelled
# BREAK_TOL_PTS through it; the suggestion "won" when the quoted option moved
# the called direction by the 30-minute mark (premium up after a BUY, down
# after a CEILING — booking before a drop is the ceiling's whole claim).
GRADE_HORIZONS_S = {"15m": 900, "30m": 1800, "60m": 3600}
GRADE_FINAL_S = 3900
BREAK_TOL_PTS = 15.0

TOUCH_TOL_PCT = 0.0004        # ~10 points at NIFTY 24.6k: "touching"
REARM_MULT = 3.0              # leave the band by 3x tol before re-arming...
# ...but CAPPED below the ladder's 25-point bin spacing (levels.py
# BIN_POINTS): at NIFTY 24.6k an uncapped 3x tol is ~30 points, more than
# the gap between two adjacent strong levels — price oscillating in that
# band could never travel far enough to re-arm either one, silencing the
# watch for the rest of the session (review catch, reproduced). With the
# cap, the edge trigger stops per-cycle repeats and the COOLDOWN is the
# spam control — as designed.
REARM_CAP_PTS = 12.0
NEAR_WINDOW_PCT = 0.015       # only levels within 1.5% of spot are watched
LEVELS_REFRESH_S = 1800       # re-read the results ladder every 30 min
MAX_WATCHED = 12              # nearest strong levels kept on watch
QUOTE_MAX_AGE_S = 120         # premium older than this stays out of the callout


def strong_levels(all_levels: list) -> list:
    """The ladder reduced to levels whose history earns an alert."""
    out = []
    for lv in all_levels or []:
        try:
            if (lv.get("days_touched", 0) >= MIN_DAYS_TOUCHED
                    and (lv.get("hold_rate") or 0.0) >= MIN_HOLD_RATE):
                out.append(lv)
        except Exception:
            continue
    return out


def _fmt_expiry(iso) -> str:
    try:
        from datetime import date

        return date.fromisoformat(str(iso)).strftime("%d-%b")
    except Exception:
        return str(iso or "?")


class LevelWatchService:
    """Watches live spot against the strong-level ladder; fires callouts.

    check() is called from the feed loop every evaluation cycle; everything
    here must therefore be cheap, non-blocking and unable to raise into the
    loop. Pushes go through notify.push_text on its own daemon thread.
    """

    def __init__(self, cfg, state, alerts_path=_ALERTS_PATH) -> None:
        self.cfg = cfg
        self.state = state
        self._lock = threading.Lock()
        self._levels: list = []
        self._levels_at = 0.0
        self._prev_spot: float | None = None
        self._armed: dict = {}          # level -> ready to fire
        self._last_fire: dict = {}      # level -> ts
        self.alerts: deque = deque(maxlen=200)
        self._alerts_path = alerts_path
        self._dirty = False
        # Constructed at FEED START (runtime), not import — loading history
        # here keeps callouts and their grades across restarts (tests pass
        # alerts_path=None for a memory-only instance).
        self._load()
        # Injection point so tests capture pushes instead of spawning threads.
        from app.notify import push_text

        self._push = push_text

    # ---- persistence ---------------------------------------------------------
    def _load(self) -> None:
        if self._alerts_path is None or not self._alerts_path.exists():
            return
        try:
            cutoff = int(time.time()) - _KEEP_DAYS * 86400
            rows = json.loads(self._alerts_path.read_text())
            for a in rows:
                if isinstance(a, dict) and (a.get("ts") or 0) >= cutoff:
                    self.alerts.append(a)
        except Exception:
            log.warning("level watch: alert history unreadable", exc_info=True)

    def _save(self) -> None:
        if self._alerts_path is None:
            return
        try:
            with self._lock:
                rows = list(self.alerts)
            tmp = self._alerts_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(rows))
            tmp.replace(self._alerts_path)
            self._dirty = False
        except Exception:
            log.warning("level watch: alert save failed", exc_info=True)

    # ---- ladder ------------------------------------------------------------
    def refresh_levels(self, now: float) -> None:
        # Time-only guard (review catch): gating on `and self._levels` made an
        # EMPTY ladder re-read the results file every 5s cycle forever.
        if now - self._levels_at < LEVELS_REFRESH_S and self._levels_at > 0:
            return
        self._levels_at = now
        try:
            from app.patterns import store as pstore

            results = pstore.load_results()
            ladder = (results or {}).get("levels", {}).get("levels", [])
            self._levels = strong_levels(ladder)
        except Exception:
            log.debug("level watch: ladder refresh failed", exc_info=True)

    def watched(self, spot: float | None) -> list:
        """The strong levels currently on watch (near spot), for the chart."""
        levels = list(self._levels)
        if spot:
            levels = [lv for lv in levels
                      if abs(lv["level"] - spot) <= spot * NEAR_WINDOW_PCT]
            levels.sort(key=lambda lv: abs(lv["level"] - spot))
        return levels[:MAX_WATCHED]

    def _next_ceiling(self, level: float) -> dict | None:
        """The nearest strong level ABOVE `level` — the BUY callout's sell
        side. From the same watched ladder, so the number quoted at buy time
        is exactly the level whose touch will fire the CEILING callout."""
        above = [lv for lv in self._levels if float(lv["level"]) > level + 1]
        if not above:
            return None
        return min(above, key=lambda lv: float(lv["level"]))

    # ---- live contract enrichment -------------------------------------------
    def _atm_quote(self, spot: float, now: int) -> dict:
        """Nearest-strike CE quote from the live chain — the 'what would I
        actually buy' line the user asked for. Degrades to strike-only when
        the quote is stale or absent: the repo's own freshness lesson (21-Jul:
        cards priced at the previous day's close) applies to a callout's
        rupee figure too — no timestamped tick within QUOTE_MAX_AGE_S, no
        premium in the push (review catch)."""
        out = {"strike": round(spot / 50) * 50, "expiry": None, "ce_ltp": None,
               "token": None}
        try:
            from app.kite.instruments import chain_key
            from app.signals.engine import premium_quote

            chain = self.state.get_option_chain(chain_key("NIFTY", "nearest"))
            if chain is None:
                return out
            out["expiry"] = chain.expiry
            row = next((r for r in chain.rows if r.strike == out["strike"]), None)
            if row is not None and row.ce_ltp and row.ce_token:
                out["token"] = int(row.ce_token)
                px, age = premium_quote(getattr(self.state, "ticks", {}),
                                        row.ce_token, float(row.ce_ltp), now)
                if age is not None and age <= QUOTE_MAX_AGE_S:
                    out["ce_ltp"] = float(px)
        except Exception:
            log.debug("level watch: chain quote failed", exc_info=True)
        return out

    # ---- the watch ----------------------------------------------------------
    def check(self, now: int | None = None) -> None:
        """One pass: edge-triggered touch detection on the index spot."""
        try:
            if not getattr(self.cfg, "level_alerts_enabled", True):
                return
            now = now or int(time.time())
            self.refresh_levels(now)
            snap = self.state.underlying_snapshot("NIFTY")
            spot = float(snap.ltp) if snap and snap.ltp else None
            if not spot:
                return
            # Grade open callouts on every pass, even when no level is near —
            # the ledger's whole job is following through after the touch.
            self._grade(now, spot)
            prev, self._prev_spot = self._prev_spot, spot
            if prev is None or not self._levels:
                if self._dirty:
                    self._save()
                return
            tol = spot * TOUCH_TOL_PCT
            rearm = min(REARM_MULT * tol, REARM_CAP_PTS)
            for lv in self.watched(spot):
                level = float(lv["level"])
                key = round(level, 2)
                dist = abs(spot - level)
                if dist > tol:
                    if dist > rearm:
                        self._armed[key] = True
                    continue
                if not self._armed.get(key, True):
                    continue
                # A level that has never fired has no cooldown — only a real
                # prior callout starts the clock.
                last = self._last_fire.get(key)
                if last is not None and now - last < self.cfg.level_alert_cooldown_s:
                    continue
                # Which face of the level is being tested comes from the
                # approach side, not from wishing: falling onto it = support
                # test; rising into it = the ceiling.
                side = "support" if prev > level else "resistance"
                self._armed[key] = False
                self._last_fire[key] = now
                self._fire(side, lv, spot, now)
            if self._dirty:
                self._save()
        except Exception:  # the feed loop must never die for a callout
            log.debug("level watch check failed", exc_info=True)

    def _fire(self, side: str, lv: dict, spot: float, now: int) -> None:
        level = float(lv["level"])
        held = f"held {lv['hold_rate']:.0%} of {lv['days_touched']} days"
        quote = self._atm_quote(spot, now)
        exp = _fmt_expiry(quote["expiry"])
        px = f" @ ₹{quote['ce_ltp']:g}" if quote["ce_ltp"] else ""
        contract = f"NIFTY {quote['strike']} CE{px} · exp {exp}"

        if side == "support":
            title = f"📍 LEVEL BUY setup · {contract}"
            # BOTH exits are named at BUY time (04-Aug, user questions): the
            # failure line below (same threshold the grader calls "broke",
            # backed by the LEVEL BROKE alert) and the first strong ceiling
            # above (where a CEILING callout will fire — the sell side).
            # There is no clock on the sell: it is price, not time.
            up = self._next_ceiling(level)
            sell_line = (
                f"Sell side: first strong ceiling above is {up['level']:,.0f} "
                f"(held {up['hold_rate']:.0%} of {up['days_touched']}d) — a "
                "CEILING callout fires if price gets there.\n"
                if up else "")
            body = (
                f"Spot {spot:,.0f} touched support {level:,.0f} — {held} "
                f"({lv.get('total_touches', '?')} touches).\n"
                f"{contract}\n"
                f"If you trade this: the thesis FAILS below "
                f"{level - BREAK_TOL_PTS:,.0f} — decide that exit before "
                "entering. You'll get a LEVEL BROKE alert if it happens.\n"
                f"{sell_line}"
                "Level context with its own odds — NOT a scored card; the "
                "engine's gate still applies."
            )
        else:
            title = f"📍 LEVEL CEILING {level:,.0f} — consider booking"
            body = (
                f"Spot {spot:,.0f} is at resistance {level:,.0f} — {held} "
                f"({lv.get('total_touches', '?')} touches). If long calls "
                f"({contract}), history says this is where rallies stalled.\n"
                "Frequency, not prophecy — a break past it invalidates this note."
            )

        alert = {
            "ts": now, "side": "buy" if side == "support" else "sell",
            "level": level, "spot": round(spot, 2),
            "hold_rate": lv.get("hold_rate"), "days_touched": lv.get("days_touched"),
            "strike": quote["strike"], "expiry": quote["expiry"],
            "ce_ltp": quote["ce_ltp"], "token": quote.get("token"),
            # The two exits named at fire time: the failure line below and
            # the first strong ceiling above (None when no ceiling is near).
            "fails_below": (round(level - BREAK_TOL_PTS, 2)
                            if side == "support" else None),
            "next_ceiling": (float(self._next_ceiling(level)["level"])
                             if side == "support" and self._next_ceiling(level)
                             else None),
            "title": title,
            # Filled in by _grade over the next hour — the callout's own
            # report card, persisted with it.
            "outcomes": {"spot_max": round(spot, 2), "spot_min": round(spot, 2),
                         "broke": False, "win": None, "final": False},
        }
        with self._lock:
            self.alerts.append(alert)
        self._dirty = True
        log.info("level watch: %s", title)
        # Owner's topic only: level context describes YOUR positioning.
        self._push(title, body, self.cfg, audience="private")

    # ---- outcome grading -----------------------------------------------------
    def _grade(self, now: int, spot: float) -> None:
        """Follow each open callout for an hour: spot extremes, whether the
        level BROKE (spot travelled BREAK_TOL_PTS through it), the spot and
        the quoted option's premium at 15/30/60 minutes, and the 30-minute
        WIN verdict — premium up after a BUY, premium down after a CEILING
        (the ceiling's claim is that booking there avoids a give-back).
        Samples at the watch cadence; extremes persist on the next save."""
        ticks = getattr(self.state, "ticks", {}) or {}
        with self._lock:
            open_alerts = [a for a in self.alerts
                           if not (a.get("outcomes") or {}).get("final")
                           and now - a["ts"] <= GRADE_FINAL_S + 600]
        for a in open_alerts:
            o = a.get("outcomes")
            if o is None:
                o = a["outcomes"] = {"spot_max": a["spot"], "spot_min": a["spot"],
                                     "broke": False, "win": None, "final": False}
            age = now - a["ts"]
            o["spot_max"] = round(max(o["spot_max"], spot), 2)
            o["spot_min"] = round(min(o["spot_min"], spot), 2)
            if not o["broke"]:
                broke = (spot < a["level"] - BREAK_TOL_PTS if a["side"] == "buy"
                         else spot > a["level"] + BREAK_TOL_PTS)
                if broke:
                    o["broke"] = True
                    self._dirty = True
                    # THE PROTECTIVE ALERT (04-Aug): the moment the reason
                    # for a BUY callout dies, say so — minutes, not the 30m
                    # report card. Once per callout (broke latches, and it
                    # persists, so a restart cannot re-buzz it). CEILING
                    # breaks matter too: a broken ceiling means the booking
                    # call was early and the rally is running.
                    mins = max(1, (now - a["ts"]) // 60)
                    if a["side"] == "buy":
                        self._push(
                            f"⚠ LEVEL BROKE — {a['level']:,.0f} failed",
                            (f"Spot {spot:,.0f} has traded through support "
                             f"{a['level']:,.0f} ({mins}m after the BUY callout). "
                             "The reason for that entry is GONE — if you took it, "
                             "this is the exit the callout named."),
                            self.cfg, audience="private")
                    else:
                        self._push(
                            f"LEVEL BROKE UP — {a['level']:,.0f} gave way",
                            (f"Spot {spot:,.0f} pushed through resistance "
                             f"{a['level']:,.0f} ({mins}m after the CEILING "
                             "callout). If you booked there, the rally kept "
                             "going — the ceiling call was early this time."),
                            self.cfg, audience="private", priority="min")
            prem = None
            tok = a.get("token")
            if tok is not None:
                lp = (ticks.get(tok) or {}).get("last_price")
                prem = float(lp) if lp else None
            for label, secs in GRADE_HORIZONS_S.items():
                if age >= secs and label not in o:
                    o[label] = {"spot": round(spot, 2),
                                "prem": round(prem, 2) if prem else None}
                    self._dirty = True
                    if label == "30m" and o.get("win") is None:
                        base = a.get("ce_ltp")
                        if prem is not None and base:
                            o["win"] = prem > base if a["side"] == "buy" else prem < base
                        else:
                            o["win"] = (o[label]["spot"] > a["spot"]
                                        if a["side"] == "buy"
                                        else o[label]["spot"] < a["spot"])
            if age >= GRADE_FINAL_S and not o["final"]:
                o["final"] = True
                self._dirty = True

    def summary(self) -> dict:
        """The scoreboard: are the callouts making money? Aggregated over the
        persisted history (last 14 days), per side, graded rows only."""
        with self._lock:
            rows = [a for a in self.alerts if (a.get("outcomes") or {}).get("win") is not None]
        out = {}
        for side in ("buy", "sell"):
            g = [a for a in rows if a["side"] == side]
            if not g:
                continue
            n = len(g)
            wins = sum(1 for a in g if a["outcomes"]["win"])
            held = sum(1 for a in g if not a["outcomes"]["broke"])
            pmoves = [ (a["outcomes"]["30m"]["prem"] - a["ce_ltp"]) / a["ce_ltp"] * 100
                       for a in g
                       if a.get("ce_ltp") and (a["outcomes"].get("30m") or {}).get("prem")]
            out[side] = {
                "n": n,
                "win_rate": round(100 * wins / n, 1),
                "held_rate": round(100 * held / n, 1),
                "avg_prem_move_30m_pct": (round(sum(pmoves) / len(pmoves), 1)
                                          if pmoves else None),
                "n_prem_graded": len(pmoves),
            }
        return out

    def recent(self) -> list:
        """Today's callouts only (review catch): the ring buffer survives
        across sessions on a long-running process, and a stale alert served
        to the tape chart would get pinned onto today's opening candle —
        a touch that never happened."""
        today = (int(time.time()) + 19800) // 86400
        with self._lock:
            return [a for a in self.alerts if (a["ts"] + 19800) // 86400 == today]
