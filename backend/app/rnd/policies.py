"""R3 policy ledgers — the three pre-registered exit policies, run as LIVE
paired twins in the paper book (docs/rnd-tab-plan-2026-08-12.md §R3).

Why live twins and not post-hoc math: "exit at the window end" needs the real
premium AT that moment, which no recorded field can reconstruct — the same
reason stopb/stopc are twins and not spreadsheets. Every clean paper fill
books three twins, identical in entry/ladder/monitor policy, each differing
ONLY in its policy overlay:

  P-5W  ("rnd-p5w"): book fully at +5% inside the mode's R&D window; if the
         window ends first, exit at the window-end premium. Standard stops
         keep protecting meanwhile — a policy that ignores stops would not be
         comparable to anything we'd actually trade.
  P-5T  ("rnd-p5t"): book fully at +5% inside the window; after the window,
         fall through to the normal exit machinery (isolates the
         window-CLOSE leg of P-5W from the take-profit leg).
  P-LAD ("rnd-plad"): book HALF at +5% inside the window; the rest rides the
         normal ratchet (vs. the current all-or-ratchet policy).

Specs are FROZEN here (the setup-detector rule: changing a parameter resets
the verdict clock). Verdicts at >= 30 diverged pairs per mode, read by a
human, never auto-applied. Twins ride the `hollow:` notes channel so every
clean-book consumer already excludes them, and they bypass the capacity
buckets by construction (1:1 with a clean fill that already cleared one).
"""
from __future__ import annotations

import logging
import time

from app.rnd.analytics import window_end as _window_end_dict

log = logging.getLogger("tradewell.rnd")

TARGET_PCT = 5.0                 # frozen with the ladder levels
_EPS = 1e-9

TAGS = {
    "rnd-p5w": "book at +5% in window, else exit at window end",
    "rnd-p5t": "book at +5% in window, else normal exits",
    "rnd-plad": "book HALF at +5% in window, rest rides the ratchet",
    # P-LAD needs >= 2 lots to physically halve, so it books at 2 lots and is
    # paired against THIS dedicated 2-lot standard-policy twin — identical
    # sizing both arms, instead of a 1-lot clean fill it can't be compared to.
    "rnd-base2": "2-lot baseline for P-LAD (standard exits, no overlay)",
}

# Which baseline each policy's pairs grade against: None = the clean fill.
BASELINE_OF = {"rnd-p5w": None, "rnd-p5t": None, "rnd-plad": "rnd-base2"}

_TWO_LOT_TAGS = ("rnd-plad", "rnd-base2")


def policy_of(trade) -> str | None:
    notes = getattr(trade, "notes", None) or ""
    for tag in TAGS:
        if notes.startswith(f"hollow: {tag}:"):
            return tag
    return None


def window_end_of(trade, cfg) -> int | None:
    """The R&D window for a twin — same math as the analytics layer, always
    (two implementations of one window would eventually disagree)."""
    return _window_end_dict(
        {"mode": trade.mode.value, "entered_at": trade.entered_at}, cfg)


# ---------------------------------------------------------------------------
# Booking (called from paper consider() for CLEAN fills only)
# ---------------------------------------------------------------------------

def book_policy_twins(store, card, lots, fill, lot, disaster_pct, quick_pct,
                      sl_pct, rr1, rr2, cfg) -> int:
    """Book the three policy twins for a clean fill. Failure books fewer
    twins, never touches the clean fill (evidence, not the trade)."""
    booked = 0
    for tag, desc in TAGS.items():
        try:
            twin_lots = max(2, lots) if tag in _TWO_LOT_TAGS else lots
            store.create_from_signal(
                card, twin_lots, fill, lot, product=None,
                disaster_pct=disaster_pct, quick_pct=quick_pct,
                sl_pct=sl_pct, rr1=rr1, rr2=rr2,
                notes=f"hollow: {tag}: {desc} — R&D policy twin",
            )
            booked += 1
        except Exception:
            log.warning("rnd: policy twin %s failed for %s — pair skipped",
                        tag, card.contract, exc_info=True)
    return booked


# ---------------------------------------------------------------------------
# Policy exits (called from paper run_once() before the standard trigger loop)
# ---------------------------------------------------------------------------

def apply_policy_exits(store, cfg, slippage_pct: float,
                       now: int | None = None) -> None:
    """Apply each twin's policy overlay. Runs at monitor cadence with live
    premiums — the same sampling every other exit gets."""
    now = int(time.time()) if now is None else now
    for trade in store.all():
        tag = policy_of(trade)
        if tag is None or trade.status.value not in ("entered", "partial"):
            continue
        px = trade.current_premium
        entry = trade.entry_premium
        if not px or not entry:
            continue
        end = window_end_of(trade, cfg)
        in_window = end is not None and now <= end
        move_pct = (px - entry) / entry * 100.0
        fill = round(max(0.05, px * (1 - slippage_pct)), 2)

        if tag in ("rnd-p5w", "rnd-p5t"):
            if in_window and move_pct >= TARGET_PCT - _EPS:
                store.auto_close(trade.id, fill, f"{tag}_target",
                                 price_source="simulated")
                continue
            if tag == "rnd-p5w" and end is not None and now > end:
                # Window over without the take-profit: P-5W closes here by
                # definition. P-5T deliberately falls through to normal exits.
                store.auto_close(trade.id, fill, "rnd-p5w_window",
                                 price_source="simulated")
                continue
            # No window (positional entered past cutoff): the policy is
            # vacuous — the twin just follows the normal machinery, and the
            # pair will read as non-diverged unless stops differ it.
        elif tag == "rnd-plad":
            if (in_window and move_pct >= TARGET_PCT - _EPS
                    and trade.status.value == "entered"):
                if store.book_partial(trade.id, fill, 0.5) is not None:
                    log.info("rnd: %s booked half of %s @ Rs%s (+5%% policy)",
                             tag, trade.contract, fill)


# ---------------------------------------------------------------------------
# Verdicts
# ---------------------------------------------------------------------------

MIN_DIVERGED = 30                # house rule; per policy per mode


def _net(t) -> float | None:
    """Net-of-charges realized rupees for a closed row. Partial-booked rows
    carry two sell legs; approximated as one round trip + one extra brokerage
    order (documented approximation, identical for both arms of a pair)."""
    from app.paper import charges as chg
    if t.realized_pnl is None or not t.entry_premium:
        return None
    exit_px = t.exit_premium or t.entry_premium
    qty = t.initial_quantity or t.quantity
    ch = chg.charges(t.entry_premium, exit_px, qty)
    if any(e.kind == "partial" for e in (t.events or [])):
        ch += 20.0 * 1.18
    return round(t.realized_pnl - ch, 2)


def policy_summary(store, cfg) -> dict:
    """Pair every policy twin with its clean sibling by signal_id; grade only
    closed pairs; verdict at MIN_DIVERGED diverged pairs per policy+mode."""
    clean: dict = {}
    twins: dict = {}
    for t in store.all():
        if not t.signal_id or t.status.value != "exited":
            continue
        tag = policy_of(t)
        notes = getattr(t, "notes", None) or ""
        if tag:
            twins.setdefault(tag, {})[t.signal_id] = t
        elif not notes.startswith("hollow:"):
            clean[t.signal_id] = t

    out: dict = {"min_diverged": MIN_DIVERGED, "policies": {}}
    for tag, desc in TAGS.items():
        if tag == "rnd-base2":
            continue                     # a baseline, not a policy under test
        base_tag = BASELINE_OF.get(tag)
        per_mode: dict = {}
        for sid, tw in twins.get(tag, {}).items():
            base = twins.get(base_tag, {}).get(sid) if base_tag else clean.get(sid)
            if base is None:
                continue
            mode = tw.mode.value
            m = per_mode.setdefault(mode, {"pairs": 0, "diverged": 0,
                                           "delta_sum": 0.0, "graded": 0})
            m["pairs"] += 1
            # Divergence is ECONOMIC, nothing else (review criticals C2/C4):
            # a final-leg exit_premium/reason compare was blind to the partial
            # legs — P-LAD's whole effect — and systematically excluded the
            # pairs where the baseline wins (both arms bank, remainders track
            # together), while reason strings alone flagged identical-money
            # pairs as diverged. |Δnet| > ₹1 absorbs rounding and the flat
            # extra-brokerage term, and is symmetric across both arms.
            tn, bn = _net(tw), _net(base)
            if tn is None or bn is None:
                continue
            if abs(tn - bn) <= 1.0:
                continue
            m["diverged"] += 1
            m["graded"] += 1
            m["delta_sum"] += tn - bn
        modes = {}
        for mode, m in per_mode.items():
            avg = round(m["delta_sum"] / m["graded"], 0) if m["graded"] else None
            verdict = None
            if m["diverged"] >= MIN_DIVERGED and avg is not None:
                verdict = (f"{'BEATS' if avg > 0 else 'LOSES TO'} baseline by "
                           f"₹{abs(avg):.0f}/diverged pair")
            modes[mode] = {**m, "avg_delta": avg, "verdict": verdict}
        out["policies"][tag] = {"desc": desc, "modes": modes}
    return out
