#!/usr/bin/env python3
"""Build the training dataset: one row per graded signal card.

Joins the three evidence layers into a flat table:
  1. the card itself (signal archive) — score components, regime read, refs;
  2. the outcome (paper book, matched on trade.signal_id; live-book rows fill
     gaps; hollow shadow rows and quarantined fills never label a card) — the
     book's realized_pnl is GROSS of charges (it equals (exit - entry) x
     quantity on every clean fill, verified 13-Oct-2026), so label_fields()
     nets it with the book's own Zerodha schedule (backend/app/paper/
     charges.py, imported read-only) before deriving label_win and net_R;
  3. the at-birth market state (recorder `card_birth` snapshot, fallback to
     nearest periodic snapshot ≤ created_at) — chain/PCR/OI, indicators, VIX.

LEAKAGE RULE: every feature must be knowable at created_at. Nothing from
after the card's birth ever enters a feature column.

Outputs (recorder/data/):
  dataset.csv          — training rows (older than HOLDOUT_DAYS)
  dataset_holdout.csv  — locked final holdout (newest HOLDOUT_DAYS of cards).
                         Experiments must NEVER read this file; only
                         `experiment/harness.py --final` may, once.

Run: backend/.venv/bin/python recorder/dataset.py
Safe any time — with no graded fills yet it just reports what's missing.
"""
from __future__ import annotations

import bisect
import importlib.util
import json
import re
import sqlite3
import sys
import time
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
DATA = Path(__file__).resolve().parent / "data"
DB = DATA / "tradewell_history.db"

IST = timezone(timedelta(hours=5, minutes=30))
HOLDOUT_DAYS = 28
CLOSED_STATUSES = {"exited", "closed", "stopped", "target", "auto_closed"}


def log(msg: str) -> None:
    print(msg, flush=True)


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(name).lower()).strip("_")


def load_jsonl_final(path: Path) -> dict[str, dict]:
    """Archive semantics: last line per card id wins (final state)."""
    final: dict[str, dict] = {}
    if not path.exists():
        return final
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        # Archive lines wrap the card: {"archived_at": ..., "card": {...}}
        card = row.get("card") if isinstance(row.get("card"), dict) else row
        cid = card.get("id")
        if cid:
            final[str(cid)] = card
    return final


def load_trades(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text())
    except json.JSONDecodeError:
        return []
    if isinstance(payload, list):
        return [t for t in payload if isinstance(t, dict)]
    if isinstance(payload, dict):
        for v in payload.values():
            if isinstance(v, list) and (not v or isinstance(v[0], dict)):
                return [t for t in v if isinstance(t, dict)]
    return []


def _load_excluded() -> set[str]:
    p = DATA / "journal_overrides.json"
    try:
        return {e["id"] for e in json.loads(p.read_text()).get("exclude", [])}
    except Exception:
        return set()


_EXCLUDED_IDS = _load_excluded()


def _off_session_exit(t: dict) -> bool:
    """True when the exit stamp falls outside Mon-Fri 09:15-15:45 IST. The
    paper monitor keeps running off-session on frozen quotes — two positional
    'invalidations' fired at 09:00 pre-open (17-Aug, 21-Aug) priced on stale
    premiums. Those exits are fiction; labels must never learn from them."""
    ts = t.get("exited_at")
    if not isinstance(ts, (int, float)):
        return False
    dt = datetime.fromtimestamp(ts, IST)
    m = dt.hour * 60 + dt.minute
    return dt.weekday() >= 5 or not (9 * 60 + 15 <= m <= 15 * 60 + 45)


def _is_hollow(t: dict) -> bool:
    """Shadow rows — vetoed-card shadows and the R&D policy twins (notes start
    with "hollow:") — never label a card. Found 13-Oct: an R&D twin shares its
    real fill's signal_id and closes on its own rule, so without this guard it
    became the card's label whenever the real fill was still open or
    quarantined (PROGRAM.md, DATA INTEGRITY 13-Oct)."""
    return str(t.get("notes") or "").startswith("hollow")


# ---------------------------------------------------------------------------
# Charges: the paper book's own Zerodha schedule, imported READ-ONLY by file
# path (no app package import, no side effects). The book's realized_pnl is
# gross of charges; every label below is net (DATA INTEGRITY 13-Oct).
# ---------------------------------------------------------------------------

def _charges_mirror(entry: float, exit_premium: float, qty: int, legs: int = 2) -> float:
    """Frozen mirror of backend/app/paper/charges.py (rates as of 13-Oct-2026)
    — the fallback if the file ever moves; cross-checked at import."""
    if qty <= 0 or entry <= 0:
        return 0.0
    buy, sell = entry * qty, (exit_premium * qty if legs == 2 else 0.0)
    turnover = buy + sell
    brokerage = 20.0 * legs
    stt = sell * 0.0015
    exch, ipft, sebi = turnover * 0.0003503, turnover * 0.000005, turnover * 0.000001
    gst = (brokerage + exch + ipft + sebi) * 0.18
    stamp = buy * 0.00003
    return round(brokerage + stt + exch + ipft + sebi + gst + stamp, 2)


def _load_charges():
    p = BACKEND / "app" / "paper" / "charges.py"
    try:
        spec = importlib.util.spec_from_file_location("tw_paper_charges", p)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        fn = mod.charges
        if abs(fn(100.0, 110.0, 65) - _charges_mirror(100.0, 110.0, 65)) > 0.02:
            log("WARN dataset: the backend's charge schedule differs from the "
                "frozen mirror — using the backend's (update the mirror)")
        return fn, "backend/app/paper/charges.py (read-only import)"
    except Exception as exc:
        log(f"WARN dataset: could not import backend charges ({exc!r}); "
            "using the frozen mirror")
        return _charges_mirror, "mirror of backend/app/paper/charges.py frozen 13-Oct-2026"


_charges, CHARGES_SOURCE = _load_charges()


def trade_charges(trade: dict) -> float | None:
    """Round-trip charges of a closed fill under the book's own schedule, or
    None when the fill lacks entry / exit / quantity (then it has no
    charges-net label at all). `quantity` is the traded size — it equals
    initial_quantity on every clean fill and is the size for which the
    stored identity realized_pnl == (exit - entry) x quantity holds."""
    entry, exit_p, qty = (trade.get("entry_premium"), trade.get("exit_premium"),
                          trade.get("quantity"))
    if not all(isinstance(v, (int, float)) for v in (entry, exit_p, qty)):
        return None
    if qty <= 0 or entry <= 0:
        return None
    return float(_charges(float(entry), float(exit_p), int(qty), 2))


# ---------------------------------------------------------------------------
# Restart catch-up exits (DATA INTEGRITY 13-Oct). The paper monitor closes a
# position on the FIRST quote after an outage without marking it: the stop /
# target / invalidation crossed during the gap fires on that first tick and
# is priced there. Five positional exits sat on the exact minute the backend
# came back (31-Aug 11:58, 01-Oct 13:49 x2, 09-Oct 10:39 x2). Mechanical
# flag: the exit stamp lies within FEED_GAP_EXIT_WINDOW_S of the first
# same-day quote of the contract that ends a gap > FEED_GAP_S in that
# contract's own snapshots — recorder 60-second chain first, the condor
# module's 5-minute store (read-only) when the 60-second chain has no row for
# the token that day. A gap ending in the session's first quote window
# (09:15-09:20) is the legitimate overnight open and never counts.
# ---------------------------------------------------------------------------
FEED_GAP_S = 600                  # > 10 min without a quote for the contract
FEED_GAP_EXIT_WINDOW_S = 120      # exit within 2 min of the first post-gap quote
FIRST_QUOTE_WINDOW_MIN = (9 * 60 + 15, 9 * 60 + 20)   # IST minutes, [open, open + 5)
_CONDOR_DB = BACKEND / ".condor_chain.db"
_rec_store: sqlite3.Connection | bool | None = None
_condor_store: sqlite3.Connection | bool | None = None
_day_samples_cache: dict[tuple[str, int], tuple[dict[int, list[int]], list[int]]] = {}
_feed_gap_cache: dict[str, dict] = {}


def _open_ro(path: Path, table: str) -> sqlite3.Connection | bool:
    """sqlite mode=ro — nothing under backend/ is ever written from here."""
    if not path.exists():
        return False
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        if not _table_exists(con, table):
            con.close()
            return False
        return con
    except sqlite3.Error:
        return False


def _chain_day_samples(symbol: str, day_lo: int) -> tuple[dict[int, list[int]], list[int]]:
    """One pass over the recorder's 60-second chain snapshots of (symbol, IST
    day): token -> sorted snapshot times at which the contract had LTP > 0,
    plus every snapshot time (the symbol-wide stream, for the diagnostic)."""
    global _rec_store
    key = (symbol, day_lo)
    hit = _day_samples_cache.get(key)
    if hit is not None:
        return hit
    by_token: dict[int, list[int]] = {}
    every: list[int] = []
    if _rec_store is None:
        _rec_store = _open_ro(DB, "snapshots")
    if _rec_store:
        lo = datetime.fromtimestamp(day_lo, timezone.utc).isoformat(timespec="seconds")
        hi = datetime.fromtimestamp(day_lo + 86400, timezone.utc).isoformat(timespec="seconds")
        for ts_utc, blob in _rec_store.execute(
            "SELECT ts_utc, payload FROM snapshots WHERE endpoint='chain' AND symbol=? "
            "AND ts_utc>=? AND ts_utc<? ORDER BY ts_utc", (symbol, lo, hi)
        ):
            try:
                ts = int(datetime.fromisoformat(ts_utc).timestamp())
            except ValueError:
                continue
            chain = decompress(blob)
            if not isinstance(chain, dict):
                continue
            every.append(ts)
            for r in chain.get("rows") or []:
                if not isinstance(r, dict):
                    continue
                for tok_key, ltp_key in (("ce_token", "ce_ltp"), ("pe_token", "pe_ltp")):
                    tok, v = r.get(tok_key), r.get(ltp_key)
                    if tok is not None and isinstance(v, (int, float)) and v > 0:
                        by_token.setdefault(int(tok), []).append(ts)
    _day_samples_cache[key] = (by_token, every)
    return by_token, every


def _condor_day_samples(symbol: str, token: int, day_lo: int) -> tuple[list[int], list[int]]:
    """The contract's 5-minute samples (LTP > 0) and the symbol-wide snapshot
    times in backend/.condor_chain.db for one IST day — read-only."""
    global _condor_store
    if _condor_store is None:
        _condor_store = _open_ro(_CONDOR_DB, "chain_snap")
    if not _condor_store:
        return [], []
    hi = day_lo + 86400
    tok = [r[0] for r in _condor_store.execute(
        "SELECT ts FROM chain_snap WHERE token=? AND ts>=? AND ts<? AND ltp>0 ORDER BY ts",
        (token, day_lo, hi))]
    every = [r[0] for r in _condor_store.execute(
        "SELECT DISTINCT ts FROM chain_snap WHERE symbol=? AND ts>=? AND ts<? ORDER BY ts",
        (symbol, day_lo, hi))]
    return tok, every


def _gap_rule(samples: list[int], exit_ts: int, open_ts: int) -> dict:
    """The rule on one sorted same-day sample list. b = the first sample at or
    after exit - 2 min; the exit must lie within 2 min of b; the gap is b
    minus the previous same-day sample (clamped to the 09:15 open — pre-open
    frozen snapshots are not quotes) or minus the open when b is the day's
    first sample."""
    if not samples:
        return {"verifiable": False, "flagged": False}
    i = bisect.bisect_left(samples, exit_ts - FEED_GAP_EXIT_WINDOW_S)
    if i >= len(samples) or samples[i] > exit_ts + FEED_GAP_EXIT_WINDOW_S:
        return {"verifiable": True, "flagged": False}
    b = samples[i]
    a = max(samples[i - 1], open_ts) if i > 0 else open_ts
    bm = datetime.fromtimestamp(b, IST)
    first_quote = FIRST_QUOTE_WINDOW_MIN[0] <= bm.hour * 60 + bm.minute < FIRST_QUOTE_WINDOW_MIN[1]
    return {"verifiable": True, "flagged": (b - a) > FEED_GAP_S and not first_quote,
            "gap_s": b - a, "gap_from": a, "first_quote_at": b,
            "exit_minus_quote_s": exit_ts - b, "first_quote_window": first_quote}


def feed_gap_check(trade: dict) -> dict:
    """Full verdict for one closed fill (cached per trade id): flagged,
    verifiable, source ("chain_60s" | "condor_5m"), the gap and — when
    flagged — whether the SYMBOL-wide snapshot stream shows the same gap. A
    contract-only gap means the strike left the recorded window rather than
    a restart: the one known false-positive mode, logged as a warning."""
    tid = str(trade.get("id"))
    hit = _feed_gap_cache.get(tid)
    if hit is not None:
        return hit
    res: dict = {"flagged": False, "verifiable": False, "source": None}
    exit_ts, token, symbol = trade.get("exited_at"), trade.get("token"), trade.get("symbol")
    try:
        token = int(token)
    except (TypeError, ValueError):
        token = None
    if isinstance(exit_ts, (int, float)) and token is not None and symbol:
        exit_ts = int(exit_ts)
        day_lo = _day_start(exit_ts)
        open_ts = day_lo + 9 * 3600 + 15 * 60
        by_token, wide = _chain_day_samples(str(symbol), day_lo)
        samples, source = by_token.get(token, []), "chain_60s"
        if not samples:
            samples, wide = _condor_day_samples(str(symbol), token, day_lo)
            source = "condor_5m"
        if samples:
            res.update(_gap_rule(samples, exit_ts, open_ts))
            res["source"] = source
            if res["flagged"]:
                res["symbol_wide"] = bool(_gap_rule(wide, exit_ts, open_ts)["flagged"])
    _feed_gap_cache[tid] = res
    return res


def exit_after_feed_gap(trade: dict) -> bool:
    """True when the fill's exit was stamped at the first quote after a > 10
    minute feed gap — a restart catch-up exit. Quarantined like off-session
    exits: never a training label, never a book number."""
    return bool(feed_gap_check(trade)["flagged"])


def outcome_by_signal(trades: list[dict]) -> dict[str, dict]:
    """signal_id → closed trade row (first terminal match wins). Hollow shadow
    rows, journal-overridden rows, off-session exits and restart catch-up
    exits never qualify — the quarantine gates live here so every consumer
    of the dataset inherits them."""
    out: dict[str, dict] = {}
    for t in trades:
        sid = t.get("signal_id")
        if not sid or str(sid) in out:
            continue
        if _is_hollow(t):
            continue
        # Human-ruled-out rows never become training labels — see
        # data/journal_overrides.json.
        if t.get("id") in _EXCLUDED_IDS:
            continue
        if _off_session_exit(t):
            continue
        status = str(t.get("status", "")).lower()
        closed = (
            any(s in status for s in CLOSED_STATUSES)
            or t.get("exit_premium") is not None
        )
        if closed:
            if exit_after_feed_gap(t):
                continue
            out[str(sid)] = t
    return out


_SL_RE = re.compile(r"SL\s*₹\s*([0-9]+(?:\.[0-9]+)?)")


def initial_stop(trade: dict, card: dict | None) -> float | None:
    """The stop the trade was BORN with. `stop_loss` on the row is the FINAL
    stop — the early-derisk and quick-target rules ratchet it to entry or
    above, which zeroed the R denominator on 66/126 fills (found 08-Oct: the
    harness saw 36 'usable' rows out of 70). Order: the fill-repriced SL in
    the `repriced` event, then the card's planned premium_sl, then the row's
    stop only if it still sits below entry."""
    for e in trade.get("events") or []:
        if isinstance(e, dict) and e.get("kind") == "repriced":
            m = _SL_RE.search(str(e.get("note", "")))
            if m:
                return float(m.group(1))
    if card and isinstance(card.get("premium_sl"), (int, float)):
        return float(card["premium_sl"])
    s, entry = trade.get("stop_loss"), trade.get("entry_premium")
    if isinstance(s, (int, float)) and isinstance(entry, (int, float)) and s < entry:
        return float(s)
    return None


def label_fields(trade: dict, card: dict | None = None) -> dict:
    """Outcome columns, every one derived from the CHARGES-NET P&L. The book's
    realized_pnl equals (exit - entry) x quantity on every clean fill
    (verified 13-Oct-2026: 483/484 closed paper rows, all closed live rows) —
    slippage-adjusted but gross of charges — so net_pnl = realized_pnl minus
    the book's own Zerodha round-trip charges (trade_charges). A fill whose
    charges cannot be computed gets no label rather than a gross one. The
    `charges` column is an outcome column: never a feature."""
    gross = trade.get("realized_pnl")
    if not isinstance(gross, (int, float)):
        return {}
    fees = trade_charges(trade)
    if fees is None:
        return {}
    pnl = round(float(gross) - fees, 2)
    out = {"label_win": int(pnl > 0), "net_pnl": pnl, "charges": fees}
    # Second label: success under the bank-at-+5% exit policy (the counter-
    # factual currently leading the exit A/B). If that policy is adopted, the
    # model must be trained against the world it will actually operate in —
    # not the written-rules world being replaced. First-touch approximation
    # from MFE, same as the nightly counterfactual.
    entry_p, mfe = trade.get("entry_premium"), trade.get("mfe_premium")
    if isinstance(entry_p, (int, float)) and isinstance(mfe, (int, float)) and entry_p:
        out["label_win_bankcut"] = int((mfe - entry_p) / entry_p >= 0.05)
    entry = trade.get("entry_premium")
    stop = initial_stop(trade, card)
    qty = trade.get("initial_quantity") or trade.get("quantity")
    if (
        isinstance(entry, (int, float)) and isinstance(stop, (int, float))
        and isinstance(qty, (int, float)) and qty > 0 and entry > stop
    ):
        out["net_R"] = round(pnl / ((entry - stop) * qty), 4)
    return out


def card_features(card: dict) -> dict:
    f: dict = {
        "card_id": card.get("id"),
        "symbol": card.get("symbol"),
        "mode": card.get("mode"),
        "direction": card.get("direction"),
        "moneyness": card.get("moneyness"),
        "confidence": card.get("confidence"),
        "risk_reward": card.get("risk_reward"),
    }
    created = card.get("created_at")
    if isinstance(created, (int, float)):
        f["created_at"] = int(created)
        dt = datetime.fromtimestamp(created, IST)
        f["hour_ist"] = dt.hour + dt.minute / 60.0
        # Two independent audits (owner's 52-session study + our fills) found
        # 10:30-12:00 IST toxic for buyers — encode it explicitly so a small-n
        # model doesn't have to rediscover a nonlinear hour effect.
        f["toxic_window"] = int(10.5 <= f["hour_ist"] < 12.0)
        f["dow"] = dt.weekday()
        f["month"] = dt.strftime("%Y-%m")
        # Days to expiry at birth + expiry-day flag (pro-trader interview,
        # 10-Oct: "expiry-day buying is a double-edged sword"). Pre-registered
        # as features; the model decides if they matter.
        exp = card.get("expiry")
        if isinstance(exp, str) and len(exp) >= 10:
            try:
                ed = datetime.strptime(exp[:10], "%Y-%m-%d").date()
                f["dte_at_entry"] = max(0, (ed - dt.date()).days)
                f["is_expiry_day"] = int(ed == dt.date())
            except ValueError:
                pass
    # card.event_note carries two different things joined by " · ": the
    # engine's scheduled-event warning (RBI/budget/US releases — the trader's
    # "wait for the IV collapse, then decide") and the late-day positional
    # overnight-gap warning. Split them: conflating the two would make
    # event_flag fire on every evening positional (all 13 notes so far).
    segs = [x.strip() for x in str(card.get("event_note") or "").split("·")]
    segs = [x for x in segs if x]
    overnight = [x for x in segs if x.lower().startswith("late-day positional")]
    f["holds_overnight"] = int(bool(overnight))
    f["event_flag"] = int(len(segs) > len(overnight))
    score = card.get("score") or {}
    if isinstance(score, dict):
        f["score_total"] = score.get("total")
        for comp in score.get("components") or []:
            if isinstance(comp, dict) and comp.get("name"):
                f[f"comp_{slug(comp['name'])}"] = comp.get("points")
    ref_spot = card.get("ref_spot")
    inval = card.get("invalidation_level")
    if isinstance(ref_spot, (int, float)) and isinstance(inval, (int, float)) and ref_spot:
        f["inval_dist_pct"] = round(abs(ref_spot - inval) / ref_spot * 100, 4)
    # Tape at birth — recorded on cards since ~11-Aug (engine stamps it);
    # older cards get the back-computed value in main().
    for k in ("tape_state", "tape_resolved_pct"):
        if card.get(k) is not None:
            f[k] = card[k]
    if isinstance(card.get("tape_aligned"), bool):
        f["tape_aligned"] = int(card["tape_aligned"])
    # macd_aligned (upstream 30-Sep): the one MACD variant that survived sizing.
    if isinstance(card.get("macd_aligned"), bool):
        f["macd_aligned"] = int(card["macd_aligned"])
    return f


def chain_features(chain: dict | None) -> dict:
    if not isinstance(chain, dict):
        return {}
    f: dict = {"pcr": chain.get("pcr")}
    rows = [r for r in chain.get("rows") or [] if isinstance(r, dict)]
    atm = chain.get("atm_strike")
    ce_oi = sum(r.get("ce_oi") or 0 for r in rows)
    pe_oi = sum(r.get("pe_oi") or 0 for r in rows)
    ce_chg = sum(r.get("ce_oi_change") or 0 for r in rows)
    pe_chg = sum(r.get("pe_oi_change") or 0 for r in rows)
    total_oi = ce_oi + pe_oi
    if total_oi:
        f["oi_change_skew"] = round((pe_chg - ce_chg) / total_oi, 6)
    if isinstance(atm, (int, float)):
        arow = next((r for r in rows if r.get("strike") == atm), None)
        if arow:
            a_ce, a_pe = arow.get("ce_oi"), arow.get("pe_oi")
            if a_ce and a_pe:
                f["atm_pe_ce_oi"] = round(a_pe / a_ce, 4)
            f["atm_iv_ce"] = arow.get("ce_iv")
            f["atm_iv_pe"] = arow.get("pe_iv")
    return f


def indicator_features(ind: dict | None, ref_spot) -> dict:
    if not isinstance(ind, dict):
        return {}
    f: dict = {}
    for k in ("rsi", "adx", "atr", "supertrend"):
        v = ind.get(k)
        if isinstance(v, (int, float)):
            f[k] = v
    vwap = ind.get("vwap")
    ema20 = ind.get("ema20")
    if isinstance(ref_spot, (int, float)) and ref_spot:
        if isinstance(vwap, (int, float)):
            f["vwap_dist_pct"] = round((ref_spot - vwap) / ref_spot * 100, 4)
        if isinstance(ema20, (int, float)):
            f["ema20_dist_pct"] = round((ref_spot - ema20) / ref_spot * 100, 4)
        if isinstance(f.get("atr"), (int, float)):
            f["atr_pct"] = round(f["atr"] / ref_spot * 100, 4)
    return f


def pulse_features(pulse: dict | None) -> dict:
    if not isinstance(pulse, dict):
        return {}
    f: dict = {}
    for k, v in pulse.items():
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            f[f"pulse_{slug(k)}"] = v
        if len(f) >= 12:  # cap width; raw payload stays in the DB anyway
            break
    return f


def decompress(blob: bytes) -> dict | None:
    try:
        return json.loads(zlib.decompress(blob))
    except Exception:
        return None


def birth_snapshots(db: sqlite3.Connection) -> dict[str, dict]:
    """card_id → decoded card_birth payload."""
    out: dict[str, dict] = {}
    if not _table_exists(db, "snapshots"):
        return out
    for (blob,) in db.execute(
        "SELECT payload FROM snapshots WHERE endpoint='card_birth'"
    ):
        payload = decompress(blob)
        if not isinstance(payload, dict):
            continue
        resp = payload.get("response") or {}
        card = resp.get("signal") if isinstance(resp, dict) else None
        cid = card.get("id") if isinstance(card, dict) else None
        if cid:
            out[str(cid)] = payload
    return out


def nearest_periodic(db: sqlite3.Connection, endpoint: str, symbol: str,
                     ts: int, tolerance_s: int = 180) -> dict | None:
    if not _table_exists(db, "snapshots"):
        return None
    when = datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")
    row = db.execute(
        "SELECT payload, ts_utc FROM snapshots WHERE endpoint=? AND symbol=? "
        "AND ts_utc<=? ORDER BY ts_utc DESC LIMIT 1",
        (endpoint, symbol, when),
    ).fetchone()
    if not row:
        return None
    payload, ts_utc = row
    try:
        age = ts - datetime.fromisoformat(ts_utc).timestamp()
    except ValueError:
        return None
    if age > tolerance_s:
        return None
    return decompress(payload)


def htf_trend(db: sqlite3.Connection, created: int) -> dict:
    """Higher-timeframe bias the pro trader builds from daily/weekly
    structure: NIFTY's 20-session return as of the session BEFORE the card
    (today's daily bar is excluded — it closes after the card, leakage)."""
    if not _table_exists(db, "candles"):
        return {}
    day_start = ((int(created) + 19800) // 86400) * 86400 - 19800
    rows = db.execute(
        "SELECT close FROM candles WHERE symbol='NIFTY_SPOT' AND tf='day' "
        "AND bar_ts < ? ORDER BY bar_ts DESC LIMIT 21", (day_start,)).fetchall()
    if len(rows) < 21 or not rows[20][0]:
        return {}
    return {"htf_ret_20d_pct": round(100 * (rows[0][0] - rows[20][0]) / rows[20][0], 3)}


def _day_start(ts: int) -> int:
    """IST midnight of the session containing ts (daily bar_ts convention)."""
    return ((int(ts) + 19800) // 86400) * 86400 - 19800


def vix_close_before(db: sqlite3.Connection, ts: int) -> float | None:
    """India VIX close of the PREVIOUS session — the number a trader knows
    before the open. Fixed 10-Oct: the old bar_ts<=ts matched the same day's
    daily bar (stamped at midnight), i.e. a morning card saw that evening's
    close. Pairs with the climatology table's prev-close convention."""
    if not _table_exists(db, "candles"):
        return None
    row = db.execute(
        "SELECT close FROM candles WHERE symbol='INDIAVIX' AND tf='day' "
        "AND bar_ts<? ORDER BY bar_ts DESC LIMIT 1", (_day_start(ts),),
    ).fetchone()
    return row[0] if row else None


_VIX_DB = BACKEND / ".closing_vix.db"
_vix_store: sqlite3.Connection | bool | None = None


def vix_at_birth(created: int) -> float | None:
    """India VIX at the card's birth minute, from the Closing lab's 5-minute
    store (read-only, fail-soft). Same-session bars only, never a stale day."""
    global _vix_store
    if _vix_store is None:
        try:
            _vix_store = sqlite3.connect(f"file:{_VIX_DB}?mode=ro", uri=True)
            _vix_store.execute("SELECT 1 FROM vix LIMIT 1")
        except sqlite3.Error:
            _vix_store = False
    if not _vix_store:
        return None
    row = _vix_store.execute(
        "SELECT close FROM vix WHERE ts<=? AND ts>=? ORDER BY ts DESC LIMIT 1",
        (int(created), _day_start(created))).fetchone()
    return float(row[0]) if row else None


def _contract_row(chain: dict | None, strike, direction: str, token,
                  expiry) -> tuple[dict | None, str | None]:
    """The card's contract row in a chain snapshot, matched by INSTRUMENT
    TOKEN (the row's ce_token / pe_token). The recorder stores the front-week
    chain, so a strike-only match read the WEEKLY contract for a monthly
    positional — 31 of 135 graded cards (found 10-Oct, fixed 13-Oct). The
    fallback is strike + identical chain expiry, used only when the card has
    no token or the snapshot's rows carry none; strike alone never matches.
    Returns (row, how) with how in {"token", "strike_expiry"}."""
    if not isinstance(chain, dict):
        return None, None
    rows = [r for r in chain.get("rows") or [] if isinstance(r, dict)]
    tok_key = "ce_token" if direction == "CE" else "pe_token"
    if token is not None and any(r.get(tok_key) is not None for r in rows):
        return next((r for r in rows if r.get(tok_key) == token), None), "token"
    if expiry and chain.get("expiry") == expiry and isinstance(strike, (int, float)):
        return next((r for r in rows if r.get("strike") == strike), None), "strike_expiry"
    return None, None


_MOM_STATS = {"token": 0, "strike_expiry": 0, "missing": 0}


def premium_momentum(db: sqlite3.Connection, symbol: str, strike,
                     direction, created: int, now_chain: dict | None,
                     token=None, expiry=None) -> dict:
    """AI-trader's 'option premium confirmation' idea, as a FEATURE (never a
    gate here): the card contract's premium change over the ~1-2 minutes
    before birth, from the recorder's 60s chain snapshots. Negative =
    entering while the premium is already falling — the falling knife.
    Three of our first 32 fills never traded one tick positive; this is the
    column aimed at exactly them. The contract is matched by instrument
    token (_contract_row); a monthly contract absent from the front-week
    chain gets NO value — a 1-2 minute momentum cannot be read from the
    condor store's 5-minute cadence, and one column never mixes cadences.
    """
    if direction not in ("CE", "PE"):
        return {}
    key = "ce_ltp" if direction == "CE" else "pe_ltp"
    try:
        token = int(token) if token is not None else None
    except (TypeError, ValueError):
        token = None

    def ltp(chain: dict | None) -> tuple[float | None, str | None]:
        r, how = _contract_row(chain, strike, direction, token, expiry)
        v = r.get(key) if r else None
        return (float(v) if isinstance(v, (int, float)) and v > 0 else None), how

    now_p, how = ltp(now_chain)
    # One polling cycle earlier: nearest snapshot at least ~105s before birth
    # (the now-chain is at most 90s old via birth capture / nearest_periodic,
    # so the two reads can never be the same snapshot).
    prev_p, _ = ltp(nearest_periodic(db, "chain", symbol, created - 105,
                                     tolerance_s=120))
    if now_p is None or prev_p is None:
        _MOM_STATS["missing"] += 1
        return {}
    _MOM_STATS[how or "strike_expiry"] += 1
    return {"premium_mom_pct": round((now_p - prev_p) / prev_p * 100, 3)}


TAPE_SPLITS = (0.35, 0.60)   # FROZEN upstream (signals/service.py::_tape_state)


def tape_from_candles(db: sqlite3.Connection, symbol: str, created: int) -> dict:
    """Back-compute the engine's tape label for cards born before the field
    existed (~11-Aug): the owner's sole surviving filter deserves a column on
    EVERY row. Faithful port of signals/service.py::_tape_state onto our
    recorded 3m bars — resolved = |close - day open| / (day high - day low)
    of today's session strictly before card birth. The 0.35/0.60 splits are
    frozen there and stay frozen here; only closed bars enter
    (bar_ts + 180 <= created), matching the engine's forming-bar drop.
    """
    if not _table_exists(db, "candles"):
        return {}
    day = (int(created) + 19800) // 86400
    day_start = day * 86400 - 19800
    for sym in (symbol, f"{symbol}_FUT"):
        rows = db.execute(
            "SELECT open, high, low, close, bar_ts FROM candles "
            "WHERE symbol=? AND tf='3m' AND bar_ts>=? AND bar_ts+180<=? "
            "ORDER BY bar_ts",
            (sym, day_start, int(created)),
        ).fetchall()
        if len(rows) < 3 or rows[-1][4] - rows[0][4] < 1800:
            continue
        o, c = rows[0][0], rows[-1][3]
        hi = max(r[1] for r in rows)
        lo = min(r[2] for r in rows)
        rng = hi - lo
        if rng <= 0:
            return {}
        sf = abs(c - o) / rng
        state = ("developing" if TAPE_SPLITS[0] < sf < TAPE_SPLITS[1]
                 else "stretched" if sf >= TAPE_SPLITS[1] else "two-way")
        return {"tape_state": state,
                "tape_resolved_pct": round(sf * 100.0, 1),
                "tape_side": "up" if c >= o else "down"}
    return {}


def _table_exists(db: sqlite3.Connection, name: str) -> bool:
    return bool(db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone())


def main() -> int:
    cards = load_jsonl_final(BACKEND / ".signals_archive.jsonl")
    paper = load_trades(BACKEND / ".paper_trades.json")
    live = load_trades(BACKEND / ".trades.json")
    outcomes = outcome_by_signal(paper)
    for sid, t in outcome_by_signal(live).items():   # live rows fill gaps only
        outcomes.setdefault(sid, t)

    log(f"cards in archive: {len(cards)} | closed outcomes matched by signal_id: "
        f"{len(set(cards) & set(outcomes))} | labels net of charges via {CHARGES_SOURCE}")
    # The restart catch-up gate, named fill by fill so the nightly report shows
    # every quarantine and flags a NEW case before anyone writes it into
    # journal_overrides.json by hand.
    clean_closed = [t for t in paper if t.get("exited_at") and not _is_hollow(t)]
    flagged = [t for t in clean_closed if exit_after_feed_gap(t)]
    unverifiable = sum(1 for t in clean_closed if not feed_gap_check(t)["verifiable"])
    log(f"exit_after_feed_gap: {len(flagged)} of {len(clean_closed)} clean closed fills "
        f"quarantined (exit within 2 min of the first quote after a > 10 min feed gap); "
        f"{unverifiable} unverifiable (no snapshot of the contract on the exit day)")
    for t in flagged:
        r = feed_gap_check(t)
        when = datetime.fromtimestamp(int(t["exited_at"]), IST).strftime("%d-%b %H:%M:%S")
        log(f"  {str(t.get('id'))[:12]} {t.get('contract')} {t.get('mode')} exit {when} "
            f"{r['source']} gap {r['gap_s'] // 60} min"
            + ("" if t.get("id") in _EXCLUDED_IDS else
               " — NEW: not yet in journal_overrides.json")
            + ("" if r.get("symbol_wide", True) else
               " !! contract-level gap only (strike left the recorded window?)"))

    if not DB.exists():
        log("recorder DB missing — market-state features will be empty")
        db = sqlite3.connect(":memory:")
    else:
        db = sqlite3.connect(DB)
    births = birth_snapshots(db)

    rows: list[dict] = []
    skipped_no_outcome = skipped_no_label = 0
    tape_both = tape_match = tape_filled = 0
    for cid, card in cards.items():
        trade = outcomes.get(cid)
        if trade is None:
            skipped_no_outcome += 1
            continue
        labels = label_fields(trade, card)
        if "label_win" not in labels:
            skipped_no_label += 1
            continue
        f = card_features(card)
        birth = births.get(cid)
        created = f.get("created_at") or int(time.time())
        sym = f.get("symbol") or ""
        chain = (birth or {}).get("chain") or nearest_periodic(db, "chain", sym, created)
        ind = (birth or {}).get("indicators") or nearest_periodic(db, "indicators", sym, created)
        pulse = (birth or {}).get("pulse") or nearest_periodic(db, "pulse", sym, created)
        f.update(chain_features(chain))
        f.update(indicator_features(ind, card.get("ref_spot")))
        f.update(pulse_features(pulse))
        adv = (birth or {}).get("advocate") or {}
        if isinstance(adv.get("counter_strength"), (int, float)):
            f["advocate_counter"] = adv["counter_strength"]
        f.update(premium_momentum(db, sym, card.get("strike"),
                                  f.get("direction"), created, chain,
                                  card.get("token"), card.get("expiry")))
        # Tape: prefer the engine-recorded value; back-compute only the gap.
        # Where both exist we count agreement — the proof the back-fill is a
        # faithful port and pre-11-Aug rows can be trusted.
        calc = tape_from_candles(db, sym, created)
        if calc:
            if f.get("tape_state"):
                tape_both += 1
                tape_match += int(calc["tape_state"] == f["tape_state"])
            else:
                tape_filled += 1
                f["tape_state"] = calc["tape_state"]
                f["tape_resolved_pct"] = calc["tape_resolved_pct"]
                f["tape_aligned"] = int(
                    (calc["tape_side"] == "up") == (f.get("direction") == "CE"))
        vix = vix_close_before(db, created)
        if vix is not None:
            f["vix_close"] = vix
        vb = vix_at_birth(created)
        if vb is not None:
            f["vix_at_birth"] = vb
        f.update(htf_trend(db, created))
        f["had_birth_snapshot"] = int(birth is not None)
        f.update(labels)
        rows.append(f)

    log(f"premium_mom_pct: {_MOM_STATS['token']} rows matched by token, "
        f"{_MOM_STATS['strike_expiry']} by strike+expiry fallback, "
        f"{_MOM_STATS['missing']} missing (contract not in the front-week chain)")

    if not rows:
        log("no graded rows yet — dataset not written "
            f"(no-outcome: {skipped_no_outcome}, no-label: {skipped_no_label})")
        return 0

    if tape_both:
        log(f"tape back-fill check: computed matches recorded on "
            f"{tape_match}/{tape_both} cards; gaps filled: {tape_filled}")

    import pandas as pd  # backend venv dependency
    df = pd.DataFrame(rows).sort_values("created_at")
    span_days = (df["created_at"].max() - df["created_at"].min()) / 86400
    if span_days >= 2 * HOLDOUT_DAYS:
        # Mature dataset: newest 28 days stay locked.
        cutoff = (datetime.now(IST) - timedelta(days=HOLDOUT_DAYS)).timestamp()
        train, hold = df[df["created_at"] < cutoff], df[df["created_at"] >= cutoff]
    else:
        # Young dataset: a fixed 28-day window would lock EVERYTHING and
        # starve the nightly learning pass for a month. Lock the newest third
        # (min 3 rows) instead — still a genuinely unseen tail.
        k = max(3, round(len(df) * 0.33))
        hold, train = df.tail(k), df.head(len(df) - k)
    DATA.mkdir(parents=True, exist_ok=True)
    train.to_csv(DATA / "dataset.csv", index=False)
    hold.to_csv(DATA / "dataset_holdout.csv", index=False)
    log(f"wrote dataset.csv: {len(train)} rows | dataset_holdout.csv (LOCKED): "
        f"{len(hold)} rows | features: {len(df.columns)} | "
        f"birth-snapshot coverage: {df['had_birth_snapshot'].mean():.0%}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
