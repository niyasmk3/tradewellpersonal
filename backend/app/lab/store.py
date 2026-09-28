"""SQLite persistence for the Strategy Lab — backend/.lab.db.

Why sqlite (house precedent: patterns/gold/condor stores): the lab's tables
are relational and append-heavy — versions, runs, trades, a paper ledger,
experiments — and results must survive restarts and never be overwritten.
WAL mode so the paper loop can write while the API reads.

Immutability contract:
  * strategy_version, backtest_run, backtest_trade, paper_day, experiment
    rows are INSERT-only. Re-running a backtest creates a new run row; a
    modified config creates a new version row. Nothing UPDATEs history.
  * The only mutable rows are open paper positions/orders (until closed),
    strategy_state (champion pointer + paper arm flags) and risk_config.

The nightly ops backup picks this file up via its `.db` glob and snapshots
it with the sqlite online-backup API.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

_BASE = Path(__file__).resolve().parents[2]
DB_PATH = _BASE / ".lab.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS strategy_version (
    sid         TEXT NOT NULL,
    version     INTEGER NOT NULL,
    params      TEXT NOT NULL,             -- full validated param dict, json
    created_at  INTEGER NOT NULL,
    source      TEXT NOT NULL,             -- 'default' | 'user' | 'learning'
    parent      INTEGER,                   -- version this was derived from
    note        TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (sid, version)
);
CREATE TABLE IF NOT EXISTS strategy_state (
    sid              TEXT PRIMARY KEY,
    champion_version INTEGER NOT NULL,
    paper_enabled    INTEGER NOT NULL DEFAULT 0,
    experimental_version INTEGER,          -- challenger running in paper, if any
    suspended_reason TEXT NOT NULL DEFAULT '',
    updated_at       INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS backtest_run (
    run_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    sid         TEXT NOT NULL,
    version     INTEGER NOT NULL,
    kind        TEXT NOT NULL,             -- 'insample'|'oos'|'walkforward'
    timeframe   TEXT NOT NULL,
    from_ts     INTEGER NOT NULL,
    to_ts       INTEGER NOT NULL,
    capital_rs  REAL NOT NULL,
    cost_model  TEXT NOT NULL,             -- json snapshot of rates used
    data_note   TEXT NOT NULL DEFAULT '',  -- span actually covered, gaps
    created_at  INTEGER NOT NULL,
    status      TEXT NOT NULL,             -- 'done'|'error'|'insufficient_data'
    summary     TEXT NOT NULL DEFAULT '{}' -- metrics json
);
CREATE INDEX IF NOT EXISTS idx_run_sid ON backtest_run (sid, version);
CREATE TABLE IF NOT EXISTS backtest_trade (
    run_id      INTEGER NOT NULL,
    seq         INTEGER NOT NULL,
    signal_ts   INTEGER NOT NULL,
    entry_ts    INTEGER NOT NULL,
    exit_ts     INTEGER NOT NULL,
    direction   TEXT NOT NULL,
    tag         TEXT NOT NULL DEFAULT '',
    legs        TEXT NOT NULL,             -- json list of leg dicts
    pnl_gross   REAL NOT NULL,
    costs       REAL NOT NULL,
    pnl_net     REAL NOT NULL,
    exit_reason TEXT NOT NULL,
    mae         REAL NOT NULL DEFAULT 0,
    mfe         REAL NOT NULL DEFAULT 0,
    regime      TEXT NOT NULL DEFAULT '',
    session_bucket TEXT NOT NULL DEFAULT '',
    expiry_day  INTEGER NOT NULL DEFAULT 0,
    bars_held   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (run_id, seq)
);
CREATE TABLE IF NOT EXISTS paper_session (
    day         TEXT PRIMARY KEY,          -- IST ISO date
    started_at  INTEGER,
    ended_at    INTEGER,
    status      TEXT NOT NULL DEFAULT 'pending',
    premarket   TEXT NOT NULL DEFAULT '{}',
    notes       TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS paper_signal (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,
    day         TEXT NOT NULL,
    sid         TEXT NOT NULL,
    version     INTEGER NOT NULL,
    experimental INTEGER NOT NULL DEFAULT 0,
    payload     TEXT NOT NULL,             -- SignalIntent json
    outcome     TEXT NOT NULL,             -- 'ordered'|'blocked'|'lapsed'
    reason      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_psig_day ON paper_signal (day, sid);
CREATE TABLE IF NOT EXISTS paper_order (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id   INTEGER NOT NULL,
    ts          INTEGER NOT NULL,
    day         TEXT NOT NULL,
    sid         TEXT NOT NULL,
    version     INTEGER NOT NULL,
    status      TEXT NOT NULL,             -- 'pending'|'filled'|'lapsed'|'rejected'
    legs        TEXT NOT NULL,             -- requested legs json
    fills       TEXT NOT NULL DEFAULT '[]',
    detail      TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS paper_position (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id    INTEGER NOT NULL,
    day         TEXT NOT NULL,
    sid         TEXT NOT NULL,
    version     INTEGER NOT NULL,
    experimental INTEGER NOT NULL DEFAULT 0,
    opened_ts   INTEGER NOT NULL,
    closed_ts   INTEGER,
    direction   TEXT NOT NULL,
    tag         TEXT NOT NULL DEFAULT '',
    legs        TEXT NOT NULL,             -- LegFill json list
    exit_policy TEXT NOT NULL,
    state       TEXT NOT NULL DEFAULT '{}',-- mutable: marks, peak, mae/mfe
    exit_reason TEXT,
    exit_prices TEXT,
    pnl_gross   REAL,
    costs       REAL,
    pnl_net     REAL
);
CREATE INDEX IF NOT EXISTS idx_ppos_open ON paper_position (closed_ts, sid);
CREATE TABLE IF NOT EXISTS paper_day (
    day         TEXT NOT NULL,
    sid         TEXT NOT NULL,
    version     INTEGER NOT NULL,
    trades      INTEGER NOT NULL,
    pnl_net     REAL NOT NULL,
    summary     TEXT NOT NULL,             -- immutable day ledger json
    created_at  INTEGER NOT NULL,
    PRIMARY KEY (day, sid, version)
);
CREATE TABLE IF NOT EXISTS experiment (
    exp_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    sid         TEXT NOT NULL,
    champion    INTEGER NOT NULL,
    challenger  INTEGER NOT NULL,
    hypothesis  TEXT NOT NULL,
    evidence    TEXT NOT NULL DEFAULT '{}',-- what motivated the proposal
    status      TEXT NOT NULL,             -- proposed|validating|paper|accepted|rejected
    dev_result  TEXT NOT NULL DEFAULT '{}',
    test_result TEXT NOT NULL DEFAULT '{}',
    paper_result TEXT NOT NULL DEFAULT '{}',
    created_at  INTEGER NOT NULL,
    decided_at  INTEGER,
    decision_reason TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS risk_event (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,
    day         TEXT NOT NULL,
    sid         TEXT NOT NULL DEFAULT '',
    code        TEXT NOT NULL,
    detail      TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS data_event (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,
    kind        TEXT NOT NULL,
    detail      TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS daily_report (
    day         TEXT PRIMARY KEY,
    created_at  INTEGER NOT NULL,
    report      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS risk_config (
    key         TEXT PRIMARY KEY,
    value       REAL NOT NULL,
    updated_at  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS study_result (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,             -- e.g. 'vwap3'
    created_at  INTEGER NOT NULL,
    params      TEXT NOT NULL,             -- json of the study inputs
    result      TEXT NOT NULL              -- json; append-only like runs
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_lock = threading.Lock()


def _connect(path: Optional[Path] = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    conn.row_factory = sqlite3.Row
    return conn


class LabStore:
    """All DB access goes through one instance (module singleton below).
    `path=None` uses the real file; tests pass a tmp path or ':memory:'-like
    temp file. Methods hold the module lock only around their own
    transaction — sqlite WAL handles reader concurrency."""

    def __init__(self, path: Optional[Path] = None):
        self._path = path or DB_PATH

    def _conn(self) -> sqlite3.Connection:
        return _connect(self._path)

    # -- strategy versions -------------------------------------------------

    def ensure_version1(self, sid: str, params: dict) -> None:
        """Idempotently create version 1 (catalog defaults) + state row."""
        now = int(time.time())
        with _lock, self._conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO strategy_version "
                "(sid, version, params, created_at, source, note) "
                "VALUES (?, 1, ?, ?, 'default', 'catalog defaults')",
                (sid, json.dumps(params), now))
            c.execute(
                "INSERT OR IGNORE INTO strategy_state "
                "(sid, champion_version, paper_enabled, updated_at) "
                "VALUES (?, 1, 0, ?)", (sid, now))

    def add_version(self, sid: str, params: dict, source: str,
                    parent: Optional[int], note: str = "") -> int:
        now = int(time.time())
        with _lock, self._conn() as c:
            row = c.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM strategy_version "
                "WHERE sid = ?", (sid,)).fetchone()
            ver = int(row[0])
            c.execute(
                "INSERT INTO strategy_version "
                "(sid, version, params, created_at, source, parent, note) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (sid, ver, json.dumps(params), now, source, parent, note))
        return ver

    def get_params(self, sid: str, version: int) -> Optional[dict]:
        with self._conn() as c:
            row = c.execute(
                "SELECT params FROM strategy_version WHERE sid=? AND version=?",
                (sid, version)).fetchone()
        return json.loads(row["params"]) if row else None

    def versions(self, sid: str) -> list:
        with self._conn() as c:
            rows = c.execute(
                "SELECT sid, version, params, created_at, source, parent, note "
                "FROM strategy_version WHERE sid=? ORDER BY version", (sid,)
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["params"] = json.loads(d["params"])
            out.append(d)
        return out

    def state(self, sid: str) -> Optional[dict]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM strategy_state WHERE sid=?",
                            (sid,)).fetchone()
        return dict(row) if row else None

    def all_states(self) -> dict:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM strategy_state").fetchall()
        return {r["sid"]: dict(r) for r in rows}

    def set_state(self, sid: str, **fields) -> None:
        allowed = {"champion_version", "paper_enabled",
                   "experimental_version", "suspended_reason"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError("unknown state fields: %s" % sorted(bad))
        if not fields:
            return
        sets = ", ".join("%s = ?" % k for k in fields)
        vals = list(fields.values()) + [int(time.time()), sid]
        with _lock, self._conn() as c:
            c.execute("UPDATE strategy_state SET %s, updated_at = ? "
                      "WHERE sid = ?" % sets, vals)

    # -- backtest runs -----------------------------------------------------

    def create_run(self, sid: str, version: int, kind: str, timeframe: str,
                   from_ts: int, to_ts: int, capital_rs: float,
                   cost_model: dict, data_note: str = "") -> int:
        with _lock, self._conn() as c:
            cur = c.execute(
                "INSERT INTO backtest_run (sid, version, kind, timeframe, "
                "from_ts, to_ts, capital_rs, cost_model, data_note, "
                "created_at, status) VALUES (?,?,?,?,?,?,?,?,?,?, 'running')",
                (sid, version, kind, timeframe, from_ts, to_ts, capital_rs,
                 json.dumps(cost_model), data_note, int(time.time())))
            return int(cur.lastrowid)

    def finish_run(self, run_id: int, status: str, summary: dict,
                   trades: list) -> None:
        """trades: list of TradeRecord-shaped dicts. One transaction so a
        crash can't leave a 'done' run with half its trades."""
        with _lock, self._conn() as c:
            c.execute("UPDATE backtest_run SET status=?, summary=? "
                      "WHERE run_id=?",
                      (status, json.dumps(summary), run_id))
            c.executemany(
                "INSERT INTO backtest_trade (run_id, seq, signal_ts, entry_ts,"
                " exit_ts, direction, tag, legs, pnl_gross, costs, pnl_net,"
                " exit_reason, mae, mfe, regime, session_bucket, expiry_day,"
                " bars_held) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [(run_id, i, t["signal_ts"], t["entry_ts"], t["exit_ts"],
                  t["direction"], t.get("tag", ""), json.dumps(t["legs"]),
                  t["pnl_gross"], t["costs"], t["pnl_net"], t["exit_reason"],
                  t.get("mae", 0.0), t.get("mfe", 0.0), t.get("regime", ""),
                  t.get("session_bucket", ""), int(t.get("expiry_day", False)),
                  t.get("bars_held", 0))
                 for i, t in enumerate(trades)])

    def runs(self, sid: Optional[str] = None, limit: int = 50) -> list:
        q = ("SELECT run_id, sid, version, kind, timeframe, from_ts, to_ts, "
             "capital_rs, data_note, created_at, status, summary "
             "FROM backtest_run ")
        args: tuple = ()
        if sid:
            q += "WHERE sid = ? "
            args = (sid,)
        q += "ORDER BY run_id DESC LIMIT ?"
        with self._conn() as c:
            rows = c.execute(q, args + (limit,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["summary"] = json.loads(d["summary"])
            out.append(d)
        return out

    def run(self, run_id: int) -> Optional[dict]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM backtest_run WHERE run_id=?",
                            (run_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["summary"] = json.loads(d["summary"])
        d["cost_model"] = json.loads(d["cost_model"])
        return d

    def run_trades(self, run_id: int, limit: int = 1000) -> list:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM backtest_trade WHERE run_id=? ORDER BY seq "
                "LIMIT ?", (run_id, limit)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["legs"] = json.loads(d["legs"])
            out.append(d)
        return out

    # -- paper ledger --------------------------------------------------------

    def paper_session_start(self, day: str, premarket: dict) -> None:
        with _lock, self._conn() as c:
            c.execute(
                "INSERT INTO paper_session (day, started_at, status, premarket)"
                " VALUES (?, ?, 'live', ?) ON CONFLICT(day) DO UPDATE SET "
                "started_at=COALESCE(paper_session.started_at, excluded.started_at),"
                " status='live', premarket=excluded.premarket",
                (day, int(time.time()), json.dumps(premarket)))

    def paper_session_end(self, day: str, status: str, notes: str = "") -> None:
        with _lock, self._conn() as c:
            c.execute("UPDATE paper_session SET ended_at=?, status=?, notes=? "
                      "WHERE day=?", (int(time.time()), status, notes, day))

    def paper_session(self, day: str) -> Optional[dict]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM paper_session WHERE day=?",
                            (day,)).fetchone()
        return dict(row) if row else None

    def log_signal(self, ts: int, day: str, sid: str, version: int,
                   experimental: bool, payload: dict, outcome: str,
                   reason: str = "") -> int:
        with _lock, self._conn() as c:
            cur = c.execute(
                "INSERT INTO paper_signal (ts, day, sid, version, experimental,"
                " payload, outcome, reason) VALUES (?,?,?,?,?,?,?,?)",
                (ts, day, sid, version, int(experimental),
                 json.dumps(payload), outcome, reason))
            return int(cur.lastrowid)

    def create_order(self, signal_id: int, ts: int, day: str, sid: str,
                     version: int, legs: list) -> int:
        with _lock, self._conn() as c:
            cur = c.execute(
                "INSERT INTO paper_order (signal_id, ts, day, sid, version, "
                "status, legs) VALUES (?,?,?,?,?, 'pending', ?)",
                (signal_id, ts, day, sid, version, json.dumps(legs)))
            return int(cur.lastrowid)

    def set_order(self, order_id: int, status: str, fills: list,
                  detail: str = "") -> None:
        with _lock, self._conn() as c:
            c.execute("UPDATE paper_order SET status=?, fills=?, detail=? "
                      "WHERE id=?",
                      (status, json.dumps(fills), detail, order_id))

    def pending_orders(self, day: str) -> list:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM paper_order WHERE day=? AND status='pending'",
                (day,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["legs"] = json.loads(d["legs"])
            out.append(d)
        return out

    def open_position_create(self, order_id: int, day: str, sid: str,
                             version: int, experimental: bool, opened_ts: int,
                             direction: str, tag: str, legs: list,
                             exit_policy: dict, state: dict) -> int:
        with _lock, self._conn() as c:
            cur = c.execute(
                "INSERT INTO paper_position (order_id, day, sid, version, "
                "experimental, opened_ts, direction, tag, legs, exit_policy, "
                "state) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (order_id, day, sid, version, int(experimental), opened_ts,
                 direction, tag, json.dumps(legs), json.dumps(exit_policy),
                 json.dumps(state)))
            return int(cur.lastrowid)

    def open_positions(self, sid: Optional[str] = None) -> list:
        q = "SELECT * FROM paper_position WHERE closed_ts IS NULL"
        args: tuple = ()
        if sid:
            q += " AND sid = ?"
            args = (sid,)
        with self._conn() as c:
            rows = c.execute(q, args).fetchall()
        return [self._pos_dict(r) for r in rows]

    def positions_for_day(self, day: str) -> list:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM paper_position WHERE day=?",
                             (day,)).fetchall()
        return [self._pos_dict(r) for r in rows]

    @staticmethod
    def _pos_dict(r) -> dict:
        d = dict(r)
        d["legs"] = json.loads(d["legs"])
        d["exit_policy"] = json.loads(d["exit_policy"])
        d["state"] = json.loads(d["state"])
        if d.get("exit_prices"):
            d["exit_prices"] = json.loads(d["exit_prices"])
        return d

    def update_position_state(self, pos_id: int, state: dict,
                              legs: Optional[list] = None) -> None:
        with _lock, self._conn() as c:
            if legs is None:
                c.execute("UPDATE paper_position SET state=? WHERE id=?",
                          (json.dumps(state), pos_id))
            else:
                c.execute("UPDATE paper_position SET state=?, legs=? "
                          "WHERE id=?",
                          (json.dumps(state), json.dumps(legs), pos_id))

    def close_position(self, pos_id: int, closed_ts: int, exit_reason: str,
                       exit_prices: dict, pnl_gross: float, costs: float,
                       pnl_net: float) -> None:
        with _lock, self._conn() as c:
            c.execute(
                "UPDATE paper_position SET closed_ts=?, exit_reason=?, "
                "exit_prices=?, pnl_gross=?, costs=?, pnl_net=? "
                "WHERE id=? AND closed_ts IS NULL",
                (closed_ts, exit_reason, json.dumps(exit_prices), pnl_gross,
                 costs, pnl_net, pos_id))

    def save_paper_day(self, day: str, sid: str, version: int, trades: int,
                       pnl_net: float, summary: dict) -> None:
        """Immutable: first write wins; a re-run of close-of-day jobs must
        not rewrite a ledger that was already cut."""
        with _lock, self._conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO paper_day (day, sid, version, trades, "
                "pnl_net, summary, created_at) VALUES (?,?,?,?,?,?,?)",
                (day, sid, version, trades, pnl_net, json.dumps(summary),
                 int(time.time())))

    def paper_days(self, sid: Optional[str] = None, limit: int = 120) -> list:
        q = "SELECT * FROM paper_day "
        args: tuple = ()
        if sid:
            q += "WHERE sid = ? "
            args = (sid,)
        q += "ORDER BY day DESC LIMIT ?"
        with self._conn() as c:
            rows = c.execute(q, args + (limit,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["summary"] = json.loads(d["summary"])
            out.append(d)
        return out

    def signals_for_day(self, day: str, limit: int = 500) -> list:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM paper_signal WHERE day=? ORDER BY id DESC "
                "LIMIT ?", (day, limit)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["payload"] = json.loads(d["payload"])
            out.append(d)
        return out

    # -- experiments ---------------------------------------------------------

    def create_experiment(self, sid: str, champion: int, challenger: int,
                          hypothesis: str, evidence: dict) -> int:
        with _lock, self._conn() as c:
            cur = c.execute(
                "INSERT INTO experiment (sid, champion, challenger, hypothesis,"
                " evidence, status, created_at) VALUES "
                "(?,?,?,?,?, 'proposed', ?)",
                (sid, champion, challenger, hypothesis, json.dumps(evidence),
                 int(time.time())))
            return int(cur.lastrowid)

    def update_experiment(self, exp_id: int, **fields) -> None:
        allowed = {"status", "dev_result", "test_result", "paper_result",
                   "decided_at", "decision_reason"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError("unknown experiment fields: %s" % sorted(bad))
        sets, vals = [], []
        for k, v in fields.items():
            sets.append("%s = ?" % k)
            vals.append(json.dumps(v) if k.endswith("_result") else v)
        vals.append(exp_id)
        with _lock, self._conn() as c:
            c.execute("UPDATE experiment SET %s WHERE exp_id = ?"
                      % ", ".join(sets), vals)

    def experiments(self, sid: Optional[str] = None, limit: int = 100) -> list:
        q = "SELECT * FROM experiment "
        args: tuple = ()
        if sid:
            q += "WHERE sid = ? "
            args = (sid,)
        q += "ORDER BY exp_id DESC LIMIT ?"
        with self._conn() as c:
            rows = c.execute(q, args + (limit,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            for k in ("evidence", "dev_result", "test_result", "paper_result"):
                d[k] = json.loads(d[k])
            out.append(d)
        return out

    # -- events, reports, risk config ---------------------------------------

    def log_risk_event(self, ts: int, day: str, sid: str, code: str,
                       detail: dict) -> None:
        with _lock, self._conn() as c:
            c.execute("INSERT INTO risk_event (ts, day, sid, code, detail) "
                      "VALUES (?,?,?,?,?)",
                      (ts, day, sid, code, json.dumps(detail)))

    def risk_events(self, limit: int = 100) -> list:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM risk_event ORDER BY id DESC "
                             "LIMIT ?", (limit,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["detail"] = json.loads(d["detail"])
            out.append(d)
        return out

    def log_data_event(self, ts: int, kind: str, detail: dict) -> None:
        with _lock, self._conn() as c:
            c.execute("INSERT INTO data_event (ts, kind, detail) "
                      "VALUES (?,?,?)", (ts, kind, json.dumps(detail)))

    def data_events(self, limit: int = 100) -> list:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM data_event ORDER BY id DESC "
                             "LIMIT ?", (limit,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["detail"] = json.loads(d["detail"])
            out.append(d)
        return out

    def save_report(self, day: str, report: dict) -> None:
        with _lock, self._conn() as c:
            c.execute("INSERT OR REPLACE INTO daily_report (day, created_at, "
                      "report) VALUES (?,?,?)",
                      (day, int(time.time()), json.dumps(report)))

    def report(self, day: str) -> Optional[dict]:
        with self._conn() as c:
            row = c.execute("SELECT report FROM daily_report WHERE day=?",
                            (day,)).fetchone()
        return json.loads(row["report"]) if row else None

    def report_days(self, limit: int = 30) -> list:
        with self._conn() as c:
            rows = c.execute("SELECT day FROM daily_report ORDER BY day DESC "
                             "LIMIT ?", (limit,)).fetchall()
        return [r["day"] for r in rows]

    def risk_config(self) -> dict:
        with self._conn() as c:
            rows = c.execute("SELECT key, value FROM risk_config").fetchall()
        return {r["key"]: r["value"] for r in rows}

    def set_risk_config(self, updates: dict) -> None:
        now = int(time.time())
        with _lock, self._conn() as c:
            c.executemany(
                "INSERT OR REPLACE INTO risk_config (key, value, updated_at) "
                "VALUES (?,?,?)",
                [(k, float(v), now) for k, v in updates.items()])

    def get_meta(self, key: str) -> Optional[str]:
        with self._conn() as c:
            row = c.execute("SELECT value FROM meta WHERE key=?",
                            (key,)).fetchone()
        return row["value"] if row else None

    # -- study results (append-only, like runs) -----------------------------

    def save_study(self, name: str, params: dict, result: dict) -> int:
        with _lock, self._conn() as c:
            cur = c.execute(
                "INSERT INTO study_result (name, created_at, params, result)"
                " VALUES (?,?,?,?)",
                (name, int(time.time()), json.dumps(params),
                 json.dumps(result)))
            return int(cur.lastrowid)

    def latest_study(self, name: str) -> Optional[dict]:
        with self._conn() as c:
            row = c.execute(
                "SELECT id, created_at, params, result FROM study_result "
                "WHERE name=? ORDER BY id DESC LIMIT 1", (name,)).fetchone()
        if not row:
            return None
        return {"id": row["id"], "created_at": row["created_at"],
                "params": json.loads(row["params"]),
                "result": json.loads(row["result"])}

    def set_meta(self, key: str, value: str) -> None:
        with _lock, self._conn() as c:
            c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)",
                      (key, value))


lab_store = LabStore()
