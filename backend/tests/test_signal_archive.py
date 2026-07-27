"""Append-only signal archive tests.

The store is deliberately day-scoped, so the archive is the ONLY multi-day
record of what the engine issued. These tests pin the properties that make it
trustworthy: adoption + retirement both land, last state wins, the day window
filters, the boot-merge harvests .signals.json exactly once, corrupt lines are
skipped, and test-constructed stores never write anywhere.

Run:  python backend/tests/test_signal_archive.py
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import json
import tempfile
import time
from pathlib import Path

from app.signals.archive import SignalArchive
from app.signals.models import (
    Action,
    Bias,
    Direction,
    MarketStatus,
    Regime,
    ScoreBreakdown,
    SignalCard,
    SignalResponse,
    SignalState,
    TradingMode,
)
from app.signals.store import SignalStore, ThrottleConfig


def _card(cid, at, direction=Direction.CE, valid=480):
    return SignalCard(
        id=cid, symbol="NIFTY", mode=TradingMode.INTRADAY, title="t",
        action=Action.BUY_CE if direction is Direction.CE else Action.BUY_PE,
        direction=direction, state=SignalState.ACTIVE,
        contract=f"NIFTY 24000 {direction.value}", strike=24000.0, expiry="2026-07-30",
        entry_low=99.0, entry_high=101.0, premium_sl=92.0, target1=112.0,
        target2=125.0, trailing_sl_rule="r", risk_reward=1.5, confidence=81.0,
        underlying_invalidation="u", invalidation_note="n",
        created_at=at, valid_until=at + valid,
        score=ScoreBreakdown(direction=direction, components=[], total=81.0),
    )


def _resp(card, at):
    bias = Bias.BULLISH if card is None or card.direction is Direction.CE else Bias.BEARISH
    return SignalResponse(
        symbol="NIFTY", mode=TradingMode.INTRADAY, evaluated_at=at,
        status=MarketStatus(
            symbol="NIFTY", mode=TradingMode.INTRADAY, regime=Regime.MODERATE_BULLISH,
            regime_label="R", bias=bias, bull_score=60, bear_score=40, headline="h",
            vix_status=None, news_label=None, news_net=None, notes=[],
        ),
        action=card.action if card else Action.AVOID,
        signal=card, no_trade_reason=None, score=None,
    )


CFG = ThrottleConfig(max_per_day=99, min_gap_s=0, cooldown_s=0, flip_guard_s=0)


def test_adoption_and_retirement_both_land_last_state_wins():
    with tempfile.TemporaryDirectory() as d:
        arch = SignalArchive(Path(d) / "a.jsonl")
        store = SignalStore(store_path=None)
        store.archive = arch
        now = int(time.time())
        store.reconcile(_resp(_card("C1", now), now), now, CFG)
        cards = arch.load(days=1)
        assert len(cards) == 1 and cards[0].state is SignalState.ACTIVE
        # Expiry: the retirement snapshot supersedes the adoption line.
        later = now + 500
        store.reconcile(_resp(None, later), later, CFG)
        cards = arch.load(days=1)
        assert len(cards) == 1 and cards[0].state is SignalState.EXPIRED, cards[0].state
        # Two lines on disk, one card served — append-only, dedupe on read.
        raw = (Path(d) / "a.jsonl").read_text().splitlines()
        assert len(raw) == 2
    print("  ARCH   -> adoption + retirement recorded; last state wins on read")


def test_close_active_archives_the_cancelled_state():
    with tempfile.TemporaryDirectory() as d:
        arch = SignalArchive(Path(d) / "a.jsonl")
        store = SignalStore(store_path=None)
        store.archive = arch
        now = int(time.time())
        store.reconcile(_resp(_card("C2", now), now), now, CFG)
        assert store.close_active("NIFTY", TradingMode.INTRADAY, now + 60, "score fell")
        cards = arch.load(days=1)
        assert len(cards) == 1 and cards[0].state is SignalState.CANCELLED
    print("  ARCH   -> refresh's close_active lands as cancelled")


def test_day_window_and_filters():
    with tempfile.TemporaryDirectory() as d:
        arch = SignalArchive(Path(d) / "a.jsonl")
        now = int(time.time())
        arch.record(_card("OLD", now - 10 * 86400))
        arch.record(_card("NEW", now - 3600))
        pe = _card("PE", now - 7200, direction=Direction.PE)
        pe.mode = TradingMode.POSITIONAL
        arch.record(pe)
        assert {c.id for c in arch.load(days=7)} == {"NEW", "PE"}
        assert {c.id for c in arch.load(days=30)} == {"OLD", "NEW", "PE"}
        assert {c.id for c in arch.load(days=7, modes=["intraday"])} == {"NEW"}
        assert arch.load(days=7, symbol="BANKNIFTY") == []
    print("  ARCH   -> day window, mode and symbol filters hold")


def test_boot_merge_harvests_store_file_once_and_never_at_import():
    """Cards issued by a PRE-ARCHIVE process exist only in .signals.json; the
    EXPLICIT boot-merge (app lifespan) imports them once. Construction alone
    must do NO I/O — an import-time merge meant collecting the test suite
    wrote the live archive (review-confirmed critical)."""
    with tempfile.TemporaryDirectory() as d:
        now = int(time.time())
        store_file = Path(d) / ".signals.json"
        store_file.write_text(json.dumps({
            "NIFTY:intraday": {
                "active": _card("A1", now - 300).model_dump(mode="json"),
                "history": [_card("H1", now - 86400).model_dump(mode="json")],
            },
        }))
        arch_path = Path(d) / ".signals_archive.jsonl"
        arch = SignalArchive(arch_path)
        assert not arch_path.exists(), "construction must not touch the disk"
        arch.merge_store_file()
        got = {c.id for c in arch.load(days=7)}
        assert got == {"A1", "H1"}, got
        # Second boot: idempotent (same ids, no growth).
        before = arch_path.read_text()
        again = SignalArchive(arch_path)
        again.merge_store_file()
        assert arch_path.read_text() == before
    print("  ARCH   -> boot-merge is explicit, harvests once, never at import")


def test_reprice_lands_in_the_archive():
    """A repriced ladder must not be lost if the process dies before the card
    retires (review-confirmed): reprice_active records the fresh snapshot."""
    with tempfile.TemporaryDirectory() as d:
        arch = SignalArchive(Path(d) / "a.jsonl")
        store = SignalStore(store_path=None)
        store.archive = arch
        now = int(time.time())
        store.reconcile(_resp(_card("R1", now), now), now, CFG)
        ladder = {"premium_sl": 101.0, "target1": 128.0, "target2": 140.0,
                  "quick_target": 123.0, "disaster_sl": None,
                  "entry_low": 109.0, "entry_high": 111.0,
                  "trailing_sl_rule": "r", "risk_reward": 1.5}
        out = store.reprice_active("NIFTY", TradingMode.INTRADAY, 110.0, ladder, now + 60)
        assert out is not None
        cards = arch.load(days=1)
        assert len(cards) == 1 and cards[0].repriced_at == now + 60, cards
    print("  ARCH   -> reprice snapshot supersedes the adoption line")


def test_load_caches_until_the_file_changes():
    """Polled every 15s forever — an unchanged file must cost a stat(), not a
    full re-parse that grows with the archive's age (review-confirmed)."""
    with tempfile.TemporaryDirectory() as d:
        arch = SignalArchive(Path(d) / "a.jsonl")
        now = int(time.time())
        arch.record(_card("C1", now - 60))
        arch.load(days=1)
        arch.load(days=1)
        arch.load(days=7)                 # different window, same parse
        assert arch._parse_count == 1, arch._parse_count
        arch.record(_card("C2", now - 30))
        got = {c.id for c in arch.load(days=1)}
        assert got == {"C1", "C2"} and arch._parse_count == 2
    print("  ARCH   -> unchanged file served from cache; appends invalidate it")


def test_corrupt_lines_are_skipped_not_fatal():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "a.jsonl"
        arch = SignalArchive(p)
        now = int(time.time())
        arch.record(_card("GOOD", now - 60))
        with open(p, "a") as fh:
            fh.write("{ torn line\n")
        arch.record(_card("ALSO", now - 30))
        got = {c.id for c in arch.load(days=1)}
        assert got == {"GOOD", "ALSO"}, got
    print("  ARCH   -> a torn line costs one line, not the archive")


def test_stores_without_archive_write_nothing():
    """The isolation rule that burned us with .signals.json: test-constructed
    stores default to archive=None and must stay silent."""
    store = SignalStore(store_path=None)
    assert store.archive is None
    now = int(time.time())
    store.reconcile(_resp(_card("X", now), now), now, CFG)   # must not raise
    disabled = SignalArchive(None)
    disabled.record(_card("Y", now))                          # silent no-op
    assert disabled.load() == []
    print("  ARCH   -> default stores and a disabled archive write nowhere")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
    print("\n" + ("ALL PASSED" if failed == 0 else f"{failed} FAILED"))
    sys.exit(1 if failed else 0)
