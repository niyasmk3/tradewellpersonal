"""Condor card/position stores + archives — house persistence pattern.

Same conventions as signals/store.py and trades/store.py: in-memory dict,
atomic temp+replace JSON writes, paths anchored to the backend directory
(never CWD), `path=None` = memory-only for tests (a test store writing the
live path would land synthetic condors in real history — the exact 20-Jul
signal-store incident), day-scoped card load, append-only JSONL archives with
last-line-wins dedupe.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

from app.condor.models import CondorCard, CondorPosition

log = logging.getLogger("tradewell.condor")

_BACKEND = Path(__file__).resolve().parents[2]
_IST_OFFSET = 19800


def _ist_day(ts: float) -> int:
    return int((ts + _IST_OFFSET) // 86400)


def _atomic_write(path: Path, payload) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=1, default=str))
    tmp.replace(path)


class CondorArchive:
    """Append-only JSONL of card lifecycle events (adopt/refresh/retire)."""

    def __init__(self, path: Path | None) -> None:
        self.path = path

    def record(self, card: CondorCard, event: str) -> None:
        if self.path is None:
            return
        try:
            row = card.model_dump()
            row["_event"] = event
            row["_at"] = int(time.time())
            with self.path.open("a") as f:
                f.write(json.dumps(row, default=str) + "\n")
        except Exception as exc:  # never let bookkeeping kill the loop
            log.warning("condor archive write failed: %s", exc)

    def rows(self, days: int = 7) -> list[dict]:
        if self.path is None or not self.path.exists():
            return []
        cutoff = time.time() - days * 86400
        out: dict[str, dict] = {}
        try:
            for line in self.path.read_text().splitlines():
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("_at", 0) >= cutoff:
                    out[row.get("id", str(len(out)))] = row   # last line wins
        except OSError:
            return []
        return sorted(out.values(), key=lambda r: r.get("created_at", 0), reverse=True)


class EvalTrace:
    """One JSONL line per evaluation — the 'which conditions produce quality
    condors' dataset, written from day one whether or not a card forms."""

    def __init__(self, path: Path | None, keep_days: int = 30) -> None:
        self.path = path
        self.keep_days = keep_days
        self._last_minute: dict = {}

    def record(self, resp) -> None:
        if self.path is None:
            return
        # One line per SYMBOL per evaluation minute (review catch: a single
        # shared minute stamp silently dropped every symbol after the first
        # each cycle — invisible with one symbol, data-destroying with two).
        minute = int(resp.evaluated_at // 60)
        if self._last_minute.get(resp.symbol) == minute:
            return
        self._last_minute[resp.symbol] = minute
        try:
            row = {
                "ts": resp.evaluated_at,
                "symbol": resp.symbol,
                "regime": resp.regime,
                "conf": resp.regime_confidence,
                "votes": sum(1 for v in resp.regime_votes.values() if v),
                "vetoes": resp.regime_vetoes,
                "breakout": resp.breakout_score,
                "vol": resp.vol_regime,
                "vix_pct": resp.vix_percentile,
                "em": resp.em_primary,
                "iv_rv": resp.iv_over_rv,
                "card": resp.card.id if resp.card else None,
                "score": resp.card.score if resp.card else None,
                "no_trade": resp.no_trade_reasons or None,
                "data": resp.data_problems or None,
            }
            with self.path.open("a") as f:
                f.write(json.dumps(row, default=str) + "\n")
        except Exception as exc:
            log.warning("condor trace write failed: %s", exc)

    def prune(self) -> None:
        if self.path is None or not self.path.exists():
            return
        cutoff = time.time() - self.keep_days * 86400
        try:
            kept = []
            for ln in self.path.read_text().splitlines():
                # Torn lines (process killed mid-append) are dropped, not
                # fatal — one bad line must not disable retention forever
                # (review catch; same per-line tolerance as rows()).
                try:
                    if json.loads(ln).get("ts", 0) >= cutoff:
                        kept.append(ln)
                except ValueError:
                    continue
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text("\n".join(kept) + ("\n" if kept else ""))
            tmp.replace(self.path)
        except Exception as exc:
            log.warning("condor trace prune failed: %s", exc)

    def rows(self, days: int = 1) -> list[dict]:
        if self.path is None or not self.path.exists():
            return []
        cutoff = time.time() - days * 86400
        out = []
        for line in self.path.read_text().splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("ts", 0) >= cutoff:
                out.append(row)
        return out


class CondorStore:
    """Latest card per symbol + open positions, both persisted."""

    def __init__(self, cards_path: Path | None, positions_path: Path | None,
                 archive: CondorArchive | None = None) -> None:
        self._lock = threading.Lock()
        self.cards_path = cards_path
        self.positions_path = positions_path
        self.archive = archive
        self.cards: dict[str, CondorCard] = {}       # symbol -> latest card
        self.responses: dict[str, dict] = {}         # symbol -> last response dump
        self.positions: dict[str, CondorPosition] = {}
        # Records that failed to parse are carried verbatim and re-emitted on
        # every save (the trades/store.py orphan pattern) — one bad record
        # must never cost the others their persistence.
        self._orphan_positions: dict[str, dict] = {}
        self._load()

    # ---- persistence --------------------------------------------------
    def _load(self) -> None:
        today = _ist_day(time.time())
        if self.cards_path and self.cards_path.exists():
            try:
                data = json.loads(self.cards_path.read_text())
                for sym, raw in (data.get("cards") or {}).items():
                    card = CondorCard(**raw)
                    # Day-scoped: yesterday's card must never resurface live.
                    if _ist_day(card.created_at) == today:
                        self.cards[sym] = card
            except Exception as exc:
                log.warning("condor card store load failed: %s", exc)
        if self.positions_path and self.positions_path.exists():
            try:
                data = json.loads(self.positions_path.read_text())
            except Exception as exc:
                # Unreadable file: quarantine before any save can atomically
                # replace it with an empty book (house .corrupt pattern —
                # positions are real-money records).
                log.error("condor position store unreadable (%s) — preserving as .corrupt", exc)
                try:
                    corrupt = self.positions_path.with_name(
                        self.positions_path.name + ".corrupt")
                    corrupt.write_bytes(self.positions_path.read_bytes())
                except OSError:
                    pass
                data = {}
            for pid, raw in (data.get("positions") or {}).items():
                try:
                    self.positions[pid] = CondorPosition(**raw)
                except Exception as exc:
                    log.warning("condor position %s unparseable — kept as orphan: %s",
                                pid, exc)
                    self._orphan_positions[pid] = raw

    def _save_cards(self) -> None:
        if self.cards_path is None:
            return
        _atomic_write(self.cards_path,
                      {"cards": {s: c.model_dump() for s, c in self.cards.items()}})

    def _save_positions(self) -> None:
        if self.positions_path is None:
            return
        payload = {p: v.model_dump() for p, v in self.positions.items()}
        payload.update(self._orphan_positions)   # orphans survive every save
        _atomic_write(self.positions_path, {"positions": payload})

    # ---- cards ---------------------------------------------------------
    def reconcile(self, symbol: str, card: CondorCard | None, now: float) -> None:
        """Adopt / refresh / retire the symbol's card against a fresh eval."""
        with self._lock:
            held = self.cards.get(symbol)
            if card is None:
                if held and held.state == "active":
                    if now >= held.valid_until:
                        held.state = "expired"
                        if self.archive:
                            self.archive.record(held, "retire")
                        self._save_cards()
                return
            if held and held.id == card.id and held.state == "active":
                # Same structure still qualifying: refresh premiums/score,
                # keep the birth timestamp (the reprice_history lesson).
                # NOT archived — a refresh line per active-card minute grew
                # the archive without bound and taught /history to lie about
                # issue-time premiums (review catch). Adopt and retire are
                # the lifecycle; the store holds the freshest numbers.
                card.created_at = held.created_at
                self.cards[symbol] = card
            else:
                if held and held.state == "active" and held.id != card.id:
                    held.state = "withdrawn"
                    if self.archive:
                        self.archive.record(held, "retire")
                self.cards[symbol] = card
                if self.archive:
                    self.archive.record(card, "adopt")
            self._save_cards()

    def latest(self, symbol: str) -> CondorCard | None:
        with self._lock:
            card = self.cards.get(symbol)
            return card.model_copy(deep=True) if card else None

    # ---- positions ------------------------------------------------------
    def add_position(self, pos: CondorPosition) -> None:
        with self._lock:
            self.positions[pos.id] = pos
            self._save_positions()

    def update_position(self, pos: CondorPosition) -> None:
        with self._lock:
            self.positions[pos.id] = pos
            self._save_positions()

    def note_excursion(self, pid: str, combined: float) -> None:
        """Latch combined-premium extremes on the STORED object, under the
        lock, only while it is still open. A monitor view holding a deep copy
        must never write that copy back (review catch: a GET racing a close
        could resurrect the closed position). Saves only on a new extreme."""
        with self._lock:
            pos = self.positions.get(pid)
            if pos is None or pos.status != "open":
                return
            changed = False
            if pos.prem_min is None or combined < pos.prem_min:
                pos.prem_min = combined
                changed = True
            if pos.prem_max is None or combined > pos.prem_max:
                pos.prem_max = combined
                changed = True
            if changed:
                self._save_positions()

    def open_positions(self) -> list[CondorPosition]:
        with self._lock:
            return [p.model_copy(deep=True) for p in self.positions.values()
                    if p.status == "open"]

    def all_positions(self) -> list[CondorPosition]:
        with self._lock:
            return [p.model_copy(deep=True) for p in self.positions.values()]

    def get_position(self, pid: str) -> CondorPosition | None:
        with self._lock:
            p = self.positions.get(pid)
            return p.model_copy(deep=True) if p else None


# Module-level singletons (persisting); tests construct their own with None
# paths, mirroring signals/store.py's injectable-path pattern.
condor_archive = CondorArchive(_BACKEND / ".condor_archive.jsonl")
condor_trace = EvalTrace(_BACKEND / ".condor_eval.jsonl")
condor_store = CondorStore(
    _BACKEND / ".condor_signals.json",
    _BACKEND / ".condor_positions.json",
    archive=condor_archive,
)
