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

import logging
import threading
import time
from collections import deque

log = logging.getLogger("tradewell.patterns")

# "Highest-frequency trends" filter: a level qualifies only with this many
# DISTINCT touch-days and this hold rate — below either, it is a coin drawn
# on a chart. Same floors the tendencies layer uses for its verdicts.
MIN_DAYS_TOUCHED = 5
MIN_HOLD_RATE = 0.60

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

    def __init__(self, cfg, state) -> None:
        self.cfg = cfg
        self.state = state
        self._lock = threading.Lock()
        self._levels: list = []
        self._levels_at = 0.0
        self._prev_spot: float | None = None
        self._armed: dict = {}          # level -> ready to fire
        self._last_fire: dict = {}      # level -> ts
        self.alerts: deque = deque(maxlen=30)
        # Injection point so tests capture pushes instead of spawning threads.
        from app.notify import push_text

        self._push = push_text

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

    # ---- live contract enrichment -------------------------------------------
    def _atm_quote(self, spot: float, now: int) -> dict:
        """Nearest-strike CE quote from the live chain — the 'what would I
        actually buy' line the user asked for. Degrades to strike-only when
        the quote is stale or absent: the repo's own freshness lesson (21-Jul:
        cards priced at the previous day's close) applies to a callout's
        rupee figure too — no timestamped tick within QUOTE_MAX_AGE_S, no
        premium in the push (review catch)."""
        out = {"strike": round(spot / 50) * 50, "expiry": None, "ce_ltp": None}
        try:
            from app.kite.instruments import chain_key
            from app.signals.engine import premium_quote

            chain = self.state.get_option_chain(chain_key("NIFTY", "nearest"))
            if chain is None:
                return out
            out["expiry"] = chain.expiry
            row = next((r for r in chain.rows if r.strike == out["strike"]), None)
            if row is not None and row.ce_ltp and row.ce_token:
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
            prev, self._prev_spot = self._prev_spot, spot
            if prev is None or not self._levels:
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
            body = (
                f"Spot {spot:,.0f} touched support {level:,.0f} — {held} "
                f"({lv.get('total_touches', '?')} touches).\n"
                f"{contract}\n"
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
            "ce_ltp": quote["ce_ltp"], "title": title,
        }
        with self._lock:
            self.alerts.append(alert)
        log.info("level watch: %s", title)
        # Owner's topic only: level context describes YOUR positioning.
        self._push(title, body, self.cfg, audience="private")

    def recent(self) -> list:
        with self._lock:
            return list(self.alerts)
