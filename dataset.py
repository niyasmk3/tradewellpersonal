#!/usr/bin/env python3
"""Build the training dataset: one row per graded signal card.

Joins the three evidence layers into a flat table:
  1. the card itself (signal archive) — score components, regime read, refs;
  2. the outcome (paper book / hollow store, matched on trade.signal_id) —
     charges-net realized P&L → label;
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


def outcome_by_signal(trades: list[dict]) -> dict[str, dict]:
    """signal_id → closed trade row (first terminal match wins)."""
    out: dict[str, dict] = {}
    for t in trades:
        sid = t.get("signal_id")
        if not sid or str(sid) in out:
            continue
        # Human-ruled-out rows never become training labels — see
        # data/journal_overrides.json.
        if t.get("id") in _EXCLUDED_IDS:
            continue
        status = str(t.get("status", "")).lower()
        closed = (
            any(s in status for s in CLOSED_STATUSES)
            or t.get("exit_premium") is not None
        )
        if closed:
            out[str(sid)] = t
    return out


def label_fields(trade: dict) -> dict:
    pnl = trade.get("realized_pnl")
    if not isinstance(pnl, (int, float)):
        return {}
    out = {"label_win": int(pnl > 0), "net_pnl": pnl}
    entry = trade.get("entry_premium")
    stop = trade.get("stop_loss")
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
        f["dow"] = dt.weekday()
        f["month"] = dt.strftime("%Y-%m")
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


def vix_close_before(db: sqlite3.Connection, ts: int) -> float | None:
    if not _table_exists(db, "candles"):
        return None
    row = db.execute(
        "SELECT close FROM candles WHERE symbol='INDIAVIX' AND tf='day' "
        "AND bar_ts<=? ORDER BY bar_ts DESC LIMIT 1", (ts,),
    ).fetchone()
    return row[0] if row else None


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
        f"{len(set(cards) & set(outcomes))}")

    if not DB.exists():
        log("recorder DB missing — market-state features will be empty")
        db = sqlite3.connect(":memory:")
    else:
        db = sqlite3.connect(DB)
    births = birth_snapshots(db)

    rows: list[dict] = []
    skipped_no_outcome = skipped_no_label = 0
    for cid, card in cards.items():
        trade = outcomes.get(cid)
        if trade is None:
            skipped_no_outcome += 1
            continue
        labels = label_fields(trade)
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
        vix = vix_close_before(db, created)
        if vix is not None:
            f["vix_close"] = vix
        f["had_birth_snapshot"] = int(birth is not None)
        f.update(labels)
        rows.append(f)

    if not rows:
        log("no graded rows yet — dataset not written "
            f"(no-outcome: {skipped_no_outcome}, no-label: {skipped_no_label})")
        return 0

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
