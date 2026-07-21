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
from app.trades.models import TradeStatus
from app.trades.store import TradeStore

NOW = 1_700_000_000
# Pin the clock to mid-session. After 15:10 IST the monitor emits TIME_EXIT for
# intraday rows, which silently changes the exit reason these tests assert on —
# so unpinned they pass all morning and fail after 15:10.
_MIDDAY = (int(time.time()) + 19800) // 86400 * 86400 - 19800 + 12 * 3600
paper_service.time.time = lambda: float(_MIDDAY)
REAL_JOURNAL = pathlib.Path(__file__).resolve().parents[1] / ".trades.json"


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
    def __init__(self, px=None, spot=24400.0):
        self.ticks = {999: {"last_price": px}} if px is not None else {}
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


# --- exit --------------------------------------------------------------------

def test_exits_on_stop_and_books_net_of_charges():
    s = _store()
    cfg = _cfg()
    PaperTradingService(cfg, _State(px=120.0), s).consider(_card())
    svc = PaperTradingService(cfg, _State(px=99.0), s)     # stop is 100
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
    PaperTradingService(cfg, _State(px=151.0), s).run_once()      # T1 = 150
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
    PaperTradingService(cfg, _State(px=151.0), s).run_once()
    t = s.all()[0]
    assert t.exit_premium == round(151.0 * 0.99, 2), t.exit_premium
    print(f"  PAPER  -> exit filled at Rs{t.exit_premium}, below the Rs151.00 tape")


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
    for i, px in enumerate((151.0, 99.0)):
        PaperTradingService(cfg, _State(px=120.0), s).consider(_card(cid=f"S{i}"))
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
