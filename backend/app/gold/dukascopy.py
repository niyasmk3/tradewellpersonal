"""XAUUSD 1-minute ingestion from Dukascopy's public datafeed (no account).

One .bi5 file per UTC day:
  https://datafeed.dukascopy.com/datafeed/XAUUSD/{yyyy}/{MM}/{dd}/BID_candles_min_1.bi5

Hard-won specifics baked in (spec §2 — every one of these was paid for):
  * The month in the URL is ZERO-indexed (July = "06").
  * Weekends/holidays 404 — normal, recorded as `missing` so they are never
    refetched. A very recent day may 404 simply because the file is not
    published yet, so the last few days are left unrecorded and retried.
  * The payload is LZMA-compressed in the legacy "alone" container; after
    decompression, 24-byte big-endian `>5If` records: seconds-from-day-start,
    open, CLOSE, LOW, high (integers, ÷1000 for USD), volume float32. Note the
    field order — close before low before high.
  * Dukascopy's edge rejects non-browser TLS clients with 503 — python-requests
    fails even with browser headers, and (measured 03-Sep-2026, this machine)
    so does macOS system curl, whose SecureTransport/LibreSSL fingerprint is
    not the OpenSSL one the reference deployment's curl passed with. Plain
    HTTP carries no TLS fingerprint and the same edge serves it 200, so the
    transport tries https first and falls back to http, remembering the
    working scheme for the rest of the process. Integrity risk of the
    unencrypted leg is accepted knowingly: public market data, whole-day
    sanity band below.
  * Whole-day sanity band on parsed prices (reject a day with any print
    outside 500–20,000 USD) — protects against silent format drift.
"""
from __future__ import annotations

import logging
import lzma
import struct
import subprocess
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from app.gold import store

log = logging.getLogger("tradewell.gold")

_URL = ("{scheme}://datafeed.dukascopy.com/datafeed/XAUUSD/"
        "{y:04d}/{m:02d}/{d:02d}/BID_candles_min_1.bi5")
# Mutable on purpose: the first scheme the edge actually serves is promoted to
# the front, so one probing miss covers the whole 940-file run. The lock guards
# the promotion — 8 workers race here on the first https miss, and an unguarded
# remove() threw ValueError for every loser (review catch).
_SCHEMES = ["https", "http"]
_SCHEME_LOCK = threading.Lock()
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
_REFERER = "https://freeserv.dukascopy.com/"
_RECORD = struct.Struct(">5If")

PRICE_LO_USD = 500.0
PRICE_HI_USD = 20_000.0
WORKERS = 8
# Days newer than this are not marked `missing` on a 404 — the file may simply
# not be published yet, and a permanently-recorded miss would leave a hole.
_RECENT_RETRY_DAYS = 3


class DukascopyError(RuntimeError):
    pass


def url_for(day: date, scheme: str = "https") -> str:
    return _URL.format(scheme=scheme, y=day.year, m=day.month - 1, d=day.day)


def _curl(url: str, timeout_s: int) -> bytes:
    with tempfile.TemporaryDirectory(prefix="tw-bi5-") as tmpdir:
        out = Path(tmpdir) / "day.bi5"
        proc = subprocess.run(
            ["curl", "-s", "--max-time", str(timeout_s),
             "-A", _UA, "-H", f"Referer: {_REFERER}",
             "-o", str(out), "-w", "%{http_code}", url],
            capture_output=True, text=True)
        if proc.returncode != 0:
            raise DukascopyError(f"curl exit {proc.returncode} for {url}")
        code = proc.stdout.strip()
        if code == "404":
            return b""
        if code != "200":
            raise DukascopyError(f"HTTP {code} for {url}")
        return out.read_bytes()


def fetch_day_raw(day: date, timeout_s: int = 45) -> bytes:
    """Raw .bi5 bytes for one UTC day, via curl. Empty bytes = 404 (no file —
    the server answered, so no scheme fallback). Tries the schemes in their
    current preference order and promotes whichever one the edge serves."""
    last_exc: Exception = DukascopyError(f"no scheme attempted for {day}")
    for scheme in list(_SCHEMES):
        try:
            data = _curl(url_for(day, scheme), timeout_s)
        except DukascopyError as exc:
            last_exc = exc
            continue
        if scheme != _SCHEMES[0]:
            with _SCHEME_LOCK:
                if scheme in _SCHEMES and scheme != _SCHEMES[0]:
                    _SCHEMES.remove(scheme)
                    _SCHEMES.insert(0, scheme)
                    log.info("Dukascopy: falling back to %s — the %s edge "
                             "rejects this machine's TLS fingerprint",
                             scheme, "/".join(_SCHEMES[1:]))
        return data
    raise last_exc


def parse_bi5(raw: bytes, day: date) -> list:
    """(ts, open, high, low, close, volume) rows, ts in UTC epoch seconds.
    Raises DukascopyError when any price in the day breaks the sanity band —
    the whole day is rejected, never half-stored."""
    if not raw:
        return []
    blob = lzma.decompress(raw, format=lzma.FORMAT_ALONE)
    if len(blob) % _RECORD.size:
        raise DukascopyError(f"{day}: payload not a multiple of 24 bytes")
    base = int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp())
    rows = []
    for secs, o, c, lo, hi, vol in _RECORD.iter_unpack(blob):
        px = (o / 1000.0, hi / 1000.0, lo / 1000.0, c / 1000.0)
        if any(p < PRICE_LO_USD or p > PRICE_HI_USD for p in px):
            raise DukascopyError(
                f"{day}: price outside {PRICE_LO_USD}-{PRICE_HI_USD} USD — "
                "format drift? day rejected")
        rows.append((base + int(secs), px[0], px[1], px[2], px[3], float(vol)))
    return rows


def backfill(years: int = 3, workers: int = WORKERS) -> dict:
    """Fetch every unrecorded weekday in the window, `workers`-way concurrent
    on the network, single-threaded on the SQLite writes. Resume-safe: a
    re-run only fetches days with no status row. Returns a summary dict."""
    end = datetime.now(timezone.utc).date() - timedelta(days=1)
    start = end - timedelta(days=int(years * 365.25))
    recorded = store.xau_recorded_days()
    todo = []
    d = start
    while d <= end:
        if d.weekday() <= 4 and d.isoformat() not in recorded:
            todo.append(d)
        d += timedelta(days=1)

    summary = {"window": f"{start} → {end}", "requested": len(todo),
               "stored_days": 0, "bars": 0, "missing": 0, "rejected": 0,
               "errors": 0}
    if not todo:
        summary["total_bars"] = store.xau_count()
        return summary

    log.info("XAUUSD backfill: %s day files to fetch (%s → %s)",
             len(todo), start, end)

    def _one(day: date):
        return day, parse_bi5(fetch_day_raw(day), day)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(_one, day): day for day in todo}
        for fut in as_completed(futures):
            day = futures[fut]
            try:
                day, rows = fut.result()
            except DukascopyError as exc:
                if "rejected" in str(exc):
                    store.xau_write_day(day.isoformat(), "rejected", [])
                    summary["rejected"] += 1
                else:
                    summary["errors"] += 1   # unrecorded → retried next run
                    log.warning("XAUUSD %s: %s", day, exc)
                continue
            except Exception as exc:  # pragma: no cover - unexpected transport
                summary["errors"] += 1
                log.warning("XAUUSD %s: %s", day, exc)
                continue
            if rows:
                store.xau_write_day(day.isoformat(), "ok", rows)
                summary["stored_days"] += 1
                summary["bars"] += len(rows)
            elif (end - day).days >= _RECENT_RETRY_DAYS:
                store.xau_write_day(day.isoformat(), "missing", [])
                summary["missing"] += 1
            # else: recent 404 — likely unpublished, leave unrecorded to retry

    store.xau_set_meta("last_xau_sync", datetime.now(timezone.utc).isoformat())
    summary["total_bars"] = store.xau_count()
    log.info("XAUUSD backfill done: %s", summary)
    return summary
