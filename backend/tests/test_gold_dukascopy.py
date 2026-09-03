"""Gold data plumbing: the bi5 parser's hard-won specifics and the two stores.

The parser facts pinned here all came out of someone's debugging time (spec
§2): the ZERO-indexed month in the URL, the open-CLOSE-LOW-high field order,
the ÷1000 integer prices, the LZMA "alone" container, and the whole-day
sanity band that catches silent format drift.
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import lzma
import struct
from datetime import date, datetime, timezone

import pytest

from app.gold import dukascopy, store


def _pack(*records):
    blob = b"".join(struct.pack(">5If", *r) for r in records)
    return lzma.compress(blob, format=lzma.FORMAT_ALONE)


def test_url_month_is_zero_indexed():
    assert "/2026/06/15/" in dukascopy.url_for(date(2026, 7, 15))
    assert "/2026/00/02/" in dukascopy.url_for(date(2026, 1, 2))


def test_parse_bi5_field_order_and_scaling():
    day = date(2026, 7, 15)
    # File order: secs, open, CLOSE, LOW, high (ints ÷1000), volume float32.
    raw = _pack((60, 2350123, 2351000, 2349000, 2352000, 123.5))
    rows = dukascopy.parse_bi5(raw, day)
    base = int(datetime(2026, 7, 15, tzinfo=timezone.utc).timestamp())
    assert len(rows) == 1
    ts, o, hi, lo, c, vol = rows[0]
    assert ts == base + 60
    assert (o, hi, lo, c) == (2350.123, 2352.0, 2349.0, 2351.0)
    assert abs(vol - 123.5) < 1e-3


def test_parse_bi5_rejects_whole_day_outside_sanity_band():
    raw = _pack((0, 2350000, 2351000, 2349000, 2352000, 1.0),
                (60, 100000, 101000, 99000, 102000, 1.0))   # 100 USD — drift
    with pytest.raises(dukascopy.DukascopyError, match="rejected"):
        dukascopy.parse_bi5(raw, date(2026, 7, 15))


def test_parse_bi5_empty_and_misaligned():
    assert dukascopy.parse_bi5(b"", date(2026, 7, 15)) == []
    bad = lzma.compress(b"x" * 25, format=lzma.FORMAT_ALONE)
    with pytest.raises(dukascopy.DukascopyError, match="24 bytes"):
        dukascopy.parse_bi5(bad, date(2026, 7, 15))


@pytest.fixture()
def gold_stores(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "MCX_DB_PATH", tmp_path / "mcx.db")
    monkeypatch.setattr(store, "XAU_DB_PATH", tmp_path / "xau.db")
    monkeypatch.setattr(store, "RESULTS_PATH", tmp_path / "results.json")
    return store


def test_mcx_store_roundtrip_and_prev_close(gold_stores):
    rows = [(1000, 1.0, 2.0, 0.5, 1.5, 10.0),
            (2000, 1.5, 2.5, 1.0, 2.0, 11.0),
            (3000, 2.0, 3.0, 1.5, 2.5, 12.0)]
    assert gold_stores.upsert_mcx("GOLDM", "3m", rows) == 3
    assert gold_stores.mcx_count("GOLDM", "3m") == 3
    assert gold_stores.mcx_count("GOLDM", "day") == 0
    assert gold_stores.mcx_last_ts("GOLDM", "3m") == 3000
    # prev_close is the last bar STRICTLY before the boundary.
    assert gold_stores.mcx_prev_close("GOLDM", "3m", 3000) == 2.0
    assert gold_stores.mcx_prev_close("GOLDM", "3m", 1000) is None
    frame = gold_stores.load_mcx("GOLDM", "3m")
    assert list(frame["ts"]) == [1000, 2000, 3000]
    # Re-upsert dedupes on the (symbol, timeframe, ts) key.
    gold_stores.upsert_mcx("GOLDM", "3m", rows[:1])
    assert gold_stores.mcx_count("GOLDM", "3m") == 3


def test_xau_store_day_statuses_drive_resume(gold_stores):
    gold_stores.xau_write_day("2026-07-15", "ok",
                              [(100, 2350.0, 2352.0, 2349.0, 2351.0, 1.0)])
    gold_stores.xau_write_day("2026-07-18", "missing", [])
    gold_stores.xau_write_day("2026-07-19", "rejected", [])
    assert gold_stores.xau_recorded_days() == {"2026-07-15", "2026-07-18", "2026-07-19"}
    assert gold_stores.xau_count() == 1
    assert gold_stores.xau_day_counts() == {"ok": 1, "missing": 1, "rejected": 1}


def test_results_roundtrip_stamps_generated_at(gold_stores):
    assert gold_stores.load_results() is None
    gold_stores.save_results({"hello": "gold"})
    back = gold_stores.load_results()
    assert back["hello"] == "gold" and back["generated_at"]


def test_scheme_promotion_race_is_safe(monkeypatch):
    """Review catch: 8 backfill workers race the https->http promotion; an
    unguarded list.remove threw ValueError for every loser, spuriously
    failing days whose bytes were already downloaded."""
    import threading
    from datetime import date

    monkeypatch.setattr(dukascopy, "_SCHEMES", ["https", "http"])

    def fake_curl(url, timeout_s):
        if url.startswith("https"):
            raise dukascopy.DukascopyError("HTTP 503 for " + url)
        return b""   # http answers (404 -> empty)

    monkeypatch.setattr(dukascopy, "_curl", fake_curl)
    errors = []

    def worker():
        try:
            assert dukascopy.fetch_day_raw(date(2026, 7, 15)) == b""
        except Exception as exc:  # pragma: no cover - the bug under test
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert dukascopy._SCHEMES == ["http", "https"]


def test_run_sync_completes_old_tail_and_never_rewrites_on_roll(gold_stores, monkeypatch):
    """Review catch: on a contract roll, refetching the overlap days with the
    NEW token silently rewrote already-graded sessions with a different
    contract's tape. The old token finishes its own tail; the new token only
    writes days the store has never seen."""
    from datetime import date, datetime

    from app.gold import service

    old_ts = int(datetime(2026, 9, 2, 9, 0, tzinfo=service.IST).timestamp())
    gold_stores.upsert_mcx("GOLDM", "3m", [(old_ts, 1.0, 2.0, 0.5, 1.5, 0.0)])
    gold_stores.set_meta("mcx_contract", "GOLDM26SEPFUT")
    gold_stores.set_meta("mcx_token", "111")

    calls = []

    class K:
        def instruments(self, seg):
            return [{"name": "GOLDM", "instrument_type": "FUT",
                     "expiry": date(2026, 10, 5),
                     "tradingsymbol": "GOLDM26OCTFUT", "instrument_token": 222}]

        def historical_data(self, token, frm, to, interval, continuous=False):
            calls.append((token, frm, interval))
            return []

    monkeypatch.setattr(service, "_contract_cache", {})
    service.run_sync(K())
    m3 = [c for c in calls if c[2] == "3minute"]
    assert m3[0][0] == 111                       # old tail, old token
    assert all(c[0] == 222 for c in m3[1:])      # then the new contract only
    assert m3[1][1].date() >= date(2026, 9, 3)   # starting AFTER the stored day
    assert gold_stores.mcx_count("GOLDM", "3m") == 1
    assert gold_stores.get_meta("mcx_token") == "222"
