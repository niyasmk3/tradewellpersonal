"""Real-quote legs on the Closing ledger: fetch-once caching, study-convention
leg picking (15:00 bar CLOSE in, 09:50 bar OPEN out), charge parity with the
modelled ledger, and the forward-only honesty rules (expired contracts are
skipped without an API call; unknown nights stay None)."""
from __future__ import annotations

import os
import sys
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.closing import realquote
from app.closing.calendar import IST
from app.paper.charges import charges

TODAY = datetime.now(IST).date()
NIGHT = TODAY - timedelta(days=1)
EXIT = TODAY
EXPIRY = TODAY + timedelta(days=6)


def _trade(night=NIGHT, exit_d=EXIT, expiry=EXPIRY, direction="PE",
           strike=24000.0, net_rs=5200.36, qty=65):
    return {"date": night.isoformat(), "exit_date": exit_d.isoformat(),
            "expiry": expiry.isoformat(), "direction": direction,
            "strike": strike, "qty": qty, "net_rs": net_rs}


def _bar(d, h, m, o, c):
    return {"date": datetime(d.year, d.month, d.day, h, m, tzinfo=IST),
            "open": o, "high": max(o, c), "low": min(o, c), "close": c}


class _Kite:
    """historical_data stub that also counts calls — the cache tests hinge on
    how many times the tape was asked, not just what came back."""

    def __init__(self, bars):
        self.bars = bars
        self.calls = 0

    def historical_data(self, token, frm, to, interval):
        self.calls += 1
        return self.bars


def _nfo(monkeypatch, rows):
    monkeypatch.setattr(realquote, "_nfo_instruments", lambda kite: rows)
    monkeypatch.setattr(realquote, "_FETCH_SLEEP_S", 0)


_CONTRACT = [{"name": "NIFTY", "instrument_type": "PE", "strike": 24000.0,
              "expiry": EXPIRY.isoformat(), "instrument_token": 10915842}]


def test_backfill_and_annotate_stamp_real_legs(tmp_path, monkeypatch):
    path = tmp_path / "rq.json"
    _nfo(monkeypatch, _CONTRACT)
    kite = _Kite([_bar(NIGHT, 15, 0, 127.05, 131.85),   # entry = this CLOSE
                  _bar(NIGHT, 15, 5, 131.90, 134.55),   # decoy — wrong clock
                  _bar(EXIT, 9, 50, 205.75, 210.80)])   # exit = this OPEN
    t = _trade()
    realquote.backfill(kite, [t], path=path)
    realquote.annotate([t], path=path)
    assert t["real_in"] == 131.85
    assert t["real_out"] == 205.75
    # Same charge schedule as the modelled ledger: the two nets may differ
    # only through the premium legs.
    net = (205.75 - 131.85) * 65 - charges(131.85, 205.75, 65, 2)
    assert abs(t["real_net_rs"] - round(net, 2)) < 0.01
    assert abs(t["real_net_pct"] - round(net / (131.85 * 65) * 100, 2)) < 0.01
    assert abs(t["real_delta_rs"] - round(net - t["net_rs"], 2)) < 0.01


def test_cached_night_is_never_refetched(tmp_path, monkeypatch):
    path = tmp_path / "rq.json"
    _nfo(monkeypatch, _CONTRACT)
    kite = _Kite([_bar(NIGHT, 15, 0, 127.0, 131.85), _bar(EXIT, 9, 50, 205.75, 210.8)])
    t = _trade()
    realquote.backfill(kite, [t], path=path)
    realquote.backfill(kite, [t], path=path)
    assert kite.calls == 1


def test_expired_contract_skipped_without_api_call(tmp_path, monkeypatch):
    path = tmp_path / "rq.json"
    _nfo(monkeypatch, _CONTRACT)
    kite = _Kite([])
    old = _trade(night=TODAY - timedelta(days=30), exit_d=TODAY - timedelta(days=29),
                 expiry=TODAY - timedelta(days=25))
    out = realquote.backfill(kite, [old], path=path)
    assert kite.calls == 0
    assert out["skipped_expired"] == 1


def test_partial_night_completes_on_a_later_run(tmp_path, monkeypatch):
    """Tonight's card has an entry print but no 09:50 bar yet; the morning
    run must complete the same cache row rather than losing the entry leg."""
    path = tmp_path / "rq.json"
    _nfo(monkeypatch, _CONTRACT)
    kite = _Kite([_bar(NIGHT, 15, 0, 127.0, 131.85)])
    t = _trade()
    realquote.backfill(kite, [t], path=path)
    realquote.annotate([t], path=path)
    assert t["real_in"] == 131.85
    assert t["real_out"] is None and t["real_net_rs"] is None
    kite.bars = kite.bars + [_bar(EXIT, 9, 50, 205.75, 210.8)]
    realquote.backfill(kite, [t], path=path)
    realquote.annotate([t], path=path)
    assert kite.calls == 2
    assert t["real_out"] == 205.75 and t["real_net_rs"] is not None


def test_unknown_nights_and_missing_kite_stay_none(tmp_path):
    t = _trade()
    realquote.backfill(None, [t], path=tmp_path / "rq.json")
    realquote.annotate([t], path=tmp_path / "rq.json")
    assert all(t[k] is None for k in realquote.FIELDS)


def test_max_fetch_caps_a_run_newest_first(tmp_path, monkeypatch):
    path = tmp_path / "rq.json"
    _nfo(monkeypatch, _CONTRACT)
    kite = _Kite([])
    trades = [_trade(night=NIGHT - timedelta(days=i), exit_d=EXIT - timedelta(days=i))
              for i in range(4, -1, -1)]   # ledger order: oldest first
    realquote.backfill(kite, trades, path=path, max_fetch=2)
    assert kite.calls == 2
