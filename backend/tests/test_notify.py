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


def test_a_dead_webhook_never_raises():
    """The signal loop must survive an unreachable endpoint."""
    cfg = _cfg(ALERT_WEBHOOK_URL="http://127.0.0.1:9/never-listening")
    assert notify.push_signal(_card(), cfg) is True     # dispatched...
    _drain()                                            # ...and failed silently
    notify._post("http://127.0.0.1:9/nope", b"x", {})   # direct call, still quiet
    print("  NOTIFY -> unreachable webhook logs and moves on")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("\nAll notify tests passed.")


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
