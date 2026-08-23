"""The 15:00 research logger — GIFT prints + option-chain skew, daily.

PRE-REGISTERED 2026-08-22. This module records the two decision-time signals
that CANNOT be backtested and therefore can only be graded prospectively:

  1. OFFSHORE LAST-HOUR DIVERGENCE (GIFT Nifty vs NSE, simultaneous):
         divergence_pts = (GIFT@15:00 - GIFT@14:00) - (NIFTY@15:00 - NIFTY@14:00)
     aligned against tonight's direction (CE keeps the sign, PE negates it);
     the frozen flag fires when aligned < -10 pts. In the Dec-23->Aug-26
     backfill (investing.com 15-min bars) this was the only candidate that
     improved BOTH windows (+Rs14,035 / +Rs1,462), but the outlier-pinned
     bootstrap put P(real) at only 75.5% against an 85% acceptance bar —
     so it logs as a FLAG, never a gate, and earns registration only if the
     live sample hardens it.

  2. IV SKEW at 15:00 (weekly chain, delta-based per the round-4 spec):
         S = (IV_30d_PE - IV_30d_CE) / IV_ATM_CE
     30-delta strikes on the nearest weekly (NEXT weekly when DTE==0), with
     mid, IV, spread, OI and the 5-level book per leg. Kite deletes expired
     contracts, so every day this does not run is a day of evidence lost
     forever; verdict horizon ~150-200 logged nights.

Editing either frozen definition resets that signal's live count — same house
rule as the calendar flags. Unknown inputs record as None, never as a pass or
a fail (the unknown-never-passes rule).

CAPTURE. A backend loop (main.py lifespan) samples the GIFT front-month last
price from NSE IX's public API in two windows — 14:00-14:10 and 15:00-15:10
IST — and writes the full row once from 15:05 (the tonight card's own settle
clock). GIFT needs no Kite auth; the chain block degrades to an error note
when Kite is not logged in. First full row per date wins, append-only, same
audit contract as .closing_tonight.jsonl.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import threading
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Optional

from app.closing.calendar import IST
from app.closing.data import _in_session
from app.closing.pricing import atm_strike
from app.market import calendar as mcal
from app.options.iv import RISK_FREE, bs_price, implied_vol, years_to_expiry
from app.patterns.data import NIFTY_TOKEN

log = logging.getLogger("tradewell.closing")

LOG_PATH = Path(__file__).resolve().parents[2] / ".closing_snapshot.jsonl"
STAMP_PATH = Path(__file__).resolve().parents[2] / ".closing_snapshot_stamps.json"

REGISTERED_ON = "2026-08-22"
DIVERGENCE_DEF = ("(GIFT@15:00 - GIFT@14:00) - (NIFTY@15:00 - NIFTY@14:00); "
                  "aligned = as-is for CE, negated for PE; flag when aligned "
                  "< -10 pts. Flag, never a gate.")
SKEW_DEF = ("S = (IV_30d_PE - IV_30d_CE) / IV_ATM_CE on the nearest weekly "
            "(next weekly when DTE==0); 30-delta strikes chosen by |BS delta|.")
DIVERGENCE_FLAG_PTS = -10.0

# NSE IX public quote API — serves the GIFT NIFTY futures LASTPRICE without
# auth or bot-walls (verified 2026-08-22). investing.com's TVC feed, used for
# the historical backfill, sits behind Cloudflare and cannot be fetched
# server-side; if this endpoint changes shape the fetch degrades to None.
NSEIX_WATCH_URL = ("https://www.nseix.com/api/derivatives-watch"
                   "?inst_type1=IDX&inst_type2=STK&inst_type3=CBI&type=top20")
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36")

# Capture windows (IST). The 15:00 stamp doubles as the "15:00 print" proxy:
# GIFT trades continuously, so the first quote at/after 15:00:00 is the
# closest server-side equivalent of the backfill's 15:00 bar open.
W1400 = (dtime(14, 0), dtime(14, 10))
W1500 = (dtime(15, 0), dtime(15, 10))
ROW_FROM = dtime(15, 5)      # full row is buildable from the settle clock
ROW_UNTIL = dtime(15, 30)    # after the NSE close, chain quotes are stale
_STRIKE_SPAN = 10            # quote ATM +/- this many strikes


# --- GIFT quote --------------------------------------------------------------

def fetch_gift_quote(timeout: float = 10.0) -> Optional[dict]:
    """Front-month GIFT NIFTY futures last price from NSE IX, or None.

    {"price": float, "at": iso-time, "expiry": "25-Aug-2026"}. Never raises —
    a dead endpoint is an UNKNOWN reading, not a crash in the capture loop.
    """
    try:
        import requests
        r = requests.get(NSEIX_WATCH_URL, headers={"User-Agent": _UA},
                         timeout=timeout)
        r.raise_for_status()
        rows = (r.json() or {}).get("data") or []
        futs = []
        for row in rows:
            if (row.get("INSTRUMENT") == "Index Futures"
                    and row.get("SYMBOL") == "NIFTY" and row.get("LASTPRICE")):
                try:
                    exp = datetime.strptime(row.get("EXPIRYDATE", ""), "%d-%b-%Y").date()
                except ValueError:
                    continue
                futs.append((exp, float(row["LASTPRICE"]), row.get("EXPIRYDATE")))
        if not futs:
            return None
        exp, price, label = min(futs)
        return {"price": price, "expiry": label,
                "at": datetime.now(IST).isoformat(timespec="seconds")}
    except Exception as exc:
        log.warning("GIFT quote fetch failed: %s", exc)
        return None


# --- intraday stamps ---------------------------------------------------------

def _load_stamps(today: date, path: Path = STAMP_PATH) -> dict:
    try:
        data = json.loads(path.read_text())
        if data.get("date") == today.isoformat():
            return data
    except (OSError, ValueError):
        pass
    return {"date": today.isoformat()}


def _save_stamps(stamps: dict, path: Path = STAMP_PATH) -> None:
    try:
        path.write_text(json.dumps(stamps))
    except OSError:
        log.warning("Snapshot stamp file unwritable at %s", path)


def stamp_gift(now: datetime, path: Path = STAMP_PATH) -> Optional[str]:
    """Capture the GIFT quote into whichever window `now` falls in (first
    reading per window wins). Returns the key stamped, else None."""
    t = now.astimezone(IST).time()
    key = None
    if W1400[0] <= t < W1400[1]:
        key = "g1400"
    elif W1500[0] <= t < W1500[1]:
        key = "g1500"
    if key is None:
        return None
    stamps = _load_stamps(now.astimezone(IST).date(), path)
    if key in stamps:
        return None
    quote = fetch_gift_quote()
    if quote is None:
        return None
    stamps[key] = quote
    _save_stamps(stamps, path)
    log.info("GIFT %s stamped: %.1f", key, quote["price"])
    return key


# --- chain skew --------------------------------------------------------------

def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _bs_delta(spot: float, strike: float, t_years: float, sigma_pct: float,
              is_call: bool) -> Optional[float]:
    if spot <= 0 or strike <= 0 or t_years <= 0 or not sigma_pct:
        return None
    sigma = sigma_pct / 100.0
    sq = sigma * math.sqrt(t_years)
    d1 = (math.log(spot / strike) + (RISK_FREE + 0.5 * sigma * sigma) * t_years) / sq
    return _norm_cdf(d1) if is_call else _norm_cdf(d1) - 1.0


def _leg(quote: dict, strike: float, is_call: bool, spot: float,
         t_years: float) -> dict:
    """One option leg's loggable record from a Kite quote payload."""
    depth = quote.get("depth") or {}
    buys, sells = depth.get("buy") or [], depth.get("sell") or []
    bid = buys[0]["price"] if buys and buys[0].get("price") else None
    ask = sells[0]["price"] if sells and sells[0].get("price") else None
    mid = round((bid + ask) / 2.0, 2) if bid and ask else quote.get("last_price")
    iv = implied_vol(mid, spot, strike, t_years, is_call)
    return {
        "strike": strike,
        "mid": mid,
        "bid": bid,
        "ask": ask,
        "spread": round(ask - bid, 2) if bid and ask else None,
        "iv": iv,
        "delta": round(_bs_delta(spot, strike, t_years, iv, is_call), 3)
                 if iv else None,
        "oi": quote.get("oi"),
        "volume": quote.get("volume"),
        "depth": depth,
    }


def chain_skew(kite, today: date) -> dict:
    """The pre-registered skew snapshot from the live weekly chain.

    Raises nothing to the caller's face: any failure returns {"error": ...} so
    the GIFT half of the row still logs. Weekly = nearest expiry >= today,
    rolled to the NEXT weekly on expiry day itself (DTE==0 quotes near the
    settle are gamma noise, per the round-4 spec).
    """
    try:
        from app.kite.instruments import _as_date, _fetch_instruments
        _, nfo = _fetch_instruments(kite)
        options = [i for i in nfo if i.get("name") == "NIFTY"
                   and i.get("instrument_type") in ("CE", "PE")]
        expiries = sorted({d for d in (_as_date(o.get("expiry")) for o in options)
                           if d and d >= today})
        if not expiries:
            return {"error": "no NIFTY option expiries in the instrument dump"}
        expiry = expiries[1] if expiries[0] == today and len(expiries) > 1 else expiries[0]

        spot_q = kite.quote(["NSE:NIFTY 50"]).get("NSE:NIFTY 50") or {}
        spot = float(spot_q.get("last_price") or 0.0)
        if spot <= 0:
            return {"error": "no NIFTY spot quote"}
        atm = atm_strike(spot)
        wanted = {atm + i * 50.0 for i in range(-_STRIKE_SPAN, _STRIKE_SPAN + 1)}
        legs = {(float(o["strike"]), o["instrument_type"]): o["tradingsymbol"]
                for o in options
                if _as_date(o.get("expiry")) == expiry and float(o["strike"]) in wanted}
        if not legs:
            return {"error": f"no strikes found around ATM for {expiry}"}
        quotes = kite.quote(["NFO:" + s for s in legs.values()])

        t_years = years_to_expiry(expiry)
        recs = {}
        for (strike, kind), symbol in legs.items():
            q = quotes.get("NFO:" + symbol)
            if q:
                recs[(strike, kind)] = _leg(q, strike, kind == "CE", spot, t_years)

        def pick_30d(kind: str, target: float) -> Optional[dict]:
            cands = [r for (s, k), r in recs.items()
                     if k == kind and r["iv"] and r["delta"] is not None]
            return min(cands, key=lambda r: abs(r["delta"] - target)) if cands else None

        atm_ce = recs.get((atm, "CE"))
        atm_pe = recs.get((atm, "PE"))
        c30 = pick_30d("CE", 0.30)
        p30 = pick_30d("PE", -0.30)
        s_metric = None
        if c30 and p30 and atm_ce and atm_ce["iv"] and c30["iv"] and p30["iv"]:
            s_metric = round((p30["iv"] - c30["iv"]) / atm_ce["iv"], 4)
        return {
            "expiry": expiry.isoformat(),
            "dte": round(t_years * 365.0, 2),
            "spot": spot,
            "atm_strike": atm,
            "atm_ce": atm_ce,
            "atm_pe": atm_pe,
            "call_30d": c30,
            "put_30d": p30,
            "S": s_metric,
            "definition": SKEW_DEF,
        }
    except Exception as exc:
        log.warning("Chain skew snapshot failed: %s", exc)
        return {"error": str(exc)}


# --- NIFTY prints ------------------------------------------------------------

def nifty_prints(kite, now: datetime) -> dict:
    """Today's 09:15 / 14:00 / 15:00 bar opens — the same prints the tonight
    card reads, fetched light and in-memory."""
    try:
        start = now - timedelta(days=1)
        bars = {}
        for c in kite.historical_data(NIFTY_TOKEN, start, now, "5minute"):
            dt = c["date"]
            if _in_session(dt) and dt.date() == now.astimezone(IST).date():
                bars[(dt.hour, dt.minute)] = float(c["open"])
        return {"day_open": bars.get((9, 15)), "p1400": bars.get((14, 0)),
                "p1500": bars.get((15, 0))}
    except Exception as exc:
        log.warning("NIFTY prints fetch failed: %s", exc)
        return {"day_open": None, "p1400": None, "p1500": None,
                "error": str(exc)}


# --- the row -----------------------------------------------------------------

def build_row(today: date, now: datetime, stamps: dict, nifty: dict,
              skew: dict) -> dict:
    g14 = (stamps.get("g1400") or {}).get("price")
    g15 = (stamps.get("g1500") or {}).get("price")
    p14, p15 = nifty.get("p1400"), nifty.get("p1500")
    day_open = nifty.get("day_open")

    direction = None
    if p15 is not None and day_open is not None:
        body = p15 - day_open
        direction = "CE" if body > 0 else "PE" if body < 0 else None

    div = aligned = flag = None
    if None not in (g14, g15, p14, p15):
        div = round((g15 - g14) - (p15 - p14), 2)
        if direction:
            aligned = div if direction == "CE" else -div
            flag = aligned < DIVERGENCE_FLAG_PTS

    return {
        "date": today.isoformat(),
        "as_of": now.astimezone(IST).isoformat(timespec="seconds"),
        "registered_on": REGISTERED_ON,
        "gift": {"g1400": stamps.get("g1400"), "g1500": stamps.get("g1500"),
                 "source": "nseix.com derivatives-watch (front-month future)"},
        "nifty": nifty,
        "direction": direction,
        "divergence": {"pts": div, "aligned_pts": round(aligned, 2)
                       if aligned is not None else None,
                       "flag": flag, "definition": DIVERGENCE_DEF},
        "skew": skew,
    }


# --- append-only log (first full row per date wins) --------------------------

_log_lock = threading.Lock()


def _first_logged(today: date, path: Path = LOG_PATH) -> Optional[dict]:
    if not path.exists():
        return None
    try:
        with path.open() as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("date") == today.isoformat():
                    return row
    except OSError:
        return None
    return None


def snapshots(limit: int = 30, path: Path = LOG_PATH) -> list:
    """Standing rows (first per date wins), newest first."""
    if not path.exists():
        return []
    rows: dict = {}
    try:
        with path.open() as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("date") and row["date"] not in rows:
                    rows[row["date"]] = row
    except OSError:
        return []
    return [rows[d] for d in sorted(rows, reverse=True)[:limit]]


def run_snapshot(kite, now: Optional[datetime] = None,
                 log_path: Path = LOG_PATH,
                 stamp_path: Path = STAMP_PATH) -> dict:
    """Build and append today's row (idempotent — the first full row stands).

    Manual/loop entry point. A missing 15:00 stamp is backfilled from a live
    quote AT ITS ACTUAL TIME (honest timestamps); a missing 14:00 stamp stays
    None — the divergence for that night is simply UNKNOWN.
    """
    now = now or datetime.now(IST)
    today = now.astimezone(IST).date()
    if not mcal.is_trading_day(today):
        return {"logged": False, "reason": "not a trading day"}

    existing = _first_logged(today, log_path)
    if existing:
        return {"logged": False, "reason": "already logged", "row": existing}

    stamps = _load_stamps(today, stamp_path)
    if "g1500" not in stamps:
        quote = fetch_gift_quote()
        if quote:
            stamps["g1500"] = quote
            _save_stamps(stamps, stamp_path)

    nifty = (nifty_prints(kite, now) if kite is not None
             else {"day_open": None, "p1400": None, "p1500": None,
                   "error": "kite not authenticated"})
    skew = (chain_skew(kite, today) if kite is not None
            else {"error": "kite not authenticated"})
    row = build_row(today, now, stamps, nifty, skew)

    with _log_lock:
        if _first_logged(today, log_path):
            return {"logged": False, "reason": "already logged"}
        try:
            with log_path.open("a") as fh:
                fh.write(json.dumps(row) + "\n")
        except OSError:
            log.warning("Snapshot log unwritable at %s", log_path)
            return {"logged": False, "reason": "log unwritable", "row": row}
    log.info("15:00 snapshot logged for %s (divergence flag=%s, S=%s)",
             today, row["divergence"]["flag"], (skew or {}).get("S"))
    return {"logged": True, "row": row}


# --- capture loop ------------------------------------------------------------

async def _log_card() -> None:
    """The decision-time card (tonight.py) rides this loop too: it logs once
    from 15:05 and, because the settled read is deterministic from Kite
    history, keeps retrying on the loop's later ticks if Kite was not logged
    in at the time. Its own file check makes the repeat calls free."""
    try:
        from app.closing import tonight
        from app.kite.client import kite_service
        kite = kite_service.kite if kite_service.is_authenticated else None
        await asyncio.to_thread(tonight.capture, kite)
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("Card capture error: %s", exc)


async def capture_loop(poll_s: float = 30.0) -> None:
    """Backend-lifetime loop: stamp GIFT in both windows, log the row AND
    the decision-time card from 15:05, and quote the card's contract for the
    10:45 shadow exit (15:05 entry; 09:50 and 10:45 next morning). Sleeps
    long outside the two active bands (the card still gets a retry on each
    long tick). Never raises."""
    while True:
        try:
            now = datetime.now(IST)
            t = now.time()
            if not mcal.is_trading_day(now.date()):
                await asyncio.sleep(1800)
                continue
            from app.closing import shadow_exit
            from app.kite.client import kite_service
            kite = kite_service.kite if kite_service.is_authenticated else None
            # Two active bands. Morning: real quotes of last night's contract
            # for the 10:45 shadow exit (shadow_exit.py). Afternoon: GIFT
            # stamps, the card's entry quote, the 15:05 row and the card log.
            morning = dtime(9, 48) <= t <= dtime(10, 52)
            afternoon = dtime(13, 58) <= t <= dtime(15, 20)
            if not (morning or afternoon):
                if t > dtime(15, 20):
                    await _log_card()
                nxt = next((datetime.combine(now.date(), hm, tzinfo=IST)
                            for hm in (dtime(9, 48), dtime(13, 58))
                            if datetime.combine(now.date(), hm, tzinfo=IST) > now), None)
                wait = (nxt - now).total_seconds() if nxt else 1800
                await asyncio.sleep(min(1800 if nxt is None else 900, max(poll_s, wait)))
                continue
            if morning:
                await asyncio.to_thread(shadow_exit.capture_quote, kite, now)
                await asyncio.sleep(poll_s)
                continue
            await asyncio.to_thread(stamp_gift, now)
            await asyncio.to_thread(shadow_exit.capture_quote, kite, now)
            if ROW_FROM <= t <= ROW_UNTIL:
                await asyncio.to_thread(run_snapshot, kite, now)
                await _log_card()
            await asyncio.sleep(poll_s)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("Snapshot capture loop error: %s", exc)
            await asyncio.sleep(poll_s)
