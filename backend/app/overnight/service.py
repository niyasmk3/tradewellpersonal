"""Orchestration for the Overnight tab: reuse closing's machinery, partition
by last-hour agreement, store separately.

The whole module is arithmetic on top of app/closing — no new pricing, no new
calendar, no new sync. What is new is the split: every night lands in exactly
one of TRADED (body and last hour agree), SKIPPED (they disagree), or
UNCONFIRMABLE (flat body, flat last hour, or no 14:00 bar). The skipped
nights' summary ships in the results because a filter is only believable next
to what it removed.

Honesty note carried from Round 3: the confirmation rule was found by
inspecting a specific losing night (2026-08-14), so its holdout numbers are
partially self-selected. It is pre-registered in app/closing/signals.py as
`lasthr_confirm`; live nights from here on are the real test.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from app.closing import store as closing_store
from app.overnight import filters
from app.closing import validate
from app.closing.pricing import build_model
from app.closing.service import (
    ClosingError,
    _config as closing_config,
    run_sync as closing_run_sync,
)
from app.closing.study import SIGNAL_DAY_OPEN, StudyConfig, run_study, summarise
from app.patterns import store as patterns_store

log = logging.getLogger("tradewell.overnight")

RESULTS_PATH = Path(__file__).resolve().parents[2] / ".overnight_results.json"

MIN_BARS = 5000
PRIMARY_YEARS = 1
CHECK_YEARS = 3


class OvernightError(RuntimeError):
    pass


def run_sync(kite, years: int = 3) -> dict:
    """Same data as Closing Day (patterns spine + India VIX)."""
    try:
        return closing_run_sync(kite, years=years)
    except ClosingError as exc:
        raise OvernightError(str(exc))


def classify(trade: dict) -> str:
    """TRADED / SKIPPED / UNCONFIRMABLE for one closing-study trade row.

    Uses the prints the row already carries: the signal (15:00), today's open,
    and the 14:00 print. Zero body or zero last-hour is UNCONFIRMABLE — a flat
    reading confirms nothing, and resolving it to a side would quietly turn
    the filter into a coin flip on exactly the quietest tapes.
    """
    p1500 = trade.get("signal_price")
    d_open = trade.get("day_open")
    p1400 = trade.get("p1400")
    if p1500 is None or d_open is None or p1400 is None:
        return "UNCONFIRMABLE"
    body = p1500 - d_open
    lasthr = p1500 - p1400
    if body == 0 or lasthr == 0:
        return "UNCONFIRMABLE"
    return "TRADED" if (body > 0) == (lasthr > 0) else "SKIPPED"


def _window(spine, vix, model, cfg: StudyConfig, start: date) -> dict:
    """One window: run the closing study unfiltered, partition, summarise each
    part with the same summariser so no number is computed two ways."""
    run = run_study(spine, vix, model, cfg, start=start)
    # Stamp the pre-registered filter flags on every trade BEFORE partitioning,
    # so both ledgers (traded and stood-aside) carry them.
    filters.annotate(run["trades"], spine, vix)
    parts = {"TRADED": [], "SKIPPED": [], "UNCONFIRMABLE": []}
    for t in run["trades"]:
        t["confirm"] = classify(t)
        parts[t["confirm"]].append(t)

    out = {
        "nights_considered": run["n"],
        "study_skipped": run.get("skipped", {}),
        "traded": summarise(parts["TRADED"], cfg),
        "skipped": summarise(parts["SKIPPED"], cfg),
        "unconfirmable": {"n": len(parts["UNCONFIRMABLE"])},
        "unfiltered": {  # the Closing Day baseline, for the comparison table
            k: run[k] for k in ("underlying", "option") if k in run
        },
    }
    out["trades"] = parts["TRADED"]
    out["skipped_trades"] = parts["SKIPPED"]
    out["filter_ladder"] = filters.ladder(parts["TRADED"])
    return out


def run_analysis(lots: int = 1) -> dict:
    spine = patterns_store.load_frame()
    if len(spine) < MIN_BARS:
        raise OvernightError(
            f"Only {len(spine)} index bars stored — run a sync first "
            f"(need >= {MIN_BARS})")
    vix = closing_store.load_vix()
    if vix.empty:
        raise OvernightError("No India VIX bars stored — run a sync first")

    spot = validate.SpotLookup(spine)
    model, fit = build_model(spot.at)
    # The SAME config builder the Closing tab uses — copied wiring here once
    # let the two tabs price identical trades with different spreads the
    # moment one side changed (review catch). Overnight is day_open by
    # definition, whatever CLOSING_SIGNAL_MODE says.
    cfg = closing_config(lots, fit, SIGNAL_DAY_OPEN)

    today = date.today()
    primary = _window(spine, vix, model, cfg,
                      start=today - timedelta(days=365 * PRIMARY_YEARS))
    check = _window(spine, vix, model, cfg,
                    start=today - timedelta(days=365 * CHECK_YEARS))
    check.pop("trades", None)
    check.pop("skipped_trades", None)

    results = {
        "rule": {
            "signal": ("At 15:00 IST compare NIFTY to TODAY'S OPEN — is the "
                       "session green or red so far."),
            "confirm": ("Trade ONLY if the last hour agrees: 15:00 on the same "
                        "side of the 14:00 print as it is of the open. A "
                        "disagreement night is a stand-aside — following the "
                        "last hour instead scored NEGATIVE skill in every "
                        "window tested."),
            "trade": "Confirmed red -> buy the ATM PE. Confirmed green -> buy the ATM CE.",
            "entry": "Filled at the close of the 15:00 bar (15:05).",
            "exit": "Sold at the 09:50 print on the next trading day.",
            "expiry": ("Nearest weekly expiry still alive the next morning; "
                       "on expiry day the trade rolls to the next week."),
            "size": f"{cfg.lots} lot ({cfg.qty} qty), identical every traded night.",
        },
        "disclaimer": (
            "Educational analysis of a historical rule, not financial advice "
            "and not a prediction. Index numbers are measured from real "
            "5-minute prints; option numbers are MODELLED (Kite deletes "
            "expired contracts). The confirmation rule was found by inspecting "
            "a specific losing night (2026-08-14), so its historical edge is "
            "partially self-selected — nights from 19-Aug-2026 on are the "
            "real test."
        ),
        "data": {
            "index_bars": int(len(spine)),
            "vix_bars": int(len(vix)),
            "last_sync": closing_store.get_meta("last_vix_sync"),
        },
        "model": model.describe(),
        "validation_brief": _validation_brief(spot, model),
        "primary": primary,
        "three_year": check,
        "filters": {
            "registered_on": filters.REGISTERED_ON.isoformat(),
            "definitions": filters.DEFINITIONS,
            "note": (
                "Flags, not gates: the headline strategy above ignores them. "
                "The Round-4 backtest holdout was mined by 46 candidate "
                "filters, so every ladder number is a pre-registered "
                "HYPOTHESIS; the live scoreboard below — nights after the "
                "registration date only — is the test that counts, graded at "
                f"{filters.MIN_LIVE_SAMPLE}+ filtered nights."),
            "live": filters.live_scoreboard(primary["trades"]),
        },
    }
    results["generated_at"] = datetime.now(timezone.utc).isoformat()
    # tmp + rename, same as closing store.save_results: the morning auto-grade
    # rewrites this while the tab's poll reads it (review catch).
    tmp = RESULTS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(results, indent=1, default=str))
    tmp.replace(RESULTS_PATH)
    log.info("Overnight analysis saved: %s traded / %s skipped nights (1y)",
             primary["traded"].get("n"), primary["skipped"].get("n"))
    return results


def _validation_brief(spot, model) -> dict:
    """The option model's error bars, compacted. The full teardown lives on
    the Closing Day tab; this tab restates the numbers a reader needs to
    weigh the P&L, not the whole methodology.

    Only the two checks whose numbers ship are run — level_check's full
    double-scoring pass was being computed and discarded here (review catch).
    The note derives its wording from the measured sign rather than asserting
    one: a bias that drifts positive must not sit beside prose insisting it
    is negative (review catch)."""
    on = validate.overnight_check(spot, model)
    oos = validate.out_of_sample_check(spot)
    bias = on.get("median_bias_pp")
    direction = ("the model UNDERSTATES real overnight moves" if (bias or 0) < 0
                 else "the model OVERSTATES real overnight moves" if bias
                 else "bias unmeasured")
    return {
        "oos_level_err_pct": oos.get("pooled_median_abs_err_pct"),
        "overnight_err_pp": on.get("median_abs_err_pp"),
        "overnight_bias_pp": bias,
        "overnight_n_pairs": on.get("n_pairs"),
        "note": (f"Modelled premiums; {direction}. "
                 "Full teardown on the Closing Day tab."),
    }


def load_results() -> Optional[dict]:
    if not RESULTS_PATH.exists():
        return None
    try:
        return json.loads(RESULTS_PATH.read_text())
    except Exception:
        return None


def status() -> dict:
    results = load_results()
    return {
        # COUNT(*), not a full-frame load — this sits on a 20s UI poll
        # (the same lesson patterns_store.load_tail records).
        "index_bars": patterns_store.candle_count(),
        "vix_bars": closing_store.vix_count(),
        "last_sync": closing_store.get_meta("last_vix_sync"),
        "results_available": results is not None,
        "results_generated_at": results.get("generated_at") if results else None,
    }
