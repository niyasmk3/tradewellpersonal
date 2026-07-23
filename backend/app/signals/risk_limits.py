"""Runtime-editable risk limits — the loss guards, settable from the UI.

WHY THIS EXISTS SEPARATELY FROM config.py: the loss limits matter most on a bad
day, and a bad day is exactly when editing a .env file and restarting the
backend is the wrong amount of friction. These four values can therefore be set
live from the dashboard and take effect on the next evaluation cycle.

Everything else in config.py stays .env-only on purpose — changing a score
threshold or the stop percentage mid-session is a footgun, but pausing the
engine after you are down a set number of rupees is a safety valve.

The persisted overlay (.risk_limits.json) wins over the .env defaults. A field
absent from the overlay falls back to config, so unsetting is possible and the
.env values remain the baseline.

CRITICAL, and surfaced in every description: these HALT SIGNALS. They do not
place, modify, or close any order. Tradewell puts nothing in the market. A loss
limit stops the engine tempting you into more trades once the day has gone bad;
it cannot stop a position you already hold from losing more.
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


# The editable set: the loss/breaker guards, plus the day's deployable fund —
# the values a trader reaches for daily or after a bad run, not the ones that
# reshape a signal's scoring.
SPECS: tuple[LimitSpec, ...] = (
    LimitSpec(
        key="daily_loss_limit", label="Daily loss limit", unit="rupees",
        min=0, max=10_000_000, step=500, zero_disables=True,
        config_attr="signal_daily_loss_limit",
        description=(
            "Once your loss for the day reaches this, the engine stops issuing new "
            "signals until tomorrow. Counts money already booked AND the unrealised "
            "loss on positions you still hold — a loss you are sitting in is not a "
            "smaller loss than one you have closed. Halts signals only; it places no "
            "order and cannot close what you already hold."
        ),
        example="At 1% risk on your capital, ₹10,000 is about two full stops.",
    ),
    LimitSpec(
        key="max_open_drawdown", label="Open-drawdown halt", unit="rupees",
        min=0, max=10_000_000, step=500, zero_disables=True,
        config_attr="signal_max_open_drawdown",
        description=(
            "Pauses new signals while the positions you currently hold are collectively "
            "down this much, even before anything is booked. This is the guard that was "
            "missing on 20-Jul: the loss was mostly unrealised while more trades kept "
            "being taken. Halts signals only — no order is placed."
        ),
        example="Set below the daily limit so it bites before the day is written off.",
    ),
    LimitSpec(
        key="max_consecutive_losses", label="Losing streak halt", unit="trades",
        min=1, max=20, step=1, zero_disables=False,
        config_attr="signal_max_consecutive_losses",
        description=(
            "After this many losing trades in a row, signals pause for the day. Counts "
            "TRADES, not rupees — two small losses trip it the same as two large ones, "
            "which is why the rupee limits above matter too."
        ),
        example="2 means the third signal is withheld after two losses back to back.",
    ),
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
        key="max_open_positions", label="Max open positions", unit="positions",
        min=0, max=20, step=1, zero_disables=True,
        config_attr="signal_max_open_positions",
        description=(
            "No new signal is issued while this many positions are already open. Stops "
            "the engine stacking correlated bets — six PE cards on one view is the engine "
            "whipsawing, not six edges."
        ),
        example="2 blocks a third concurrent position until one is closed.",
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
                    "The loss limits HALT SIGNALS — they place, modify, or close no order. "
                    "Tradewell puts nothing in the market. They stop the engine offering "
                    "more trades once the day has gone against you; your actual loss is "
                    "still capped only by the stop you act on yourself. The trading fund "
                    "is different: it only prefills lot quantities on signal cards."
                ),
            }


risk_limit_store = RiskLimitStore()
