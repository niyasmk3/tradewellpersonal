"""Off-desk signal push.

This runs inside the signal reconcile path, so the properties that matter are
defensive ones: it must not raise, must not block, and must not fire when it was
never configured. Delivery itself is best-effort — the card is issued either way.

Run:  python backend/tests/test_notify.py
"""
import os
import sys
import threading

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import notify
from app.config import Settings
from app.signals.models import (
    Action, Direction, ScoreBreakdown, SignalCard, SignalState, TradingMode,
)

SENT = []


def _cfg(**over):
    base = {"ALERT_WEBHOOK_URL": "https://ntfy.example/tw", "ALERT_MIN_SCORE": 0.0}
    base.update(over)
    return Settings(_env_file=None, **base)


def _card(score=92.9):
    return SignalCard(
        id="S1", symbol="NIFTY", mode=TradingMode.INTRADAY, title="t",
        action=Action.BUY_PE, direction=Direction.PE, state=SignalState.ACTIVE,
        contract="NIFTY 23950 PE", strike=23950.0, token=999, expiry="2026-07-28",
        entry_low=141.7, entry_high=146.0, premium_sl=117.4, target1=181.8,
        target2=207.5, trailing_sl_rule="r", risk_reward=1.5, confidence=score,
        underlying_invalidation="NIFTY must stay below 24001", invalidation_note="n",
        invalidation_level=24001.05, invalidation_dir="below",
        created_at=1_700_000_000, valid_until=1_700_000_480,
        score=ScoreBreakdown(direction=Direction.PE, components=[], total=score, max=100),
        lot_size=65, suggested_lots=3,
    )


def _capture(monkeypatched=True):
    """Replace the network call, and run the thread to completion."""
    SENT.clear()
    real_thread = notify.threading.Thread

    def fake_thread(target, args, daemon, name):
        return real_thread(target=lambda: SENT.append(args), daemon=daemon, name=name)

    notify.threading.Thread = fake_thread
    return real_thread


def _drain():
    for t in threading.enumerate():
        if t.name == "tradewell-alert":
            t.join(timeout=2)


def test_no_url_configured_sends_nothing():
    real = _capture()
    try:
        assert notify.push_signal(_card(), _cfg(ALERT_WEBHOOK_URL="")) is False
        assert not SENT
    finally:
        notify.threading.Thread = real
    print("  NOTIFY -> unconfigured webhook makes no network call")


def test_push_carries_everything_needed_to_act():
    real = _capture()
    try:
        assert notify.push_signal(_card(), _cfg()) is True
        _drain()
        url, payload, headers = SENT[0][:3]
        body = payload.decode()
        assert url == "https://ntfy.example/tw"
        assert "BUY PE NIFTY 23950 PE" in body and "score 93" in body
        assert "Entry ₹141.7–146.0" in body
        assert "SL ₹117.4" in body and "T1 ₹181.8" in body
        assert "Suggested 3 lot(s)" in body
        assert "Valid for 8 min" in body          # 480s validity, stale-evident
        # The wrong-series trade is the mistake these two lines prevent: the
        # fixture's 2026-07-28 expiry must appear in title AND body.
        assert "exp 28-Jul" in headers["Title"], headers["Title"]
        assert "Strike 23950 · expiry 28-Jul" in body, body
    finally:
        notify.threading.Thread = real
    print("  NOTIFY -> push is actionable without opening the dashboard")


def test_below_min_score_is_suppressed():
    real = _capture()
    try:
        cfg = _cfg(ALERT_MIN_SCORE=85.0)
        assert notify.push_signal(_card(score=83.0), cfg) is False
        assert notify.push_signal(_card(score=92.9), cfg) is True
        assert len(SENT) == 1
    finally:
        notify.threading.Thread = real
    print("  NOTIFY -> ALERT_MIN_SCORE filters, boundary inclusive")


def test_json_format_posts_structured_body():
    import json

    real = _capture()
    try:
        notify.push_signal(_card(), _cfg(ALERT_WEBHOOK_FORMAT="json"))
        _drain()
        _, payload, headers = SENT[0][:3]
        data = json.loads(payload.decode())
        assert headers["Content-Type"] == "application/json"
        assert data["signal_id"] == "S1" and data["score"] == 92.9
        assert data["mode"] == "intraday"
    finally:
        notify.threading.Thread = real
    print("  NOTIFY -> json format carries the card's identity for routing")


def test_non_http_schemes_are_refused():
    """urlopen honours file:// — a typo must not turn the pusher into a reader."""
    real = _capture()
    try:
        for bad in ("file:///etc/passwd", "ftp://example/x", "/etc/passwd"):
            assert notify.push_signal(_card(), _cfg(ALERT_WEBHOOK_URL=bad)) is False, bad
        assert not SENT
        assert notify.push_signal(_card(), _cfg(ALERT_WEBHOOK_URL="http://x/y")) is True
    finally:
        notify.threading.Thread = real
    print("  NOTIFY -> only http(s) webhooks are dispatched")


def _at_ist(hh, mm, base=1_700_000_000):
    """Epoch for hh:mm IST on base's IST day — deterministic, no wall clock."""
    midnight = base - (base + 19800) % 86400
    return midnight + hh * 3600 + mm * 60


def test_evening_positional_is_marked():
    """A positional card born at/after 14:30 IST pushes with the overnight
    marker in title and body; earlier positional and evening INTRADAY don't.
    Same minute as the gap caution and the ledger split (EVENING_MIN)."""
    from app.signals.models import TradingMode

    def _pcard(hh, mm, mode=TradingMode.POSITIONAL):
        c = _card()
        c.mode = mode
        c.created_at = _at_ist(hh, mm)
        c.valid_until = c.created_at + 6 * 3600
        return c

    real = _capture()
    try:
        notify.push_signal(_pcard(14, 30), _cfg())          # boundary inclusive
        _drain()
        _, payload, headers = SENT[0][:3]
        assert "EVENING POSITIONAL" in headers["Title"]
        assert "Overnight-hold candidate" in payload.decode()

        SENT.clear()
        notify.push_signal(_pcard(14, 29), _cfg())          # one minute early
        notify.push_signal(_pcard(14, 40, TradingMode.INTRADAY), _cfg())
        _drain()
        for _, payload, headers, *_ in SENT:
            assert "EVENING POSITIONAL" not in headers.get("Title", "")
            assert "Overnight-hold candidate" not in payload.decode()
    finally:
        notify.threading.Thread = real
    print("  NOTIFY -> evening positional pushes carry the overnight marker")


def test_evening_positional_json_flag():
    """json consumers get a routing flag so this pattern can ring its own bell."""
    import json as _json
    from app.signals.models import TradingMode

    real = _capture()
    try:
        c = _card()
        c.mode = TradingMode.POSITIONAL
        c.created_at = _at_ist(15, 5)
        c.valid_until = c.created_at + 6 * 3600
        notify.push_signal(c, _cfg(ALERT_WEBHOOK_FORMAT="json"))
        _drain()
        data = _json.loads(SENT[0][1].decode())
        assert data["evening_positional"] is True
        SENT.clear()
        notify.push_signal(_card(), _cfg(ALERT_WEBHOOK_FORMAT="json"))
        _drain()
        assert _json.loads(SENT[0][1].decode())["evening_positional"] is False
    finally:
        notify.threading.Thread = real
    print("  NOTIFY -> json payload carries the evening_positional routing flag")


def test_a_dead_webhook_never_raises():
    """The signal loop must survive an unreachable endpoint."""
    cfg = _cfg(ALERT_WEBHOOK_URL="http://127.0.0.1:9/never-listening")
    assert notify.push_signal(_card(), cfg) is True     # dispatched...
    _drain()                                            # ...and failed silently
    notify._post("http://127.0.0.1:9/nope", b"x", {})   # direct call, still quiet
    print("  NOTIFY -> unreachable webhook logs and moves on")


def test_push_text_uses_the_same_pipe_and_guards():
    """Watchdog pages and armed confirmations ride the signal-alert transport:
    same webhook, same never-raise, same no-op without configuration."""
    real = _capture()
    try:
        assert notify.push_text("TRADEWELL FEED SILENT", "no ticks for 180s", _cfg()) is True
        assert len(SENT) == 1
        url, payload, headers = SENT[0][:3]
        assert url == "https://ntfy.example/tw"
        assert headers["Title"] == "TRADEWELL FEED SILENT"
        assert b"180s" in payload
        # Unconfigured -> silent no-op, never an exception.
        assert notify.push_text("x", "y", _cfg(ALERT_WEBHOOK_URL="")) is False
        assert notify.push_text("x", "y", _cfg(ALERT_WEBHOOK_URL="file:///etc/passwd")) is False
        assert len(SENT) == 1
    finally:
        notify.threading.Thread = real
    print("  NOTIFY -> push_text: same transport, scheme-guarded, no-op unconfigured")


def test_post_reports_delivery_not_dispatch():
    """`on_result` must reflect the HTTP outcome — armed means DELIVERED (2xx),
    because 'dispatched' once wore the armed chip while a dead URL ate alerts."""
    results = []

    class _Resp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False

    real = notify.urllib.request.urlopen
    try:
        notify.urllib.request.urlopen = lambda req, timeout=None: _Resp()
        notify._post("https://x.example/t", b"b", {}, on_result=results.append)
        def _boom(req, timeout=None): raise OSError("dns failure")
        notify.urllib.request.urlopen = _boom
        notify._post("https://x.example/t", b"b", {}, on_result=results.append)
        class _Bad(_Resp):
            status = 404
        notify.urllib.request.urlopen = lambda req, timeout=None: _Bad()
        notify._post("https://x.example/t", b"b", {}, on_result=results.append)
    finally:
        notify.urllib.request.urlopen = real
    assert results == [True, False, False], results
    print("  NOTIFY -> on_result: 2xx True; DNS failure and 404 both False")


def test_second_webhook_receives_signals_only():
    """ALERT_WEBHOOK_URL_2 is the shared, signals-only topic: cards and card
    retirements go to both topics; watchdog pages, invalidation nags and armed
    confirmations stay on the primary. The guest follows the cards — they do
    not get to watch YOUR positions or YOUR infrastructure."""
    real = _capture()
    two = {"ALERT_WEBHOOK_URL_2": "https://ntfy.example/shared"}
    try:
        # A card fans out to both topics — but the shared copy is SANITIZED:
        # suggested lots are TRADING_CAPITAL worked backwards through the stop
        # distance, so they never leave the owner's own topic.
        assert notify.push_signal(_card(), _cfg(**two)) is True
        _drain()
        assert [s[0] for s in SENT] == ["https://ntfy.example/tw", "https://ntfy.example/shared"]
        primary_body, shared_body = SENT[0][1].decode(), SENT[1][1].decode()
        assert "Suggested 3 lot(s)" in primary_body
        assert "Suggested" not in shared_body, shared_body
        # Everything a follower legitimately needs survives sanitisation.
        for needed in ("Entry", "SL", "T1", "Valid for", "23950"):
            assert needed in shared_body, needed

        # Default (private) push_text: primary only.
        SENT.clear()
        assert notify.push_text("TRADEWELL FEED SILENT", "180s", _cfg(**two)) is True
        _drain()
        assert [s[0] for s in SENT] == ["https://ntfy.example/tw"]

        # Signals-audience push_text (card retirements): both.
        SENT.clear()
        assert notify.push_text("card expired", "window closed", _cfg(**two),
                                audience="signals") is True
        _drain()
        assert [s[0] for s in SENT] == ["https://ntfy.example/tw", "https://ntfy.example/shared"]
    finally:
        notify.threading.Thread = real
    print("  SHARE  -> cards + retirements fan out; private pushes stay private")


def test_second_webhook_edge_cases():
    real = _capture()
    try:
        # Same URL twice must not double-buzz the phone.
        dup = _cfg(ALERT_WEBHOOK_URL_2="https://ntfy.example/tw")
        assert notify.push_signal(_card(), dup) is True
        _drain()
        assert len(SENT) == 1, [s[0] for s in SENT]

        # Secondary alone still delivers cards (a guest-only setup is legal),
        # but private pushes have nowhere to go, and the lone shared copy is
        # still sanitized — no primary does not mean no privacy.
        SENT.clear()
        only2 = _cfg(ALERT_WEBHOOK_URL="", ALERT_WEBHOOK_URL_2="https://ntfy.example/shared")
        assert notify.push_signal(_card(), only2) is True
        _drain()
        assert [s[0] for s in SENT] == ["https://ntfy.example/shared"]
        assert "Suggested" not in SENT[0][1].decode()
        assert notify.push_text("page", "x", only2) is False

        # A bad scheme on the secondary is refused without touching the primary.
        SENT.clear()
        bad2 = _cfg(ALERT_WEBHOOK_URL_2="file:///etc/passwd")
        assert notify.push_signal(_card(), bad2) is True
        _drain()
        assert [s[0] for s in SENT] == ["https://ntfy.example/tw"]

        # The min-score filter guards both topics equally.
        SENT.clear()
        strict = _cfg(ALERT_MIN_SCORE=95.0, ALERT_WEBHOOK_URL_2="https://ntfy.example/shared")
        assert notify.push_signal(_card(score=90.0), strict) is False
        assert not SENT
    finally:
        notify.threading.Thread = real
    print("  SHARE  -> dedup, guest-only, bad-scheme and min-score edges hold")


def test_retire_push_respects_min_score_and_reaches_both():
    """A guest must never hear about the death of a card they were never shown:
    push_retire applies the SAME score gate as the adoption push."""
    real = _capture()
    two = {"ALERT_WEBHOOK_URL_2": "https://ntfy.example/shared"}
    try:
        # Above the gate: retirement fans out to both topics.
        assert notify.push_retire(_card(score=92.9), "expired", _cfg(**two)) is True
        _drain()
        assert [s[0] for s in SENT] == ["https://ntfy.example/tw", "https://ntfy.example/shared"]
        assert b"expired" in SENT[0][1] and b"entry window closed" in SENT[0][1]

        # Below the gate: the card was never announced, so neither is its death.
        SENT.clear()
        strict = _cfg(ALERT_MIN_SCORE=95.0, **two)
        assert notify.push_retire(_card(score=90.0), "cancelled", strict) is False
        assert not SENT

        # A card with no score must not crash the formatter.
        SENT.clear()
        unscored = _card()
        unscored.confidence = None
        assert notify.push_retire(unscored, "cancelled", _cfg(**two)) is True
        _drain()
        assert b"unscored" in SENT[0][1] and b"do not chase" in SENT[0][1]
    finally:
        notify.threading.Thread = real
    print("  SHARE  -> retirements gated like adoptions; unscored cards safe")


def test_push_signal_on_result_binds_to_primary_only():
    """With ALERT_STARTUP_PING=false the FIRST REAL card push is what arms the
    chip — so push_signal must report the primary topic's delivery outcome,
    and never a guest's."""
    real = _capture()
    try:
        got = []
        cfg = _cfg(ALERT_WEBHOOK_URL_2="https://ntfy.example/shared")
        assert notify.push_signal(_card(), cfg, on_result=got.append) is True
        _drain()
        callbacks = [s[3] for s in SENT]
        assert callbacks[0] is not None and callbacks[1] is None, callbacks
        # Default stays callback-free (regression guard for existing callers).
        SENT.clear()
        assert notify.push_signal(_card(), _cfg()) is True
        _drain()
        assert SENT[0][3] is None
    finally:
        notify.threading.Thread = real
    print("  SHARE  -> push_signal on_result rides only the primary send")


def test_startup_ping_flag_default():
    """ALERT_STARTUP_PING defaults ON — the no-ping mode is an explicit opt-in,
    because the ping is what catches a silently-dead webhook at startup."""
    from app.config import Settings
    assert Settings(_env_file=None).alert_startup_ping is True
    assert Settings(_env_file=None, ALERT_STARTUP_PING=False).alert_startup_ping is False
    print("  PING   -> startup ping on by default; opt-out honoured")


def test_on_result_binds_to_primary_only():
    """The armed chip vouches for YOUR phone: with a secondary configured, the
    delivery callback must fire once, for the primary URL's outcome."""
    real = _capture()
    try:
        got = []
        cfg = _cfg(ALERT_WEBHOOK_URL_2="https://ntfy.example/shared")
        assert notify.push_text("armed?", "test", cfg, on_result=got.append,
                                audience="signals") is True
        _drain()
        callbacks = [s[3] for s in SENT]
        assert callbacks[0] is not None and callbacks[1] is None, callbacks
    finally:
        notify.threading.Thread = real
    print("  SHARE  -> on_result rides only the primary send")


def test_guest_verify_only_fires_for_a_distinct_topic():
    """push_guest_verify proves the SHARED topic on startup, the same way the
    primary is proven — but only when URL_2 is a real, distinct destination.
    An unset / bad-scheme / primary-identical URL_2 is a no-op: the primary's
    own verification already covers those, and a duplicate would double-buzz."""
    real = _capture()
    try:
        got = []

        # Distinct guest topic: one ping to URL_2 only, on_result rides it.
        cfg = _cfg(ALERT_WEBHOOK_URL_2="https://ntfy.example/shared")
        assert notify.push_guest_verify("live", "cards arrive here", cfg,
                                        on_result=got.append) is True
        _drain()
        assert [s[0] for s in SENT] == ["https://ntfy.example/shared"], [s[0] for s in SENT]
        assert SENT[0][3] is not None  # the guest chip's delivery callback
        body = SENT[0][1].decode()
        # A guest reads this: no sizing, no position — just "channel is live".
        assert "cards arrive here" in body
        assert "Suggested" not in body and "lot" not in body

        # No second topic at all -> nothing sent, returns False.
        SENT.clear()
        assert notify.push_guest_verify("live", "x", _cfg()) is False
        assert not SENT

        # URL_2 identical to the primary -> the primary already proves it.
        SENT.clear()
        dup = _cfg(ALERT_WEBHOOK_URL_2="https://ntfy.example/tw")
        assert notify.push_guest_verify("live", "x", dup) is False
        assert not SENT

        # Bad scheme on URL_2 -> refused, no send.
        SENT.clear()
        bad = _cfg(ALERT_WEBHOOK_URL_2="file:///etc/passwd")
        assert notify.push_guest_verify("live", "x", bad) is False
        assert not SENT

        # Guest-only setup (no primary) is legal and still gets proven.
        SENT.clear()
        only2 = _cfg(ALERT_WEBHOOK_URL="", ALERT_WEBHOOK_URL_2="https://ntfy.example/shared")
        assert notify.push_guest_verify("live", "x", only2) is True
        _drain()
        assert [s[0] for s in SENT] == ["https://ntfy.example/shared"]
    finally:
        notify.threading.Thread = real
    print("  GUEST  -> startup verify fires once for a distinct topic, else no-op")


def test_silent_priority_split():
    """The 31-Jul contract: the phone SOUNDS only for signal cards. Armed and
    guest verification pings and card retirements are delivered at ntfy's
    "min" priority (silent — the 2xx proof survives, the buzz does not);
    cards and watchdog pages carry no priority header (audible default)."""
    real = _capture()
    two = {"ALERT_WEBHOOK_URL_2": "https://ntfy.example/shared"}
    try:
        # Armed-style ping with priority=min -> Priority header on the send.
        assert notify.push_text("Tradewell armed", "feed starting", _cfg(),
                                priority="min") is True
        _drain()
        assert SENT[0][2].get("Priority") == "min", SENT[0][2]

        # Retirements: silent on BOTH topics.
        SENT.clear()
        assert notify.push_retire(_card(score=92.9), "expired", _cfg(**two)) is True
        _drain()
        assert len(SENT) == 2 and all(s[2].get("Priority") == "min" for s in SENT)

        # Guest verification: silent.
        SENT.clear()
        assert notify.push_guest_verify("guest live", "hello", _cfg(**two)) is True
        _drain()
        assert SENT[0][2].get("Priority") == "min", SENT[0][2]

        # Signal cards: NO priority header — this is the buzz that remains.
        SENT.clear()
        assert notify.push_signal(_card(), _cfg(**two)) is True
        _drain()
        assert all("Priority" not in s[2] for s in SENT), [s[2] for s in SENT]

        # Watchdog-style default push_text: audible too.
        SENT.clear()
        assert notify.push_text("TRADEWELL FEED SILENT", "180s", _cfg()) is True
        _drain()
        assert "Priority" not in SENT[0][2]
    finally:
        notify.threading.Thread = real
    print("  QUIET  -> pings/retires silent (min); cards and pages keep the buzz")


def test_expiry_reaches_every_push_shape():
    """Expiry in the json payload, in retirements, and in the SANITIZED guest
    copy (series identity is card data, not the owner's sizing)."""
    import json as _json

    real = _capture()
    two = {"ALERT_WEBHOOK_URL_2": "https://ntfy.example/shared"}
    try:
        notify.push_signal(_card(), _cfg(ALERT_WEBHOOK_FORMAT="json"))
        _drain()
        data = _json.loads(SENT[0][1].decode())
        assert data["expiry"] == "2026-07-28" and data["strike"] == 23950.0

        SENT.clear()
        assert notify.push_retire(_card(score=92.9), "cancelled", _cfg(**two)) is True
        _drain()
        assert b"exp 28-Jul" in SENT[0][1], SENT[0][1]

        SENT.clear()
        notify.push_signal(_card(), _cfg(**two))
        _drain()
        shared = SENT[1][1].decode()
        assert "expiry 28-Jul" in shared and "Suggested" not in shared
    finally:
        notify.threading.Thread = real
    print("  EXPIRY -> in json fields, retirements, and the guest copy (sans sizing)")


if __name__ == "__main__":
    import sys as _sys

    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except AssertionError as e:
                failed += 1
                print(f"  FAIL  {name}: {e}")
            except Exception as e:  # noqa: BLE001
                failed += 1
                print(f"  ERROR {name}: {type(e).__name__}: {e}")
    print("\n" + ("ALL PASSED" if failed == 0 else f"{failed} FAILED"))
    _sys.exit(1 if failed else 0)
