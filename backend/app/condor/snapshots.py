"""Chain snapshot logger — the dataset the Phase-1 audit said must ship first.

Every CONDOR_SNAPSHOT_S seconds during market hours, one row per subscribed
option token (both expiries): ltp, bid, ask, oi, volume, iv. This is what
makes IV percentile/rank, IV-vs-RV promotion, and premium-true condor
validation possible LATER — none of it can be reconstructed after the fact
(per-strike IV was being computed live and discarded for weeks before this).

SQLite for the same reason patterns/store.py chose it: incremental upserts,
one file, no server. WAL mode so the 60s condor loop never blocks a reader.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path

from app.condor.greeks import solve_iv
from app.options.iv import years_to_expiry

log = logging.getLogger("tradewell.condor")

DB_PATH = Path(__file__).resolve().parents[2] / ".condor_chain.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS chain_snap (
    ts      INTEGER NOT NULL,   -- snapshot wall time
    token   INTEGER NOT NULL,
    tick_ts INTEGER,            -- the quote's OWN exchange timestamp
    symbol  TEXT,
    expiry  TEXT,
    strike  REAL,
    right   TEXT,
    ltp     REAL,
    bid     REAL,
    ask     REAL,
    oi      REAL,
    volume  REAL,
    iv      REAL,
    PRIMARY KEY (ts, token)
);
CREATE TABLE IF NOT EXISTS daily_summary (
    day     TEXT PRIMARY KEY,
    data    TEXT
);
"""


class SnapshotLogger:
    def __init__(self, state, path: Path | None = DB_PATH) -> None:
        self.state = state
        self.path = path
        self._last_at = 0.0
        self._last_prune_day: int | None = None

    def _conn(self) -> sqlite3.Connection | None:
        if self.path is None:
            return None
        conn = sqlite3.connect(self.path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA)
        return conn

    def maybe_snapshot(self, universes: dict, interval_s: float,
                       keep_days: int, now_ts: float | None = None) -> int:
        """Write one snapshot if `interval_s` has elapsed. Returns rows written."""
        now = time.time() if now_ts is None else now_ts
        if now - self._last_at < interval_s:
            return 0
        self._last_at = now
        conn = self._conn()
        if conn is None:
            return 0
        rows = []
        ts = int(now)
        seen: set[int] = set()
        try:
            for key, uni in universes.items():
                symbol = uni.symbol
                meta = self.state.underlyings.get(symbol.upper())
                spot = None
                if meta:
                    spot = self.state.ticks.get(meta.spot_token, {}).get("last_price")
                t = years_to_expiry(uni.expiry, now)
                exp = uni.expiry.isoformat() if uni.expiry else None
                for strike, pair in uni.strikes.items():
                    for token, right in ((pair.ce_token, "CE"), (pair.pe_token, "PE")):
                        if not token or token in seen:
                            continue
                        seen.add(token)
                        tick = self.state.ticks.get(token)
                        if not tick:
                            continue
                        ltp = tick.get("last_price")
                        tick_ts = tick.get("ts")
                        depth = tick.get("depth") or {}
                        try:
                            bid = (depth.get("buy") or [{}])[0].get("price")
                            ask = (depth.get("sell") or [{}])[0].get("price")
                        except (IndexError, AttributeError, TypeError):
                            bid = ask = None
                        # The row keeps the quote's own exchange timestamp so
                        # consumers can age-filter, and a stale quote never
                        # gets an IV solved against the CURRENT spot — that
                        # pairing poisons the one dataset that can't be
                        # rebuilt (review catch: an untraded wing's day-old
                        # print recorded as a live vol observation).
                        fresh = tick_ts is not None and now - tick_ts <= 600
                        iv = None
                        if fresh and ltp and spot and t > 0:
                            ref = (bid + ask) / 2.0 if bid and ask else ltp
                            iv = solve_iv(ref, spot, strike, t, right == "CE")
                        rows.append((ts, token, tick_ts, symbol, exp, strike,
                                     right, ltp, bid, ask, tick.get("oi"),
                                     tick.get("volume_traded"), iv))
            if rows:
                conn.executemany(
                    "INSERT OR REPLACE INTO chain_snap VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    rows)
                conn.commit()
            self._maybe_prune(conn, keep_days, now)
        except Exception as exc:
            log.warning("condor snapshot failed: %s", exc)
        finally:
            conn.close()
        return len(rows)

    def _maybe_prune(self, conn: sqlite3.Connection, keep_days: int, now: float) -> None:
        day = int((now + 19800) // 86400)
        if self._last_prune_day == day:
            return
        self._last_prune_day = day
        cutoff = int(now - keep_days * 86400)
        try:
            conn.execute("DELETE FROM chain_snap WHERE ts < ?", (cutoff,))
            conn.commit()
        except Exception as exc:
            log.warning("condor snapshot prune failed: %s", exc)

    def row_count(self) -> int:
        conn = self._conn()
        if conn is None:
            return 0
        try:
            return conn.execute("SELECT COUNT(*) FROM chain_snap").fetchone()[0]
        finally:
            conn.close()
