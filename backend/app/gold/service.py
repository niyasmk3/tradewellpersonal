"""Orchestration for the Gold tab: MCX sync, XAUUSD backfill, the ledger
rebuild, and the live card read.

Two venues, one metal, by design (spec §1): MCX GOLDM is the legal Indian
venue today (residents with the commodity segment; NRIs cannot trade MCX
commodity derivatives at all), XAUUSD spot is the venue an NRI can legally
trade from abroad via regulated brokers — and offshore forex platforms are a
FEMA violation for residents. Rules are graded on MCX; XAUUSD's job here is
the independent climatology cross-check (and later re-validation).

The ledger is a deterministic cache of raw candles: every analyze rebuilds it
whole through rules.run_ledger — nothing simulated is ever persisted as
independent truth.
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta

from app.gold import climatology, dukascopy, rules, store
from app.gold.rules import IST

log = logging.getLogger("tradewell.gold")

# GOLDM preferred over GOLD: the mini (100g) is the contract the ₹10/point
# sizing is written against, and the two share one tape.
_NAME_PREF = ("GOLDM", "GOLD")
_INTERVAL_3M = "3minute"
_CHUNK_DAYS = 60          # 3minute allows 100 days/request; 60 matches patterns
_CHUNK_SLEEP_S = 0.4      # stay under the historical API's 3 req/s cap
_INTRADAY_LOOKBACK_D = 120   # contract listing limit — Kite has nothing older
_DAY_YEARS = 5
_MIN_SESSIONS = 10

# MCX session: Mon–Fri, 09:00 open, evening driven by London/US. The close is
# SEASONAL — 23:30 IST while the US is on daylight saving, 23:55 in the US
# winter (review catch: the spec's flat 23:30 is only the summer half). The
# cap accepts the winter close; in summer no bars print past 23:27 anyway.
_SESSION_OPEN = (9, 0)
_SESSION_LAST_BAR = (23, 52)   # last 3m bar of the 23:55 winter close


class GoldError(RuntimeError):
    pass


def _in_session(dt: datetime) -> bool:
    if dt.weekday() > 4:
        return False
    return _SESSION_OPEN <= (dt.hour, dt.minute) <= _SESSION_LAST_BAR


# ---- contract resolution -----------------------------------------------------

_contract_cache: dict = {}


def resolve_contract(kite) -> dict:
    """Nearest-expiry GOLDM (fallback GOLD) future on MCX. Cached per IST day —
    the instrument dump is ~13MB and the answer changes at most at a roll."""
    today = datetime.now(IST).date()
    if _contract_cache.get("day") == today:
        return _contract_cache["contract"]
    rows = kite.instruments("MCX")
    for name in _NAME_PREF:
        futs = []
        for r in rows:
            if r.get("name") != name or r.get("instrument_type") != "FUT":
                continue
            exp = r.get("expiry")
            if isinstance(exp, datetime):
                exp = exp.date()
            elif isinstance(exp, str):
                try:
                    exp = date.fromisoformat(exp[:10])
                except ValueError:
                    continue
            if exp and exp >= today:
                futs.append((exp, r))
        if futs:
            exp, r = min(futs, key=lambda x: x[0])
            contract = {
                "symbol": name,
                "tradingsymbol": r["tradingsymbol"],
                "token": int(r["instrument_token"]),
                "expiry": exp.isoformat(),
            }
            _contract_cache.update({"day": today, "contract": contract})
            return contract
    raise GoldError("No GOLD/GOLDM future found in the MCX instrument dump")


def _fetch_chunks(kite, token: int, from_dt: datetime, to_dt: datetime,
                  interval: str, continuous: bool = False):
    cursor = from_dt
    while cursor < to_dt:
        chunk_end = min(cursor + timedelta(days=_CHUNK_DAYS), to_dt)
        for d in kite.historical_data(token, cursor, chunk_end, interval,
                                      continuous=continuous):
            yield d
        cursor = chunk_end
        time.sleep(_CHUNK_SLEEP_S)


# ---- sync --------------------------------------------------------------------

def run_sync(kite) -> dict:
    """MCX pull: 3m bars for the current contract (~120 days exist upstream,
    and they EXPIRE with the contract — this store is the archive) plus ~5y of
    continuous day candles for the gap/range climatology. Incremental: resumes
    from the last stored bar's day; the (symbol, timeframe, ts) key dedupes."""
    contract = resolve_contract(kite)
    symbol = contract["symbol"]
    prev = store.get_meta("mcx_contract")
    now = datetime.now()

    start = now - timedelta(days=_INTRADAY_LOOKBACK_D)
    last = store.mcx_last_ts(symbol, "3m")
    if last is not None:
        start = max(start, datetime.fromtimestamp(last).replace(
            hour=0, minute=0, second=0, microsecond=0))
    rows, seen = [], set()

    def _collect_3m(token: int, from_dt: datetime) -> None:
        for d in _fetch_chunks(kite, token, from_dt, now, _INTERVAL_3M):
            dt = d["date"]  # tz-aware IST
            epoch = int(dt.timestamp())
            if epoch in seen or not _in_session(dt):
                continue
            seen.add(epoch)
            rows.append((epoch, d["open"], d["high"], d["low"], d["close"],
                         float(d.get("volume", 0) or 0)))

    if prev and prev != contract["tradingsymbol"]:
        # A roll splices two contracts into one spine. Refetching the overlap
        # days with the NEW token would silently rewrite already-graded
        # sessions with a different contract's tape (review catch) — so the
        # old contract's tail is completed with the OLD token first, and the
        # new contract only writes days the store has never seen. The roll
        # boundary's overnight gap still carries the basis step; logged.
        log.warning("Gold: contract rolled %s -> %s; completing the old tail "
                    "before switching tokens", prev, contract["tradingsymbol"])
        old_token = store.get_meta("mcx_token")
        if old_token:
            try:
                _collect_3m(int(old_token), start)
            except Exception:
                log.warning("Gold: old-contract tail fetch failed (delisted?) "
                            "— its final bars stay as already stored",
                            exc_info=True)
        covered = [x for x in (last, max(seen) if seen else None) if x is not None]
        if covered:
            start = datetime.fromtimestamp(max(covered)).replace(
                hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    _collect_3m(contract["token"], start)
    m3_written = store.upsert_mcx(symbol, "3m", rows)

    # Day candles: best-effort — the 3m rules stand on their own if the
    # continuous pull fails, so a day-candle error must not fail the sync.
    day_written, day_error = 0, None
    try:
        day_start = now - timedelta(days=int(_DAY_YEARS * 365.25))
        last_day = store.mcx_last_ts(symbol, "day")
        if last_day is not None:
            day_start = max(day_start, datetime.fromtimestamp(last_day))
        day_rows = [
            (int(d["date"].timestamp()), d["open"], d["high"], d["low"],
             d["close"], float(d.get("volume", 0) or 0))
            for d in kite.historical_data(contract["token"], day_start, now,
                                          "day", continuous=True)
        ]
        day_written = store.upsert_mcx(symbol, "day", day_rows)
    except Exception as exc:
        day_error = str(exc)
        log.warning("Gold day-candle sync failed (3m analysis continues): %s", exc)

    store.set_meta("mcx_contract", contract["tradingsymbol"])
    store.set_meta("mcx_token", str(contract["token"]))
    store.set_meta("mcx_symbol", symbol)
    store.set_meta("last_mcx_sync", datetime.now().isoformat())
    summary = {
        "contract": contract,
        "m3_bars_written": m3_written,
        "day_bars_written": day_written,
        "m3_bars_total": store.mcx_count(symbol, "3m"),
        "day_bars_total": store.mcx_count(symbol, "day"),
        "day_error": day_error,
    }
    log.info("Gold MCX sync done: %s", summary)
    return summary


def run_xau_sync(years: int = 3) -> dict:
    """Dukascopy backfill — public data, needs no Kite login."""
    return dukascopy.backfill(years=years)


# ---- analysis ----------------------------------------------------------------

def _symbol() -> str:
    return store.get_meta("mcx_symbol") or _NAME_PREF[0]


def run_analysis() -> dict:
    symbol = _symbol()
    m3 = store.load_mcx(symbol, "3m")
    sessions = len({datetime.fromtimestamp(int(t), IST).date()
                    for t in m3["ts"]}) if len(m3) else 0
    if sessions < _MIN_SESSIONS:
        raise GoldError(
            f"Only {sessions} MCX sessions stored — run a sync first "
            f"(need >= {_MIN_SESSIONS})")

    ledger = rules.run_ledger(m3)
    closed = [c for c in ledger if c["state"] == "closed"]

    def _phase_block(phase: str) -> dict:
        mine = [c for c in closed if c["phase"] == phase]
        return {
            "per_rule": {r.key: rules.summarise(
                [c for c in mine if c["rule"] == r.key]) for r in rules.RULES},
            "total": rules.summarise(mine),
        }

    forward_counts = {
        r.key: sum(1 for c in closed
                   if c["phase"] == "forward" and c["rule"] == r.key)
        for r in rules.RULES}

    results = {
        "symbol": symbol,
        "contract": store.get_meta("mcx_contract"),
        "freeze_date": rules.FREEZE_DATE.isoformat(),
        "rules": rules.rule_table(),
        "sizing": {
            "note": ("GOLDM mini (100g): ₹10 per point of the per-10g quote, "
                     "~₹250 round-trip charges assumed. Exits are IDEALISED — "
                     "filled at the level, no slippage model yet; the forward "
                     "book's job includes measuring how honest that is."),
            "rs_per_point": rules.RS_PER_POINT,
            "charges_rt_rs": rules.CHARGES_RT_RS,
        },
        "disclaimer": (
            "Educational shadow research on pre-registered candle rules — paper "
            "everything, advisory nothing, not financial advice and not a "
            "prediction. Backtest rows are hindsight-risked homework; ONLY "
            "forward samples (blind days after the freeze) count toward any "
            "promotion, and early forward results promote or kill nothing — "
            "the first three forward days of this very study inverted the "
            "backtest's rule ranking."),
        "compliance": [
            "NRIs cannot trade MCX commodity derivatives, period. XAUUSD via "
            "regulated brokers becomes legal only with genuine non-resident "
            "status.",
            "Offshore forex/CFD platforms are FEMA violations for Indian "
            "residents.",
            "Advisory-only posture throughout; distributing actionable signals "
            "to others can trip SEBI RA/IA rules.",
        ],
        "data": {
            "symbol": symbol,
            "m3_bars": int(len(m3)),
            "sessions": sessions,
            "day_bars": store.mcx_count(symbol, "day"),
            "xau_bars": store.xau_count(),
            "last_mcx_sync": store.get_meta("last_mcx_sync"),
            "last_xau_sync": store.xau_get_meta("last_xau_sync"),
        },
        "climatology": climatology.build(
            m3, store.load_mcx(symbol, "day"), store.load_xau()),
        "backtest": _phase_block("backtest"),
        "forward": _phase_block("forward"),
        "gates": {
            "min_forward_samples": rules.MIN_FORWARD_SAMPLES,
            "live_money_bar": rules.LIVE_MONEY_BAR,
            "per_rule": {
                k: {"forward_n": n,
                    "verdict_due": max(0, rules.MIN_FORWARD_SAMPLES - n)}
                for k, n in forward_counts.items()},
        },
        "trades": closed,
    }
    store.save_results(results)
    log.info("Gold analysis saved: %s sessions, %s backtest / %s forward trades",
             sessions,
             results["backtest"]["total"].get("n", 0),
             results["forward"]["total"].get("n", 0))
    return results


# ---- live --------------------------------------------------------------------

_LIVE_LOOKBACK_D = 7   # enough for the fetch to always contain the prev session


def split_live_bars(raw: list, now_epoch: int, today) -> tuple:
    """Pure: Kite candle dicts -> (today's CLOSED session bars, prev_close).

    Two review catches live here. Kite's last candle is the FORMING one —
    feeding it to simulate_day let the live path qualify, enter and time-exit
    on partial prints up to 3 minutes before the close the ledger grades (the
    same forming-bar lesson patterns/tendencies and the signal engine already
    record), so any bar whose scheduled close is still in the future is
    dropped. And prev_close comes from THIS fetch — the previous session's
    last in-session close — instead of the local store, which only advances
    on a manual sync and once handed H1 a mid-session print as "yesterday's
    close", flipping the gap's sign against the eventual ledger."""
    bars, prev_close = [], None
    for d in raw:
        dt = d["date"]
        if not _in_session(dt):
            continue
        epoch = int(dt.timestamp())
        if epoch + rules.BAR_SECONDS > now_epoch:
            continue
        if dt.date() == today:
            bars.append({"ts": epoch, "open": d["open"], "high": d["high"],
                         "low": d["low"], "close": d["close"]})
        else:
            prev_close = float(d["close"])
    return bars, prev_close


def live_cards(kite) -> dict:
    """Today's closed 3m bars through the SAME simulate_day, live=True.
    Stateless recomputation each call — restart-proof, no open-position
    persistence, and no dependence on the store being synced."""
    contract = resolve_contract(kite)
    now = datetime.now(IST)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    raw = kite.historical_data(
        contract["token"],
        (day_start - timedelta(days=_LIVE_LOOKBACK_D)).replace(tzinfo=None),
        now.replace(tzinfo=None), _INTERVAL_3M)
    bars, prev_close = split_live_bars(raw, int(now.timestamp()), now.date())
    if prev_close is None:
        # A holiday stretch longer than the lookback — fall back to the store.
        prev_close = store.mcx_prev_close(contract["symbol"], "3m",
                                          int(day_start.timestamp()))
    return {
        "date": now.date().isoformat(),
        "contract": contract,
        "prev_close": prev_close,
        "market_hours": (now.weekday() <= 4
                         and _SESSION_OPEN <= (now.hour, now.minute) <= (23, 55)),
        "cards": rules.simulate_day(bars, prev_close, live=True),
        "fetched_at": int(time.time()),
        "note": "Paper/TRIAL cards — stateless recomputation of today's tape "
                "on every read; nothing here places orders.",
    }


def status() -> dict:
    symbol = _symbol()
    results = store.load_results()
    xau_days = store.xau_day_counts()
    from app.config import get_settings

    return {
        "symbol": symbol,
        "contract": store.get_meta("mcx_contract"),
        # COUNT(*) reads only — this sits on a 20s UI poll.
        "m3_bars": store.mcx_count(symbol, "3m"),
        "day_bars": store.mcx_count(symbol, "day"),
        "xau_bars": store.xau_count(),
        "xau_days_ok": xau_days.get("ok", 0),
        "last_mcx_sync": store.get_meta("last_mcx_sync"),
        "last_xau_sync": store.xau_get_meta("last_xau_sync"),
        "results_available": results is not None,
        "results_generated_at": results.get("generated_at") if results else None,
        "live_enabled": get_settings().gold_live_enabled,
        "freeze_date": rules.FREEZE_DATE.isoformat(),
    }
