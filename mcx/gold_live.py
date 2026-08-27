#!/usr/bin/env python3
"""Live feeder for the gold dashboard (frontend/public/gold.html).

During MCX hours (Mon-Fri 09:00-23:30 IST) it refreshes today's 3-minute
candles for the current GOLD contract once a minute (one historical-API call)
and writes frontend/public/gold_live.json for the page to poll. Off-hours it
sleeps. Fail-soft: any error waits and retries — this loop must never crash
into launchd respawn spirals.

NO SIGNALS. The page renders watch-conditions for the pre-registered
hypothesis CANDIDATES (gap fill, 09:00 burst, US window) — labelled as
experiments, never as trade advice. Cards appear only if P2 grading ever
earns them, the same road the condor walked.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backfill import find_access_token, read_env_key  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "frontend" / "public" / "gold_live.json"
CLIM = Path(__file__).resolve().parents[1] / "data" / "gold_climatology.json"
IST = timezone(timedelta(hours=5, minutes=30))


def in_session(now: datetime) -> bool:
    return now.weekday() < 5 and (9, 0) <= (now.hour, now.minute) <= (23, 30)


def connect():
    from kiteconnect import KiteConnect
    kite = KiteConnect(api_key=read_env_key("KITE_API_KEY"))
    kite.set_access_token(find_access_token())
    futs = sorted((i for i in kite.instruments("MCX")
                   if i.get("name") == "GOLD"
                   and i.get("instrument_type") == "FUT"),
                  key=lambda i: i.get("expiry") or datetime.max)
    return kite, futs[0]


def snapshot(kite, fut) -> dict:
    now = datetime.now(IST)
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    bars = kite.historical_data(fut["instrument_token"],
                                day0 - timedelta(days=7), now, "3minute")
    today = [b for b in bars if b["date"].astimezone(IST) >= day0]
    prev_close = next((b["close"] for b in reversed(bars)
                       if b["date"].astimezone(IST) < day0), None)
    clim = {}
    try:
        clim = json.loads(CLIM.read_text())
    except Exception:
        pass
    # Live TRIAL evaluation — the exact simulate_day the ledger grades with.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from hypotheses import RULES, simulate_day
    b5 = [[int(b["date"].timestamp()), b["open"], b["high"],
           b["low"], b["close"]] for b in today]
    live_trades = {t["rule"]: t for t in simulate_day(b5, prev_close,
                                                      live=True)}
    cards = []
    for rule, meta in RULES.items():
        t = live_trades.get(rule)
        state = ("open" if t and t.get("exit") is None
                 else "closed" if t else "none")
        cards.append({"rule": rule, "label": meta["label"],
                      "qualify": meta["qualify"], "state": state,
                      **{k: t.get(k) for k in
                         ("dir", "entry", "exit", "reason", "pct",
                          "rupees", "last") if t}})

    return {
        "updated": now.strftime("%H:%M:%S IST"),
        "cards": cards,
        "contract": fut.get("tradingsymbol"),
        "expiry": str(fut.get("expiry") or "")[:10],
        "prev_close": prev_close,
        "bars": [[int(b["date"].timestamp()), b["open"], b["high"],
                  b["low"], b["close"]] for b in today],
        "clim": {
            "avg_range_pct": (clim.get("mcx_day") or {}).get("avg_range_pct"),
            "avg_gap_pct": (clim.get("mcx_day") or {}).get("avg_gap_pct"),
            "gap_over_half_share": (clim.get("mcx_day") or {}).get(
                "gap_over_half_pct_share"),
            "hour_shares": clim.get("mcx_hour_shares_ist") or {},
        },
    }


def push(msg: str) -> None:
    """TRIAL-card alert to the same Telegram webhook the NIFTY stack uses.
    Every message says TRIAL/paper — these are experiments, never advice."""
    url = read_env_key("ALERT_WEBHOOK_URL")
    if not url:
        return
    try:
        import requests
        requests.post(url, json={"text": msg}, timeout=6)
    except Exception:
        pass


def main() -> int:
    kite = fut = None
    fut_day = None
    prev_state: dict[str, str] = {}
    while True:
        now = datetime.now(IST)
        if not in_session(now):
            time.sleep(300)
            continue
        try:
            if kite is None or fut_day != now.date():
                kite, fut = connect()
                fut_day = now.date()
            data = snapshot(kite, fut)
            tmp = OUT.with_suffix(".tmp")
            tmp.write_text(json.dumps(data))
            tmp.replace(OUT)
            # Alert on TRIAL state changes (first pass records silently, so a
            # feeder restart never replays old cards to the phone).
            for c in data.get("cards", []):
                r, s = c["rule"], c["state"]
                was = prev_state.get(r)
                if was is not None and was != s:
                    if s == "open":
                        push(f"🥇 GOLD TRIAL {r}: OPEN {c.get('dir')} @ "
                             f"{c.get('entry')} — paper only, no order exists")
                    elif s == "closed":
                        push(f"🥇 GOLD TRIAL {r}: closed via {c.get('reason')} "
                             f"{c.get('pct', 0):+.2f}% (₹{c.get('rupees', 0):+,} "
                             f"paper, 1 GOLDM lot)")
                prev_state[r] = s
        except Exception as e:
            print(f"{now:%H:%M:%S} gold_live: {e} — retrying", flush=True)
            kite = None            # forces re-auth pickup after morning login
            time.sleep(120)
            continue
        time.sleep(60)


if __name__ == "__main__":
    sys.exit(main())
