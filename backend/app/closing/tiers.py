"""Clean Gold / Silver / Bronze — the card's verdict graded by its two
replicated shadow signals. A LABEL over stamped evidence, never a new rule.

REGISTERED 2026-08-23. The five checks decide CLEAN; the OI wall and the CPR
then sort CLEAN nights into the three cells that held rank in BOTH backtest
windows (22-Aug studies, 198 CLEAN nights, 3y modelled ledger):

    GOLD    CLEAN + OI wall not capped (ROAD/BEHIND) + narrow CPR
            3y 61% win (in-sample 57%, holdout 71%), n=44, median +14%
    SILVER  CLEAN + OI wall not capped + CPR not narrow
            3y 51% (48% / 56%), n=88, median +2%
    BRONZE  CLEAN + OI wall CAPPED (a max-OI strike within 150 pts ahead),
            any CPR — 3y 37% (35% / 44%), n=65, median -9%: FLAGGED-level
            odds; the wall is where the move dies.

Unknown never promotes or demotes: a CLEAN night whose OI state is unknown,
or a not-capped night whose CPR is unknown, carries NO tier and stays a
plain CLEAN. FLAGGED and INCOMPLETE carry no tier — the verdict speaks.

HONESTY BOX. These cells were found by looking at the data and then checked
on the window they were not found in; the hit-rate ordering replicated, the
P&L magnitudes did not (the OI wall's +49% holdout mean fell to +9%
in-sample). Gold's 61% is the backtest's shape; the live cards grade it.
Nothing here touches the verdict, the checks, or the ledger of record.
Editing a cell definition resets its live count.
"""
from __future__ import annotations

from datetime import date
from typing import Optional

REGISTERED_ON = "2026-08-23"
TIERS = ("GOLD", "SILVER", "BRONZE")
LABELS = {"GOLD": "Clean Gold", "SILVER": "Clean Silver", "BRONZE": "Clean Bronze"}
DEFINITION = ("CLEAN verdict graded by two shadow signals: GOLD = OI wall not "
              "capped + narrow CPR; SILVER = not capped + wide CPR; BRONZE = OI "
              "wall capped (any CPR). Unknown inputs -> no tier. Never a gate.")
NOTES = {
    "GOLD": "Clean Gold — all five checks clear, no OI wall within 150 pts in the "
            "trade's path, previous session closed mid-range (narrow CPR). 3y "
            "backtest 61% win (57% / 71% by window), n=44. Shadow tier, graded live.",
    "SILVER": "Clean Silver — all five checks clear, open road ahead, but the "
              "previous session's CPR was wide. 3y 51% win (48% / 56%), n=88.",
    "BRONZE": "Clean Bronze — all five checks clear but a max-OI strike sits within "
              "150 pts ahead; the move tends to die into it. 3y 37% win (35% / 44%), "
              "n=65 — FLAGGED-level odds despite the clean card.",
}


# --- Crash-day Gold: a sub-label on GOLD, registered separately ---------------
# REGISTERED 2026-10-09, out of the 08-Oct post-mortem (a Gold PE issued at the
# dead low of a -396-pt day; index bounced +252, -64% real). Gold's 61%
# headline is two populations: calm Gold (day body inside +/-300 pts) won 68%
# with median +25% and held in BOTH windows (67%/71%, n=38); stretched Gold
# (body at/beyond 300 pts) won 25% with median -25%, the index continued only
# 38% of the time, all eight were crash-side PEs, and the cell's positive mean
# (+56%) hangs on ONE +633% continuation night. Same bet, different odds: the
# lottery-tail variety.
#
# HONESTY BOX. n=8 over three years, found by inspecting a losing night — this
# is an expectations label, pre-registered to be graded on live Gold nights,
# and at Gold's ~1.2 nights/month it will take YEARS to reach the 30-night
# house bar. It never gates: skipping stretched Golds in the backtest forfeits
# the +633% night and is value-destroying in expectation. Editing the
# threshold or the definition resets the live count.
STRETCH_PTS = 300.0
STRETCH_REGISTERED_ON = "2026-10-09"
STRETCH_NOTE = (
    "Crash-day Gold — a Gold card issued after a day body of 300+ pts. "
    "Backtest: 25% win, median -25%, index continues 38% (n=8, all crash-side "
    "PEs); the +56% mean hangs on one +633% night. Calm Gold: 68% win, median "
    "+25% (n=38, 67%/71% by window). Same bet, lottery-tail odds — an "
    "expectations label, never a gate.")

# Crash-day SILVER — REGISTERED 2026-10-09, same day, separate standing. The
# trigger is the same frozen body (|15:00 - open| >= 300) but the EVIDENCE is
# deliberately neutral: the backtest does NOT replicate Gold's degradation on
# Silver (stretched Silver n=9, 56% win, and the windows FLIP — 25% in-sample
# / 80% holdout — which is what noise looks like), and the second-most-
# stretched live Silver (28-Sep, body -268) was the biggest winner (+180%
# real). Registered at the user's instruction after 15-Sep (body -401, the
# one live crash-Silver, -7% real) so the live ledger can decide. A tracking
# label with NO expectation attached — never a warning, never a gate.
SILVER_STRETCH_REGISTERED_ON = "2026-10-09"
SILVER_STRETCH_NOTE = (
    "Crash-day Silver — a Silver card issued after a day body of 300+ pts. "
    "Unlike Gold, the backtest shows NO consistent degradation here "
    "(stretched Silver n=9, 56% win, windows flip 25%/80% — noise-shaped), "
    "and one near-miss live night (-268 body) was the biggest Silver winner. "
    "A tracking label with no expectation attached: the live ledger decides. "
    "Never a gate.")
STRETCH_NOTES = {"GOLD": STRETCH_NOTE, "SILVER": SILVER_STRETCH_NOTE}

# --- STRETCHED-BODY HALF SIZE (pre-registered 2026-10-14) --------------------
# The sizing counterpart of the crash-day label, keyed on the SAME frozen body
# (|15:00 - open| >= STRETCH_PTS) so label and size can never drift apart.
# Scope is wider than the label: ANY CLEAN card, tier or not. The 1y replay
# (72 clean nights): half size on the 7 stretched nights gave the full
# drawdown relief available from any cut (max DD -348% -> -316%, worst
# losing streak -291% -> -259%) at ZERO return cost (+2129% vs +2106%),
# whereas a 250-pt cut paid -3% for four extra nights that won 75%. It is a
# SIZE rule, never a skip: a right-tail strategy that skips nights misses the
# nights that pay. Graded forward on clean stretched nights after the
# registration date; the factor is a knob (1.0 disables), the body is not.
SIZE_REGISTERED_ON = "2026-10-14"
SIZE_NOTE = (
    "HALF SIZE — stretched day body (300+ pts). The overnight continuation "
    "bet's catastrophic nights cluster here; halving them removed ~a tenth of "
    "the year's drawdown at no return cost. Size rule, not a skip "
    "(registered 2026-10-14, graded by live stretched nights)."
)


def size_factor(verdict: Optional[str], gap_pts: Optional[float],
                factor: float) -> float:
    """Multiplier for the card's suggested size. `factor` (<1) applies only to a
    CLEAN card on a stretched body; everything else — FLAGGED, INCOMPLETE,
    unknown body, factor 1.0 — is full size. Unknown never cuts."""
    if verdict != "CLEAN" or gap_pts is None or factor >= 1.0:
        return 1.0
    return factor if abs(gap_pts) >= STRETCH_PTS else 1.0


def stretched(tier_label: Optional[str], gap_pts: Optional[float]) -> Optional[bool]:
    """True for a GOLD or SILVER card on a 300+ pt day body. None when the
    night carries neither tier or the body is unknown — Bronze already has
    FLAGGED-level odds and carries no sub-label, and unknown never labels.
    Gold's cell semantics are untouched by the Silver extension (its live
    count is preserved; Silver counts from its own registration date)."""
    if tier_label not in ("GOLD", "SILVER") or gap_pts is None:
        return None
    return abs(gap_pts) >= STRETCH_PTS


def tier(verdict: Optional[str], oi_state: Optional[str],
         cpr_narrow: Optional[bool]) -> Optional[str]:
    if verdict != "CLEAN":
        return None
    if oi_state == "CAPPED":
        return "BRONZE"
    if oi_state in ("ROAD", "BEHIND"):
        if cpr_narrow is True:
            return "GOLD"
        if cpr_narrow is False:
            return "SILVER"
    return None


def annotate(trades: list) -> None:
    """Stamp `tier` from fields the shadow modules have already stamped
    (card_verdict, f_oi_state, f_cpr_narrow). Run after both annotators."""
    for t in trades:
        t["tier"] = tier(t.get("card_verdict"), t.get("f_oi_state"), t.get("f_cpr_narrow"))
        # The registered quantity is the DAY BODY (15:00 print vs day open) —
        # never gap_pts, which equals the body only under the day_open signal
        # mode; in prev_close mode it is the gap vs yesterday's close and
        # would stamp a different population than was registered (review
        # catch). Missing open -> unknown -> never labels.
        body = (t["signal_price"] - t["day_open"]
                if t.get("signal_price") is not None and t.get("day_open") is not None
                else None)
        t["tier_stretched"] = stretched(t["tier"], body)


def summary(trades: list, split: date) -> dict:
    from app.closing.attribution import _compact

    def cells(sub):
        out = {k.lower(): _compact([t for t in sub if t.get("tier") == k]) for k in TIERS}
        out["clean_untiered"] = _compact([t for t in sub if t.get("card_verdict") == "CLEAN"
                                          and not t.get("tier")])
        # Crash-day sub-labels: the same Gold/Silver rows split by the body.
        out["gold_stretched"] = _compact([t for t in sub if t.get("tier") == "GOLD"
                                          and t.get("tier_stretched") is True])
        out["gold_calm"] = _compact([t for t in sub if t.get("tier") == "GOLD"
                                     and t.get("tier_stretched") is False])
        out["silver_stretched"] = _compact([t for t in sub if t.get("tier") == "SILVER"
                                            and t.get("tier_stretched") is True])
        out["silver_calm"] = _compact([t for t in sub if t.get("tier") == "SILVER"
                                       and t.get("tier_stretched") is False])
        return out

    windows = {}
    for key, sub in (
            ("in_sample_2y", [t for t in trades if date.fromisoformat(t["date"]) < split]),
            ("holdout_1y", [t for t in trades if date.fromisoformat(t["date"]) >= split]),
            ("pooled_3y", trades)):
        windows[key] = cells(sub)
    return {
        "registered_on": REGISTERED_ON,
        "definition": DEFINITION,
        "stretch": {"registered_on": STRETCH_REGISTERED_ON, "pts": STRETCH_PTS,
                    "note": STRETCH_NOTE,
                    "silver_registered_on": SILVER_STRETCH_REGISTERED_ON,
                    "silver_note": SILVER_STRETCH_NOTE},
        "labels": LABELS,
        "notes": NOTES,
        "windows": windows,
        "note": ("Gold / Silver / Bronze grade a CLEAN card by the two shadow "
                 "signals that replicated across windows (OI wall, CPR). The "
                 "tier changes the expectation, never the verdict. Bronze is the "
                 "cell to treat like FLAGGED. Graded by live nights."),
    }
