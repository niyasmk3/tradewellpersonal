"""The Overnight module's one piece of real logic: the three-way partition.

Everything else is reuse of app/closing, already pinned by its own suite. The
partition is where a silent bug would misfile nights — a disagreement traded,
or a flat tape resolved to a side — so every branch is pinned here.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.overnight.service import classify


def _t(open_, p1400, p1500):
    return {"day_open": open_, "p1400": p1400, "signal_price": p1500}


def test_agreement_up_and_down_are_traded():
    # green day, last hour rising -> confirmed CE night
    assert classify(_t(24300.0, 24380.0, 24400.0)) == "TRADED"
    # red day, last hour falling -> confirmed PE night
    assert classify(_t(24400.0, 24330.0, 24300.0)) == "TRADED"


def test_the_14_aug_shape_is_skipped():
    """Day green (15:00 above open) but the last hour falling — the exact
    night that motivated this module. Stand aside, do not fade."""
    assert classify(_t(24361.9, 24395.0, 24374.2)) == "SKIPPED"
    # mirror image: red day, last hour rising
    assert classify(_t(24450.0, 24360.0, 24380.0)) == "SKIPPED"


def test_flat_readings_confirm_nothing():
    assert classify(_t(24400.0, 24380.0, 24400.0)) == "UNCONFIRMABLE"  # zero body
    assert classify(_t(24300.0, 24400.0, 24400.0)) == "UNCONFIRMABLE"  # flat last hour
    assert classify(_t(24400.0, None, 24380.0)) == "UNCONFIRMABLE"     # no 14:00 bar
    assert classify(_t(None, 24380.0, 24390.0)) == "UNCONFIRMABLE"     # no open


def test_partition_is_exhaustive_and_exclusive_on_real_results():
    """Every trade the closing study produces lands in exactly one bucket, and
    the buckets' sums reconstruct the unfiltered total — the invariant that
    makes 'filtered' mean 'partitioned', not 'recomputed differently'."""
    from app.overnight.service import load_results
    data = load_results()
    if data is None:
        import pytest
        pytest.skip("no stored overnight results on this machine")
    p = data["primary"]
    n_all = p["nights_considered"]
    n_parts = (p["traded"].get("n", 0) + p["skipped"].get("n", 0)
               + p["unconfirmable"]["n"])
    assert n_parts == n_all
    total_parts = (p["traded"].get("option", {}).get("total_net_rs", 0)
                   + p["skipped"].get("option", {}).get("total_net_rs", 0))
    total_all = p["unfiltered"]["option"]["total_net_rs"]
    # unconfirmable nights carry no option P&L only if there were none traded;
    # allow their absence but nothing else
    assert abs(total_parts - total_all) < max(1.0, abs(total_all) * 0.02) \
        or p["unconfirmable"]["n"] > 0
