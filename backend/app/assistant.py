"""The dashboard's ask-anything box — Claude, grounded in the live screen.

WHY: the panels answer "what is happening" with numbers; a beginner's real
question is usually "what does this MEAN" ("why is the chart's NIFTY not the
header's NIFTY?", "is ADX 46 good?"). This wires those questions to Claude
with the CURRENT dashboard state attached, so answers are about this screen
right now, not generic textbook trivia.

DESIGN CONSTRAINTS, same family as the news analyzer:
  * Claude EXPLAINS; it never signals. The system prompt forbids inventing
    predictions or trade instructions beyond what the engine already shows —
    Tradewell's own gates stay the only signal source.
  * Grounded or silent: the context block is assembled server-side from live
    state; the prompt orders "if the context doesn't show it, say so".
  * Cheap by default: Haiku, small max_tokens, client-capped history. Every
    call is user-initiated — nothing polls this.
  * Lazy import + graceful off-switch: without ANTHROPIC_API_KEY the endpoint
    reports itself disabled instead of erroring.
"""
from __future__ import annotations

import logging

from app.config import get_settings

log = logging.getLogger("tradewell.assistant")

MAX_TURNS = 8              # question/answer pairs the client may replay
MAX_TOKENS = 500

_SYSTEM = """You are the in-dashboard explainer for Tradewell, a personal,
advisory-only NIFTY options decision-support tool. The user is a beginner
options trader reading their live dashboard; a LIVE CONTEXT block from that
dashboard accompanies every question.

Rules, in order:
1. Ground every answer in the LIVE CONTEXT. If the context doesn't contain
   what's asked, say so plainly instead of guessing.
2. Explain — never signal. You must not invent buy/sell recommendations,
   price predictions, or targets beyond what the context itself shows. If
   asked "should I buy?", explain what the engine's own gates/callouts say
   and remind them the decision and its risk are theirs.
3. Be concrete and brief: 2-6 sentences, plain language, numbers from the
   context quoted exactly. Define any jargon you use in one clause.
4. Terminology you may rely on: the header shows the NIFTY 50 index (spot);
   the main chart shows the near-month FUTURE (runs above spot by the
   "basis"); level callouts (#B1 etc.) come from 3 years of touch history;
   the engine issues scored signal cards gated at 78/100 intraday.
5. When the context includes the IMPLIED ODDS block, you may quote those
   probabilities for "what is the chance..." questions — ALWAYS labelled as
   what the option market currently implies (risk-neutral, derived from
   live IVs), never as your own forecast. Note that they move with IV, and
   that the chance of TOUCHING a level before a date is roughly double the
   finish-beyond odds for out-of-the-money strikes.
6. Never present yourself as a licensed advisor; this is educational
   decision support for the tool's owner."""


def _fmt(v, nd=2):
    try:
        return f"{float(v):,.{nd}f}"
    except Exception:
        return "n/a"


def build_context() -> str:
    """One compact text block of what the dashboard is showing RIGHT NOW.
    Every section degrades to absence rather than raising — a half-dead feed
    must still produce a usable context."""
    lines: list[str] = []
    try:
        from app.state import market_state

        snap = market_state.underlying_snapshot("NIFTY")
        if snap:
            basis = (snap.fut_ltp - snap.ltp) if snap.fut_ltp and snap.ltp else None
            lines.append(
                f"NIFTY spot {_fmt(snap.ltp)} ({_fmt(snap.change_pct, 2)}% today, "
                f"day {_fmt(snap.day_low)}-{_fmt(snap.day_high)}, prev close {_fmt(snap.prev_close)}); "
                f"near-month future {_fmt(snap.fut_ltp)}"
                + (f" (basis {basis:+.0f} pts)" if basis is not None else ""))
        vix = market_state.vix_snapshot()
        if vix and vix.ltp:
            lines.append(f"India VIX {_fmt(vix.ltp)} ({vix.status or 'n/a'})")
        age = market_state.last_tick_age()
        if age is not None and age > 120:
            lines.append(f"WARNING: tick feed is {age}s stale — treat numbers as frozen.")
    except Exception:
        log.debug("assistant: snapshot context failed", exc_info=True)
    try:
        from app.signals.store import signal_store
        from app.signals.models import TradingMode

        latest = signal_store.latest("NIFTY", TradingMode.INTRADAY)
        if latest is not None:
            st = latest.status
            lines.append(
                f"Engine (intraday): regime {st.regime.value}, bull score {st.bull_score:.0f}, "
                f"bear score {st.bear_score:.0f} (gate 78); "
                + (f"ACTIVE CARD: {latest.signal.contract}, entry "
                   f"{latest.signal.entry_low}-{latest.signal.entry_high}, SL {latest.signal.premium_sl}"
                   if latest.signal else
                   f"no card — {latest.no_trade_reason or 'no reason recorded'}"))
    except Exception:
        log.debug("assistant: signal context failed", exc_info=True)
    try:
        from app.market.pulse import compute_pulse, narrate
        from app.state import market_state as _ms

        p = compute_pulse(_ms, "NIFTY")
        text = narrate(p)
        if text:
            lines.append("Market pulse read: " + " ".join(text.split()))
    except Exception:
        log.debug("assistant: pulse context failed", exc_info=True)
    try:
        odds = _implied_odds()
        if odds:
            lines.append(odds)
    except Exception:
        log.debug("assistant: implied odds failed", exc_info=True)
    try:
        from app.services import feed

        lw = getattr(feed, "level_watch", None)
        if lw is not None:
            todays = lw.recent()
            if todays:
                for a in todays[-4:]:
                    o = a.get("outcomes") or {}
                    lines.append(
                        f"Level callout {a.get('code') or a['side'].upper()} at "
                        f"{a['level']:.0f} (spot was {a['spot']:.0f}); "
                        f"level {'BROKE' if o.get('broke') else 'holding'}"
                        + (f", 30m verdict {'right' if o.get('win') else 'wrong'}"
                           if o.get("win") is not None else ""))
            else:
                lines.append("No level callouts yet today.")
    except Exception:
        log.debug("assistant: level context failed", exc_info=True)
    return "\n".join(lines) if lines else "No live data available (feed may be down)."


_RISK_FREE = 0.065


def _p_above(spot: float, strike: float, sigma: float, days: float) -> float:
    """Risk-neutral P(spot > strike) after `days`, Black-Scholes N(d2)."""
    import math

    from app.options.iv import _norm_cdf

    t = max(days, 0.25) / 365.0
    d2 = ((math.log(spot / strike) + (_RISK_FREE - 0.5 * sigma * sigma) * t)
          / (sigma * math.sqrt(t)))
    return _norm_cdf(d2)


def _implied_odds(now_ts: float | None = None) -> str | None:
    """The market's own probabilities, read off live option IVs.

    P(spot finishes above strike K at horizon T) = N(d2) under Black-Scholes
    with the strike's OWN implied vol (per-strike IV is computed in-process
    for ±5 strikes around ATM). Risk-neutral odds — the market's current
    pricing, explicitly NOT a forecast; the block says so and the system
    prompt makes Claude repeat it. Horizons: the next three calendar days
    and expiry, labelled with real dates so "by 7th August" maps directly."""
    import math
    import time as _t
    from datetime import date, timedelta

    from app.kite.instruments import chain_key
    from app.options.iv import _norm_cdf
    from app.state import market_state

    chain = market_state.get_option_chain(chain_key("NIFTY", "nearest"))
    snap = market_state.underlying_snapshot("NIFTY")
    spot = float(snap.ltp) if snap and snap.ltp else None
    if chain is None or not spot or not chain.expiry:
        return None
    try:
        exp = date.fromisoformat(str(chain.expiry))
    except Exception:
        return None
    now_ts = now_ts or _t.time()
    today = date.fromtimestamp(now_ts + 19800)          # IST calendar day
    days_to_exp = (exp - today).days
    if days_to_exp <= 0:
        return None
    # chain IVs are stored as PERCENT (implied_vol returns e.g. 14.3 — its
    # own docstring says so); Black-Scholes wants the fraction. Caught by
    # the first live computation printing a 1250% IV.
    ivs = {r.strike: float(r.ce_iv) / 100.0 for r in chain.rows
           if r.ce_iv and 0 < float(r.ce_iv) < 200}
    if not ivs:
        return None
    atm = min(ivs, key=lambda k: abs(k - spot))
    atm_iv = float(ivs[atm])

    horizons = [d for d in (1, 2, 3) if d < days_to_exp] + [days_to_exp]
    labels = [(f"at expiry {exp.strftime('%d-%b')}" if d == days_to_exp
               else f"by {(today + timedelta(days=d)).strftime('%d-%b')}")
              for d in horizons]

    def p_above(strike: float, days: int) -> float:
        return _p_above(spot, strike, float(ivs.get(strike, atm_iv)), days)

    sig_day = spot * atm_iv / math.sqrt(365.0)
    rows = [f"IMPLIED ODDS from live option IVs (risk-neutral market pricing, "
            f"NOT a forecast; ATM IV {atm_iv:.1%}, weekly expiry "
            f"{exp.strftime('%d-%b')}, {days_to_exp}d away). "
            f"1-sigma move ≈ ±{sig_day:.0f} pts/day, "
            f"±{sig_day * math.sqrt(days_to_exp):.0f} pts to expiry.",
            "P(NIFTY finishes ABOVE strike) — " + " / ".join(labels) + ":"]
    for strike in sorted(ivs):
        probs = " / ".join(f"{p_above(strike, d):.0%}" for d in horizons)
        rows.append(f"  {strike:.0f}: {probs}")
    rows.append("(Odds of TOUCHING a strike before a date are roughly double "
                "the finish-above odds for OTM strikes. All numbers move with IV.)")
    return "\n".join(rows)


def ask(question: str, history: list[dict] | None = None) -> dict:
    """One grounded answer. `history` is [{role, content}] from the client,
    capped and passed through so follow-ups keep their thread."""
    cfg = get_settings()
    if not cfg.anthropic_api_key:
        return {"enabled": False,
                "answer": "Chat is off — set ANTHROPIC_API_KEY in backend/.env."}
    import anthropic  # lazy, same rule as the news analyzer

    client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)
    # Filter THEN cap: an invalid entry must not shrink the replayed window.
    valid = [m for m in (history or [])
             if m.get("role") in ("user", "assistant") and m.get("content")]
    msgs = [{"role": m["role"], "content": str(m["content"])[:2000]}
            for m in valid[-(MAX_TURNS * 2):]]
    msgs.append({"role": "user",
                 "content": f"LIVE CONTEXT:\n{build_context()}\n\nQUESTION: {question[:1000]}"})
    resp = client.messages.create(
        model=cfg.chat_model,
        max_tokens=MAX_TOKENS,
        system=_SYSTEM,
        messages=msgs,
    )
    answer = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    return {"enabled": True, "answer": answer.strip()}
