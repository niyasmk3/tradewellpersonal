"""Loss attribution and calendar flags for the Closing Day study.

WHY EVERY LOSER LOST. Each trade's net % is decomposed by repricing the exit
premium in three steps with the study's own model — spot moved (entry clock and
VIX held), clock advanced (entry VIX held), VIX moved — with spread + charges
as the exact remainder, so the four components always sum to the trade's net %.
The decomposition is sequential, so interaction terms fold into the later
steps; it is an attribution, not four independent measurements. The result on
three years of trades: every loss is dominated by one of exactly two things,
the index going the wrong way (~55% mean loss) or theta outrunning a correct
call (~24% mean loss). Vol crush and costs never dominate a single night.

TWO CALENDAR FLAGS, PRE-REGISTERED 2026-08-19 — flags, never gates:

  HOLIDAY BRIDGE — the exit is 2 trading-calendar days away, or more than a
    plain weekend (carry of exactly 2, or 4+). Mechanism: the position pays
    two-plus days of decay for one night of edge. Negative in both the 2y
    in-sample and 1y holdout windows (-19%/-18% mean), and found independently
    of this table by the weekend-theta audit. Plain Fri->Mon weekends are NOT
    flagged: they flipped sign between windows when tested (in-sample -₹44k,
    holdout +₹22k) — the extra decay is real but the bigger Monday gaps pay
    for it often enough that removal is not established.

  MONTH-END ENTRY — the entry day is the month's last trading session.
    Negative in both windows (-18%/-21% mean) with a flows prior (turn-of-month
    rebalancing), but only ~36 trades in 3y — below the house 30-per-window
    rule, which is exactly why it is registered as a hypothesis rather than
    banked as a result.

Why the bucket table around them is a DIAGNOSTIC and not a menu: buckets are
recomputed fresh every run, and this dataset has already shown two documented
cells ("quiet signal is the worst bucket", "VIX>20 is the losing corner")
flipping sign under a different reference rule. A bucket earns a flag only
with sign-consistency across windows AND a mechanism; it earns the headline
only by surviving the live scoreboard below, which grades nights strictly
after the registration date. Editing a flag definition resets the live count.
"""
from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import date
from typing import Optional

from app.closing.pricing import OptionModel
from app.closing.study import EXIT_BAR, SIGNAL_BAR, Day

REGISTERED_ON = date(2026, 8, 19)
MIN_LIVE_SAMPLE = 30   # house rule: no verdict below 30 flagged live nights
MIN_BUCKET_WINDOW = 10  # below this a bucket cell is not even worth a verdict

FLAG_DEFINITIONS = [
    {
        "key": "holiday_bridge",
        "label": "Holiday bridge",
        "rule": ("The next trading day is 2 calendar days away, or more than a "
                 "plain Fri->Mon weekend (carry of exactly 2 days, or 4+)."),
        "status": "hypothesis",
        "evidence": ("Mean -19.2% in-sample / -18.0% holdout over 45 trades; "
                     "mechanism is structural (2+ days of decay for one night "
                     "of edge). Plain weekends are NOT flagged — they flipped "
                     "sign between windows (-₹44k in-sample, +₹22k holdout)."),
    },
    {
        "key": "month_end",
        "label": "Month-end entry",
        "rule": "The entry day is the last trading session of its calendar month.",
        "status": "hypothesis",
        "evidence": ("Mean -18.2% in-sample / -20.8% holdout, but only 36 "
                     "trades in 3y — below the 30-per-window house rule. "
                     "Flows prior (turn-of-month rebalancing). The weakest of "
                     "the two flags; judged live only."),
    },
]

REASON_LABELS = {
    "wrong_direction": "wrong direction",
    "theta_decay": "theta decay",
    "vol_crush": "vol crush",
    "costs": "spread + charges",
}


# --- decomposition ----------------------------------------------------------

def decompose(trades: list, days: "dict[date, Day]", model: OptionModel) -> None:
    """Stamp each trade with direction/theta/vega/costs (% of entry fill) and
    the dominant loss reason. Components sum to net_pct by construction:
    costs is the exact remainder, so rounding is the only slack."""
    for t in trades:
        d, xd = date.fromisoformat(t["date"]), date.fromisoformat(t["exit_date"])
        sig, xb = days[d].bar(SIGNAL_BAR), days[xd].bar(EXIT_BAR)
        expiry, is_call = date.fromisoformat(t["expiry"]), t["direction"] == "CE"
        p0, f = t["mid_in"], t["fill_in"]
        ps = pt = None
        if sig and xb:
            ps = model.premium(t["exit_spot"], t["strike"], sig[4], expiry,
                               is_call, t["vix_in"])
            pt = model.premium(t["exit_spot"], t["strike"], xb[4], expiry,
                               is_call, t["vix_in"])
        if ps is None or pt is None or not f:
            t["direction_pct"] = t["theta_pct"] = t["vega_pct"] = t["costs_pct"] = None
            t["loss_reason"] = None
            continue
        t["direction_pct"] = round((ps - p0) / f * 100, 1)
        t["theta_pct"] = round((pt - ps) / f * 100, 1)
        t["vega_pct"] = round((t["mid_out"] - pt) / f * 100, 1)
        t["costs_pct"] = round(t["net_pct"] - (t["mid_out"] - p0) / f * 100, 1)
        if t["net_pct"] < 0:
            comps = {"wrong_direction": t["direction_pct"],
                     "theta_decay": t["theta_pct"],
                     "vol_crush": t["vega_pct"],
                     "costs": t["costs_pct"]}
            t["loss_reason"] = min(comps, key=lambda k: comps[k])
        else:
            t["loss_reason"] = None


def reasons_summary(trades: list) -> dict:
    """Losers grouped by dominant reason, plus how often the index actually
    went the signal's way while the option still lost."""
    losers = [t for t in trades if t["net_pct"] < 0 and t.get("loss_reason")]
    rows = []
    by = defaultdict(list)
    for t in losers:
        by[t["loss_reason"]].append(t)
    for reason, tt in sorted(by.items(), key=lambda kv: -len(kv[1])):
        pct = [t["net_pct"] for t in tt]
        rows.append({
            "reason": reason,
            "label": REASON_LABELS.get(reason, reason),
            "n": len(tt),
            "share_of_losers_pct": round(len(tt) / len(losers) * 100, 1),
            "mean_pct": round(statistics.mean(pct), 1),
            "total_rs": round(sum(t["net_rs"] for t in tt), 0),
            "index_right_way_n": sum(1 for t in tt if t["signed_move_pts"] > 0),
        })
    return {"n_trades": len(trades), "n_losers": len(losers), "rows": rows,
            "note": ("Dominant component of each losing night's net %. "
                     "`index right way` counts losers where the index moved "
                     "the way the signal pointed and decay/spread still ate "
                     "the premium — the toll-booth failure mode, as opposed "
                     "to the coin landing wrong.")}


# --- calendar flags ---------------------------------------------------------

def annotate(trades: list, sessions: "list[date]") -> None:
    """Stamp carry days, the two registered flags, and the diagnostic tags.

    `sessions` is the full trading calendar the study ran on (sorted). Flags
    are True / False / None — None means the calendar context was unavailable,
    and an unknown night is never silently passed or failed.
    """
    nxt = {d: sessions[i + 1] for i, d in enumerate(sessions[:-1])}
    prv = {d: sessions[i - 1] for i, d in enumerate(sessions) if i}
    for t in trades:
        d = date.fromisoformat(t["date"])
        carry = (date.fromisoformat(t["exit_date"]) - d).days
        t["carry_days"] = carry
        t["f_holiday_bridge"] = carry == 2 or carry > 3
        after = nxt.get(d)
        t["f_month_end"] = (after.month != d.month) if after else None
        # A bridge night is never clear; otherwise an unknown month-end makes
        # the night UNKNOWN, not clear — unknowns never pass silently.
        if t["f_holiday_bridge"]:
            t["f_calendar_clear"] = False
        elif t["f_month_end"] is None:
            t["f_calendar_clear"] = None
        else:
            t["f_calendar_clear"] = not t["f_month_end"]

        tags = []
        if carry == 3:
            tags.append("weekend carry")
        if t["f_holiday_bridge"]:
            tags.append("holiday bridge")
        if t["f_month_end"]:
            tags.append("month-end entry")
        before = prv.get(d)
        if before and before.month != d.month:
            tags.append("month-start entry")
        if t.get("dte_bucket") == "0-1":
            tags.append("expiry-morning exit")
        if t.get("gap_bucket") == "<25":
            tags.append("quiet signal (<25pt)")
        if t.get("vix_in") is not None and t["vix_in"] > 20:
            tags.append("VIX>20 entry")
        tags.append(f"{d.strftime('%a')} entry")
        t["tags"] = tags


# --- buckets (diagnostic) ---------------------------------------------------

def _verdict(mean_in: Optional[float], mean_out: Optional[float]) -> str:
    if mean_in is None or mean_out is None:
        return "too few trades"
    if mean_in < 0 and mean_out < 0:
        return "consistent-bad"
    if mean_in > 0 and mean_out > 0:
        return "consistent-positive"
    return "flips — noise"


def bucket_table(trades: list, split: date) -> dict:
    """Every tag scored on the in-sample/holdout split, with a verdict.

    Recomputed fresh on every run ON PURPOSE: a bucket that was consistent
    last month and flips this month should be seen flipping, not remembered
    fondly. Verdicts are diagnostics — only the frozen flags above carry any
    standing, and only via the live scoreboard."""
    all_tags = sorted({tag for t in trades for tag in t.get("tags", [])})
    rows = []
    for tag in all_tags:
        tt = [t for t in trades if tag in t.get("tags", [])]
        cells = {}
        means = []
        for key, sub in (
                ("in_sample", [t for t in tt if date.fromisoformat(t["date"]) < split]),
                ("holdout", [t for t in tt if date.fromisoformat(t["date"]) >= split])):
            if len(sub) < MIN_BUCKET_WINDOW:
                cells[key] = {"n": len(sub)}
                means.append(None)
                continue
            m = statistics.mean(t["net_pct"] for t in sub)
            cells[key] = {"n": len(sub), "mean_pct": round(m, 1),
                          "total_rs": round(sum(t["net_rs"] for t in sub), 0)}
            means.append(m)
        rows.append({
            "tag": tag,
            "n": len(tt),
            "registered": tag in ("holiday bridge", "month-end entry"),
            "in_sample": cells["in_sample"],
            "holdout": cells["holdout"],
            "verdict": _verdict(means[0], means[1]),
            "below_house_n": min(cells["in_sample"]["n"], cells["holdout"]["n"]) < MIN_LIVE_SAMPLE,
        })
    rows.sort(key=lambda r: (r["verdict"] != "consistent-bad", -r["n"]))
    return {
        "rows": rows,
        "split": split.isoformat(),
        "note": (
            "Diagnostic, recomputed every run and UNCORRECTED for the number "
            "of buckets tested. This same table has already watched two "
            "documented cells (quiet signal, VIX>20) flip sign under a "
            "different reference rule — a verdict here is a candidate for "
            "pre-registration, never a licence to remove trades. Day-of-week "
            "buckets are listed precisely so their flips stay visible; "
            "day-of-week conditioning was tested and refuted in Round 3/4."),
    }


# --- the tonight card, backfilled -------------------------------------------

# Same five checks the live card reads, in the same order. The keys are
# deliberately duplicated from tonight.CHECK_LABELS rather than imported —
# a test pins the two sets equal, so drift breaks loudly instead of
# creating an import edge for a constant.
CARD_CHECKS = ("lasthr", "volexp", "midrange", "bridge", "monthend")


def stamp_card(trades: list) -> None:
    """Backfill the live card's verdict onto historical trades.

    Uses only entry-time fields already stamped on each trade (the overnight
    flags for volexp/midrange, this module's calendar flags, and the tape
    prints for the last-hour check), so every verdict is knowable at 15:00
    of its own night. Semantics mirror tonight.evaluate exactly: any red ->
    FLAGGED, all five strictly clear -> CLEAN, else INCOMPLETE — unknowns
    never pass."""
    for t in trades:
        checks = {}
        p1500, dopen, p1400 = t.get("signal_price"), t.get("day_open"), t.get("p1400")
        if dopen is None or p1400 is None:
            checks["lasthr"] = None
        else:
            body, lh = p1500 - dopen, p1500 - p1400
            checks["lasthr"] = False if (body == 0 or lh == 0) else (body > 0) == (lh > 0)
        checks["volexp"] = t.get("f_vol_expand")
        checks["midrange"] = t.get("f_midrange")
        fb, fm = t.get("f_holiday_bridge"), t.get("f_month_end")
        checks["bridge"] = (not fb) if fb is not None else None
        checks["monthend"] = (not fm) if fm is not None else None
        red = [k for k in CARD_CHECKS if checks[k] is False]
        t["card_red"] = red
        if red:
            t["card_verdict"] = "FLAGGED"
        elif all(checks[k] is True for k in CARD_CHECKS):
            t["card_verdict"] = "CLEAN"
        else:
            t["card_verdict"] = "INCOMPLETE"


def card_summary(trades: list, split: date) -> dict:
    """CLEAN vs FLAGGED vs INCOMPLETE, per window — the card against what the
    ledger actually paid. The honest read is the pair of sub-windows, not the
    pooled column: the five checks were assembled AFTER seeing this data, so
    the backfill shows the shape of the hypothesis, never its proof."""
    def block(tt):
        if not tt:
            return {"n": 0}
        c = _compact(tt)
        c["idx_continued_pct"] = round(
            sum(1 for t in tt if t["signed_move_pts"] > 0) / len(tt) * 100, 1)
        return c

    windows = {}
    for key, sub in (
            ("in_sample_2y", [t for t in trades if date.fromisoformat(t["date"]) < split]),
            ("holdout_1y", [t for t in trades if date.fromisoformat(t["date"]) >= split]),
            ("pooled_3y", trades)):
        windows[key] = {v: block([t for t in sub if t.get("card_verdict") == v])
                        for v in ("CLEAN", "FLAGGED", "INCOMPLETE")}
    return {
        "windows": windows,
        "split": split.isoformat(),
        # No empirical claims in this prose: the windows roll daily, and a
        # baked-in "CLEAN made money" sentence would eventually sit under a
        # table showing the opposite sign (review catch). The table speaks;
        # the note carries only what stays true by construction.
        "note": (
            "The live card's five checks stamped onto every historical night "
            "from that night's own 15:00 data. The composite was assembled "
            "AFTER seeing these windows, so this table is the hypothesis's "
            "shape, never its proof — the proof budget belongs to the live "
            "cards. A FLAGGED night is a coin you paid full theta for, not a "
            "guaranteed loser: some flagged nights win big. Backfill caveat: "
            "bridge/month-end carry comes from the synced spine's own "
            "sessions and the volexp prev-close from the VIX store, so a "
            "data gap can make a backfilled verdict differ from what the "
            "live card honestly said that night."),
    }


def live_card_status(card_date: str, graded: bool,
                     latest_entry: Optional[str]) -> str:
    """Classify a logged live card against the ledger: GRADED when its night
    has a trade; NO_TRADE when the ledger has moved past that date without
    one (the study skipped it — flat body, unresolvable expiry); PENDING only
    while the night's exit genuinely hasn't printed yet. Without this split,
    skipped nights would read 'pending' forever and quietly overstate how
    many proof-budget samples are still coming (review catch)."""
    if graded:
        return "graded"
    if latest_entry is not None and card_date <= latest_entry:
        return "no_trade"
    return "pending"


# --- ladder + live scoreboard -----------------------------------------------

def _compact(trades: list) -> dict:
    if not trades:
        return {"n": 0}
    pct = [t["net_pct"] for t in trades]
    rs = [t["net_rs"] for t in trades]
    peak = worst = cum = 0.0
    for x in rs:
        cum += x
        peak = max(peak, cum)
        worst = min(worst, cum - peak)
    return {
        "n": len(trades),
        "win_pct": round(sum(1 for p in pct if p > 0) / len(pct) * 100, 1),
        "mean_pct": round(statistics.mean(pct), 2),
        "median_pct": round(statistics.median(pct), 2),
        "total_rs": round(sum(rs), 0),
        "max_dd_rs": round(worst, 0),
    }


def ladder(trades: list) -> list:
    """base -> skip holiday bridges -> also skip month-end entries.

    Kept requires the flag to be strictly False; an unknown month-end falls
    out at its rung with its own tally, so unknowns can never inflate a rung."""
    rungs = [{"label": "every night (the strategy)", "kept": _compact(trades)}]
    no_bridge = [t for t in trades if t.get("f_holiday_bridge") is False]
    rungs.append({
        "label": "skip holiday bridges",
        "kept": _compact(no_bridge),
        "removed": _compact([t for t in trades if t.get("f_holiday_bridge") is not False]),
        "unknown_n": sum(1 for t in trades if t.get("f_holiday_bridge") is None),
    })
    clear = [t for t in no_bridge if t.get("f_month_end") is False]
    rungs.append({
        "label": "also skip month-end entries",
        "kept": _compact(clear),
        "removed": _compact([t for t in no_bridge if t.get("f_month_end") is not False]),
        "unknown_n": sum(1 for t in no_bridge if t.get("f_month_end") is None),
    })
    return rungs


def live_scoreboard(trades: list) -> dict:
    """Nights strictly after the registration date. The hypothesis under test
    is that the FLAGGED nights are bad, so the verdict clock counts flagged
    nights — the smaller class — not clear ones."""
    live = [t for t in trades if date.fromisoformat(t["date"]) > REGISTERED_ON]
    flagged = [t for t in live if t.get("f_holiday_bridge") is True
               or t.get("f_month_end") is True]
    clear = [t for t in live if t.get("f_calendar_clear") is True]
    return {
        "registered_on": REGISTERED_ON.isoformat(),
        "min_sample": MIN_LIVE_SAMPLE,
        "live_nights": len(live),
        "flagged_nights": len(flagged),
        "verdict_due": max(0, MIN_LIVE_SAMPLE - len(flagged)),
        "all": _compact(live),
        "flagged": _compact(flagged),
        "clear": _compact(clear),
        "unknown_n": len(live) - len(flagged) - len(clear),
        "note": (f"The flags claim these nights lose money; graded at "
                 f"{MIN_LIVE_SAMPLE}+ flagged live nights (house rule). At "
                 "~2-3 flagged nights a month, expect the verdict to take "
                 "about a year — that is the honest pace. Until then this "
                 "panel is a tally, not evidence."),
    }


def build(primary_trades: list, check_trades: list, days: "dict[date, Day]",
          model: OptionModel, split: date) -> dict:
    """The whole attribution block: decorate both trade sets in place, then
    assemble the results payload. `split` is the in-sample/holdout boundary
    (one year back), matching the signal search's windows."""
    sessions = sorted(days)
    for tt in (primary_trades, check_trades):
        decompose(tt, days, model)
        annotate(tt, sessions)
    return {
        "reasons_1y": reasons_summary(primary_trades),
        "reasons_3y": reasons_summary(check_trades),
        "buckets": bucket_table(check_trades, split),
        "flags": {
            "registered_on": REGISTERED_ON.isoformat(),
            "definitions": FLAG_DEFINITIONS,
            "ladder_1y": ladder(primary_trades),
            "ladder_3y": ladder(check_trades),
            "live": live_scoreboard(check_trades),
            "note": (
                "Flags, never gates: the headline strategy ignores them, the "
                "ladder shows what each would have removed, and the verdict "
                "belongs to live nights only. These buckets were found by "
                "inspecting historical losers, so their backtest numbers are "
                "partially self-selected by construction — the same standing "
                "Round 4's filters have, one tab over."),
        },
        "note": (
            "Attribution is sequential repricing with the study's own model "
            "(spot moved, then the clock, then VIX; costs are the exact "
            "remainder), so the four components sum to each trade's net % "
            "and interaction terms fold into the later steps. It inherits "
            "the model's published error bars — read it as anatomy, not as "
            "four independent measurements."),
    }
