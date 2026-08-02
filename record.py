#!/usr/bin/env python3
"""Tradewell sidecar recorder — archives the live data the app throws away.

The backend keeps candles, option-chain OI and tape analytics in memory only;
at market close the day's chain history is gone forever (Kite has no historical
chain API). This sidecar polls the backend's own REST endpoints — the same
stable surface the dashboard uses — and appends everything to a local SQLite
file, building the dataset Phase-6 ML training will need.

Deliberately OUT-OF-TREE: no app code is imported or modified, so `git pull`
on the upstream repo can never conflict with it. Python stdlib only, so it
survives requirements.txt changes too.

Usage:
  record.py             poll loop (records during market hours, idles outside)
  record.py --once      one polling cycle right now, then exit (testing)
  record.py --selftest  write + read back a synthetic row, then exit

Reading the data later (pandas):
  import sqlite3, zlib, json, pandas as pd
  db = sqlite3.connect("recorder/data/tradewell_history.db")
  candles = pd.read_sql("SELECT * FROM candles WHERE symbol='NIFTY' AND tf='3m'", db)
  raw = db.execute("SELECT ts_ist, payload FROM snapshots WHERE endpoint='chain' AND symbol='NIFTY' ORDER BY id DESC LIMIT 1").fetchone()
  chain = json.loads(zlib.decompress(raw[1]))
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
import urllib.error
import urllib.request
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

BACKEND = "http://127.0.0.1:8777"  # local port remap — see ./local-start.sh
SYMBOLS = ["NIFTY", "BANKNIFTY", "FINNIFTY"]
DB_PATH = Path(__file__).resolve().parent / "data" / "tradewell_history.db"

IST = timezone(timedelta(hours=5, minutes=30))

# Polling cadences (seconds). Chain/pulse/indicators once a minute is plenty
# for ML features and keeps the DB small; candles are deduped by PK so a
# 5-minute fetch of the last 600 bars loses nothing.
CYCLE_S = 60
CANDLES_EVERY_S = 300
MOOD_EVERY_S = 1800
IDLE_SLEEP_S = 300
HTTP_TIMEOUT_S = 10

# Record Mon-Fri 09:05-15:40 IST — a buffer around the 09:15-15:30 session so
# the opening auction context and the closing minutes are both captured.
REC_START = (9, 5)
REC_END = (15, 40)


def log(msg: str) -> None:
    print(f"{datetime.now(IST).strftime('%d/%m/%Y %H:%M:%S')} IST  {msg}", flush=True)


def in_market_window(now: datetime) -> bool:
    if now.weekday() > 4:  # Sat/Sun
        return False
    hm = (now.hour, now.minute)
    return REC_START <= hm <= REC_END


def http_json(path: str) -> object | None:
    """GET backend JSON; None on any failure (backend down is a normal state)."""
    try:
        with urllib.request.urlopen(f"{BACKEND}{path}", timeout=HTTP_TIMEOUT_S) as r:
            return json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None


def open_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute(
        """CREATE TABLE IF NOT EXISTS snapshots (
             id INTEGER PRIMARY KEY,
             ts_utc TEXT NOT NULL,
             ts_ist TEXT NOT NULL,
             endpoint TEXT NOT NULL,
             symbol TEXT,
             payload BLOB NOT NULL)"""
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS ix_snap ON snapshots (endpoint, symbol, ts_utc)"
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS candles (
             symbol TEXT NOT NULL,
             tf TEXT NOT NULL,
             bar_ts INTEGER NOT NULL,
             open REAL, high REAL, low REAL, close REAL, volume REAL,
             PRIMARY KEY (symbol, tf, bar_ts))"""
    )
    # Card-birth registry: which signal ids already got their at-birth capture
    # (survives restarts, so a card is snapshotted exactly once).
    db.execute(
        """CREATE TABLE IF NOT EXISTS card_births (
             card_id TEXT PRIMARY KEY,
             ts_utc TEXT NOT NULL,
             symbol TEXT)"""
    )
    return db


def save_snapshot(db: sqlite3.Connection, endpoint: str, symbol: str | None, payload: object) -> None:
    now = datetime.now(timezone.utc)
    db.execute(
        "INSERT INTO snapshots (ts_utc, ts_ist, endpoint, symbol, payload) VALUES (?,?,?,?,?)",
        (
            now.isoformat(timespec="seconds"),
            now.astimezone(IST).isoformat(timespec="seconds"),
            endpoint,
            symbol,
            zlib.compress(json.dumps(payload, separators=(",", ":")).encode("utf-8")),
        ),
    )


def save_candles(db: sqlite3.Connection, symbol: str, tf: str, bars: object) -> int:
    if not isinstance(bars, list):
        return 0
    rows = [
        (symbol, tf, b["ts"], b.get("open"), b.get("high"), b.get("low"), b.get("close"), b.get("volume"))
        for b in bars
        if isinstance(b, dict) and isinstance(b.get("ts"), (int, float))
    ]
    db.executemany(
        "INSERT OR REPLACE INTO candles (symbol, tf, bar_ts, open, high, low, close, volume) VALUES (?,?,?,?,?,?,?,?)",
        rows,
    )
    return len(rows)


def capture_card_births(db: sqlite3.Connection, seen: set[str]) -> bool:
    """Event-driven capture: the moment a NEW signal card appears, snapshot the
    full market state (chain + pulse + indicators) tagged with the card id.

    The periodic 60s snapshots can be up to a minute stale relative to a
    card's birth; training joins want the state the engine actually fired on.
    One combined row per card, deduped across restarts via card_births.
    """
    alive = False
    for s in SYMBOLS:
        for mode in ("intraday", "positional"):
            resp = http_json(f"/signals/{s}?mode={mode}")
            if resp is None:
                continue
            alive = True
            card = resp.get("signal") if isinstance(resp, dict) else None
            cid = card.get("id") if isinstance(card, dict) else None
            if not cid or cid in seen:
                continue
            birth = {
                "mode": mode,
                "response": resp,
                "chain": http_json(f"/options/{s}"),
                "pulse": http_json(f"/market/{s}/pulse"),
                "indicators": http_json(f"/market/{s}/indicators"),
            }
            save_snapshot(db, "card_birth", s, birth)
            db.execute(
                "INSERT OR IGNORE INTO card_births (card_id, ts_utc, symbol) VALUES (?,?,?)",
                (cid, datetime.now(timezone.utc).isoformat(timespec="seconds"), s),
            )
            seen.add(cid)
            log(f"card birth captured: {s} {mode} {cid}")
    return alive


def poll_cycle(db: sqlite3.Connection, last: dict[str, float], seen: set[str]) -> bool:
    """One polling pass. Returns True if the backend answered at all."""
    alive = False
    now = time.monotonic()

    if capture_card_births(db, seen):
        alive = True

    snap = http_json("/market/snapshot")
    if snap is not None:
        alive = True
        save_snapshot(db, "market_snapshot", None, snap)

    for s in SYMBOLS:
        chain = http_json(f"/options/{s}")
        if chain is not None:
            alive = True
            save_snapshot(db, "chain", s, chain)
        pulse = http_json(f"/market/{s}/pulse")
        if pulse is not None:
            alive = True
            save_snapshot(db, "pulse", s, pulse)
        ind = http_json(f"/market/{s}/indicators")
        if ind is not None:
            alive = True
            save_snapshot(db, "indicators", s, ind)

    if now - last.get("candles", 0) >= CANDLES_EVERY_S:
        wrote = 0
        for s in SYMBOLS:
            for tf in ("3m", "15m"):
                bars = http_json(f"/market/{s}/candles?tf={tf}&limit=600")
                if bars is not None:
                    alive = True
                    wrote += save_candles(db, s, tf, bars)
        if wrote:
            last["candles"] = now
            log(f"candles upserted: {wrote} bars")

    if now - last.get("mood", 0) >= MOOD_EVERY_S:
        mood = http_json("/market/mood")
        if mood is not None:
            alive = True
            save_snapshot(db, "mood", None, mood)
            last["mood"] = now

    db.commit()
    return alive


def selftest() -> int:
    db = open_db()
    save_snapshot(db, "selftest", "TEST", {"ok": True, "n": 42})
    save_candles(db, "TEST", "3m", [{"ts": 1, "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 100}])
    db.commit()
    row = db.execute(
        "SELECT payload FROM snapshots WHERE endpoint='selftest' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    back = json.loads(zlib.decompress(row[0]))
    bar = db.execute("SELECT close FROM candles WHERE symbol='TEST'").fetchone()
    db.execute("DELETE FROM snapshots WHERE endpoint='selftest'")
    db.execute("DELETE FROM candles WHERE symbol='TEST'")
    db.commit()
    assert back == {"ok": True, "n": 42} and bar[0] == 1.5
    log(f"selftest OK — db at {DB_PATH}")
    return 0


def main() -> int:
    if "--selftest" in sys.argv:
        return selftest()

    db = open_db()
    last: dict[str, float] = {}
    seen: set[str] = {
        row[0] for row in db.execute("SELECT card_id FROM card_births")
    }

    if "--once" in sys.argv:
        alive = poll_cycle(db, last, seen)
        n = db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
        log(f"single cycle done — backend {'answered' if alive else 'UNREACHABLE'}; snapshots in db: {n}")
        return 0

    log(f"recorder up — db: {DB_PATH}")
    backend_was_up: bool | None = None
    while True:
        now_ist = datetime.now(IST)
        if not in_market_window(now_ist):
            time.sleep(IDLE_SLEEP_S)
            continue
        alive = poll_cycle(db, last, seen)
        if alive is not backend_was_up:
            log("backend reachable — recording" if alive else "backend unreachable — will keep retrying quietly")
            backend_was_up = alive
        time.sleep(CYCLE_S)


if __name__ == "__main__":
    sys.exit(main())
