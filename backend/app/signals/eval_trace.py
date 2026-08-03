"""Per-bar evaluation trace — the blind-spot instrumentation (audit P1-2).

WHY THIS EXISTS: the 30-Jul audit found 36 of 41 missed moves had NO candidate
at all, and could not attribute 27 of them any further — regime-vote lockout,
non-tradeable label and slot-busy all look identical in hindsight because the
engine records only what it OFFERED, never what it scored and declined. This
file records the decline: one JSONL line per closed bar per (symbol, mode)
with both directions' scores, the regime vote, and the exact veto that stopped
a card. It is the dataset that sizes the setup-detection prize (P1-4) — how
many missed moves had a bear score ≥ gate while the regime voted bull, how
many died in the warm-up window, how many were slot-starved — measured, not
guessed.

Sampling: the FIRST evaluation of each closed bar (the eval loop re-runs every
~5s; the score is constant within a bar, so later evaluations of the same bar
add nothing but time-keyed veto flips — rare and not worth 60x the rows).
~375 lines/day at three modes. Retention is pruned at APP BOOT from the
lifespan (never at import — the archive's fixture-pollution lesson applies
verbatim: collecting the test suite must not touch live files).

Never raises into the caller: recorded from the live evaluation loop, and a
diagnostics feature must not be able to stop signal generation.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

log = logging.getLogger("tradewell.signals")

_TRACE_PATH = Path(__file__).resolve().parents[2] / ".eval_trace.jsonl"


class EvalTrace:
    def __init__(self, path: Path | None) -> None:
        """path=None -> disabled (unit tests construct their own tmp paths).
        Construction performs NO I/O; pruning is called from the app lifespan.
        """
        self._path = path
        self._lock = threading.Lock()
        # (symbol, mode) -> last bar_ts written; the once-per-bar dedupe. A
        # restart forgets it, costing at most one duplicate line per key —
        # readers dedupe on (symbol, mode, bar) keeping the first line.
        self._last_bar: dict[tuple[str, str], int] = {}

    def record(self, entry: dict) -> None:
        """Append one bar's evaluation snapshot, once per (symbol, mode, bar).

        `entry` must carry symbol/mode/bar; everything else rides through
        verbatim. Swallows everything — see module doc.
        """
        if self._path is None:
            return
        try:
            key = (str(entry["symbol"]), str(entry["mode"]))
            bar = int(entry["bar"])
            with self._lock:
                if self._last_bar.get(key) == bar:
                    return
                line = json.dumps(entry, default=str)
                with open(self._path, "a") as fh:
                    fh.write(line + "\n")
                self._last_bar[key] = bar
        except Exception:
            log.debug("eval trace write failed", exc_info=True)

    def load(self, days: int = 1, symbol: str | None = None,
             mode: str | None = None) -> list[dict]:
        """Trace lines from the last `days`, oldest first, deduped on
        (symbol, mode, bar) keeping the FIRST line (the bar's first eval is
        the definitive sample; a restart's duplicate adds nothing).
        Corrupt lines are skipped — a torn line must not blank the dataset.
        """
        if self._path is None or not self._path.exists():
            return []
        cutoff = int(time.time()) - days * 86400
        out: list[dict] = []
        seen: set[tuple] = set()
        try:
            for raw in self._path.read_text().splitlines():
                try:
                    e = json.loads(raw)
                    if (e.get("ts") or 0) < cutoff:
                        continue
                    if symbol and str(e.get("symbol", "")).upper() != symbol.upper():
                        continue
                    if mode and e.get("mode") != mode:
                        continue
                    k = (e.get("symbol"), e.get("mode"), e.get("bar"))
                    if k in seen:
                        continue
                    seen.add(k)
                    out.append(e)
                except Exception:
                    continue
        except Exception:
            log.warning("eval trace unreadable", exc_info=True)
            return []
        out.sort(key=lambda e: (e.get("bar") or 0, e.get("ts") or 0))
        return out

    def prune(self, keep_days: int) -> None:
        """Drop lines older than `keep_days`. Called from the app lifespan at
        boot (never at import). keep_days<=0 means the trace is disabled —
        prune everything so a turned-off knob doesn't leave a growing file.
        Never raises — maintenance must not stop the backend from starting.
        """
        if self._path is None or not self._path.exists():
            return
        try:
            cutoff = int(time.time()) - max(0, keep_days) * 86400
            with self._lock:
                lines = self._path.read_text().splitlines()
                kept = []
                for raw in lines:
                    try:
                        if keep_days > 0 and (json.loads(raw).get("ts") or 0) >= cutoff:
                            kept.append(raw)
                    except Exception:
                        continue                 # torn line: dropped at prune
                if len(kept) != len(lines):
                    tmp = self._path.with_suffix(".jsonl.tmp")
                    tmp.write_text("\n".join(kept) + ("\n" if kept else ""))
                    tmp.replace(self._path)
                    log.info("eval trace pruned: %d -> %d line(s)", len(lines), len(kept))
        except Exception:
            log.warning("eval trace prune failed", exc_info=True)


# The ONLY live-writing instance (same singleton rule as the archive):
# test-constructed instances get their own tmp paths and stay silent.
eval_trace = EvalTrace(_TRACE_PATH)
