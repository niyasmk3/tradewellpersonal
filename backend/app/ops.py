"""Daily post-close operations: state backup and verdict-threshold watch.

Two jobs, one loop, both running INSIDE the backend process on purpose:
launchd cannot reach this repo while it lives under ~/Documents (macOS TCC
denies file access to background agents), but the engine process itself was
started from a terminal the user already granted access — so the process that
OWNS the state files is the only reliable scheduler for jobs that read them.
The state changes exactly when the engine runs, so backup-when-running is
also the correct coverage, not a compromise.

  * BACKUP (05-Aug): every ledger the evidence program depends on — the paper
    book, the shadow archives, the journals — exists as a single copy on one
    laptop. One tarball per day into an iCloud-synced folder makes any laptop
    disposable. Off by default; STATE_BACKUP_DIR arms it.
  * VERDICT WATCH (05-Aug): the ledgers render verdicts, but a verdict nobody
    reads changes nothing — the exit A/B, the scalp audition and the floor's
    30-fill read all mature on their own clocks. When a ledger first crosses
    its evidence bar, ONE audible push announces it. Announcements are
    once-ever per ledger (persisted), policy flips stay human.

The decision logic is pure functions over plain data (no clock, no network,
no event loop) so it unit-tests the same way FeedWatchdog does.
"""
from __future__ import annotations

import asyncio
import json
import logging
import tarfile
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("tradewell.ops")

IST = timezone(timedelta(hours=5, minutes=30))
# Post-close line: the paper monitor's 15:20 time exits and the 15:15-15:35
# CAS window are both settled by now, so the day's rows are final.
_RUN_AFTER_HM = (15, 40)
_STATE_FILE = ".daily_ops.json"
# Regenerated from Kite on every boot — bulk without evidence value.
_BACKUP_EXCLUDE = {".instruments_cache.json"}


# ---- state ------------------------------------------------------------------

def load_state(path: Path | None = None) -> dict:
    p = path or Path(_STATE_FILE)
    try:
        d = json.loads(p.read_text())
        if isinstance(d, dict):
            d.setdefault("announced", [])
            return d
    except Exception:
        pass
    return {"backup_date": None, "backup_fail_paged": None,
            "verdict_date": None, "announced": []}


def save_state(state: dict, path: Path | None = None) -> None:
    p = path or Path(_STATE_FILE)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(p)


# ---- backup -----------------------------------------------------------------

def backup_paths(src_dir: Path) -> list[Path]:
    """Every state file worth surviving the laptop: .env (secrets + tuned
    knobs), the dot-JSON stores/journals, the append-only archives, and the
    Patterns lab's candle cache."""
    out = []
    for pat in (".env", ".*.json", ".*.jsonl", ".patterns_candles.db",
                ".condor_chain.db"):
        for p in sorted(src_dir.glob(pat)):
            if p.is_file() and p.name not in _BACKUP_EXCLUDE:
                out.append(p)
    return out


def make_backup(src_dir: Path, dest_dir: Path, date_tag: str, keep: int = 14) -> Path:
    """One tarball per IST day, written atomically, oldest pruned to `keep`.

    Overwrite-by-rename on the same day is deliberate: a ⟳ redeploy evening
    may run this twice, and the later tarball is simply the truer snapshot.
    """
    files = backup_paths(src_dir)
    if not files:
        raise RuntimeError(f"no state files found under {src_dir}")
    dest_dir.mkdir(parents=True, exist_ok=True)
    final = dest_dir / f"tradewell-state-{date_tag}.tgz"
    # Temp file on the DESTINATION filesystem so replace() stays atomic.
    fd_path = tempfile.mktemp(prefix=".tw-backup-", dir=str(dest_dir))
    try:
        with tarfile.open(fd_path, "w:gz") as tar:
            for p in files:
                tar.add(str(p), arcname=p.name)
        Path(fd_path).replace(final)
    finally:
        Path(fd_path).unlink(missing_ok=True)
    for old in sorted(dest_dir.glob("tradewell-state-*.tgz"))[:-max(keep, 1)]:
        old.unlink(missing_ok=True)
    return final


# ---- verdict watch ----------------------------------------------------------

def _stats_line(d: dict) -> str:
    return (f"net ₹{d.get('net_pnl', 0)}, expectancy ₹{d.get('expectancy', 0)}"
            f"/trade, win rate {d.get('win_rate', 0)}%")


def verdict_events(summary: dict, announced: set) -> list[dict]:
    """Ledgers that JUST crossed their evidence bar — each announced once ever.

    Reads the same summarize() dict the dashboard shows, so the push and the
    screen can never disagree about a verdict. Every threshold mirrors the
    bar written into the ledger's own docstring: 30 closed fills for the
    counterfactual ledgers, 30 DIVERGED pairs for the paired A/Bs (agreeing
    pairs carry no information), 50 for scalp's live gate, 10 lock-outs for
    the early-derisk aftermath.
    """
    events = []

    def ready(key: str, title: str, body: str) -> None:
        if key not in announced:
            events.append({"key": key, "title": title, "body": body})

    ab = summary.get("exit_ab") or {}
    if ab.get("n_diverged", 0) >= 30:
        ready("exit_ab_30", "Tradewell verdict ready: exit A/B",
              f"{ab.get('n_diverged')} diverged fills. "
              f"{ab.get('verdict', '')} Decision: QUICK_BANK_SINGLE_LOT.")
    sb = summary.get("stop_ab") or {}
    if sb.get("n_diverged", 0) >= 30:
        ready("stop_ab_30", "Tradewell verdict ready: stop-basis A/B",
              f"{sb.get('n_diverged')} diverged pairs. "
              f"{sb.get('verdict', '')} Decision: STOP_PRIMARY.")
    sc = summary.get("stop_calib") or {}
    if sc.get("n_diverged", 0) >= 30:
        ready("stop_calib_30", "Tradewell verdict ready: calibrated stop",
              f"{sc.get('n_diverged')} diverged pairs. "
              f"{sc.get('verdict', '')} Decision: intraday premium SL%.")
    scalp = (summary.get("by_mode") or {}).get("scalp") or {}
    if scalp.get("trades", 0) >= 50:
        ready("scalp_50", "Tradewell verdict ready: scalp audition",
              f"{scalp.get('trades')} honest scalp fills — {_stats_line(scalp)}. "
              "Gate decision: SCALP_LIVE_ENABLED (positive net-of-charges "
              "expectancy required).")
    for key, block, name, knob in (
        ("floor_30", "hollow", "participation floor", "SIGNAL_MIN_VOLUME/OI_SCORE"),
        ("late_30", "late_shadow", "14:15 cutoff", "SIGNAL_ENTRY_CUTOFF_IST"),
        ("refire_30", "refire_shadow", "re-fire guard", "SIGNAL_REFIRE_GUARD_S"),
        ("confirm_30", "confirm_shadow", "WATCH->CONFIRM gate", "SIGNAL_CONFIRM_BARS"),
    ):
        b = summary.get(block) or {}
        if b.get("trades", 0) >= 30:
            ready(key, f"Tradewell verdict ready: {name}",
                  f"{b.get('trades')} vetoed-card fills closed — {_stats_line(b)}. "
                  f"Negative = the veto earns its keep. Decision: {knob}.")
    da = summary.get("derisk_aftermath") or {}
    if da.get("locked_out", 0) >= 10:
        ready("derisk_10", "Tradewell verdict ready: early de-risk",
              f"{da.get('locked_out')} lock-outs recorded. "
              f"{da.get('verdict', da.get('note', ''))} Decision: EARLY_DERISK_MFE_PCT.")
    return events


# ---- the daily driver -------------------------------------------------------

def _ist_now() -> datetime:
    return datetime.now(IST)


def run_daily_ops_once(now: datetime | None = None) -> dict:
    """One post-close pass, self-gated by the state file; safe to call every
    few minutes. Returns what it did, for logs and tests."""
    from app.config import get_settings
    from app.notify import push_text

    ist = now or _ist_now()
    done: dict = {"backup": None, "verdicts": []}
    if (ist.hour, ist.minute) < _RUN_AFTER_HM:
        return done
    today = ist.date().isoformat()
    state = load_state()
    cfg = get_settings()

    # BACKUP — retried each pass until it succeeds (iCloud may be briefly
    # unavailable), but a failure pages at most once per day.
    if cfg.state_backup_dir and state.get("backup_date") != today:
        try:
            dest = Path(cfg.state_backup_dir).expanduser()
            out = make_backup(Path.cwd(), dest, today, keep=cfg.state_backup_keep)
            state["backup_date"] = today
            done["backup"] = str(out)
            log.info("ops: state backed up -> %s", out)
        except Exception as exc:
            log.warning("ops: backup failed: %s", exc)
            if state.get("backup_fail_paged") != today:
                state["backup_fail_paged"] = today
                push_text(
                    "Tradewell backup FAILED",
                    f"Tonight's state backup did not write: {exc}. The evidence "
                    "ledgers exist only on this laptop until it succeeds.",
                    cfg,
                )

    # VERDICT WATCH — once per day, after the book has settled.
    if state.get("verdict_date") != today:
        try:
            from app.paper.service import summarize
            from app.services import feed

            store = getattr(feed, "paper_store", None)
            if store is not None:
                summary = summarize(
                    store, exit_slippage_pct=cfg.paper_slippage_pct,
                    quick_bank_live=cfg.quick_bank_single_lot)
                events = verdict_events(summary, set(state.get("announced", [])))
                for ev in events:
                    # Audible on purpose (no min priority): a matured verdict
                    # is actionable — but PRIVATE, it grades your own book.
                    push_text(ev["title"], ev["body"], cfg)
                    state["announced"] = sorted(set(state["announced"]) | {ev["key"]})
                    done["verdicts"].append(ev["key"])
                    log.info("ops: verdict announced: %s", ev["key"])
                state["verdict_date"] = today
        except Exception:
            log.warning("ops: verdict watch failed", exc_info=True)

    save_state(state)
    return done


async def ops_loop(interval: float = 300.0) -> None:
    """App-lifetime task, started next to the watchdog in lifespan."""
    while True:
        await asyncio.sleep(interval)
        try:
            run_daily_ops_once()
        except Exception:  # pragma: no cover - ops must outlive their own bugs
            log.debug("ops pass failed", exc_info=True)
