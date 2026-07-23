"""Paper-trading tests.

The point of paper trading is EVIDENCE, so the two things that must hold are:
its numbers are honest (net of the same charges a real fill pays), and it can
never contaminate the real journal that gates live signals.

Run:  python backend/tests/test_paper.py
"""
import os
import pathlib
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.config import Settings
from app.paper import charges as chg
from app.paper import service as paper_service
from app.paper.service import PaperTradingService, summarize
from app.signals.models import (
    Action, Direction, ScoreBreakdown, SignalCard, SignalState, TradingMode,
)
from app.signals.risk_limits import RiskLimitStore
from app.trades.models import TradeStatus
from app.trades.store import TradeStore

NOW = 1_700_000_000
# Pin the clock to mid-session. After 15:10 IST the monitor emits TIME_EXIT for
# intraday rows, which silently changes the exit reason these tests assert on —
# so unpinned they pass all morning and fail after 15:10.
_MIDDAY = (int(time.time()) + 19800) // 86400 * 86400 - 19800 + 12 * 3600
paper_service.time.time = lambda: float(_MIDDAY)
REAL_JOURNAL = pathlib.Path(__file__).resolve().parents[1] / ".trades.json"

# The open-position cap now reads the runtime overlay, exactly as the live
# engine does. Point it at a path-less store so these tests see the .env values
# they pass in, and not whatever the developer last saved in .risk_limits.json.
paper_service.risk_limit_store = RiskLimitStore(path=None)


def _levels(store):
    """The open trade's live stop/targets.

    Read rather than hardcoded: the journal re-prices the whole ladder from the
    actual fill, so a test that pins the CARD's 100/150 is asserting geometry no
    trade carries. What must hold is the behaviour — it exits at its own stop,
    at its own target — which stays true whatever the multipliers become.
    """
    t = store.all()[0]
    return t.stop_loss, t.target1


def _cfg(**over):
    """Settings built from ALIASES, with the .env file disabled.

    Both details matter. Settings fields declare `Field(alias=...)`, so pydantic
    populates by alias — passing `paper_slippage_pct=` is silently dropped and
    the default is used, which quietly made three of these tests assert the
    default instead of the value under test. And loading the real .env would
    make results depend on the developer's own configuration.
    """
    base = {"PAPER_TRADING": True, "PAPER_LOTS": 1, "PAPER_SLIPPAGE_PCT": 0.0,
            "SIGNAL_MAX_OPEN_POSITIONS": 2}
    base.update(over)
    return Settings(_env_file=None, **base)


def _store(name="/tmp/tw-paper-test.json"):
    p = pathlib.Path(name)
    p.unlink(missing_ok=True)
    return TradeStore(path=p)


class _State:
    # Ticks carry a fresh exchange stamp by default, matching what the live
    # ticker always attaches: the freshness gate treats an UNSTAMPED tick as
    # unverifiable-hence-stale, and that case gets its own dedicated test.
    def __init__(self, px=None, spot=24400.0, ts=None):
        self.ticks = (
            {999: {"last_price": px, "ts": ts if ts is not None else _MIDDAY}}
            if px is not None else {}
        )
        self._spot = spot

    def underlying_snapshot(self, sym):
        return type("S", (), {"ltp": self._spot})()


def _card(cid="S1", token=999, lot=75):
    """Always issued relative to NOW-in-real-time: `consider` refuses cards the
    engine no longer considers valid, so a fixed 2023 epoch would expire them
    all and every entry test would silently assert nothing."""
    live = _MIDDAY
    return SignalCard(
        id=cid, symbol="NIFTY", mode=TradingMode.INTRADAY, title="t", action=Action.BUY_CE,
        direction=Direction.CE, state=SignalState.ACTIVE, contract="NIFTY 24350 CE",
        strike=24350.0, token=token, expiry="2026-07-24",
        entry_low=118.0, entry_high=124.0, premium_sl=100.0, target1=150.0, target2=180.0,
        trailing_sl_rule="r", risk_reward=1.5, confidence=80.0,
        underlying_invalidation="u", invalidation_note="n", invalidation_level=24300.0,
        invalidation_dir="above", created_at=live, valid_until=live + 600,
        score=ScoreBreakdown(direction=Direction.CE, components=[], total=80.0, max=100),
        lot_size=lot,
    )


# --- charges must match the TypeScript model exactly -------------------------

def test_charges_match_the_frontend_model():
    """These are the same worked numbers frontend/tests/tradeMath.test.ts pins.
    If the two ever disagree, one of them is lying to the trader."""
    assert chg.charges(100.0, 175.0, 75, 2) == 69.22, chg.charges(100.0, 175.0, 75, 2)
    assert chg.charges(100.0, 0.0, 75, 1) == 26.98, chg.charges(100.0, 0.0, 75, 1)
    print("  CHARGES-> round trip Rs69.22 / lapse Rs26.98 — matches tradeMath.ts")


def test_net_pnl_is_below_gross():
    gross = (150.0 - 120.0) * 75
    net = chg.net_pnl(120.0, 150.0, 75)
    assert net < gross
    assert abs((gross - net) - chg.charges(120.0, 150.0, 75, 2)) < 0.01
    print(f"  CHARGES-> gross Rs{gross:.0f} -> net Rs{net:.2f}")


# --- entry -------------------------------------------------------------------

def test_enters_once_per_signal():
    s, svc = _store(), None
    svc = PaperTradingService(_cfg(), _State(px=120.0), s)
    svc.consider(_card())
    svc.consider(_card())                     # same id again
    assert len(s.all()) == 1, len(s.all())
    print("  PAPER  -> one entry per signal id, repeats ignored")


def test_entry_pays_slippage_but_never_above_entry_high():
    s = _store()
    svc = PaperTradingService(_cfg(PAPER_SLIPPAGE_PCT=0.01), _State(px=120.0), s)
    svc.consider(_card())
    t = s.all()[0]
    assert t.entry_premium == 121.2, t.entry_premium          # 120 x 1.01
    # A live premium already at the cap must not fill above it: entry_high is
    # the LIMIT a real basket would send.
    s2 = _store("/tmp/tw-paper-test2.json")
    PaperTradingService(_cfg(PAPER_SLIPPAGE_PCT=0.05), _State(px=123.0), s2).consider(_card())
    assert s2.all()[0].entry_premium == 124.0, s2.all()[0].entry_premium
    print("  PAPER  -> slippage paid on entry, capped at entry_high Rs124.00")


def test_open_position_cap_is_respected():
    """The simulation must not take trades the live throttle would have blocked."""
    s = _store()
    svc = PaperTradingService(_cfg(SIGNAL_MAX_OPEN_POSITIONS=2), _State(px=120.0), s)
    for i in range(4):
        svc.consider(_card(cid=f"S{i}"))
    assert len(s.all()) == 2, len(s.all())
    print("  PAPER  -> stopped at the 2-position cap, like the live throttle")


def test_capacity_block_does_not_burn_the_card():
    """A full book DEFERS a signal; it must not consume it.

    On 22-Jul the highest-scoring card the engine has ever produced (92.9) was
    offered while the book was full, marked seen, and never looked at again —
    the slot freed minutes later and the card was already gone. Capacity is
    temporary, so it belongs with the retryable checks, not the terminal ones.
    """
    s = _store()
    svc = PaperTradingService(_cfg(SIGNAL_MAX_OPEN_POSITIONS=1), _State(px=120.0), s)
    svc.consider(_card(cid="FIRST"))
    svc.consider(_card(cid="BEST"))                    # blocked: book is full
    assert len(s.all()) == 1, len(s.all())
    assert "BEST" not in svc._seen, "the blocked card was burned"

    # Free the slot the way the real thing does, then re-offer the same card.
    stop, _ = _levels(s)
    PaperTradingService(_cfg(SIGNAL_MAX_OPEN_POSITIONS=1), _State(px=stop - 1.0), s).run_once()
    svc.consider(_card(cid="BEST"))
    assert [t.signal_id for t in s.all() if t.status is TradeStatus.ENTERED] == ["BEST"]
    print("  PAPER  -> card deferred while full, taken once a slot freed")


def test_missing_lot_size_is_retried_when_it_arrives():
    """Instrument metadata loads asynchronously; a card must survive the gap."""
    s = _store()
    svc = PaperTradingService(_cfg(), _State(px=120.0), s)
    svc.consider(_card(cid="S1", lot=0))
    assert not s.all() and "S1" not in svc._seen
    svc.consider(_card(cid="S1", lot=75))
    assert len(s.all()) == 1, len(s.all())
    print("  PAPER  -> entered once the lot size resolved")


def test_skips_when_lot_size_or_premium_is_unknown():
    s = _store()
    PaperTradingService(_cfg(), _State(px=120.0), s).consider(_card(lot=0))
    assert not s.all()
    s2 = _store("/tmp/tw-paper-test3.json")
    svc = PaperTradingService(_cfg(), _State(px=None), s2)
    c = _card(cid="S9")
    c.ref_entry_premium = None
    c.entry_high = 0.0
    svc.consider(c)
    assert not s2.all()
    print("  PAPER  -> refuses to invent a lot size or a premium")


def test_card_reference_is_not_a_fill_price():
    """No tape, no trade — the card's own reference must never become a fill.

    On 22-Jul the 09:15:03 card carried YESTERDAY'S CLOSE as its reference; no
    tick had arrived, the fallback 'filled' ₹18 below the day's actual range,
    and the book recorded ₹8,261 of profit from an entry that never traded."""
    s = _store("/tmp/tw-paper-test-noref.json")
    svc = PaperTradingService(_cfg(), _State(px=None), s)
    c = _card()                     # ref_entry_premium 120-ish, entry_high 124
    c.ref_entry_premium = 120.0
    svc.consider(c)
    assert not s.all(), "entered a paper trade with no live tick"
    # The card is NOT burned: the first real tick can still take it.
    svc2 = PaperTradingService(_cfg(), _State(px=120.0), s)
    svc2._seen = svc._seen
    svc2.consider(c)
    assert len(s.all()) == 1
    print("  PAPER  -> no tick = no fill; first real tick still takes the card")


def test_above_zone_waits_like_a_resting_limit():
    """A tape above entry_high must WAIT, not fill at entry_high.

    The old cap manufactured fills at prices the market never offered — on
    22-Jul it bought ₹143.72 while the contract traded ₹153.90, booking +₹521
    on a trade that actually lost. A real LIMIT rests; so does paper now."""
    s = _store("/tmp/tw-paper-test-above.json")
    svc = PaperTradingService(_cfg(), _State(px=130.0), s)   # zone tops at 124
    svc.consider(_card())
    assert not s.all(), "filled above the entry zone"
    # Not burned: a pullback INTO the zone fills at the tape, not the cap.
    svc2 = PaperTradingService(_cfg(), _State(px=122.0), s)
    svc2._seen = svc._seen
    svc2.consider(_card())
    assert len(s.all()) == 1
    assert s.all()[0].entry_premium == 122.0, s.all()[0].entry_premium
    print("  PAPER  -> ₹130 tape rests above the ₹124 zone; fills the ₹122 pullback")


def test_stale_tick_defers_entry():
    """A tick older than the freshness cutoff is yesterday's market — wait."""
    s = _store("/tmp/tw-paper-test-stale.json")
    st = _State(px=120.0, ts=_MIDDAY - 100_000)   # yesterday's stamp
    svc = PaperTradingService(_cfg(), st, s)
    svc.consider(_card())
    assert not s.all(), "entered on a day-old tick"
    # A fresh stamp on the same premium takes the card.
    svc2 = PaperTradingService(_cfg(), _State(px=120.0, ts=_MIDDAY - 2), s)
    svc2._seen = svc._seen
    svc2.consider(_card())
    assert len(s.all()) == 1
    print("  PAPER  -> day-old tick deferred; 2s-old tick fills")


def test_unstamped_tick_is_unverifiable_unless_gate_disabled():
    """No exchange stamp = age unknown = same as stale (mirrors the engine).
    SIGNAL_MAX_PREMIUM_AGE_S=0 is the deliberate escape hatch for replays."""
    s = _store("/tmp/tw-paper-test-unstamped.json")
    st = _State(px=120.0)
    del st.ticks[999]["ts"]
    PaperTradingService(_cfg(), st, s).consider(_card())
    assert not s.all(), "filled on a tick whose age nothing verified"
    PaperTradingService(_cfg(SIGNAL_MAX_PREMIUM_AGE_S=0), st, s).consider(_card())
    assert len(s.all()) == 1
    print("  PAPER  -> unstamped tick refused; gate=0 accepts it (replay mode)")


def test_runtime_position_cap_override_is_honoured():
    """The paper cap must read the UI override, not the .env default — a
    simulation gated differently from the live engine measures nothing."""
    store_obj = paper_service.risk_limit_store        # memory-only (patched above)
    cfg = _cfg(SIGNAL_MAX_OPEN_POSITIONS=2)
    store_obj.set_many({"max_open_positions": 1}, cfg)
    try:
        s = _store("/tmp/tw-paper-test-capovr.json")
        svc = PaperTradingService(cfg, _State(px=120.0), s)
        svc.consider(_card(cid="S1"))
        svc.consider(_card(cid="S2"))
        assert len(s.all()) == 1, len(s.all())        # override 1 beats env 2
    finally:
        store_obj.set_many({"max_open_positions": 2}, cfg)
    print("  PAPER  -> UI override (1) outranks .env cap (2), like the live engine")


def test_expired_unfilled_cards_are_counted():
    """A card the simulator was offered but never filled must not vanish
    silently — each one is a hole in the evidence base."""
    s = _store("/tmp/tw-paper-test-expiry.json")
    svc = PaperTradingService(_cfg(), _State(px=130.0), s)   # above the zone
    c = _card()
    svc.consider(c)                                   # deferred: limit rests
    assert not s.all() and svc.expired_unfilled == 0
    svc.sweep_expired(c.valid_until + 1)              # validity lapses
    assert svc.expired_unfilled == 1, svc.expired_unfilled
    assert not svc._deferred
    print("  PAPER  -> above-zone card expired unfilled: counted, not forgotten")


# --- exit --------------------------------------------------------------------

def test_exits_on_stop_and_books_net_of_charges():
    s = _store()
    cfg = _cfg()
    PaperTradingService(cfg, _State(px=120.0), s).consider(_card())
    stop, _ = _levels(s)
    svc = PaperTradingService(cfg, _State(px=stop - 1.0), s)
    svc.run_once()
    t = s.all()[0]
    assert t.status is TradeStatus.EXITED and t.auto_close_reason == "stop"
    out = summarize(s)
    assert out["trades"] == 1 and out["losses"] == 1
    assert out["net_pnl"] < out["gross_pnl"], (out["net_pnl"], out["gross_pnl"])
    print(f"  PAPER  -> stopped out: gross Rs{out['gross_pnl']} net Rs{out['net_pnl']} "
          f"(charges Rs{out['charges']})")


def test_exits_on_target_and_on_invalidation():
    s = _store()
    cfg = _cfg()
    PaperTradingService(cfg, _State(px=120.0), s).consider(_card())
    _, t1 = _levels(s)
    PaperTradingService(cfg, _State(px=t1 + 1.0), s).run_once()
    assert s.all()[0].auto_close_reason == "target1"

    s2 = _store("/tmp/tw-paper-test4.json")
    PaperTradingService(cfg, _State(px=120.0), s2).consider(_card())
    PaperTradingService(cfg, _State(px=130.0, spot=24290.0), s2).run_once()  # spot < 24300
    assert s2.all()[0].auto_close_reason == "invalidation"
    print("  PAPER  -> exits on target1 and on underlying invalidation")


def test_exit_slippage_reduces_the_fill():
    s = _store()
    cfg = _cfg(PAPER_SLIPPAGE_PCT=0.01)
    PaperTradingService(cfg, _State(px=120.0), s).consider(_card())
    _, t1 = _levels(s)
    tape = t1 + 1.0
    PaperTradingService(cfg, _State(px=tape), s).run_once()
    t = s.all()[0]
    assert t.exit_premium == round(tape * 0.99, 2), t.exit_premium
    print(f"  PAPER  -> exit filled at Rs{t.exit_premium}, below the Rs{tape:.2f} tape")


def test_no_exit_without_a_live_premium():
    s = _store()
    cfg = _cfg()
    PaperTradingService(cfg, _State(px=120.0), s).consider(_card())
    PaperTradingService(cfg, _State(px=None), s).run_once()
    assert s.all()[0].status is TradeStatus.ENTERED
    print("  PAPER  -> a dead ticker does not close paper positions")


# --- the separation that matters ---------------------------------------------

def test_paper_never_touches_the_real_journal():
    before = REAL_JOURNAL.read_text() if REAL_JOURNAL.exists() else ""
    s = _store()
    cfg = _cfg()
    PaperTradingService(cfg, _State(px=120.0), s).consider(_card())
    PaperTradingService(cfg, _State(px=99.0), s).run_once()
    after = REAL_JOURNAL.read_text() if REAL_JOURNAL.exists() else ""
    assert before == after, "paper trading modified the REAL journal"
    print("  PAPER  -> .trades.json byte-identical; simulated book is isolated")


def test_restart_does_not_re_enter_open_signals():
    """A mid-session restart must not double-enter a card already taken."""
    s = _store()
    cfg = _cfg()
    PaperTradingService(cfg, _State(px=120.0), s).consider(_card())
    fresh = PaperTradingService(cfg, _State(px=120.0), s)   # simulates a restart
    fresh.consider(_card())
    assert len(s.all()) == 1, len(s.all())
    print("  PAPER  -> seen-set rebuilt from the store; no double entry")


def test_summary_reports_expectancy_and_reasons():
    s = _store()
    cfg = _cfg()
    for i, win in enumerate((True, False)):
        PaperTradingService(cfg, _State(px=120.0), s).consider(_card(cid=f"S{i}"))
        stop, t1 = _levels(s)
        px = t1 + 1.0 if win else stop - 1.0
        PaperTradingService(cfg, _State(px=px), s).run_once()
    out = summarize(s)
    assert out["trades"] == 2 and out["wins"] == 1 and out["losses"] == 1
    assert out["win_rate"] == 50.0
    assert set(out["by_reason"]) == {"target1", "stop"}, out["by_reason"]
    assert out["expectancy"] == round(out["net_pnl"] / 2, 2)
    print(f"  PAPER  -> 2 trades, 50% win, expectancy Rs{out['expectancy']}, "
          f"reasons {out['by_reason']}")


# --- the loop wiring, which shipped broken and untested -----------------------

def test_scan_reaches_the_real_signal_store():
    """Regression: the feed loop passed configured modes through as STRINGS
    where a TradingMode was required, so scan threw
    'str' object has no attribute 'value' on every cycle for a whole session.
    The loop caught and logged it, so nothing was simulated and nothing looked
    broken. This drives a REAL SignalStore, not a stub, so the enum boundary
    is actually crossed."""
    from app.signals.models import Bias, MarketStatus, Regime, SignalResponse
    from app.signals.store import SignalStore

    sig = SignalStore(store_path=None)          # memory-only
    card = _card(cid="LOOP-1")
    resp = SignalResponse(
        symbol="NIFTY", mode=TradingMode.INTRADAY, evaluated_at=NOW,
        status=MarketStatus(
            symbol="NIFTY", mode=TradingMode.INTRADAY, regime=Regime.MODERATE_BULLISH,
            regime_label="R", bias=Bias.BULLISH, bull_score=80, bear_score=20,
            headline="h", vix_status=None, news_label=None, news_net=None, notes=[],
        ),
        action=Action.BUY_CE, signal=card, no_trade_reason=None, score=None,
    )
    sig.reconcile(resp, int(time.time()))

    store = _store()
    svc = PaperTradingService(_cfg(), _State(px=120.0), store)
    svc.scan(sig)                                # must not raise, and must enter
    assert len(store.all()) == 1, f"scan did not enter: {len(store.all())}"
    print("  PAPER  -> scan() crosses the mode-enum boundary and enters")


def test_scan_tolerates_an_unknown_configured_mode():
    from app.signals.store import SignalStore
    store = _store()
    svc = PaperTradingService(_cfg(SIGNAL_MODES="intraday,bogus"), _State(px=120.0), store)
    svc.scan(SignalStore(store_path=None))       # must not raise
    print("  PAPER  -> an unknown mode in config is skipped, not fatal")


def test_expired_or_inactive_cards_are_not_entered():
    """A fill after the entry window simulates a trade the engine was not
    offering. And an expired card must stay retryable, not be burned."""
    s = _store()
    svc = PaperTradingService(_cfg(), _State(px=120.0), s)

    stale = _card(cid="OLD")
    stale.valid_until = int(time.time()) - 1
    svc.consider(stale)
    assert not s.all(), "entered an expired card"
    assert "OLD" not in svc._seen, "expired card was burned; it may be live next cycle"

    from app.signals.models import SignalState
    dead = _card(cid="DEAD")
    dead.valid_until = int(time.time()) + 600
    dead.state = SignalState.CANCELLED
    svc.consider(dead)
    assert not s.all(), "entered a cancelled card"
    print("  PAPER  -> expired/cancelled cards skipped, and not marked seen")


def test_below_zone_fills_are_refused_and_stay_retryable():
    """The 21-Jul-2026 incident. Card zone 26.85-27.65; the simulator filled at
    23.59 (12% below), inherited the card's 22.20 stop, and was left Rs 1.39 of
    room. It must wait instead — and the card must NOT be burned, because the
    premium can come back into the zone while the card is still valid."""
    s = _store()
    svc = PaperTradingService(_cfg(), _State(px=23.49), s)
    c = _card(cid="ZONE")
    c.entry_low, c.entry_high, c.premium_sl = 26.85, 27.65, 22.20
    c.ref_entry_premium = 27.10

    svc.consider(c)
    assert not s.all(), "entered below the published entry zone"
    assert "ZONE" not in svc._seen, "below-zone card was burned; it may re-enter the zone"

    # Premium recovers into the zone -> the same card is now taken.
    svc.state = _State(px=27.00)
    svc.consider(c)
    assert len(s.all()) == 1, "did not enter once the premium came back into the zone"
    assert 26.85 <= s.all()[0].entry_premium <= 27.65, s.all()[0].entry_premium
    print("  PAPER  -> below-zone fill refused, retried, entered at Rs%.2f once in zone"
          % s.all()[0].entry_premium)


def test_no_tick_yet_is_retryable_not_burned():
    s = _store()
    svc = PaperTradingService(_cfg(), _State(px=None), s)
    c = _card(cid="NOTICK")
    c.ref_entry_premium = None
    c.entry_high = 0.0
    svc.consider(c)
    assert "NOTICK" not in svc._seen, "a missing tick permanently burned the card"
    print("  PAPER  -> no tick yet: card stays eligible next cycle")


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
