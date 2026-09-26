"""Shared datatypes for the Strategy Lab.

Design rules:
  * Frozen dataclasses for anything that describes a decision (a mutated
    decision record is a lie waiting to be audited).
  * Timestamps are epoch seconds; wall-clock semantics are always IST.
  * Prices are floats in rupees (index points for the underlying spine).
  * Nothing here touches the network, the clock, or a file.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# --- instruments -----------------------------------------------------------

FUT = "FUT"
OPT = "OPT"

CE = "CE"
PE = "PE"

WEEKLY = "weekly"
MONTHLY = "monthly"

BUY = "BUY"
SELL = "SELL"


# --- parameter schema ------------------------------------------------------

@dataclass(frozen=True)
class ParamSpec:
    """One configurable input of a strategy.

    `tunable=True` marks parameters the learning engine may perturb when it
    proposes challengers; everything else is user-set only. Bounds are hard:
    a value outside [lo, hi] is rejected at version-creation time, so a
    runaway optimiser cannot write an absurd config.
    """
    key: str
    label: str
    kind: str                      # "int" | "float" | "choice" | "bool"
    default: object
    lo: Optional[float] = None
    hi: Optional[float] = None
    step: Optional[float] = None
    choices: tuple = ()
    tunable: bool = False
    unit: str = ""
    help: str = ""

    def validate(self, value: object) -> object:
        """Coerce + bound-check a raw value. Raises ValueError on nonsense."""
        if self.kind == "bool":
            if isinstance(value, bool):
                return value
            raise ValueError("%s: expected bool, got %r" % (self.key, value))
        if self.kind == "choice":
            if value in self.choices:
                return value
            raise ValueError("%s: %r not in %s" % (self.key, value, self.choices))
        try:
            num = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            raise ValueError("%s: expected number, got %r" % (self.key, value))
        if self.lo is not None and num < self.lo:
            raise ValueError("%s: %s below min %s" % (self.key, num, self.lo))
        if self.hi is not None and num > self.hi:
            raise ValueError("%s: %s above max %s" % (self.key, num, self.hi))
        return int(round(num)) if self.kind == "int" else num


def validate_params(specs: tuple, raw: dict) -> dict:
    """Full param dict from specs + overrides. Unknown keys are rejected —
    a typo'd override silently falling back to the default has burned every
    backtester ever written."""
    by_key = {s.key: s for s in specs}
    unknown = set(raw) - set(by_key)
    if unknown:
        raise ValueError("unknown params: %s" % sorted(unknown))
    out = {}
    for spec in specs:
        out[spec.key] = spec.validate(raw[spec.key]) if spec.key in raw else spec.default
    return out


# --- signals and orders ----------------------------------------------------

@dataclass(frozen=True)
class LegSpec:
    """One leg of an intended position, in strategy terms (not yet a
    contract). The engine resolves it against what was actually listed and
    quoted at fill time."""
    side: str                      # BUY | SELL
    right: str                     # CE | PE | FUT
    expiry_rule: str = WEEKLY      # weekly | monthly (ignored for FUT)
    strike_rule: str = "ATM_OFF"   # ATM_OFF | DELTA | PREMIUM_NEAR
    # ATM_OFF: strike = ATM + offset*step *in the OTM direction of `right`*
    #   (offset +1 for a CE = one step above ATM; +1 for a PE = one step
    #   below; negative = ITM). For FUT this is ignored.
    strike_offset: int = 0
    delta_target: Optional[float] = None   # for strike_rule == "DELTA"
    premium_target: Optional[float] = None # for strike_rule == "PREMIUM_NEAR"
    lots: int = 1


@dataclass(frozen=True)
class ExitPolicy:
    """Engine-managed bracket. Kinds:
      pct_value      : % of the position's combined entry value (options)
      points_spine   : points on the signal spine (futures/index strategies)
      atr_spine      : multiples of the spine ATR at entry
      credit_pct     : % of net credit received (short-premium structures)
    A None target/stop disables that side. time_exit_min is minute-of-IST-day.
    """
    stop_kind: Optional[str] = None
    stop_value: Optional[float] = None
    target_kind: Optional[str] = None
    target_value: Optional[float] = None
    trail_kind: Optional[str] = None       # "pct_value" | "atr_spine"
    trail_value: Optional[float] = None
    time_exit_min: Optional[int] = None
    max_hold_bars: Optional[int] = None


@dataclass(frozen=True)
class SignalIntent:
    """What a strategy wants to do at a closed bar. `direction` is
    bookkeeping for analytics ("LONG"/"SHORT"/"NEUTRAL"), the legs are the
    truth. `tag` lets a strategy label its own setups for the report."""
    ts: int
    direction: str
    legs: tuple                    # tuple[LegSpec, ...]
    exit_policy: ExitPolicy
    tag: str = ""
    note: str = ""


@dataclass(frozen=True)
class LegFill:
    """One resolved, filled leg. quote_ts is the market observation the fill
    price came from — never later than the fill decision allowed."""
    side: str
    right: str
    tradingsymbol: str
    token: int
    strike: float
    expiry: str                    # ISO date, "" for FUT
    lots: int
    qty: int                       # lots * lot_size
    price: float
    quote_ts: int
    lot_size: int


@dataclass
class Position:
    """An open or closed simulated position (one signal's legs together)."""
    sid: str
    version: int
    signal_ts: int
    direction: str
    tag: str
    legs: list                     # list[LegFill]
    exit_policy: ExitPolicy
    entry_ts: int = 0
    exit_ts: Optional[int] = None
    exit_reason: Optional[str] = None
    exit_prices: dict = field(default_factory=dict)   # leg index -> price
    entry_costs: float = 0.0
    exit_costs: float = 0.0
    # combined value bookkeeping (signed: long value positive, credit negative)
    entry_value: float = 0.0       # rupees paid (+) or received (-) net of nothing
    peak_value: float = 0.0        # best mark since entry (for trailing)
    mae: float = 0.0               # worst adverse move, rupees
    mfe: float = 0.0               # best favourable move, rupees
    spine_entry: float = 0.0       # spine price at entry (for spine brackets)
    spine_atr_entry: float = 0.0

    @property
    def is_open(self) -> bool:
        return self.exit_ts is None


@dataclass(frozen=True)
class TradeRecord:
    """Immutable, storable outcome of one closed position."""
    sid: str
    version: int
    signal_ts: int
    entry_ts: int
    exit_ts: int
    direction: str
    tag: str
    legs: tuple                    # tuple of dicts (json-ready)
    pnl_gross: float
    costs: float
    pnl_net: float
    exit_reason: str
    mae: float
    mfe: float
    regime: str
    session_bucket: str            # "open" | "mid" | "close"
    expiry_day: bool
    bars_held: int


REJECT_NO_QUOTE = "no_fresh_quote"
REJECT_NO_STRIKE = "strike_not_listed"
REJECT_RISK = "risk_blocked"
REJECT_CAPACITY = "max_trades"
REJECT_WINDOW = "outside_window"
REJECT_LAPSED = "order_lapsed"
