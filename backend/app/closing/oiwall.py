"""OI wall — the option chain's heaviest strike in the trade's path. SHADOW.

PRE-REGISTERED 2026-08-22. From the PREVIOUS session's close-of-day open
interest on the expiry the trade holds (NSE's daily F&O bhavcopy — the same
number whether read live on the night or backfilled years later):

    call wall = max-OI CE strike within +/-SPAN of the 15:00 print
    put wall  = max-OI PE strike within +/-SPAN of the 15:00 print
    ahead     = (call wall - print) for CE, (print - put wall) for PE
    state     = ROAD   when ahead > ROAD_PTS
                CAPPED when 0 < ahead <= ROAD_PTS
                BEHIND when ahead <= 0         (the wall is already passed)
    pcr       = sum PE OI / sum CE OI over the same +/-SPAN strikes

THE EVIDENCE (22-Aug, 198 CLEAN nights, 734 bhavcopies). Discovered on the
1y holdout: ROAD 61% win / +49% / median +21 vs 49% / +12% / -3 for the rest,
Spearman wall-ahead vs index move +0.25. Replicated on the 2y in-sample it had
never seen: the HIT RATE held (ROAD 55% vs 40%, bootstrap P 0.95) and so did
the median ordering, but the P&L magnitude did NOT (ROAD mean +9% vs +16%,
P 0.34 — the holdout's big ROAD nights were discovery luck). The most
consistent cell across both windows is the negative one: a wall within
ROAD_PTS ahead (CAPPED) won 32-45% and, within one strike, the index finished
the trade's way under half the time in both windows. PCR 'already positioned
your way' was <= 0 in both windows but mean-only; futures basis flipped sign
and is not carried. FLAGGED nights with open road still lost (-10% / -7%):
an intensifier inside CLEAN, never a rescue.

So the honest expectation is "capped vs not", not "open road pays big". The
raw numbers (walls, distance, PCR) are what get logged, so the live sample
can grade any binning later without a re-registration. Shadow only — chip
on the card, values in the decision log, ledger stamp, backfill table. It
never touches the five checks or the verdict. Editing SPAN or ROAD_PTS
resets the live count.

DATA. Live: the previous session's bhavcopy is fetched once from NSE's
public archive (UDiFF zip, ~1.2MB) and cached under .oiwall_cache/; a dead
endpoint is an UNKNOWN read, never a crash. Backfill: data/oi_prevclose.json.gz
holds the same strikes per night for the 3y ledger, built by
scripts/build_oi_backfill.py from a directory of bhavcopies.
"""
from __future__ import annotations

import csv
import gzip
import io
import json
import logging
import time
import zipfile
from datetime import date, datetime
from pathlib import Path
from typing import Optional

log = logging.getLogger("tradewell.closing")

REGISTERED_ON = "2026-08-22"
SPAN = 500.0          # strikes considered, either side of the 15:00 print
ROAD_PTS = 150.0      # open road when the wall is further than this ahead
STATES = ("ROAD", "CAPPED", "BEHIND")
DEFINITION = ("Previous session's close OI (NSE bhavcopy) on the held expiry; "
              f"walls = max-OI CE/PE strike within +/-{SPAN:.0f} of the 15:00 print; "
              "ahead = call wall - print (CE) or print - put wall (PE); "
              f"ROAD > {ROAD_PTS:.0f} ahead, CAPPED 0-{ROAD_PTS:.0f}, BEHIND <= 0; "
              "PCR = PE OI / CE OI over the same strikes. Shadow only — never a gate.")

CACHE_DIR = Path(__file__).resolve().parents[2] / ".oiwall_cache"
BACKFILL_PATH = Path(__file__).resolve().parent / "data" / "oi_prevclose.json.gz"
UDIFF_URL = ("https://nsearchives.nseindia.com/content/fo/"
             "BhavCopy_NSE_FO_0_0_0_{ymd}_F_0000.csv.zip")
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36")


# --- the read ----------------------------------------------------------------

def walls(chain: dict, print_: float, direction: Optional[str]) -> Optional[dict]:
    """`chain` maps (strike, 'CE'|'PE') -> OI for ONE expiry. Returns the
    walls and, when a direction is given, the distance ahead and the state.
    None when either side has no strikes in the span (unknown, never a pass)."""
    ce = {s: oi for (s, k), oi in chain.items() if k == "CE" and abs(s - print_) <= SPAN}
    pe = {s: oi for (s, k), oi in chain.items() if k == "PE" and abs(s - print_) <= SPAN}
    if not ce or not pe:
        return None
    cw = max(ce, key=ce.get)
    pw = max(pe, key=pe.get)
    pcr = sum(pe.values()) / sum(ce.values()) if sum(ce.values()) else None
    out = {"ce_wall": cw, "pe_wall": pw, "pcr": round(pcr, 3) if pcr is not None else None,
           "ahead_pts": None, "state": None}
    if direction in ("CE", "PE"):
        ahead = (cw - print_) if direction == "CE" else (print_ - pw)
        out["ahead_pts"] = round(ahead, 1)
        out["state"] = ("ROAD" if ahead > ROAD_PTS else "CAPPED" if ahead > 0 else "BEHIND")
    return out


def pick_expiry(chains: dict, held: Optional[date], session: date) -> Optional[str]:
    """The held expiry's chain if the bhavcopy lists it, else the nearest
    expiry after the session (the study's own fallback)."""
    if not chains:
        return None
    if held and held.isoformat() in chains:
        return held.isoformat()
    later = sorted(e for e in chains if e > session.isoformat())
    return later[0] if later else None


# --- bhavcopy parsing --------------------------------------------------------

def parse_udiff(text: str) -> dict:
    """NIFTY index options from a UDiFF bhavcopy -> {expiry_iso: {(strike, right): oi}}."""
    out: dict = {}
    for r in csv.reader(io.StringIO(text)):
        if len(r) < 24 or r[7] != "NIFTY" or r[4] != "IDO":
            continue
        try:
            out.setdefault(r[9], {})[(float(r[11]), r[12])] = float(r[22])
        except ValueError:
            continue
    return out


def parse_legacy(text: str) -> dict:
    """Pre-Jul-2024 layout: INSTRUMENT,SYMBOL,EXPIRY_DT,STRIKE_PR,OPTION_TYP,...,OPEN_INT(12)."""
    out: dict = {}
    for r in csv.reader(io.StringIO(text)):
        if len(r) < 14 or r[0] != "OPTIDX" or r[1] != "NIFTY":
            continue
        try:
            exp = datetime.strptime(r[2], "%d-%b-%Y").date().isoformat()
            out.setdefault(exp, {})[(float(r[3]), r[4])] = float(r[12])
        except ValueError:
            continue
    return out


# --- live: previous session's bhavcopy, cached ------------------------------

def _cache_path(session: date) -> Path:
    return CACHE_DIR / f"{session.isoformat()}.json"


def _from_cache(session: date) -> Optional[dict]:
    p = _cache_path(session)
    if not p.exists():
        return None
    try:
        raw = json.loads(p.read_text())
        return {e: {(float(s), k): oi for s, k, oi in rows} for e, rows in raw.items()}
    except (OSError, ValueError, TypeError):
        return None


def _to_cache(session: date, chains: dict) -> None:
    try:
        CACHE_DIR.mkdir(exist_ok=True)
        _cache_path(session).write_text(json.dumps(
            {e: [[s, k, oi] for (s, k), oi in ch.items()] for e, ch in chains.items()}))
    except OSError:
        log.warning("OI wall cache unwritable at %s", CACHE_DIR)


def fetch_bhavcopy(session: date, timeout: float = 10.0) -> Optional[dict]:
    """The session's close-of-day NIFTY option OI from NSE's archive, or None."""
    try:
        import requests
        r = requests.get(UDIFF_URL.format(ymd=session.strftime("%Y%m%d")),
                         headers={"User-Agent": _UA}, timeout=timeout)
        if r.status_code != 200 or not r.content[:2] == b"PK":
            log.warning("Bhavcopy for %s not available (http %s)", session, r.status_code)
            return None
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            name = z.namelist()[0]
            chains = parse_udiff(z.read(name).decode("utf-8", "replace"))
        return chains or None
    except Exception as exc:
        log.warning("Bhavcopy fetch failed for %s: %s", session, exc)
        return None


# A failed fetch is not retried for RETRY_AFTER_S — the card is polled, and a
# missing or slow archive must cost one request per window, not one per poll.
RETRY_AFTER_S = 600.0
_failed_at: dict = {}


def prev_close_chains(session: date) -> Optional[dict]:
    """All NIFTY option chains as of `session`'s close — cache first, then the
    archive (with a backoff on failure). `session` is the PREVIOUS trading
    day relative to the card."""
    cached = _from_cache(session)
    if cached:
        return cached
    last = _failed_at.get(session)
    if last is not None and time.monotonic() - last < RETRY_AFTER_S:
        return None
    chains = fetch_bhavcopy(session)
    if chains:
        _to_cache(session, chains)
        _failed_at.pop(session, None)
    else:
        _failed_at[session] = time.monotonic()
    return chains


# --- backfill ----------------------------------------------------------------

_backfill_cache: Optional[dict] = None


def load_backfill(path: Path = BACKFILL_PATH) -> dict:
    """{session_iso: {expiry_iso: {(strike, right): oi}}} — the previous-close
    OI per night, as built by scripts/build_oi_backfill.py. Empty when absent."""
    global _backfill_cache
    if _backfill_cache is not None and path == BACKFILL_PATH:
        return _backfill_cache
    if not path.exists():
        return {}
    try:
        with gzip.open(path, "rt") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return {}
    out = {d: {e: {(float(s), k): float(oi) for s, k, oi in rows} for e, rows in ex.items()}
           for d, ex in raw.items()}
    if path == BACKFILL_PATH:
        _backfill_cache = out
    return out


def annotate(trades: list, days: dict, backfill: Optional[dict] = None) -> None:
    """Stamp oi_ce_wall / oi_pe_wall / oi_ahead_pts / oi_pcr / f_oi_state on
    ledger rows from the previous session's close OI. Unknown stays None."""
    data = load_backfill() if backfill is None else backfill
    sessions = sorted(days)
    pos = {d: i for i, d in enumerate(sessions)}
    for t in trades:
        d = date.fromisoformat(t["date"])
        i = pos.get(d)
        w = None
        if i:
            prev = sessions[i - 1]
            chains = data.get(prev.isoformat())
            exp = pick_expiry(chains, date.fromisoformat(t["expiry"]) if t.get("expiry") else None, d)
            if exp:
                w = walls(chains[exp], t["signal_price"], t.get("direction"))
        t["oi_ce_wall"] = w["ce_wall"] if w else None
        t["oi_pe_wall"] = w["pe_wall"] if w else None
        t["oi_ahead_pts"] = w["ahead_pts"] if w else None
        t["oi_pcr"] = w["pcr"] if w else None
        t["f_oi_state"] = w["state"] if w else None


def summary(trades: list, split: date) -> dict:
    """CLEAN x ROAD/CAPPED/BEHIND (+ FLAGGED x ROAD as the no-rescue control),
    per window."""
    from app.closing.attribution import _compact

    def cells(sub):
        clean = [t for t in sub if t.get("card_verdict") == "CLEAN"]
        flagged = [t for t in sub if t.get("card_verdict") == "FLAGGED"]
        out = {f"clean_{s.lower()}": _compact([t for t in clean if t.get("f_oi_state") == s])
               for s in STATES}
        out["flagged_road"] = _compact([t for t in flagged if t.get("f_oi_state") == "ROAD"])
        return out

    windows = {}
    for key, sub in (
            ("in_sample_2y", [t for t in trades if date.fromisoformat(t["date"]) < split]),
            ("holdout_1y", [t for t in trades if date.fromisoformat(t["date"]) >= split]),
            ("pooled_3y", trades)):
        windows[key] = cells(sub)
    covered = sum(1 for t in trades if t.get("f_oi_state"))
    return {
        "registered_on": REGISTERED_ON,
        "definition": DEFINITION,
        "road_pts": ROAD_PTS,
        "span": SPAN,
        "windows": windows,
        "coverage": {"stamped": covered, "ledger": len(trades)},
        "note": ("Shadow signal, registered 22-Aug: the previous session's close "
                 "OI on the held expiry, stamped on every ledger night. Never "
                 "touches the verdict. Discovered on the holdout, replicated on "
                 "the in-sample for HIT RATE only (ROAD 55% vs 40%), not for "
                 "P&L magnitude — read it as capped-vs-not, not as a payout "
                 "signal. FLAGGED x ROAD is the control: an intensifier inside "
                 "CLEAN, not a rescue. Graded by live nights."),
    }
