"""Clean Gold / Silver / Bronze — a label over the two replicated shadow
signals. The tests pin the cell semantics, the unknown-never-promotes rule,
and that the tier rides the card and the ledger without touching verdicts."""
from __future__ import annotations

import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.closing import tiers, tonight


def test_tier_cells():
    assert tiers.tier("CLEAN", "ROAD", True) == "GOLD"
    assert tiers.tier("CLEAN", "BEHIND", True) == "GOLD"        # behind = not capped
    assert tiers.tier("CLEAN", "ROAD", False) == "SILVER"
    assert tiers.tier("CLEAN", "BEHIND", False) == "SILVER"
    assert tiers.tier("CLEAN", "CAPPED", True) == "BRONZE"      # capped trumps CPR
    assert tiers.tier("CLEAN", "CAPPED", False) == "BRONZE"
    assert tiers.tier("CLEAN", "CAPPED", None) == "BRONZE"      # CPR irrelevant once capped
    assert tiers.REGISTERED_ON == "2026-08-23" and set(tiers.NOTES) == set(tiers.TIERS)


def test_unknown_never_promotes_or_demotes():
    assert tiers.tier("CLEAN", None, True) is None              # OI unknown -> plain CLEAN
    assert tiers.tier("CLEAN", "ROAD", None) is None            # CPR unknown, not capped -> plain CLEAN
    assert tiers.tier("FLAGGED", "ROAD", True) is None          # the verdict speaks
    assert tiers.tier("INCOMPLETE", "ROAD", True) is None
    assert tiers.tier(None, "ROAD", True) is None


def test_annotate_and_summary():
    tr = [{"date": "2025-06-01", "card_verdict": "CLEAN", "f_oi_state": "ROAD", "f_cpr_narrow": True, "net_pct": 30.0, "net_rs": 300},
          {"date": "2025-06-02", "card_verdict": "CLEAN", "f_oi_state": "BEHIND", "f_cpr_narrow": False, "net_pct": 5.0, "net_rs": 50},
          {"date": "2025-06-03", "card_verdict": "CLEAN", "f_oi_state": "CAPPED", "f_cpr_narrow": True, "net_pct": -20.0, "net_rs": -200},
          {"date": "2025-06-04", "card_verdict": "CLEAN", "f_oi_state": None, "f_cpr_narrow": True, "net_pct": 1.0, "net_rs": 10},
          {"date": "2025-06-05", "card_verdict": "FLAGGED", "f_oi_state": "ROAD", "f_cpr_narrow": True, "net_pct": -10.0, "net_rs": -100}]
    tiers.annotate(tr)
    assert [t["tier"] for t in tr] == ["GOLD", "SILVER", "BRONZE", None, None]
    s = tiers.summary(tr, date(2026, 1, 1))
    w = s["windows"]["in_sample_2y"]
    assert w["gold"]["n"] == 1 and w["silver"]["n"] == 1 and w["bronze"]["n"] == 1 and w["clean_untiered"]["n"] == 1
    assert s["labels"]["GOLD"] == "Clean Gold"


def test_card_carries_tier_without_touching_verdict():
    from tests.test_closing_oiwall import CHAIN
    from tests.test_closing_tonight import EXPIRY, NEXT, NOW, TODAY, _tape, _vix
    chains = {EXPIRY.isoformat(): CHAIN}
    # Fixture: print 24400 CE, call wall 24500 -> CAPPED; PREV session's CPR is narrow.
    r = tonight.evaluate(_tape(), _vix(), TODAY, NOW, NEXT, EXPIRY, chains)
    assert r["verdict"] == "CLEAN" and r["values"]["oi_state"] == "CAPPED" and r["values"]["cpr_narrow"] is True
    assert r["tier"] == "BRONZE" and "Bronze" in r["tier_note"]
    # Same night, no chain -> OI unknown -> no tier, verdict unchanged.
    r2 = tonight.evaluate(_tape(), _vix(), TODAY, NOW, NEXT, EXPIRY)
    assert r2["verdict"] == "CLEAN" and r2["tier"] is None and r2["tier_note"] is None
    # Open road + narrow CPR -> GOLD.
    road = {EXPIRY.isoformat(): {(24900.0, "CE"): 9e5, (23900.0, "PE"): 8e5}}
    r3 = tonight.evaluate(_tape(), _vix(), TODAY, NOW, NEXT, EXPIRY, road)
    assert r3["values"]["oi_state"] == "ROAD" and r3["tier"] == "GOLD" and r3["verdict"] == "CLEAN"
