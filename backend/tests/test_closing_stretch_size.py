"""Closing card stretched-body half size (pre-registered 14-Oct).

Pins tiers.size_factor: only a CLEAN card on a body at/beyond STRETCH_PTS is
cut; FLAGGED/INCOMPLETE, unknown bodies, sub-threshold bodies and a factor of
1.0 are full size. The threshold is the crash-day label's frozen constant.

Run:  python backend/tests/test_closing_stretch_size.py
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.closing import tiers
from app.config import Settings


def test_clean_stretched_is_halved():
    assert tiers.STRETCH_PTS == 300.0, "threshold must stay the registered label constant"
    assert tiers.size_factor("CLEAN", -395.9, 0.5) == 0.5     # 08-Oct shape
    assert tiers.size_factor("CLEAN", 300.0, 0.5) == 0.5      # boundary inclusive
    assert tiers.size_factor("CLEAN", -259.2, 0.5) == 1.0     # 12-Oct: NOT caught, by design
    print("  CUT    -> CLEAN on 300+ body halves; 259 stays full")


def test_only_clean_and_known_bodies():
    for verdict in ("FLAGGED", "INCOMPLETE", None):
        assert tiers.size_factor(verdict, -400.0, 0.5) == 1.0
    assert tiers.size_factor("CLEAN", None, 0.5) == 1.0       # unknown never cuts
    print("  SCOPE  -> flagged/incomplete/unknown never cut")


def test_factor_one_disables_and_config_default():
    assert tiers.size_factor("CLEAN", -400.0, 1.0) == 1.0
    cfg = Settings(_env_file=None)
    assert cfg.closing_stretch_size_factor == 0.5
    assert Settings(_env_file=None, CLOSING_STRETCH_SIZE_FACTOR=1.0).closing_stretch_size_factor == 1.0
    print("  KNOB   -> default 0.5, 1.0 is the off-switch")


def test_note_only_when_cut():
    assert "HALF SIZE" in tiers.SIZE_NOTE and "300" in tiers.SIZE_NOTE
    assert tiers.SIZE_REGISTERED_ON == "2026-10-14"
    print("  NOTE   -> registered text carries the threshold and date")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
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
