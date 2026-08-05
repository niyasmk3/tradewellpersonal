"""The dashboard's ask-anything box (app/assistant.py).

What matters here: the context assembly must never raise on a half-dead
feed (it runs against live singletons), the history cap must hold, the
no-key path must degrade to a polite off-switch instead of erroring, and
the system prompt must keep its explain-don't-signal contract.

Run:  python backend/tests/test_assistant.py
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import types

from app import assistant


def test_build_context_never_raises():
    """Assembled from live singletons — on a test box most are empty/stale.
    Whatever their state, the context is a string, never an exception."""
    ctx = assistant.build_context()
    assert isinstance(ctx, str) and len(ctx) > 0
    print("  CTX    -> context assembles on a cold/dead state without raising")


def test_ask_without_key_is_a_polite_off_switch(monkeypatch=None):
    from app.config import Settings, get_settings

    class _Cfg:
        anthropic_api_key = ""
        chat_model = "claude-haiku-4-5-20251001"

    orig = assistant.get_settings
    assistant.get_settings = lambda: _Cfg()
    try:
        out = assistant.ask("what is vwap?")
        assert out["enabled"] is False and "ANTHROPIC_API_KEY" in out["answer"]
    finally:
        assistant.get_settings = orig
    print("  OFF    -> no key = disabled message, not an exception")


def test_ask_grounds_history_and_context():
    """A fake anthropic client captures exactly what would be sent: capped
    history, context+question in the last user turn, the explain-don't-
    signal system prompt, and the configured model."""
    captured = {}

    class _Resp:
        content = [types.SimpleNamespace(type="text", text="Because the chart shows the future.")]

    class _Messages:
        def create(self, **kw):
            captured.update(kw)
            return _Resp()

    class _Client:
        def __init__(self, api_key=None):
            captured["api_key"] = api_key
            self.messages = _Messages()

    class _Cfg:
        anthropic_api_key = "sk-test"
        chat_model = "claude-haiku-4-5-20251001"

    fake_mod = types.SimpleNamespace(Anthropic=_Client)
    orig_get, orig_mod = assistant.get_settings, sys.modules.get("anthropic")
    assistant.get_settings = lambda: _Cfg()
    sys.modules["anthropic"] = fake_mod
    try:
        history = ([{"role": "user", "content": f"q{i}"} for i in range(30)]
                   + [{"role": "tool", "content": "must be dropped"}])
        out = assistant.ask("why two NIFTY values?", history)
        assert out == {"enabled": True, "answer": "Because the chart shows the future."}
        assert captured["model"] == "claude-haiku-4-5-20251001"
        assert captured["max_tokens"] == assistant.MAX_TOKENS
        assert "never signal" in captured["system"]
        msgs = captured["messages"]
        # 16-turn cap on replayed history + the grounded question on top;
        # non-user/assistant roles never pass through.
        assert len(msgs) == assistant.MAX_TURNS * 2 + 1
        assert all(m["role"] in ("user", "assistant") for m in msgs)
        last = msgs[-1]["content"]
        assert last.startswith("LIVE CONTEXT:") and "why two NIFTY values?" in last
    finally:
        assistant.get_settings = orig_get
        if orig_mod is not None:
            sys.modules["anthropic"] = orig_mod
        else:
            sys.modules.pop("anthropic", None)
    print("  ASK    -> capped history, grounded question, honest system prompt")


def test_implied_odds_math_is_sane():
    """N(d2) sanity at the boundaries the answer leans on: deep ITM ~ 1,
    deep OTM ~ 0, ATM near a coin, and monotonically falling with strike."""
    spot, iv = 24500.0, 0.12
    deep_itm = assistant._p_above(spot, 23000.0, iv, 3)
    deep_otm = assistant._p_above(spot, 26000.0, iv, 3)
    atm = assistant._p_above(spot, 24500.0, iv, 3)
    assert deep_itm > 0.99 and deep_otm < 0.01
    assert 0.45 < atm < 0.55
    ladder = [assistant._p_above(spot, k, iv, 2)
              for k in (24300, 24450, 24500, 24650, 24800)]
    assert all(a > b for a, b in zip(ladder, ladder[1:])), ladder
    # More time = more chance for an OTM strike.
    assert (assistant._p_above(spot, 24650, iv, 6)
            > assistant._p_above(spot, 24650, iv, 1))
    print("  ODDS   -> N(d2) boundaries, strike monotonicity, time direction")


def test_implied_odds_block_degrades_without_a_chain():
    """On a cold state (no chain/spot) the block is simply absent — the
    context never raises and never fabricates numbers."""
    out = assistant._implied_odds()
    assert out is None or isinstance(out, str)
    print("  COLD   -> no chain = no odds block, no exception")


def test_implied_odds_converts_percent_ivs():
    """Chain IVs are PERCENT (14.3), Black-Scholes wants fractions — the
    live bug printed a 1250% ATM IV. Pin the conversion end-to-end."""
    import time as _t
    import types

    from app.state import market_state

    class _Row:
        def __init__(self, strike, iv):
            self.strike, self.ce_iv = strike, iv

    chain = types.SimpleNamespace(
        expiry=_t.strftime("%Y-%m-%d", _t.gmtime(_t.time() + 19800 + 4 * 86400)),
        rows=[_Row(24500.0, 12.5), _Row(24650.0, 13.0)])
    snap = types.SimpleNamespace(ltp=24500.0)
    orig_chain = market_state.get_option_chain
    orig_snap = market_state.underlying_snapshot
    market_state.get_option_chain = lambda key: chain
    market_state.underlying_snapshot = lambda sym: snap
    try:
        block = assistant._implied_odds()
        assert block is not None
        assert "ATM IV 12.5%" in block, block.splitlines()[0]
        # ATM ~ coin, OTM meaningfully below it, sane daily sigma (~160 pts).
        import re

        atm_line = next(l for l in block.splitlines() if l.strip().startswith("24500"))
        first_p = int(re.search(r"(\d+)%", atm_line).group(1))
        assert 40 <= first_p <= 60, atm_line
        sig = int(re.search(r"±(\d+) pts/day", block).group(1))
        assert 100 <= sig <= 250, sig
    finally:
        market_state.get_option_chain = orig_chain
        market_state.underlying_snapshot = orig_snap
    print("  UNITS  -> percent IVs convert to fractions; odds are sane")


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
