"""Runtime-editable trading settings — the day's fund and the paper simulator's
position cap, settable from the UI without a restart.

These were once the live "loss guards": daily-loss, open-drawdown, losing-streak
and open-position circuit breakers that halted signals after a bad run. Those
breakers were removed 25-Jul at the user's instruction — this is a personal
advisory tool that places no orders, and the trader chose to keep the engine
issuing signals through a bad day rather than have it fall silent. What remains
here are two settings that were never breakers:

  * trading_fund — the day's deployable premium budget; prefills the lot
    quantity on each signal card so the Kite basket opens ready.
  * max_open_positions — bounds the PAPER simulator's concurrent positions only.
    It no longer gates live signals; it shapes how many trades the paper book
    (the evidence engine) holds at once.

The persisted overlay (.risk_limits.json) wins over the .env defaults; a field
absent from the overlay falls back to config. Nothing here places, modifies, or
closes any order — Tradewell puts nothing in the market.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from app.config import Settings

log = logging.getLogger("tradewell.risk")

_STORE_PATH = Path(__file__).resolve().parents[2] / ".risk_limits.json"


@dataclass(frozen=True)
class LimitSpec:
    key: str
    label: str
    unit: str                # "rupees" | "trades" | "positions"
    min: float
    max: float
    step: float
    zero_disables: bool
    config_attr: str         # the Settings field this overlays
    description: str
    example: str


# The editable set: the day's deployable fund and the paper simulator's
# position cap. The live loss/streak breakers were removed 25-Jul (see the
# module docstring); these two were never breakers.
SPECS: tuple[LimitSpec, ...] = (
    LimitSpec(
        key="trading_fund", label="Today's trading fund", unit="rupees",
        min=0, max=100_000_000, step=1000, zero_disables=True,
        config_attr="trading_fund",
        description=(
            "The premium budget you are deploying today. Each signal card then "
            "prefills how many lots this fund buys at the live premium — "
            "lots = fund ÷ (premium × lot size) — so the Kite basket opens with "
            "the right quantity already set. Affordability only: it is not a "
            "risk suggestion (that is TRADING_CAPITAL's job) and places no order."
        ),
        example="₹10,00,000 at a ₹100 premium (lot 65) prefills 153 lots = 9,945 qty.",
    ),
    LimitSpec(
        key="max_open_positions", label="Paper: max open positions", unit="positions",
        min=0, max=20, step=1, zero_disables=True,
        config_attr="signal_max_open_positions",
        description=(
            "Bounds the PAPER simulator only — how many positions the paper book "
            "(the evidence engine behind the scenes) holds at once per mode. It no "
            "longer gates your live signals; the live circuit breakers were removed. "
            "Places no order either way."
        ),
        example="2 means the simulator holds at most two concurrent paper trades per mode.",
    ),
)

_SPEC_BY_KEY = {s.key: s for s in SPECS}


class RiskLimitStore:
    def __init__(self, path: Path | None = _STORE_PATH) -> None:
        self._lock = threading.Lock()
        self._path = path
        self._overrides: dict[str, float] = {}
        self._updated_at: Optional[int] = None
        self._load()

    def _load(self) -> None:
        if self._path is None or not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text())
            raw = data.get("overrides", {})
            # Only keep known, valid keys — a stale file must not smuggle in a
            # field that no longer exists or an out-of-range value.
            for k, v in raw.items():
                spec = _SPEC_BY_KEY.get(k)
                if spec and isinstance(v, (int, float)) and spec.min <= v <= spec.max:
                    self._overrides[k] = float(v)
            self._updated_at = data.get("updated_at")
        except Exception as exc:  # pragma: no cover
            log.warning("risk-limits overlay unreadable, using .env defaults: %s", exc)

    def _save_locked(self) -> None:
        if self._path is None:
            return
        try:
            tmp = self._path.with_name(self._path.name + ".tmp")
            tmp.write_text(json.dumps(
                {"overrides": self._overrides, "updated_at": self._updated_at}, indent=2))
            tmp.replace(self._path)
        except Exception as exc:  # pragma: no cover
            log.warning("risk-limits persist failed: %s", exc)

    def effective(self, cfg: Settings) -> dict[str, float]:
        """Current value of every editable limit: override if set, else .env."""
        with self._lock:
            return {
                s.key: self._overrides.get(s.key, float(getattr(cfg, s.config_attr)))
                for s in SPECS
            }

    def set_many(self, updates: dict[str, float], cfg: Settings) -> dict[str, Any]:
        """Validate and apply a batch of edits, returning the fresh view."""
        with self._lock:
            for k, v in updates.items():
                spec = _SPEC_BY_KEY.get(k)
                if spec is None:
                    raise ValueError(f"Unknown risk limit: {k}")
                if not isinstance(v, (int, float)):
                    raise ValueError(f"{spec.label} must be a number")
                if not (spec.min <= float(v) <= spec.max):
                    raise ValueError(
                        f"{spec.label} must be between {spec.min:g} and {spec.max:g}")
                # Round counts to whole numbers so "2.5 positions" can't persist.
                self._overrides[k] = float(int(v)) if spec.unit != "rupees" else float(v)
            self._updated_at = int(time.time())
            self._save_locked()
        return self.view(cfg)

    def view(self, cfg: Settings) -> dict[str, Any]:
        """Everything the UI needs: value, source, bounds, and how it works."""
        with self._lock:
            fields = []
            for s in SPECS:
                overridden = s.key in self._overrides
                value = self._overrides.get(s.key, float(getattr(cfg, s.config_attr)))
                fields.append({
                    "key": s.key, "label": s.label, "unit": s.unit,
                    "value": value,
                    "min": s.min, "max": s.max, "step": s.step,
                    "zero_disables": s.zero_disables,
                    "disabled": s.zero_disables and value == 0,
                    "source": "override" if overridden else "env",
                    "env_default": float(getattr(cfg, s.config_attr)),
                    "description": s.description,
                    "example": s.example,
                })
            return {
                "fields": fields,
                "updated_at": self._updated_at,
                "note": (
                    "Trading settings, not order controls — Tradewell places, modifies, "
                    "or closes nothing. The trading fund only prefills lot quantities on "
                    "signal cards; the paper-position cap only bounds the background "
                    "simulator. The old loss/streak circuit breakers were removed — the "
                    "engine now keeps issuing signals through a bad run, and your loss is "
                    "capped only by the stop you act on yourself."
                ),
            }


risk_limit_store = RiskLimitStore()
