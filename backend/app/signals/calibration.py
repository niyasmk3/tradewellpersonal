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
  * floored at 1.5x the quick target (audit P1-6): price_ladder DROPS a quick
    target that is not strictly below T1, so an unguarded calibration landing
    at the P75 MFE (~+10%) would silently null the +12% quick target and
    collapse the two-stage exit the ratchet depends on. The floor lifts T1
    off the quick target but never past the 27% ceiling — the clamp stays
    the hard bound (review catch: an operator raising QUICK_TARGET_PCT past
    18% must not silently push T1 looser than the static plan);
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
    from app.paper.service import HONEST_FILLS_FROM, is_hollow_row

    samples = []
    for t in trades:
        if t.mode.value != "intraday":
            continue
        if t.status.value != "exited" or not t.entry_premium or t.mfe_premium is None:
            continue
        # Pre-honest-fill entries were booked at prices the tape never printed,
        # so MFE% measured against them is fiction — same cutoff as summarize().
        if t.entered_at < HONEST_FILLS_FROM:
            continue
        # Hollow rows are fills of VETOED cards — the counterfactual sample.
        # T1 must calibrate to trades the system would actually offer.
        if is_hollow_row(t):
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
        # P1-6 GUARD. T1 must clear the quick target by enough that the
        # two-stage exit survives calibration: price_ladder nulls any quick
        # target >= T1, so a T1 calibrated into the quick target's
        # neighbourhood would trade the banked +12% for nothing. The floor
        # is itself capped at _CLAMP_HI — the 27% ceiling stays the hard
        # bound, so a QUICK_TARGET_PCT raised past 18% cannot silently issue
        # a T1 looser than the static plan (review catch).
        qt = float(getattr(cfg, "quick_target_pct", 0.0) or 0.0)
        floor = min(1.5 * qt, _CLAMP_HI) if qt > 0 else 0.0
        if t1_pct < floor:
            log.info("T1 calibration %.1f%% floored to %.1f%% (1.5x quick target"
                     " %.0f%%, capped at the %.0f%% ceiling) — preserving the"
                     " two-stage exit",
                     t1_pct * 100, floor * 100, qt * 100, _CLAMP_HI * 100)
            t1_pct = floor
        return round(t1_pct / profile.premium_sl_pct, 3)
    except Exception:  # calibration must never stop a signal
        log.debug("T1 calibration failed — static ladder stands", exc_info=True)
        return None
