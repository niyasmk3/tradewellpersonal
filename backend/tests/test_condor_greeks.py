"""Greeks, IV round-trip, POP, and 4-leg charges for the condor module.

Charges are pinned to a hand-verified worked example (house pattern from
test_paper.py): 4 entry orders on 100/90/35/30 premiums at qty 75 =
80 brokerage + 6.81 exchange-family + 15.63 GST + 21.38 STT (sells only)
+ 0.15 stamp (buys only) = Rs 123.96.
"""
from __future__ import annotations

import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.condor.charges import entry_charges, exit_charges, roundtrip_estimate
from app.condor.greeks import bs_greeks, condor_pop, prob_above, solve_iv
from app.options.iv import bs_price


def test_atm_delta_and_put_call_relation():
    c = bs_greeks(25000, 25000, 0.02, 0.14, True)
    p = bs_greeks(25000, 25000, 0.02, 0.14, False)
    assert 0.50 <= c["delta"] <= 0.56          # slightly >0.5 with r>0
    assert abs((c["delta"] - p["delta"]) - 1.0) < 1e-9   # N(d1) - (N(d1)-1)
    assert c["gamma"] == p["gamma"]
    assert c["vega"] == p["vega"]
    assert c["theta"] < 0 and p["theta"] < 0   # long options decay


def test_otm_deltas_shrink_with_distance():
    d1 = abs(bs_greeks(25000, 25300, 4 / 365, 0.14, True)["delta"])
    d2 = abs(bs_greeks(25000, 25600, 4 / 365, 0.14, True)["delta"])
    assert d1 > d2 > 0


def test_degenerate_inputs_refuse():
    assert bs_greeks(25000, 25000, 0.0, 0.14, True) is None
    assert bs_greeks(25000, 25000, 0.02, 0.0, True) is None
    assert bs_greeks(0, 25000, 0.02, 0.14, True) is None


def test_iv_roundtrip_is_a_fraction():
    px = bs_price(25000, 25400, 4 / 365, 0.14, True)
    iv = solve_iv(px, 25000, 25400, 4 / 365, True)
    assert iv is not None
    assert abs(iv - 0.14) < 0.002              # fraction, NOT percentage


def test_pop_bounds_and_monotonicity():
    t = 4 / 365
    narrow = condor_pop(25000, 24900, 25100, t, 0.14, 0.14)
    wide = condor_pop(25000, 24400, 25600, t, 0.14, 0.14)
    assert 0.0 < narrow < wide < 1.0
    # Symmetric ~1.19-sigma breakevens land near the analytic 76.5%.
    ref = condor_pop(25000, 24564, 25436, t, 0.14, 0.14)
    assert abs(ref - 0.765) < 0.01
    p = prob_above(25000, 25000, t, 0.14)
    assert 0.45 < p < 0.55


def test_charges_worked_example():
    assert entry_charges(100.0, 90.0, 35.0, 30.0, 75) == 123.96
    assert exit_charges(50.0, 45.0, 17.5, 15.0, 75) == 102.29


def test_charges_sell_side_stt_asymmetry():
    # Same premiums as shorts (sells) must cost more than as wings (buys):
    # STT 0.15% on sell turnover dwarfs 0.003% stamp on buys.
    shorts_only = entry_charges(100.0, 90.0, 0.0, 0.0, 75)
    wings_only = entry_charges(0.0, 0.0, 100.0, 90.0, 75)
    assert shorts_only > wings_only


def test_lapsed_legs_cost_nothing():
    assert exit_charges(0.0, 0.0, 0.0, 0.0, 75) == 0.0
    assert entry_charges(0.0, 0.0, 0.0, 0.0, 75) == 0.0


def test_roundtrip_estimate_sane():
    est = roundtrip_estimate(40.0, 100.0, 75)
    # 8 orders exist: floor is 8 x 20 brokerage + taxes; ceiling sanity.
    assert 160 < est < 600
    assert roundtrip_estimate(0.0, 100.0, 75) == 0.0
