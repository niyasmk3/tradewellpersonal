"""Gold live evaluation loop (spec §6) — paper cards, state-transition alerts.

App-lifetime task next to the watchdog, but gated per-pass by
GOLD_LIVE_ENABLED (default off, same shadow-first posture as CONDOR_ENABLED):
the loop always exists, and flipping the flag in .env + a restart arms it.

Each pass during MCX hours recomputes simulate_day(live=True) from a fresh
candle pull — stateless recomputation makes the loop restart-proof with no
open-position persistence. Alerts fire on STATE TRANSITIONS only, are
suppressed on the first pass after a restart (no replay spam), and every one
is labelled PAPER/TRIAL. Fail-soft everywhere: a stale token or network error
logs and waits for the next pass; nothing here may crash or block the main
app's pipeline.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from app.gold.rules import IST
from app.gold.service import _SESSION_OPEN, live_cards

log = logging.getLogger("tradewell.gold")

_SESSION_CLOSE = (23, 55)   # the venue's US-winter close; summer just idles after 23:30


def transitions(prev: dict, cards: list) -> list:
    """Cards whose state changed into open/closed since the last pass — pure
    over plain data so it unit-tests like the watchdog. `prev` maps rule key ->
    (state, exit_reason)."""
    out = []
    for c in cards:
        key = (c["state"], c.get("exit_reason"))
        if prev.get(c["rule"]) != key and c["state"] in ("open", "closed"):
            out.append(c)
    return out


def states_of(cards: list) -> dict:
    return {c["rule"]: (c["state"], c.get("exit_reason")) for c in cards}


def _alert_body(c: dict) -> str:
    if c["state"] == "open":
        return (f"{c['name']}: paper {c['direction']} @ {c['entry_px']} "
                f"(signal {c['signal_pct']}%). Target {c['target_px']}, "
                f"stop {c['stop_px']}.")
    return (f"{c['name']}: paper {c['direction']} closed on {c['exit_reason']} "
            f"@ {c['exit_px']} — net ₹{c['net_rs']:.0f} (idealised fills).")


async def gold_live_loop() -> None:
    from app.config import get_settings
    from app.kite.client import kite_service
    from app.notify import push_text

    prev = None  # None = first active pass: record states, alert nothing
    while True:
        await asyncio.sleep(get_settings().gold_live_poll_s)
        try:
            cfg = get_settings()
            now = datetime.now(IST)
            in_hours = (now.weekday() <= 4
                        and _SESSION_OPEN <= (now.hour, now.minute) <= _SESSION_CLOSE)
            if not (cfg.gold_live_enabled and in_hours
                    and kite_service.is_authenticated):
                # Reset across any inactive stretch so the first pass back is
                # a record-only pass — re-enabling mid-day must not replay the
                # morning's transitions as fresh alerts.
                prev = None
                continue
            data = await asyncio.to_thread(live_cards, kite_service.kite)
            cards = data["cards"]
            if prev is not None:
                for c in transitions(prev, cards):
                    push_text(f"Gold {c['rule']} · PAPER TRIAL", _alert_body(c), cfg)
                    log.info("Gold live transition: %s -> %s", c["rule"], c["state"])
            prev = states_of(cards)
        except Exception:  # pragma: no cover - the loop must outlive its bugs
            log.debug("Gold live pass failed", exc_info=True)
