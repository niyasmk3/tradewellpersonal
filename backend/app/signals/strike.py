"""Strike selection.

Picks ATM / 1-ITM / 1-OTM per the spec: default ATM, step to OTM only when
momentum is strong. Applies liquidity guards (min OI, bid-ask spread from tick
depth) and explains why the chosen strike beat the alternatives.
"""
from __future__ import annotations

from typing import Optional

from app.models.schemas import OptionChain
from app.signals.models import Direction, StrikePick


def _compact(n: float | None) -> str:
    if n is None:
        return "—"
    if abs(n) >= 1e7:
        return f"{n / 1e7:.2f}Cr"
    if abs(n) >= 1e5:
        return f"{n / 1e5:.2f}L"
    return f"{n:.0f}"


def _infer_step(chain: OptionChain) -> float:
    strikes = sorted(r.strike for r in chain.rows)
    diffs = [b - a for a, b in zip(strikes, strikes[1:]) if b > a]
    return min(diffs) if diffs else 50.0


def _spread_ok(token: int | None, ltp: float, ticks: dict | None, max_pct: float):
    """Return (ok, note). Tolerant of missing depth (market closed)."""
    if not token or not ticks:
        return True, None
    depth = (ticks.get(token) or {}).get("depth") or {}
    buys, sells = depth.get("buy") or [], depth.get("sell") or []
    if not buys or not sells:
        return True, None
    bid, ask = buys[0].get("price"), sells[0].get("price")
    if not bid or not ask or ltp <= 0:
        return True, None
    pct = (ask - bid) / ltp
    return pct <= max_pct, f"spread {pct * 100:.1f}%"


def select(
    symbol: str,
    direction: Direction,
    chain: Optional[OptionChain],
    spot: float,
    strong_momentum: bool,
    min_oi: float,
    max_spread_pct: float,
    ticks: dict | None = None,
    strike_bias: str = "atm_otm",
) -> Optional[StrikePick]:
    if chain is None or not chain.rows:
        return None

    step = _infer_step(chain)
    atm = chain.atm_strike or round(spot / step) * step
    ce = direction is Direction.CE

    # ITM is the deeper-in-the-money strike for each side.
    candidates = (
        {"ATM": atm, "ITM": atm - step, "OTM": atm + step}
        if ce
        else {"ATM": atm, "ITM": atm + step, "OTM": atm - step}
    )
    rows = {r.strike: r for r in chain.rows}

    def leg(strike: float):
        r = rows.get(strike)
        if r is None:
            return None
        return {
            "ltp": r.ce_ltp if ce else r.pe_ltp,
            "oi": r.ce_oi if ce else r.pe_oi,
            "token": r.ce_token if ce else r.pe_token,
        }

    considered: list[str] = []
    for m in ("ITM", "ATM", "OTM"):
        info = leg(candidates[m])
        if info and info["ltp"] is not None:
            considered.append(f"{m} {int(candidates[m])}: ₹{info['ltp']:.1f}, OI {_compact(info['oi'])}")

    # Intraday (atm_otm): ATM, step to OTM only on strong momentum.
    # Positional (itm_atm): favour higher delta / less theta — ATM on strong, else ITM.
    if strike_bias == "itm_atm":
        preferred = "ATM" if strong_momentum else "ITM"
    else:
        preferred = "OTM" if strong_momentum else "ATM"
    order = [preferred] + [m for m in ("ATM", "ITM", "OTM") if m != preferred]

    chosen_m = None
    chosen = None
    for m in order:
        info = leg(candidates[m])
        if not info or info["ltp"] is None or info["ltp"] <= 0:
            continue
        if info["oi"] is not None and info["oi"] < min_oi:
            continue
        ok, _ = _spread_ok(info["token"], info["ltp"], ticks, max_spread_pct)
        if not ok:
            continue
        chosen_m, chosen = m, info
        break

    # Fallback: take ATM even if guards fail, so we still surface *something*.
    if chosen is None:
        atm_info = leg(atm)
        if not atm_info or atm_info["ltp"] is None or atm_info["ltp"] <= 0:
            return None
        chosen_m, chosen = "ATM", atm_info

    strike = candidates[chosen_m]
    rationale = [
        f"{chosen_m} strike selected"
        + (" (strong momentum → OTM)" if chosen_m == "OTM" and strong_momentum else ""),
    ]
    if chosen["oi"] is not None:
        rationale.append(f"OI {_compact(chosen['oi'])}")
    _, spread_note = _spread_ok(chosen["token"], chosen["ltp"], ticks, max_spread_pct)
    if spread_note:
        rationale.append(spread_note)

    return StrikePick(
        strike=strike,
        option_type=direction,
        tradingsymbol=f"{symbol} {int(strike)} {direction.value}",
        token=chosen["token"],
        ltp=chosen["ltp"],
        oi=chosen["oi"],
        moneyness=chosen_m,
        rationale=rationale,
        considered=considered,
    )
