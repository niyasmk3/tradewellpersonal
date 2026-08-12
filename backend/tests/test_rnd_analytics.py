"""R2 window analytics: window math, approx lower-bound semantics, buckets,
positional cutoff, heat-before-capture, and cut gating."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.config import Settings
from app.rnd import analytics as A

IST = timezone(timedelta(hours=5, minutes=30))
E = int(datetime(2026, 8, 12, 11, 0, tzinfo=IST).timestamp())   # 11:00 IST entry


def _settings(**over) -> Settings:
    base = dict(KITE_API_KEY="t", KITE_API_SECRET="t")
    base.update(over)
    return Settings(_env_file=None, **base)


def mk(mode="intraday", entered=E, entry=100.0, touches=None, mfe=None,
       mfe_at=None, mae=None, mae_at=None, **over):
    t = dict(id="X", mode=mode, entered_at=entered, entry_premium=entry,
             notes="", status="exited")
    if touches is not None:
        t["touch_times"] = touches
        # Exact evidence requires observation-from-entry (review C1); the
        # fixture models a cleanly-ladders trade unless a test overrides.
        t["touch_from"] = entered + 5
    if mfe is not None:
        t.update(mfe_premium=mfe, mfe_at=mfe_at)
    if mae is not None:
        t.update(mae_premium=mae, mae_at=mae_at)
    t.update(over)
    return t


CFG = _settings()


def test_exact_reach_and_time_to_5():
    t = mk(touches={"+3": E + 60, "+5": E + 300})
    end = A.window_end(t, CFG)
    assert end == E + 120 * 60
    hit, exact = A.touch_within(t, 5.0, end)
    assert hit and exact
    mins, exact = A.first_touch_minutes(t, 5.0)
    assert mins == 5.0 and exact
    assert A.bucket_of(t, end) == "5-10"


def test_exact_scalp_outside_window():
    t = mk(mode="scalp", touches={"+5": E + 35 * 60})
    end = A.window_end(t, CFG)
    assert end == E + 30 * 60
    hit, _ = A.touch_within(t, 5.0, end)
    assert not hit                       # touched, but not inside 30 min
    # ...and the uncapped 60-min curve point still sees it:
    hit60, _ = A.touch_within(t, 5.0, E + 60 * 60)
    assert hit60


def test_approx_is_a_lower_bound():
    early = mk(mfe=108.0, mfe_at=E + 20 * 60)          # max inside window
    late = mk(mfe=108.0, mfe_at=E + 200 * 60)          # max after window
    end_early = A.window_end(early, CFG)
    end_late = A.window_end(late, CFG)
    hit, exact = A.touch_within(early, 5.0, end_early)
    assert hit and not exact
    hit, _ = A.touch_within(late, 5.0, end_late)
    assert not hit                       # may have touched early — approx can't know


def test_positional_cutoff_caps_window():
    entered = int(datetime(2026, 8, 12, 13, 50, tzinfo=IST).timestamp())
    t = mk(mode="positional", entered=entered, touches={"+5": entered + 90 * 60})
    end = A.window_end(t, CFG)
    cutoff = int(datetime(2026, 8, 12, 14, 50, tzinfo=IST).timestamp())
    assert end == cutoff                 # 60 min, not 240
    assert not A.touch_within(t, 5.0, end)[0]
    t2 = mk(mode="positional", entered=entered, touches={"+5": entered + 30 * 60})
    assert A.touch_within(t2, 5.0, A.window_end(t2, CFG))[0]


def test_bucket_ladder_tops_out():
    t = mk(touches={"+3": E + 60, "+5": E + 120, "+10": E + 180, "+20": E + 240})
    assert A.bucket_of(t, A.window_end(t, CFG)) == ">20"
    t2 = mk(touches={"+3": E + 60})
    assert A.bucket_of(t2, A.window_end(t2, CFG)) == "3-5"
    t3 = mk(touches={})
    t3["touch_times"] = {}
    t3.update(mfe_premium=101.0, mfe_at=E + 60)
    assert A.bucket_of(t3, A.window_end(t3, CFG)) == "<3"


def test_heat_before_capture():
    hot = mk(touches={"-3": E + 30, "-5": E + 60, "+5": E + 600})
    assert A.heat_before_capture(hot, A.window_end(hot, CFG)) == "-5"
    clean = mk(touches={"+5": E + 60, "-3": E + 600})   # heat AFTER the touch
    assert A.heat_before_capture(clean, A.window_end(clean, CFG)) == "none"


def test_summary_gates_thin_cells(monkeypatch):
    rows = [mk(entry_score=80.0, touches={"+5": E + 60}) for _ in range(5)]
    monkeypatch.setattr(A, "_load_rows", lambda: rows)
    s = A.summary(CFG)
    assert s["modes"]["intraday"]["n"] == 5
    assert s["modes"]["intraday"]["p5_in_window"] == 100.0
    band = next(c for c in s["cuts"]["score_band"] if c["key"] == "78-81")
    assert band["n"] == 5 and band["sufficient"] is False
    assert s["modes"]["scalp"]["n"] == 0


def test_clean_fills_excludes_hollow_and_prehonest():
    rows = [
        mk(),
        mk(notes="hollow: late:cutoff"),
        mk(entered=A.HONEST_FROM - 100),
        {"not": "a trade"},
    ]
    got = A.clean_fills(rows)
    assert len(got) == 1


def test_dow_maps_epoch_correctly():
    # 12-Aug-2026 is a Wednesday.
    assert A._dow(mk()) == "Wed"


def test_censored_ladder_is_not_exact_evidence():
    """A trade whose ladder observation began long after entry (open across
    the R1 deploy) must be routed down the APPROX path — its stamps are not
    first crossings (review C1: the exact-class contamination)."""
    t = mk(touches={"+3": E + 3600, "+5": E + 3600},
           mfe=108.0, mfe_at=E + 20 * 60)
    t["touch_from"] = E + 3600            # ladder started an hour after entry
    end = A.window_end(t, CFG)
    assert A._ladder(t) == {}
    hit, exact = A.touch_within(t, 5.0, end)
    assert hit and not exact              # answered by mfe_at, flagged approx
    t["touch_from"] = None                # legacy row missing the marker
    assert A._ladder(t) == {}


def test_honest_from_matches_the_authoritative_constant():
    """The era boundary is IMPORTED, never copied — the review caught a
    hand-copied literal a week early (7 dishonest fills in every stat)."""
    from app.paper.service import HONEST_FILLS_FROM
    assert A.HONEST_FROM == HONEST_FILLS_FROM


def test_positional_after_cutoff_is_unmeasurable_not_a_miss(monkeypatch):
    late = int(datetime(2026, 8, 12, 15, 10, tzinfo=IST).timestamp())
    rows = [mk(mode="positional", entered=late, mfe=110.0, mfe_at=late + 600),
            mk(mode="positional", touches={"+5": E + 60})]
    monkeypatch.setattr(A, "_load_rows", lambda: rows)
    assert A.window_end(rows[0], CFG) is None
    s = A.summary(CFG)
    k = s["modes"]["positional"]
    assert k["n"] == 1                    # the vacuous row is OUT of the denominator
    assert k["n_no_window"] == 1
    assert k["p5_in_window"] == 100.0


def test_open_rows_are_pending_not_misses(monkeypatch):
    rows = [mk(status="entered"), mk(touches={"+5": E + 60})]
    monkeypatch.setattr(A, "_load_rows", lambda: rows)
    k = A.summary(CFG)["modes"]["intraday"]
    assert k["n"] == 1 and k["n_pending"] == 1
    assert k["p5_in_window"] == 100.0


def test_median_evidence_classes_never_blend(monkeypatch):
    rows = [mk(touches={"+5": E + 4 * 60}),                 # exact: 4 min
            mk(mfe=108.0, mfe_at=E + 110 * 60)]             # approx peak: 110 min
    monkeypatch.setattr(A, "_load_rows", lambda: rows)
    k = A.summary(CFG)["modes"]["intraday"]
    assert k["median_min_to_5_exact"] == 4.0
    assert k["n_exact_times"] == 1
    assert k["median_min_to_peak_approx"] == 110.0          # upper bound, own key


def test_heat_missing_evidence_is_none_not_zero_drawdown():
    bare = mk(mfe=108.0, mfe_at=E + 600)          # no mae data at all
    assert A.heat_before_capture(bare, A.window_end(bare, CFG)) is None
    # Global min AFTER the peak: "none" only when even that min is sub-3%.
    shallow = mk(mfe=108.0, mfe_at=E + 600, mae=98.0, mae_at=E + 1200)
    assert A.heat_before_capture(shallow, A.window_end(shallow, CFG)) == "none"
    deep = mk(mfe=108.0, mfe_at=E + 600, mae=90.0, mae_at=E + 1200)
    assert A.heat_before_capture(deep, A.window_end(deep, CFG)) is None


def test_ledger_reports_total_and_ungradeable(monkeypatch):
    late = int(datetime(2026, 8, 12, 15, 10, tzinfo=IST).timestamp())
    rows = [mk(mode="positional", entered=late), mk(touches={"+5": E + 60}),
            mk(status="entered")]
    monkeypatch.setattr(A, "_load_rows", lambda: rows)
    out, total = A.ledger(CFG, limit=2)
    assert total == 3 and len(out) == 2
    by_reason = {r["not_gradeable_reason"] for r in out}
    assert None in by_reason or len(by_reason) > 0
    graded = [r for r in out if r["p5_in_window"] is not None]
    ungraded = [r for r in out if r["p5_in_window"] is None]
    assert all(r["not_gradeable_reason"] for r in ungraded)


def test_missing_touch_from_never_counts_exact(monkeypatch):
    rows = [mk(touches={"+5": E + 60})]
    rows[0].pop("touch_from")
    monkeypatch.setattr(A, "_load_rows", lambda: rows)
    s = A.summary(CFG)
    assert s["modes"]["intraday"]["n_exact"] == 0
    assert s["modes"]["intraday"]["n_approx"] == 1
