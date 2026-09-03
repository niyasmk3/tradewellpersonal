"""Orchestration for the Closing Day Strategy: sync -> calibrate -> study.

Order is not arbitrary. The pricing model is fitted before any backtest runs,
and the backtest is handed the fit's own VIX range so it can report how much
of its result sits outside it. Validation runs on the same model object the
study priced with — scoring a differently-built model would publish an error
bar that belongs to nothing.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Optional

from app.closing import attribution, cpr, events, flags as overnight_filters, oiwall, shadow_exit, signals, store, tiers, tonight, validate
from app.closing.calendar import IST
from app.closing.data import sync
from app.closing.pricing import build_model
from app.closing.study import (
    DEFAULT_SIGNAL_MODE,
    SIGNAL_MODES,
    StudyConfig,
    VixLookup,
    build_days,
    run_study,
    variant_sweep,
)
from app.config import get_settings
from app.patterns import store as patterns_store

log = logging.getLogger("tradewell.closing")

MIN_BARS = 5000          # ~3 months of 5-min bars; below this nothing is stable
PRIMARY_YEARS = 1
CHECK_YEARS = 3


class ClosingError(RuntimeError):
    pass


def run_sync(kite, years: int = 3) -> dict:
    if kite is None:
        raise ClosingError("Kite is not authenticated — log in first")
    return sync(kite, years=years)


def run_tonight(kite) -> dict:
    """The live 15:00 pre-trade card — today's values + the five registered
    risk checks. Reads a light in-memory window from Kite; never writes the
    synced stores."""
    if kite is None:
        raise ClosingError("Kite is not authenticated — log in first")
    return tonight.run(kite)


def _signal_comparison(spine, vix, model, lots: int, fit: dict, today) -> dict:
    """The two reference rules, side by side, on both windows.

    Kept as its own block rather than folded into the headline: switching the
    reference from yesterday's close to today's open is a change to the
    HYPOTHESIS, not a tuning knob, and the reader should see both rather than
    be handed the winner.
    """
    rows = []
    for mode in SIGNAL_MODES:
        for years in (1, 3):
            cfg = _config(lots, fit, mode)
            run = run_study(spine, vix, model, cfg,
                            start=today - timedelta(days=365 * years))
            u, o = run.get("underlying"), run.get("option")
            if not u or not o:
                continue
            rows.append({
                "signal_mode": mode, "years": years, "n": run["n"],
                "continued_pct": u["continued_pct"],
                "median_signed_pts": u["median_signed_pts"],
                "mean_signed_pts": u["mean_signed_pts"],
                "mean_signed_ci95": u["mean_signed_ci95"],
                "ce_share_pct": round(
                    sum(1 for t in run["trades"] if t["direction"] == "CE")
                    / max(run["n"], 1) * 100, 1),
                "win_rate_pct": o["win_rate_pct"],
                "mean_pct": o["mean_pct"],
                "median_pct": o["median_pct"],
                "total_net_rs": o["total_net_rs"],
                "mean_net_pct_ci95": o["mean_net_pct_ci95"],
            })
    return {"rows": rows, "note": (
        "`prev_close` is the original hypothesis; `day_open` compares the 15:00 "
        "print to today's own open. Watch ce_share: `prev_close` picks CE on "
        "~54% of nights and so collects part of NIFTY's upward drift for free, "
        "which the ALWAYS CE control in the signal search reproduces on its own.")}


def _config(lots: int, fit: dict, mode: Optional[str] = None) -> StudyConfig:
    """The one place a StudyConfig is built from a calibration fit — the
    Overnight service imports this rather than copying it, so the two tabs can
    never price the same trades with different spreads (review catch). The
    lot-size and expiry-switch knobs are wired HERE for the same reason: they
    were declared in config.py but never read, which made every rupee figure
    immune to CLOSING_LOT_SIZE (review catch — a config that promises control
    it doesn't have is worse than a constant)."""
    settings = get_settings()
    spread = fit.get("spread") or {}
    entry = (spread.get("entry_window") or {}).get("spread_pct")
    exit_ = (spread.get("exit_window") or {}).get("spread_pct")
    iv = fit.get("iv_curve") or {}
    cfg = StudyConfig(lots=max(1, int(lots)),
                      lot_size=max(1, settings.closing_lot_size))
    try:
        cfg.expiry_switch = date.fromisoformat(settings.closing_expiry_switch)
    except ValueError:
        log.warning("CLOSING_EXPIRY_SWITCH %r is not a date — keeping %s",
                    settings.closing_expiry_switch, cfg.expiry_switch)
    if entry:
        cfg.entry_spread_pct = float(entry)
    if exit_:
        cfg.exit_spread_pct = float(exit_)
    cfg.calib_vix_lo = iv.get("vix_min")
    cfg.calib_vix_hi = iv.get("vix_max")
    cfg.signal_mode = mode or DEFAULT_SIGNAL_MODE
    return cfg


_RULE_TEXT = {
    "day_open": ("At 15:00 IST compare NIFTY to TODAY'S OPEN — is the session "
                 "green or red so far."),
    "prev_close": ("At 15:00 IST compare NIFTY to the previous session's close."),
}
_RULE_WHY = {
    "day_open": ("Chosen over the previous-close reference because that one "
                 "picks CE on 54% of nights and so collects NIFTY's upward "
                 "drift for free — over three years it barely separated from "
                 "an always-buy-CE control. This reference splits CE/PE ~48/52, "
                 "so what it earns it earns from direction."),
    "prev_close": ("The original hypothesis. Note that it picks CE on ~54% of "
                   "nights, and the always-buy-CE control in the signal search "
                   "scores nearly as well — much of this rule's result is drift, "
                   "not direction."),
}


def run_analysis(lots: int = 1, signal_mode: Optional[str] = None) -> dict:
    spine = patterns_store.load_frame()
    if len(spine) < MIN_BARS:
        raise ClosingError(
            f"Only {len(spine)} index bars stored — run a sync first "
            f"(need >= {MIN_BARS})")
    vix = store.load_vix()
    if vix.empty:
        raise ClosingError("No India VIX bars stored — run a sync first")

    mode = signal_mode or get_settings().closing_signal_mode
    if mode not in SIGNAL_MODES:
        raise ClosingError(
            f"Unknown signal mode {mode!r} — expected one of {list(SIGNAL_MODES)}")

    spot = validate.SpotLookup(spine)
    model, fit = build_model(spot.at)
    cfg = _config(lots, fit, mode)
    days = build_days(spine)

    # IST, never host-local: on a non-IST box a naive date.today() would shift
    # every window boundary (and the attribution split) by a day (review catch).
    today = datetime.now(IST).date()
    primary = run_study(spine, vix, model, cfg,
                        start=today - timedelta(days=365 * PRIMARY_YEARS))
    check = run_study(spine, vix, model, cfg,
                      start=today - timedelta(days=365 * CHECK_YEARS))
    # Loss anatomy + the registered calendar flags. Decorates the primary
    # ledger's rows in place (the /trades endpoint serves them) and needs the
    # 3-year trades for its in-sample/holdout bucket split, so it runs before
    # the 3-year ledger is dropped from the payload.
    loss_attribution = attribution.build(
        primary.get("trades") or [], check.get("trades") or [], days, model,
        split=today - timedelta(days=365 * PRIMARY_YEARS))
    # The live card, backfilled: stamp the overnight flags (vol expansion,
    # mid-range) plus this module's calendar flags into a per-night verdict,
    # so every ledger row shows what the 15:00 card would have said, and the
    # summary compares CLEAN vs FLAGGED against the P&L that followed. Runs
    # after attribution.build (which stamps the calendar flags it reads).
    vix_lookup = VixLookup(vix)
    for tt in (primary.get("trades") or [], check.get("trades") or []):
        overnight_filters.annotate(tt, spine, vix)
        attribution.stamp_card(tt)
        cpr.annotate(tt, days)          # shadow — stamped, never scored
        oiwall.annotate(tt, days)       # shadow — stamped, never scored
        tiers.annotate(tt)              # Gold/Silver/Bronze over the two shadows
        events.annotate(tt)             # shadow — US event in the hold window
        # 10:45 shadow exit — same trades re-priced at the later print;
        # the 09:50 ledger stays the ledger of record.
        shadow_exit.annotate(tt, days, vix_lookup, model, cfg)
    # Join against the 3-YEAR ledger, not the 1-year one: the decision log is
    # append-only and forever, and a card older than the primary window would
    # silently lose its grade right as the sample count approached the house
    # bar (review catch).
    by_date = {t["date"]: t for t in check.get("trades") or []}
    latest_entry = max(by_date) if by_date else None
    live_cards = []
    for row in tonight.logged_cards():
        graded = by_date.get(row["date"])
        live_cards.append({
            "date": row["date"], "verdict": row["verdict"], "red": row["red"],
            "as_of": row.get("as_of"),
            "status": attribution.live_card_status(row["date"], graded is not None,
                                                   latest_entry),
            "net_pct": graded["net_pct"] if graded else None,
            "signed_move_pts": graded["signed_move_pts"] if graded else None,
            "backfill_verdict": graded["card_verdict"] if graded else None,
            "cpr_narrow": (row.get("values") or {}).get("cpr_narrow"),
            "oi_state": (row.get("values") or {}).get("oi_state"),
            "events": (row.get("values") or {}).get("events"),
            "tier": tiers.tier(row.get("verdict"), (row.get("values") or {}).get("oi_state"),
                               (row.get("values") or {}).get("cpr_narrow")),
            "x1045_net_pct": graded.get("x1045_net_pct") if graded else None,
        })
    card_backfill = attribution.card_summary(check.get("trades") or [],
                                             split=today - timedelta(days=365 * PRIMARY_YEARS))
    card_backfill["cpr_shadow"] = cpr.summary(
        check.get("trades") or [], split=today - timedelta(days=365 * PRIMARY_YEARS))
    card_backfill["oi_shadow"] = oiwall.summary(
        check.get("trades") or [], split=today - timedelta(days=365 * PRIMARY_YEARS))
    card_backfill["tiers"] = tiers.summary(
        check.get("trades") or [], split=today - timedelta(days=365 * PRIMARY_YEARS))
    card_backfill["event_shadow"] = events.summary(
        check.get("trades") or [], split=today - timedelta(days=365 * PRIMARY_YEARS))
    card_backfill["shadow_exit"] = shadow_exit.summary(
        check.get("trades") or [], split=today - timedelta(days=365 * PRIMARY_YEARS))
    card_backfill["live"] = {
        "rows": live_cards,
        "note": ("Decision-time cards from .closing_tonight.jsonl joined "
                 "against the ledger once each night grades. This is the "
                 "composite's only proof budget; the house 30-sample rule "
                 "applies before any verdict. backfill_verdict is the same "
                 "night recomputed from synced bars — a mismatch is a "
                 "data-integrity signal to investigate: either the live read "
                 "moved between 15:05 and the sync, or the synced store has "
                 "a gap (spine session / VIX hole) that skewed the backfill. "
                 "no-trade rows are nights the study itself skipped."),
    }
    check.pop("trades", None)   # the 3-year leg is a robustness read, not a ledger

    # One validation pass per analyze; the debiased sensitivity below reuses
    # its overnight block instead of recomputing it (review catch: the same
    # chain-DB scans ran twice per click).
    validation = validate.run(spot, model)

    # Sensitivity: the same year re-priced with the model's own measured
    # overnight bias removed. If the headline flips sign here, the headline was
    # never a finding about the market — it was a finding about the model.
    overnight = validation.get("overnight") or {}
    bias_pp = overnight.get("median_bias_pp") if overnight.get("available") else None
    debiased = None
    if bias_pp:
        cfg_db = _config(lots, fit, mode)
        cfg_db.bias_adjust_pp = -float(bias_pp)
        run = run_study(spine, vix, model, cfg_db,
                        start=today - timedelta(days=365 * PRIMARY_YEARS))
        run.pop("trades", None)
        debiased = {
            "applied_pp": round(-float(bias_pp), 1),
            "basis_n_pairs": overnight.get("n_pairs"),
            "result": run,
            "note": ("Every exit premium shifted by the model's measured "
                     "overnight bias. The correction rests on "
                     f"{overnight.get('n_pairs')} real overnight quote pairs, "
                     "so treat it as a direction-of-error check, not a "
                     "second estimate."),
        }

    results = {
        "rule": {
            "mode": mode,
            "signal": _RULE_TEXT[mode],
            "why": _RULE_WHY[mode],
            "trade": "Below it -> buy the ATM PE. Above it -> buy the ATM CE.",
            "entry": "Filled at the close of the 15:00 bar (15:05) — five "
                     "minutes after the print the signal is read from.",
            "exit": "Sold at the 09:50 print on the next trading day.",
            "expiry": "Nearest weekly expiry that is still alive the next "
                      "morning; on expiry day the trade rolls to the next week.",
            "size": f"{cfg.lots} lot ({cfg.qty} qty), identical every night.",
        },
        "disclaimer": (
            "Educational analysis of a historical rule, not financial advice "
            "and not a prediction. The index layer is measured from real "
            "5-minute prints; the option layer is MODELLED, because Kite "
            "deletes expired contracts and a year of real premiums cannot be "
            "fetched at any price. Read every option number against the error "
            "published in `validation`."
        ),
        "data": {
            "index_bars": int(len(spine)),
            "vix_bars": int(len(vix)),
            "sessions": len(days),
            "spine_from": min(days).isoformat() if days else None,
            "spine_to": max(days).isoformat() if days else None,
            "last_sync": store.get_meta("last_vix_sync"),
        },
        "model": model.describe(),
        "calibration": fit,
        "validation": validation,
        "primary": primary,
        "attribution": loss_attribution,
        "card_backfill": card_backfill,
        "signal_search": signals.search(days, today=today),
        "signal_comparison": _signal_comparison(spine, vix, model, lots, fit, today),
        "robustness": {
            "three_year": check,
            "debiased": debiased,
            "variants": variant_sweep(days, cfg,
                                      start=today - timedelta(days=365 * PRIMARY_YEARS)),
            "variants_note": (
                "Index points only — no option model. Sweeping the clock "
                "through the pricing model would be shopping a modelled "
                "surface for its best cell."),
        },
    }
    store.save_results(results)
    log.info("Closing analysis saved: %s primary trades, %s check trades",
             primary.get("n"), check.get("n"))
    return results


def status() -> dict:
    results = store.load_results()
    return {
        "vix_bars": store.vix_count(),
        # COUNT(*), not a full-frame load — this sits on a 20s UI poll
        # (the same lesson patterns_store.load_tail records).
        "index_bars": patterns_store.candle_count(),
        "last_sync": store.get_meta("last_vix_sync"),
        "results_available": results is not None,
        "results_generated_at": results.get("generated_at") if results else None,
    }
