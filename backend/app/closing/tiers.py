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


def summary(trades: list, split: date) -> dict:
    from app.closing.attribution import _compact

    def cells(sub):
        out = {k.lower(): _compact([t for t in sub if t.get("tier") == k]) for k in TIERS}
        out["clean_untiered"] = _compact([t for t in sub if t.get("card_verdict") == "CLEAN"
                                          and not t.get("tier")])
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
        "labels": LABELS,
        "notes": NOTES,
        "windows": windows,
        "note": ("Gold / Silver / Bronze grade a CLEAN card by the two shadow "
                 "signals that replicated across windows (OI wall, CPR). The "
                 "tier changes the expectation, never the verdict. Bronze is the "
                 "cell to treat like FLAGGED. Graded by live nights."),
    }
