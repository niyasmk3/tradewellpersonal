"""The 10:45 shadow exit — a second exit clock on the Closing Day ledger.

PRE-REGISTERED 2026-08-22. The registered rule exits at the 09:50 print.
This module prices the SAME trades as if exited at the 10:45 print instead,
stamps both on every ledger row, and compares them — a shadow, never the
ledger of record. Verdict by live nights.

WHY 10:45, AND WHY IT IS NOT A SWEEP ARTIFACT. An 11-cell modelled exit
sweep on 22-Aug found a contiguous 10:15-11:30 plateau that beat 09:50 in
both windows (CLEAN cell: +7% holdout, +20% in-sample at 10:45 with the
study's own bar-OPEN convention). study.py warns against exactly that kind
of search, so the cell was decomposed before registration:

    index points (Layer 1, model-free): mean signed move 09:50 -> 10:45 is
      FLAT (+67.6 -> +69.0 holdout, +33.9 -> +33.8 in-sample)
    VIX at the two exits: flat (14.70 vs 14.66)
    VIX+time leg of the option P&L: NEGATIVE (-Rs6.1k / -Rs7.7k = 55 more
      minutes of theta)
    spot leg: +Rs11.0k / +Rs21.2k, with the signed-move dispersion widening
      (sd 177 -> 190 pts holdout, 134 -> 153 in-sample)

A flat mean with a wider spread paying a long option is convexity — gamma
harvested from 55 extra minutes of dispersion, net of theta. That is the
option's payoff shape, not a calibration, which is why it earns a shadow
ledger rather than a rejection. Honest caveats: the plateau was found after
seeing both windows; the exit spread is the 09:50-measured figure reused
at 10:45; and the backfill is still modelled premium.

MODEL-FREE LIVE LEG. Kite deletes expired contracts, so historical premiums
are modelled — but the night's contract is ALIVE at 09:50 and 10:45 the
next morning. snapshot.capture_loop captures the card's contract quote at
15:05 (entry), 09:50 and 10:45, so the live comparison is real mid vs real
mid. Editing the exit clock resets the live count.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import date, datetime, time as dtime
from pathlib import Path
from typing import Optional

from app.closing.calendar import IST
from app.closing.pricing import OptionModel
from app.closing.study import Day, StudyConfig, VixLookup
from app.paper.charges import charges

log = logging.getLogger("tradewell.closing")

SHADOW_EXIT_BAR = (10, 45)   # its OPEN is the 10:45 print, same convention as EXIT_BAR
REGISTERED_ON = "2026-08-22"
DEFINITION = ("Same entry, same contract, same fill/charge model; exit at the "
              "10:45 print (bar open) instead of 09:50. Shadow only — the "
              "09:50 ledger stays the ledger of record.")
QUOTES_PATH = Path(__file__).resolve().parents[2] / ".closing_exit_quotes.jsonl"

# Live capture windows (IST): the card's contract is quoted once per window.
W_ENTRY = (dtime(15, 5), dtime(15, 10))     # tonight's card, at the fill clock
W_EXIT_0950 = (dtime(9, 50), dtime(9, 55))   # last night's card
W_EXIT_1045 = (dtime(10, 45), dtime(10, 50))


# --- modelled backfill -------------------------------------------------------

def annotate(trades: list, days: "dict[date, Day]", vix: VixLookup,
             model: OptionModel, cfg: StudyConfig) -> None:
    """Stamp x1045_* on each ledger row — the identical trade exited at the
    10:45 print. Unknown (no bar / no VIX / unpriceable) stays None."""
    for t in trades:
        for k in ("x1045_exit_spot", "x1045_vix_out", "x1045_mid_out",
                  "x1045_fill_out", "x1045_net_pct", "x1045_net_rs",
                  "x1045_signed_move_pts", "x1045_delta_rs"):
            t[k] = None
        xd = date.fromisoformat(t["exit_date"])
        day = days.get(xd)
        bar = day.bar(SHADOW_EXIT_BAR) if day else None
        if bar is None:
            continue
        exit_spot, exit_ts = bar[0], bar[4]
        v_out = vix.at(exit_ts)
        if v_out is None:
            continue
        expiry = date.fromisoformat(t["expiry"])
        is_call = t["direction"] == "CE"
        mid_out = model.premium(exit_spot, t["strike"], exit_ts, expiry, is_call, v_out)
        if mid_out is None:
            continue
        if cfg.bias_adjust_pp:
            mid_out = max(mid_out + t["mid_in"] * cfg.bias_adjust_pp / 100.0, 0.0)
        fill_out = round(max(mid_out * (1 - cfg.exit_spread_pct / 2), 0.05), 2)
        qty = t["qty"]
        gross = (fill_out - t["fill_in"]) * qty
        net = gross - charges(t["fill_in"], fill_out, qty, 2)
        t["x1045_exit_spot"] = round(exit_spot, 2)
        t["x1045_vix_out"] = round(v_out, 2)
        t["x1045_mid_out"] = mid_out
        t["x1045_fill_out"] = fill_out
        t["x1045_net_pct"] = round(net / (t["fill_in"] * qty) * 100, 2)
        t["x1045_net_rs"] = round(net, 2)
        t["x1045_signed_move_pts"] = round(
            (exit_spot - t["entry_spot"]) * (1 if is_call else -1), 2)
        t["x1045_delta_rs"] = round(net - t["net_rs"], 2)


def _as_rows(trades: list, key_pct: str, key_rs: str) -> list:
    return [{"net_pct": t[key_pct], "net_rs": t[key_rs]} for t in trades]


def summary(trades: list, split: date) -> dict:
    """09:50 vs 10:45 per verdict per window, paired on nights where BOTH
    exits are known, so the two columns describe the same nights."""
    from app.closing.attribution import _compact

    def cells(sub):
        paired = [t for t in sub if t.get("x1045_net_rs") is not None]
        out = {}
        for verdict, tag in (("CLEAN", "clean"), ("FLAGGED", "flagged")):
            vv = [t for t in paired if t.get("card_verdict") == verdict]
            out[f"{tag}_0950"] = _compact(_as_rows(vv, "net_pct", "net_rs"))
            out[f"{tag}_1045"] = _compact(_as_rows(vv, "x1045_net_pct", "x1045_net_rs"))
        out["unknown_n"] = len(sub) - len(paired)
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
        "exit_bar": "10:45",
        "windows": windows,
        "live": live_summary(trades),
        "note": ("Modelled backfill of the same nights exited at 10:45. Found "
                 "as a plateau in a modelled sweep, then decomposed: the gain is "
                 "convexity from wider 10:45 dispersion net of extra theta, not "
                 "a VIX artifact — but it was chosen after seeing both windows, "
                 "so it rides as a shadow. The live block is REAL contract mids "
                 "(15:05 entry, 09:50 and 10:45 exits) captured by the snapshot "
                 "loop — the only model-free comparison this question can have."),
    }


# --- live real-quote capture ---------------------------------------------------

_lock = threading.Lock()


def quotes(path: Path = QUOTES_PATH) -> list:
    if not path.exists():
        return []
    rows = []
    try:
        with path.open() as fh:
            for line in fh:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    return rows


def _logged(night: str, kind: str, path: Path) -> bool:
    return any(r.get("night") == night and r.get("kind") == kind for r in quotes(path))


def _card_for(window_kind: str, today: date) -> Optional[dict]:
    """The decision-time card whose contract this window should quote:
    tonight's card for the entry window, last night's for the exits."""
    from app.closing import tonight
    cards = tonight.logged_cards()
    for row in reversed(cards):
        vals = row.get("values") or {}
        if not (vals.get("direction") and vals.get("strike") and vals.get("expiry")):
            continue
        if window_kind == "entry" and row.get("date") == today.isoformat():
            return row
        if window_kind != "entry" and vals.get("next_trading_day") == today.isoformat():
            return row
    return None


def _tradingsymbol(kite, expiry: date, strike: float, kind: str) -> Optional[str]:
    from app.kite.instruments import _as_date, _fetch_instruments
    _, nfo = _fetch_instruments(kite)
    for i in nfo:
        if (i.get("name") == "NIFTY" and i.get("instrument_type") == kind
                and _as_date(i.get("expiry")) == expiry
                and float(i.get("strike") or 0) == float(strike)):
            return i["tradingsymbol"]
    return None


def capture_quote(kite, now: Optional[datetime] = None,
                  path: Path = QUOTES_PATH) -> Optional[str]:
    """Quote the relevant card's contract if `now` is inside a capture window
    and that (night, kind) is not logged yet. Returns the kind logged."""
    if kite is None:
        return None
    now = now or datetime.now(IST)
    t = now.astimezone(IST).time()
    today = now.astimezone(IST).date()
    if W_ENTRY[0] <= t < W_ENTRY[1]:
        kind = "entry"
    elif W_EXIT_0950[0] <= t < W_EXIT_0950[1]:
        kind = "exit_0950"
    elif W_EXIT_1045[0] <= t < W_EXIT_1045[1]:
        kind = "exit_1045"
    else:
        return None
    card = _card_for(kind, today)
    if card is None:
        return None
    night = card["date"]
    if _logged(night, kind, path):
        return None
    vals = card["values"]
    try:
        expiry = date.fromisoformat(vals["expiry"])
        symbol = _tradingsymbol(kite, expiry, vals["strike"], vals["direction"])
        if symbol is None:
            log.warning("Shadow exit: no contract for %s %s %s", expiry, vals["strike"],
                        vals["direction"])
            return None
        q = kite.quote([f"NFO:{symbol}"]).get(f"NFO:{symbol}") or {}
        depth = q.get("depth") or {}
        buys, sells = depth.get("buy") or [], depth.get("sell") or []
        bid = buys[0]["price"] if buys and buys[0].get("price") else None
        ask = sells[0]["price"] if sells and sells[0].get("price") else None
        mid = round((bid + ask) / 2.0, 2) if bid and ask else q.get("last_price")
        row = {"night": night, "kind": kind, "symbol": symbol,
               "strike": vals["strike"], "expiry": vals["expiry"],
               "direction": vals["direction"], "verdict": card.get("verdict"),
               "at": now.astimezone(IST).isoformat(timespec="seconds"),
               "ltp": q.get("last_price"), "bid": bid, "ask": ask, "mid": mid,
               "oi": q.get("oi")}
    except Exception as exc:
        log.warning("Shadow exit quote capture failed: %s", exc)
        return None
    with _lock:
        if _logged(night, kind, path):
            return None
        try:
            with path.open("a") as fh:
                fh.write(json.dumps(row) + "\n")
        except OSError:
            log.warning("Exit-quote log unwritable at %s", path)
            return None
    log.info("Shadow exit %s quoted for %s: %s mid %s", kind, night, symbol, mid)
    return kind


def live_summary(trades: list, path: Path = QUOTES_PATH) -> dict:
    """Real-mid comparison per night: needs entry + both exits. Modelled
    figures for the same nights sit alongside so drift is visible."""
    by_night: dict = {}
    for r in quotes(path):
        if r.get("mid") is not None:
            by_night.setdefault(r["night"], {})[r["kind"]] = r
    by_date = {t["date"]: t for t in trades}
    rows = []
    for night in sorted(by_night):
        q = by_night[night]
        if not all(k in q for k in ("entry", "exit_0950", "exit_1045")):
            continue
        e, a, b = q["entry"]["mid"], q["exit_0950"]["mid"], q["exit_1045"]["mid"]
        if not e:
            continue
        t = by_date.get(night) or {}
        rows.append({"night": night, "verdict": q["entry"].get("verdict"),
                     "direction": q["entry"].get("direction"),
                     "entry_mid": e, "mid_0950": a, "mid_1045": b,
                     "real_0950_pct": round((a - e) / e * 100, 1),
                     "real_1045_pct": round((b - e) / e * 100, 1),
                     "model_0950_pct": t.get("net_pct"),
                     "model_1045_pct": t.get("x1045_net_pct")})
    out = {"n": len(rows), "rows": rows[-60:]}
    if rows:
        out["mean_real_0950_pct"] = round(sum(r["real_0950_pct"] for r in rows) / len(rows), 1)
        out["mean_real_1045_pct"] = round(sum(r["real_1045_pct"] for r in rows) / len(rows), 1)
        out["nights_1045_better"] = sum(1 for r in rows if r["real_1045_pct"] > r["real_0950_pct"])
    out["note"] = ("Real contract mids, gross of charges, captured live at 15:05 / "
                   "09:50 / 10:45 for the card's own strike. House rule: no "
                   "verdict below 30 paired nights.")
    return out
