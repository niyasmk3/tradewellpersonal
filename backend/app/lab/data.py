"""Strategy Lab data layer: real sources, readiness, quality checks.

Sources (all read-only except the lab's own futures store):
  index_5m     app/patterns store        .patterns_candles.db  (~3y, 5-min)
  fut_candles  THIS module's sync        .lab_futures.db       (Kite, 5-min,
               volume + OI; continuous=1 attempted for expired months and the
               outcome recorded — never assumed)
  chain_hist   app/condor snapshots      .condor_chain.db      (12-Aug-2026 →,
               per-strike ltp/bid/ask/oi/volume/iv, ~5-min cadence)
  vix_daily    app/closing store         .closing_vix.db       (daily bars)

The honesty layer lives here: `readiness()` reports what each dataset
actually covers, `strategy_readiness()` intersects that with a strategy's
declared needs, and the engine refuses spans the data does not cover.
Nothing in this module fabricates a row.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd

from app.market.calendar import IST, is_trading_day

log = logging.getLogger("tradewell.lab.data")

_BASE = Path(__file__).resolve().parents[2]
FUT_DB_PATH = _BASE / ".lab_futures.db"
INDEX_DB_PATH = _BASE / ".patterns_candles.db"
CHAIN_DB_PATH = _BASE / ".condor_chain.db"
VIX_DB_PATH = _BASE / ".closing_vix.db"

CHAIN_ERA_START = int(datetime(2026, 8, 12, 9, 15, tzinfo=IST).timestamp())

# Session maths (5-min bars): 09:15..15:25 bar opens = 75 bars/day.
_BARS_PER_DAY_5M = 75

_FUT_SCHEMA = """
CREATE TABLE IF NOT EXISTS candles (
    ts     INTEGER PRIMARY KEY,   -- epoch seconds (bar open, IST)
    open   REAL NOT NULL,
    high   REAL NOT NULL,
    low    REAL NOT NULL,
    close  REAL NOT NULL,
    volume REAL NOT NULL DEFAULT 0,
    oi     REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _connect(path: Path, schema: Optional[str] = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    if schema:
        conn.executescript(schema)
    return conn


def _in_session(dt: datetime) -> bool:
    if dt.weekday() > 4:
        return False
    hm = (dt.hour, dt.minute)
    return (9, 15) <= hm <= (15, 25)


# ==========================================================================
# Futures store + Kite sync
# ==========================================================================

class FuturesStore:
    def __init__(self, path: Path = FUT_DB_PATH):
        self._path = path

    def _conn(self) -> sqlite3.Connection:
        return _connect(self._path, _FUT_SCHEMA)

    def upsert(self, rows: list) -> int:
        """rows: (ts, o, h, l, c, volume, oi)."""
        if not rows:
            return 0
        with self._conn() as c:
            c.executemany(
                "INSERT OR REPLACE INTO candles (ts, open, high, low, close, "
                "volume, oi) VALUES (?,?,?,?,?,?,?)", rows)
        return len(rows)

    def span(self) -> Optional[tuple]:
        with self._conn() as c:
            row = c.execute("SELECT MIN(ts), MAX(ts), COUNT(*) "
                            "FROM candles").fetchone()
        if not row or row[0] is None:
            return None
        return int(row[0]), int(row[1]), int(row[2])

    def has_oi(self) -> bool:
        with self._conn() as c:
            row = c.execute("SELECT COUNT(*) FROM candles WHERE oi > 0"
                            ).fetchone()
        return bool(row and row[0] > 100)

    def load_frame(self, from_ts: Optional[int] = None,
                   to_ts: Optional[int] = None) -> pd.DataFrame:
        q = ("SELECT ts, open, high, low, close, volume, oi FROM candles ")
        conds, args = [], []
        if from_ts is not None:
            conds.append("ts >= ?")
            args.append(from_ts)
        if to_ts is not None:
            conds.append("ts <= ?")
            args.append(to_ts)
        if conds:
            q += "WHERE " + " AND ".join(conds) + " "
        q += "ORDER BY ts"
        with self._conn() as c:
            return pd.read_sql_query(q, c, params=args or None)

    def set_meta(self, key: str, value: str) -> None:
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)",
                      (key, value))

    def get_meta(self, key: str) -> Optional[str]:
        with self._conn() as c:
            row = c.execute("SELECT value FROM meta WHERE key=?",
                            (key,)).fetchone()
        return row[0] if row else None


futures_store = FuturesStore()

_CHUNK_DAYS = 60
_CHUNK_SLEEP_S = 0.4


def _fetch_chunks(kite, token: int, from_dt: datetime, to_dt: datetime,
                  interval: str, continuous: bool, oi: bool):
    cursor = from_dt
    while cursor < to_dt:
        chunk_end = min(cursor + timedelta(days=_CHUNK_DAYS), to_dt)
        for d in kite.historical_data(token, cursor, chunk_end, interval,
                                      continuous=continuous, oi=oi):
            yield d
        cursor = chunk_end
        time.sleep(_CHUNK_SLEEP_S)


def sync_futures(kite, fut_token: int, years: float = 2.0) -> dict:
    """Incremental sync of NIFTY futures 5-min candles (+OI) into the lab
    store. Tries continuous=1 first so expired contract months are covered
    if the subscription serves them; on failure falls back to the token's
    own (current-contract) history and RECORDS the downgrade. Runs only
    with an authenticated Kite client; caller handles auth errors."""
    now = datetime.now(IST)
    start = now - timedelta(days=int(years * 365.25))
    span = futures_store.span()
    if span:
        resume = datetime.fromtimestamp(span[1], IST).replace(
            hour=0, minute=0, second=0)
        start = max(start, resume)

    mode = "continuous"
    rows: list = []
    try:
        for d in _fetch_chunks(kite, fut_token, start, now, "5minute",
                               continuous=True, oi=True):
            dt = d["date"]
            if not _in_session(dt):
                continue
            rows.append((int(dt.timestamp()), d["open"], d["high"], d["low"],
                         d["close"], float(d.get("volume", 0) or 0),
                         float(d.get("oi", 0) or 0)))
    except Exception as exc:
        log.warning("continuous futures fetch failed (%s); falling back to "
                    "current-contract history", exc)
        mode = "current_contract"
        rows = []
        for d in _fetch_chunks(kite, fut_token, start, now, "5minute",
                               continuous=False, oi=True):
            dt = d["date"]
            if not _in_session(dt):
                continue
            rows.append((int(dt.timestamp()), d["open"], d["high"], d["low"],
                         d["close"], float(d.get("volume", 0) or 0),
                         float(d.get("oi", 0) or 0)))

    written = futures_store.upsert(rows)
    futures_store.set_meta("last_sync", now.isoformat())
    futures_store.set_meta("sync_mode", mode)
    futures_store.set_meta("fut_token", str(fut_token))
    out_span = futures_store.span()
    summary = {
        "written": written,
        "mode": mode,
        "from_ts": out_span[0] if out_span else None,
        "to_ts": out_span[1] if out_span else None,
        "rows_total": out_span[2] if out_span else 0,
    }
    log.info("Lab futures sync: %s", summary)
    return summary


# ==========================================================================
# Index candles (read-only over the patterns DB)
# ==========================================================================

def load_index_frame(from_ts: Optional[int] = None,
                     to_ts: Optional[int] = None) -> pd.DataFrame:
    if not INDEX_DB_PATH.exists():
        return pd.DataFrame(columns=["ts", "open", "high", "low", "close",
                                     "volume"])
    q = "SELECT ts, open, high, low, close, vol_proxy AS volume FROM candles "
    conds, args = [], []
    if from_ts is not None:
        conds.append("ts >= ?")
        args.append(from_ts)
    if to_ts is not None:
        conds.append("ts <= ?")
        args.append(to_ts)
    if conds:
        q += "WHERE " + " AND ".join(conds) + " "
    q += "ORDER BY ts"
    with sqlite3.connect(INDEX_DB_PATH) as c:
        return pd.read_sql_query(q, c, params=args or None)


def index_span() -> Optional[tuple]:
    if not INDEX_DB_PATH.exists():
        return None
    with sqlite3.connect(INDEX_DB_PATH) as c:
        row = c.execute("SELECT MIN(ts), MAX(ts), COUNT(*) FROM candles"
                        ).fetchone()
    if not row or row[0] is None:
        return None
    return int(row[0]), int(row[1]), int(row[2])


def resample(df: pd.DataFrame, tf: str) -> pd.DataFrame:
    """5m -> 15m/30m, aligned to the session open (09:15 IST). Bars are
    labelled by OPEN time, matching every other store in the repo. 'day'
    collapses each IST session to one bar."""
    if df.empty or tf == "5m":
        return df
    step = {"15m": 3, "30m": 6}.get(tf)
    d = df.copy()
    if step is None and tf != "day":
        raise ValueError("unsupported timeframe: %s" % tf)
    ist_ts = d["ts"] + 19800  # IST wall seconds
    day_key = ist_ts // 86400
    if tf == "day":
        group = day_key
    else:
        secs_into_day = ist_ts % 86400
        open_s = 9 * 3600 + 15 * 60
        bucket = (secs_into_day - open_s) // (300 * step)
        group = day_key * 1000 + bucket
    agg = d.groupby(group).agg(
        ts=("ts", "first"), open=("open", "first"), high=("high", "max"),
        low=("low", "min"), close=("close", "last"),
        volume=("volume", "sum"),
        **({"oi": ("oi", "last")} if "oi" in d.columns else {}),
    )
    return agg.reset_index(drop=True).sort_values("ts").reset_index(drop=True)


# ==========================================================================
# Chain history reader (read-only over the condor snapshot DB)
# ==========================================================================

class ChainReader:
    """As-of access to the option-chain archive. Every method takes the
    decision timestamp and only ever reads snapshots at or before it —
    the look-ahead guard for options lives here, in one place."""

    def __init__(self, path: Path = CHAIN_DB_PATH, symbol: str = "NIFTY"):
        self._path = path
        self._symbol = symbol
        self._snap_ts_cache: Optional[list] = None

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path)

    def available(self) -> bool:
        return self._path.exists()

    def span(self) -> Optional[tuple]:
        if not self.available():
            return None
        with self._conn() as c:
            row = c.execute(
                "SELECT MIN(ts), MAX(ts), COUNT(*) FROM chain_snap "
                "WHERE symbol=?", (self._symbol,)).fetchone()
        if not row or row[0] is None:
            return None
        return int(row[0]), int(row[1]), int(row[2])

    def snap_ts_list(self, refresh: bool = False) -> list:
        if self._snap_ts_cache is None or refresh:
            if not self.available():
                self._snap_ts_cache = []
            else:
                with self._conn() as c:
                    rows = c.execute(
                        "SELECT DISTINCT ts FROM chain_snap WHERE symbol=? "
                        "ORDER BY ts", (self._symbol,)).fetchall()
                self._snap_ts_cache = [int(r[0]) for r in rows]
        return self._snap_ts_cache

    def snap_at_or_before(self, ts: int, max_age_s: int = 900
                          ) -> Optional[int]:
        """Latest snapshot <= ts and younger than max_age_s. Used for
        marks/decisions."""
        import bisect
        lst = self.snap_ts_list()
        i = bisect.bisect_right(lst, ts)
        if i == 0:
            return None
        snap = lst[i - 1]
        return snap if ts - snap <= max_age_s else None

    def snap_after(self, ts: int, ttl_s: int = 900) -> Optional[int]:
        """First snapshot strictly AFTER ts (fills: the first observation
        that could not have been known at decision time), within ttl."""
        import bisect
        lst = self.snap_ts_list()
        i = bisect.bisect_right(lst, ts)
        if i >= len(lst):
            return None
        snap = lst[i]
        return snap if snap - ts <= ttl_s else None

    def chain_at(self, snap_ts: int, expiry: Optional[str] = None
                 ) -> pd.DataFrame:
        q = ("SELECT token, expiry, strike, right, ltp, bid, ask, oi, "
             "volume, iv FROM chain_snap WHERE symbol=? AND ts=?")
        args: list = [self._symbol, snap_ts]
        if expiry:
            q += " AND expiry=?"
            args.append(expiry)
        with self._conn() as c:
            return pd.read_sql_query(q, c, params=args)

    def expiries_at(self, snap_ts: int) -> list:
        with self._conn() as c:
            rows = c.execute(
                "SELECT DISTINCT expiry FROM chain_snap WHERE symbol=? AND "
                "ts=? ORDER BY expiry", (self._symbol, snap_ts)).fetchall()
        return [r[0] for r in rows]

    def pick_expiry(self, snap_ts: int, rule: str) -> Optional[str]:
        """weekly = nearest stored expiry; monthly = the farthest stored one
        when it falls in a later month (the snapshot logger tracks the
        nearest + monthly universes), else the nearest (expiry week, where
        weekly == monthly)."""
        exps = self.expiries_at(snap_ts)
        if not exps:
            return None
        if rule == "monthly" and len(exps) > 1:
            near, far = exps[0], exps[-1]
            if far[:7] != near[:7]:
                return far
        return exps[0]

    # -- derived features (all as-of snap_ts, nothing later) ---------------

    @staticmethod
    def atm_strike(chain: pd.DataFrame, spot: float) -> Optional[float]:
        if chain.empty:
            return None
        strikes = sorted(chain["strike"].unique())
        if not strikes:
            return None
        return min(strikes, key=lambda s: abs(s - spot))

    @staticmethod
    def quote(chain: pd.DataFrame, strike: float, right: str
              ) -> Optional[dict]:
        rows = chain[(chain["strike"] == strike) & (chain["right"] == right)]
        if rows.empty:
            return None
        r = rows.iloc[0]
        ltp = float(r["ltp"] or 0)
        if ltp <= 0:
            return None
        bid = float(r["bid"] or 0)
        ask = float(r["ask"] or 0)
        return {"token": int(r["token"]), "expiry": str(r["expiry"]),
                "strike": float(r["strike"]), "right": right, "ltp": ltp,
                "bid": bid if bid > 0 else None,
                "ask": ask if ask > 0 else None,
                "oi": float(r["oi"] or 0), "volume": float(r["volume"] or 0),
                "iv": float(r["iv"]) if r["iv"] else None}

    @staticmethod
    def pcr(chain: pd.DataFrame) -> Optional[float]:
        ce = chain[chain["right"] == "CE"]["oi"].sum()
        pe = chain[chain["right"] == "PE"]["oi"].sum()
        if not ce or ce <= 0:
            return None
        return float(pe / ce)

    @staticmethod
    def oi_walls(chain: pd.DataFrame) -> dict:
        out = {}
        for right, key in (("CE", "call_wall"), ("PE", "put_wall")):
            side = chain[chain["right"] == right]
            if side.empty or side["oi"].max() <= 0:
                out[key] = None
            else:
                out[key] = float(side.loc[side["oi"].idxmax(), "strike"])
        return out

    def atm_iv(self, snap_ts: int, spot: float) -> Optional[float]:
        """Mean stored IV at the ATM strike (both rights), nearest expiry."""
        exp = self.pick_expiry(snap_ts, "weekly")
        if not exp:
            return None
        chain = self.chain_at(snap_ts, exp)
        atm = self.atm_strike(chain, spot)
        if atm is None:
            return None
        ivs = chain[(chain["strike"] == atm) & chain["iv"].notna() &
                    (chain["iv"] > 0)]["iv"]
        return float(ivs.mean()) if len(ivs) else None

    def straddle_premium(self, snap_ts: int, spot: float) -> Optional[float]:
        exp = self.pick_expiry(snap_ts, "weekly")
        if not exp:
            return None
        chain = self.chain_at(snap_ts, exp)
        atm = self.atm_strike(chain, spot)
        if atm is None:
            return None
        ce = self.quote(chain, atm, "CE")
        pe = self.quote(chain, atm, "PE")
        if not ce or not pe:
            return None
        return ce["ltp"] + pe["ltp"]


chain_reader = ChainReader()


# ==========================================================================
# VIX daily (read-only over the closing store)
# ==========================================================================

def load_vix_daily() -> pd.DataFrame:
    """The closing store keeps intraday VIX bars; collapse each IST session
    to its last close. ts = the day's last bar open."""
    if not VIX_DB_PATH.exists():
        return pd.DataFrame(columns=["ts", "close"])
    with sqlite3.connect(VIX_DB_PATH) as c:
        raw = pd.read_sql_query("SELECT ts, close FROM vix ORDER BY ts", c)
    if raw.empty:
        return raw
    day = (raw["ts"] + 19800) // 86400
    return raw.groupby(day).agg(ts=("ts", "last"), close=("close", "last")
                                ).reset_index(drop=True)


def vix_asof(vix_df: pd.DataFrame, ts: int) -> Optional[float]:
    """Latest daily VIX close STRICTLY BEFORE the ts's session (same-day
    close would be look-ahead intraday)."""
    if vix_df.empty:
        return None
    day_start = ((ts + 19800) // 86400) * 86400 - 19800
    prior = vix_df[vix_df["ts"] < day_start]
    return float(prior.iloc[-1]["close"]) if len(prior) else None


def vix_percentile(vix_df: pd.DataFrame, ts: int, lookback: int = 252
                   ) -> Optional[float]:
    if vix_df.empty:
        return None
    day_start = ((ts + 19800) // 86400) * 86400 - 19800
    prior = vix_df[vix_df["ts"] < day_start].tail(lookback)
    if len(prior) < 60:
        return None
    latest = float(prior.iloc[-1]["close"])
    rank = (prior["close"] <= latest).mean()
    return float(rank * 100.0)


# ==========================================================================
# Readiness + quality
# ==========================================================================

def _fmt_day(ts: Optional[int]) -> Optional[str]:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, IST).date().isoformat()


def readiness() -> dict:
    """Live truth about each dataset. Computed from the files, never cached
    beliefs."""
    out = {}
    idx = index_span()
    out["index_5m"] = {
        "status": "ready" if idx else "missing",
        "from": _fmt_day(idx[0]) if idx else None,
        "to": _fmt_day(idx[1]) if idx else None,
        "rows": idx[2] if idx else 0,
        "note": "NIFTY 50 index 5-min (volume = NIFTYBEES proxy)",
    }
    fut = futures_store.span()
    mode = futures_store.get_meta("sync_mode")
    out["fut_candles"] = {
        "status": "ready" if fut else "awaiting_sync",
        "from": _fmt_day(fut[0]) if fut else None,
        "to": _fmt_day(fut[1]) if fut else None,
        "rows": fut[2] if fut else 0,
        "note": ("sync mode: %s" % mode) if mode else
                "no sync yet — needs an authenticated Kite session",
    }
    out["fut_oi"] = {
        "status": "ready" if (fut and futures_store.has_oi())
                  else "awaiting_sync",
        "from": out["fut_candles"]["from"],
        "to": out["fut_candles"]["to"],
        "rows": fut[2] if fut else 0,
        "note": "futures OI from the same sync (oi=1)",
    }
    ch = chain_reader.span()
    out["chain_hist"] = {
        "status": "window_limited" if ch else "missing",
        "from": _fmt_day(ch[0]) if ch else None,
        "to": _fmt_day(ch[1]) if ch else None,
        "rows": ch[2] if ch else 0,
        "note": "REAL option quotes exist only in this window; Kite deletes "
                "expired contracts. Grows daily; cannot be backfilled.",
    }
    vix = load_vix_daily()
    out["vix_daily"] = {
        "status": "ready" if len(vix) >= 60 else
                  ("missing" if vix.empty else "thin"),
        "from": _fmt_day(int(vix.iloc[0]["ts"])) if len(vix) else None,
        "to": _fmt_day(int(vix.iloc[-1]["ts"])) if len(vix) else None,
        "rows": int(len(vix)),
        "note": "India VIX daily closes",
    }
    return out


def strategy_readiness(needs: tuple, ready: Optional[dict] = None) -> dict:
    """Intersect a strategy's data needs with reality. Returns
    status: ready | window_limited | awaiting_sync | blocked, plus the
    usable [from, to] span and per-need notes."""
    r = ready or readiness()
    worst = "ready"
    froms, tos, notes = [], [], []
    rank = {"ready": 0, "window_limited": 1, "thin": 1, "awaiting_sync": 2,
            "missing": 3}
    for need in needs:
        info = r.get(need)
        if info is None:
            worst = "blocked"
            notes.append("%s: unknown dataset" % need)
            continue
        st = info["status"]
        if rank.get(st, 3) > rank.get(worst, 0):
            worst = "awaiting_sync" if st == "awaiting_sync" else (
                "blocked" if st == "missing" else "window_limited")
        if info["from"]:
            froms.append(info["from"])
        if info["to"]:
            tos.append(info["to"])
        if st != "ready":
            notes.append("%s: %s" % (need, info["note"]))
    usable_from = max(froms) if froms and worst not in ("blocked",
                                                        "awaiting_sync") else None
    usable_to = min(tos) if tos and worst not in ("blocked",
                                                  "awaiting_sync") else None
    return {"status": worst, "from": usable_from, "to": usable_to,
            "notes": notes}


def quality_report(days: int = 30) -> dict:
    """Detect missing bars, session-coverage gaps and abnormal prints over
    the recent window. Pure reporting — repair is a human decision."""
    now = int(time.time())
    from_ts = now - days * 86400
    issues: list = []

    today = datetime.now(IST).date()

    def _bar_coverage(df: pd.DataFrame, label: str):
        if df.empty:
            return
        d = df[df["ts"] >= from_ts].copy()
        if d.empty:
            return
        day_key = (d["ts"] + 19800) // 86400
        counts = d.groupby(day_key).size()
        for dk, n in counts.items():
            day = datetime.fromtimestamp(int(dk) * 86400 - 19800, IST).date()
            # today's session is still printing bars — not a gap
            if not is_trading_day(day) or day >= today:
                continue
            if n < int(_BARS_PER_DAY_5M * 0.9):
                issues.append({"kind": "missing_bars", "source": label,
                               "day": day.isoformat(), "bars": int(n),
                               "expected": _BARS_PER_DAY_5M})
        rets = d["close"].pct_change().abs()
        for i in rets[rets > 0.03].index:
            issues.append({"kind": "abnormal_move", "source": label,
                           "ts": int(d.loc[i, "ts"]),
                           "pct": round(float(rets[i]) * 100, 2)})

    _bar_coverage(load_index_frame(from_ts=from_ts), "index_5m")
    _bar_coverage(futures_store.load_frame(from_ts=from_ts), "fut_candles")

    # Chain session coverage: gaps > 20 min inside 09:20-15:25.
    snaps = [t for t in chain_reader.snap_ts_list(refresh=True)
             if t >= from_ts]
    by_day: dict = {}
    for t in snaps:
        by_day.setdefault((t + 19800) // 86400, []).append(t)
    for dk, ts_list in sorted(by_day.items()):
        day = datetime.fromtimestamp(int(dk) * 86400 - 19800, IST).date()
        if not is_trading_day(day):
            continue
        ts_list.sort()
        prev = None
        for t in ts_list:
            if prev is not None and t - prev > 1200:
                issues.append({"kind": "chain_gap", "day": day.isoformat(),
                               "from_ts": prev, "to_ts": t,
                               "gap_min": round((t - prev) / 60, 1)})
            prev = t
    return {"generated_at": now, "window_days": days,
            "issues": issues, "issue_count": len(issues)}


def as_json_ready(obj) -> str:
    return json.dumps(obj, default=str)
