"""Append-only signal archive — the permanent record the day-scoped store isn't.

WHY THIS EXISTS: SignalStore is deliberately day-scoped — a card priced off an
earlier session must never resurface as live after a restart, so `_load` drops
anything not from today and the next save erases it from disk. Correct for the
LIVE feed, fatal for history: every restart destroyed yesterday, and on 27-Jul
the user asked for last week's cards and only Friday's were still recoverable
(and only because nothing had saved yet that morning).

So retirement-grade history lives here instead: one JSONL line per event
(adoption, retirement), appended and never rewritten. Readers dedupe by card
id keeping the LAST line, so a card's final state supersedes its adoption
snapshot. The store stays day-scoped; this file only grows (a handful of KB a
day — years before size matters).

Boot-merge: at APP BOOT (main.py lifespan — deliberately never at import,
so collecting the test suite cannot write this file) the archive folds in any
cards sitting in the sibling `.signals.json` that it has not seen yet. That
closes every deploy gap — cards adopted by an old process (which never knew
this file existed) reach `.signals.json` via the store's own saves, and the
first boot of the new code imports them. Idempotent by id.

Wired to the module SINGLETON only (same rule as the store's persistence and
notify hooks): a test store must never write synthetic cards into the real
archive.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

from app.signals.models import SignalCard

log = logging.getLogger("tradewell.signals")

_ARCHIVE_PATH = Path(__file__).resolve().parents[2] / ".signals_archive.jsonl"


class SignalArchive:
    def __init__(self, path: Path | None) -> None:
        """path=None -> disabled (unit tests construct their own tmp paths).

        Construction performs NO I/O. The boot-merge is called explicitly from
        the app lifespan (main.py) — an import-time merge meant merely
        COLLECTING the test suite wrote the live archive file, the exact
        fixture-pollution failure the store's own docstring warns about.
        """
        self._path = path
        self._lock = threading.Lock()
        # (mtime_ns, size) -> parsed last-state-per-id dict. The dashboard
        # polls every 15s forever; without this every poll re-reads and
        # re-parses the whole ever-growing file.
        self._cache_key: tuple | None = None
        self._cache: dict[str, dict] = {}
        self._parse_count = 0            # observability for tests

    # ---- write ---------------------------------------------------------------
    def record(self, card: SignalCard) -> None:
        """Append one snapshot. Never raises — archiving must not break issuing."""
        if self._path is None:
            return
        try:
            line = json.dumps({"archived_at": int(time.time()),
                               "card": card.model_dump(mode="json")})
            with self._lock:
                with open(self._path, "a") as fh:
                    fh.write(line + "\n")
        except Exception:
            log.warning("signal archive write failed", exc_info=True)

    # ---- read ----------------------------------------------------------------
    def load(self, days: int = 7, symbol: str | None = None,
             modes: list[str] | None = None) -> list[SignalCard]:
        """Cards from the last `days` IST days, one per id (last state wins),
        newest first. Corrupt lines are skipped, not fatal — an archive that
        refuses to read because one line is torn defeats its purpose.

        The parse is cached against (mtime, size): the dashboard polls this
        every 15s, and the file only changes when a card is adopted, repriced
        or retired — a handful of times a day. Between changes a poll costs
        one stat(), not a full-file read that grows with the archive's age.
        """
        if self._path is None or not self._path.exists():
            return []
        cutoff = int(time.time()) - days * 86400
        with self._lock:
            try:
                st = self._path.stat()
                key = (st.st_mtime_ns, st.st_size)
                if key != self._cache_key:
                    latest: dict[str, dict] = {}
                    for raw in self._path.read_text().splitlines():
                        try:
                            entry = json.loads(raw)
                            card = entry["card"]
                            latest[card["id"]] = card   # later lines supersede
                        except Exception:
                            continue                    # torn/corrupt line — skip
                    self._cache_key, self._cache = key, latest
                    self._parse_count += 1
                latest = self._cache
            except Exception:
                log.warning("signal archive unreadable", exc_info=True)
                return []
        out: list[SignalCard] = []
        for data in latest.values():
            # Cheap raw-field filters BEFORE pydantic validation — validating
            # years of out-of-window cards on every request is pure waste.
            if (data.get("created_at") or 0) < cutoff:
                continue
            if symbol and str(data.get("symbol", "")).upper() != symbol.upper():
                continue
            if modes and data.get("mode") not in modes:
                continue
            try:
                out.append(SignalCard.model_validate(data))
            except Exception:
                continue
        out.sort(key=lambda c: c.created_at, reverse=True)
        return out

    def _archived_ids(self) -> set[str]:
        if self._path is None or not self._path.exists():
            return set()
        ids: set[str] = set()
        for raw in self._path.read_text().splitlines():
            try:
                ids.add(json.loads(raw)["card"]["id"])
            except Exception:
                continue
        return ids

    def merge_store_file(self, store_names: tuple = (".signals.json",)) -> None:
        """Fold in cards from the sibling .signals.json the archive hasn't seen.

        The store file is the only place cards issued by a PRE-ARCHIVE process
        exist; its own loader will destroy the older ones at the next restart.
        Called from the app lifespan (never at import), keyed by id, so every
        BOOT is a safe, idempotent harvest. Never raises — a history feature
        must not stop the backend from starting.
        """
        if self._path is None:
            return
        try:
            seen = self._archived_ids()
            merged = 0
            for name in store_names:
                store_file = self._path.with_name(name)
                if not store_file.exists():
                    continue
                data = json.loads(store_file.read_text())
                for slot in data.values():
                    cards = list(slot.get("history") or [])
                    if slot.get("active"):
                        cards.append(slot["active"])
                    for raw in cards:
                        try:
                            card = SignalCard.model_validate(raw)
                        except Exception:
                            continue
                        if card.id in seen:
                            continue
                        seen.add(card.id)
                        self.record(card)
                        merged += 1
            if merged:
                log.info("signal archive: harvested %d card(s) from %s", merged, ", ".join(store_names))
        except Exception:
            log.warning("signal archive boot-merge failed", exc_info=True)


# The archiving instances (see the module docstring). Test-constructed
# stores get archive=None and stay silent. Shadow cards write to their own
# file so live-archive consumers never see counterfactuals.
signal_archive = SignalArchive(_ARCHIVE_PATH)
shadow_signal_archive = SignalArchive(
    _ARCHIVE_PATH.with_name(".shadow_signals_archive.jsonl") if _ARCHIVE_PATH else None)
