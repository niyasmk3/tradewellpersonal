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
