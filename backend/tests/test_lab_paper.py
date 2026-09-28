"""Paper-engine suspension rules, pinned after the 28-Sep incident: the
morning's pre-login state suspended all five armed strategies by 09:18 and
the first live session traded nothing.

  * kite_unauthenticated NEVER suspends — it is the stack's normal state
    until the user's daily login;
  * a real feed error (while authenticated) still suspends after 10 polls;
  * premarket wipes feed suspensions, so an outage benches a strategy for
    ONE session at most, never silently forever.

Run:  python backend/tests/test_lab_paper.py   (also pytest-compatible)
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__),
                                                "..")))

from app.lab.paper import PaperEngine
from app.lab.store import LabStore


class _StubRunner:
    def __init__(self, sid):
        self.sid = sid
        self._feed_fails = 0


def test_unauthenticated_mornings_never_suspend(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        st = LabStore(Path(td) / "lab.db")
        st.ensure_version1("F10", {})
        import app.lab.paper as paper_mod
        monkeypatch.setattr(paper_mod, "lab_store", st)
        eng = PaperEngine()
        r = _StubRunner("F10")
        for _ in range(50):
            eng._suspend_if_persistent(r, "kite_unauthenticated")
        assert st.state("F10")["suspended_reason"] == "", \
            "pre-login state must never suspend"
        for _ in range(9):
            eng._suspend_if_persistent(r, "fetch_failed: boom")
        assert st.state("F10")["suspended_reason"] == "", \
            "nine failures are not yet persistent"
        eng._suspend_if_persistent(r, "fetch_failed: boom")
        assert st.state("F10")["suspended_reason"].startswith("feed:"), \
            "ten authenticated failures must suspend"


def test_premarket_clears_feed_suspensions_only():
    with tempfile.TemporaryDirectory() as td:
        st = LabStore(Path(td) / "lab.db")
        st.ensure_version1("F10", {})
        st.ensure_version1("B01", {})
        st.set_state("F10", suspended_reason="feed: fetch_failed: x")
        st.set_state("B01", suspended_reason="strategy_error: manual")
        cleared = PaperEngine.clear_feed_suspensions(st)
        assert cleared == ["F10"]
        assert st.state("F10")["suspended_reason"] == ""
        assert st.state("B01")["suspended_reason"].startswith(
            "strategy_error"), "non-feed suspensions are a human's to clear"


if __name__ == "__main__":
    class _MP:
        def setattr(self, obj, name, value):
            setattr(obj, name, value)
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(_MP()) if "monkeypatch" in fn.__code__.co_varnames else fn()
            print("PASS", name)
    print("done")
