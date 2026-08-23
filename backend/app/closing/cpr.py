"""Central Pivot Range — a SHADOW signal on the Closing Day card.

PRE-REGISTERED 2026-08-22. Ochoa's CPR from the PREVIOUS session's H/L/C:

    P  = (H + L + C) / 3        BC = (H + L) / 2        TC = 2P - BC
    width_pct = |TC - BC| / C * 100
    NARROW  <=>  width_pct <= 0.083     (frozen: the in-sample Q33 on 22-Aug)

Algebra worth knowing: TC - BC = (2/3) * |C - (H+L)/2|. The width is the
distance of yesterday's close from its range MIDPOINT, not the size of the
range — a 1,000-pt day that closes dead centre has a zero-width CPR. So
"narrow CPR" reads as "yesterday closed undecided, mid-range", and the
textbook claim is that such a session tends to be followed by a trend day.

The 22-Aug scratch study (198 CLEAN nights, four pre-stated cuts) found ONE
cut that held rank across both windows: inside CLEAN, a narrow today's-CPR
beat the rest at every threshold Q20-Q50 (in-sample +32%/48% win vs +4%/44%;
holdout +77%/72% win vs +11%/48%; bootstrap P 0.92 / 0.97), with a
mechanism — the index moved further in the trade's direction by 09:50 and
VIX sat lower. It did NOT rescue FLAGGED nights (-9%/-6%). Honest caveats:
in-sample narrow is outlier-carried (median -5%), the holdout cell is n=18,
and it was the one survivor of four cuts chosen after seeing the holdout —
so this is a candidate on the same footing the divergence flag had, never a
gate. It rides the card as an informational chip, stamps the backfill
ledger, and is graded by live nights. The `C` here is close_ref (the CAS
freeze stepped over), the same close the study reads everywhere else.

Editing the definition or the threshold resets its live count.
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from app.closing.study import Day, close_ref

REGISTERED_ON = "2026-08-22"
NARROW_PCT = 0.083
DEFINITION = ("P=(H+L+C)/3, BC=(H+L)/2, TC=2P-BC from the previous session's "
              "full H/L and close_ref; width_pct=|TC-BC|/C*100; narrow when "
              f"width_pct <= {NARROW_PCT}. Shadow only — never a gate.")


def levels(h: float, l: float, c: float) -> dict:
    p = (h + l + c) / 3.0
    bc = (h + l) / 2.0
    tc = 2.0 * p - bc
    lo, hi = min(bc, tc), max(bc, tc)
    width_pct = (hi - lo) / c * 100.0 if c else None
    return {"bc": round(lo, 2), "p": round(p, 2), "tc": round(hi, 2),
            "width_pts": round(hi - lo, 2),
            "width_pct": round(width_pct, 4) if width_pct is not None else None,
            "narrow": (width_pct <= NARROW_PCT) if width_pct is not None else None}


def from_session(day: Optional[Day]) -> Optional[dict]:
    """The CPR a session hands to the NEXT one, or None when the session is
    missing or its close is unreadable (CAS freeze with no prior bar)."""
    if day is None or not day.bars:
        return None
    c = close_ref(day)
    if c is None:
        return None
    h = max(b[1] for b in day.bars.values())
    l = min(b[2] for b in day.bars.values())
    return levels(h, l, c)


def annotate(trades: list, days: dict) -> None:
    """Stamp cpr_width_pct / f_cpr_narrow on ledger rows from each night's
    previous session. Unknown stays None — never a pass or a fail."""
    sessions = sorted(days)
    pos = {d: i for i, d in enumerate(sessions)}
    for t in trades:
        d = date.fromisoformat(t["date"])
        i = pos.get(d)
        cp = from_session(days[sessions[i - 1]]) if i else None
        t["cpr_width_pct"] = cp["width_pct"] if cp else None
        t["f_cpr_narrow"] = cp["narrow"] if cp else None


def summary(trades: list, split: date) -> dict:
    """CLEAN x narrow/rest (and FLAGGED x narrow as the no-rescue control),
    per window — the shadow signal against what the ledger paid."""
    from app.closing.attribution import _compact

    def cells(sub):
        clean = [t for t in sub if t.get("card_verdict") == "CLEAN"]
        flagged = [t for t in sub if t.get("card_verdict") == "FLAGGED"]
        return {
            "clean_narrow": _compact([t for t in clean if t.get("f_cpr_narrow") is True]),
            "clean_rest": _compact([t for t in clean if t.get("f_cpr_narrow") is False]),
            "flagged_narrow": _compact([t for t in flagged if t.get("f_cpr_narrow") is True]),
        }

    windows = {}
    for key, sub in (
            ("in_sample_2y", [t for t in trades if date.fromisoformat(t["date"]) < split]),
            ("holdout_1y", [t for t in trades if date.fromisoformat(t["date"]) >= split]),
            ("pooled_3y", trades)):
        windows[key] = cells(sub)
    return {
        "registered_on": REGISTERED_ON,
        "definition": DEFINITION,
        "narrow_pct": NARROW_PCT,
        "windows": windows,
        "note": ("Shadow signal, registered 22-Aug: the previous session's CPR "
                 "width, stamped on every ledger night. It never touches the "
                 "verdict. The threshold is frozen at the in-sample Q33 of the "
                 "day it was registered and is not refit as the windows roll. "
                 "FLAGGED x narrow is the control: the signal is an intensifier "
                 "inside CLEAN, not a rescue. Graded by live nights."),
    }
