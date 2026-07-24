"""Target calibration from recorded excursions — honest targets, not hopes.

The intraday ladder advertises Target 1 at entry +27% (0.18 stop x 1.5 R:R).
Across every recorded intraday trade it has never been hit; the +12% quick
target is what lands. A target derived from what premiums ACTUALLY did — the
P75 of recorded MFE% — replaces a number derived from what would be nice.

Guardrails, because a calibrator that overfits ten trades is worse than the
static lie it replaces:
  * needs >= 30 clean intraday samples (paper book: honest tape fills only);
  * clamped to [8%, 27%] — never looser than the static plan, never tighter
    than the quick target's neighbourhood;
  * cached for 5 minutes — the distribution moves per trade, not per tick;
  * any failure returns None and the static profile stands.
"""
from __future__ import annotations

import logging
import time

log = logging.getLogger("tradewell.signals")

_MIN_SAMPLES = 30
_CLAMP_LO, _CLAMP_HI = 0.08, 0.27
_CACHE_S = 300

_cache: dict = {"at": 0.0, "value": None}


def _p75_mfe_pct(trades) -> float | None:
    samples = []
    for t in trades:
        if t.mode.value != "intraday":
            continue
        if t.status.value != "exited" or not t.entry_premium or t.mfe_premium is None:
            continue
        if t.excursion_from is None or t.entered_at is None:
            continue
        # Clean sample: excursion tracking covered the trade from entry
        # (within one monitor cadence), so the MFE is the trade's, not a tail.
        if t.excursion_from - t.entered_at > 30:
            continue
        samples.append((t.mfe_premium - t.entry_premium) / t.entry_premium)
    if len(samples) < _MIN_SAMPLES:
        return None
    samples.sort()
    idx = int(0.75 * (len(samples) - 1))
    return samples[idx]


def intraday_rr1(profile, cfg) -> float | None:
    """rr1 override for the intraday profile, or None to keep the static plan.

    Converts the excursion-derived T1%% into the R:R multiple the ladder
    expects: T1%% = premium_sl_pct x rr1  =>  rr1 = T1%% / premium_sl_pct.
    """
    try:
        if not cfg.signal_t1_from_excursions:
            return None
        if profile.mode.value != "intraday":
            return None
        now = time.time()
        if now - _cache["at"] < _CACHE_S:
            p75 = _cache["value"]
        else:
            from app.services import feed

            store = getattr(feed, "paper_store", None)
            p75 = _p75_mfe_pct(store.all()) if store is not None else None
            _cache["at"], _cache["value"] = now, p75
        if p75 is None:
            return None
        t1_pct = max(_CLAMP_LO, min(_CLAMP_HI, p75))
        return round(t1_pct / profile.premium_sl_pct, 3)
    except Exception:  # calibration must never stop a signal
        log.debug("T1 calibration failed — static ladder stands", exc_info=True)
        return None
