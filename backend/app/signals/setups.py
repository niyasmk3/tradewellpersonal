"""Setup detectors — structural candidates the score engine cannot see.

WHY (audit P1-4 + the P1-3 verdict): the capture ceiling is DETECTION —
~78% of mechanical moves never produce a gate-78 candidate under any cadence,
largely because a move's ONSET scores poorly on trend-confirmation and the
regime lockout only lets the bias direction act. Detectors fire on structure,
both directions, no lockout.

V1 ships exactly ONE detector, chosen by measurement (see
docs/p1-4-setup-detectors-plan.md): the VWAP cross. On the 60-day sizing
replay it was the first candidate stream this project has measured with
non-negative expectancy on the underlying (+0.04R, 47% WR at 1.5R, n=51)
and it captured 9 moves the entire engine missed. The classical breakout
detector measured 25% WR / -0.43R with a-priori params and was deferred,
not tuned into looking better.

PARAMETERS ARE FROZEN AS REGISTERED. Changing any of them resets the paper
ledger's 30-fill verdict counter to zero — a tuned detector is a new
detector. Everything here is pure: DataFrame in, hit or None out; the
service wires dedupe, gates, card building and the shadow ledger.
"""
from __future__ import annotations

from typing import Optional

import numpy as np

from app.signals.models import Direction

# Registered 03-Aug-2026 (docs/p1-4-setup-detectors-plan.md). Frozen.
SIDE_BARS = 8            # consecutive closes on one side of session VWAP
VOL_MULT = 1.2           # cross bar's volume vs the 20-bar median
VOL_LOOKBACK = 20
DEDUPE_S = 1800          # per-direction re-fire spacing (wired in the service)

SETUP_VWAP = "vwap_cross"


class SetupHit:
    __slots__ = ("name", "direction", "bar_ts", "note")

    def __init__(self, name: str, direction: Direction, bar_ts: int, note: str):
        self.name = name
        self.direction = direction
        self.bar_ts = bar_ts
        self.note = note


def _session_rows(df):
    """Rows belonging to the LAST session in the frame — VWAP is a session
    quantity, and a frame that spans days must not average yesterday in."""
    ts = df["ts"]
    last_day = (int(ts.iloc[-1]) + 19800) // 86400
    return df[(ts + 19800) // 86400 == last_day]


def vwap_cross(df) -> Optional[SetupHit]:
    """A close through session VWAP after >= SIDE_BARS closes on one side,
    on volume >= VOL_MULT x the 20-bar median. Both directions.

    The frame must contain CLOSED candles only (the caller's contract —
    evaluate_symbol already trims the forming bar). Returns a hit for the
    LAST bar or None; firing once per bar is the caller's dedupe job.
    """
    try:
        d = _session_rows(df)
        need = SIDE_BARS + 2             # prior run + cross bar + a seed bar
        if len(d) < need:
            return None
        h, l, c, v = (d["high"].to_numpy(), d["low"].to_numpy(),
                      d["close"].to_numpy(), d["volume"].to_numpy().astype(float))
        tp = (h + l + c) / 3
        cum_v = v.cumsum()
        if cum_v[-1] <= 0:
            return None                  # volume-less frame: nothing to measure
        vwap = (tp * v).cumsum() / cum_v.clip(min=1e-9)

        win = v[-(VOL_LOOKBACK + 1):-1]
        if len(win) < 5:
            return None
        # np.median, EXACTLY as the sizing replay computed it (review catch:
        # the upper-middle order statistic is a slightly different — stricter
        # — instrument than the one whose numbers were registered).
        med = float(np.median(win))
        if med <= 0 or v[-1] < VOL_MULT * med:
            return None

        prior_c = c[-(SIDE_BARS + 1):-1]
        prior_w = vwap[-(SIDE_BARS + 1):-1]
        below = bool((prior_c < prior_w).all())
        above = bool((prior_c > prior_w).all())
        bar_ts = int(d["ts"].iloc[-1])
        ratio = v[-1] / med

        if below and c[-1] > vwap[-1]:
            return SetupHit(
                SETUP_VWAP, Direction.CE, bar_ts,
                f"reclaimed session VWAP after {SIDE_BARS}+ bars below it "
                f"(volume {ratio:.1f}x the recent median)")
        if above and c[-1] < vwap[-1]:
            return SetupHit(
                SETUP_VWAP, Direction.PE, bar_ts,
                f"lost session VWAP after {SIDE_BARS}+ bars above it "
                f"(volume {ratio:.1f}x the recent median)")
        return None
    except Exception:
        return None                      # a detector bug must never stop the engine
