"""Bridges live MarketState into the (stateless) SignalEngine + SignalStore,
evaluating every enabled trading mode for every signal symbol.
"""
from __future__ import annotations

import logging
import time

from app.config import Settings
from app.kite.instruments import chain_key
from app.market import calendar as mcal
from app.market.candles import TIMEFRAME_SECONDS
from app.market.indicators import compute_snapshot
from app.news.store import news_store
from app.signals.engine import SignalEngine
from app.signals.modes import ModeProfile, build_profiles
from app.signals.models import (
    Action,
    Bias,
    Direction,
    MarketStatus,
    Regime,
    SignalCard,
    SignalResponse,
    SignalState,
    TradingMode,
)
from app.signals import calibration
from app.signals.eval_trace import eval_trace
from app.signals.score_history import score_history
from app.signals.sizing import apply_fund_sizing
from app.signals.store import SignalStore, ThrottleConfig, shadow_store_for
from app.signals.risk_limits import risk_limit_store
from app.state import MarketState
from app.trades.store import trade_store

log = logging.getLogger("tradewell.signals")


def _parse_hhmm(raw: str) -> int | None:
    """'14:15' -> IST minutes-of-day (855). Empty/invalid -> None (gate off)."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        hh, mm = raw.split(":")
        v = int(hh) * 60 + int(mm)
        return v if 0 <= v < 1440 else None
    except Exception:
        log.warning("SIGNAL_ENTRY_CUTOFF_IST %r is not HH:MM — cutoff disabled", raw)
        return None


def _option_lot_size(token: int | None) -> int:
    """Lot size of the exact option contract behind `token`, or 0.

    Matched by INSTRUMENT TOKEN only — never by strike. A token identifies one
    contract unambiguously, whereas a strike match across universes once handed
    a positional card the weekly contract at the same strike. Imported lazily
    and failure-tolerant: this is a display refinement on the live signal path
    and must never be able to stop a card being issued.
    """
    if not token:
        return 0
    try:
        from app.services import feed

        builder = getattr(feed, "chain_builder", None)
        if builder is None:
            return 0
        for universe in builder.universes.values():
            for sp in universe.strikes.values():
                if sp.ce_token == token:
                    return int(sp.ce_lot_size or 0)
                if sp.pe_token == token:
                    return int(sp.pe_lot_size or 0)
    except Exception:  # pragma: no cover - never break signal generation
        log.debug("option lot-size lookup failed", exc_info=True)
    return 0


def overnight_gap_note(mode_value: str, ist_minutes: int) -> str | None:
    """Caution line for POSITIONAL cards issued late in the session.

    A positional entry after ~14:30 IST barely trades before the close — the
    position's first real test is tomorrow's OPEN, and a gap settles that test
    before any stop can act (28->29 Jul: the evening read was bearish, the
    index opened +230 the next morning; a stop cannot fire inside a gap).
    Advisory text only — sizing and gates are untouched.
    """
    if mode_value != "positional" or ist_minutes < mcal.EVENING_MIN:
        return None
    return ("Late-day positional: holds overnight, and tomorrow's gap can open "
            "beyond the stop before it can act — size for gap risk, not just "
            "the stop distance.")


class SignalService:
    def __init__(self, cfg: Settings, state: MarketState, store: SignalStore) -> None:
        self.cfg = cfg
        self.state = state
        self.store = store
        self.engine = SignalEngine(cfg)
        self.profiles = build_profiles(cfg)

    def _throttle(self) -> ThrottleConfig:
        """Signal CADENCE only. The outcome-based circuit breakers (losing
        streak, daily loss, open positions, open drawdown) were removed
        25-Jul at the user's instruction — see ThrottleConfig. All four timing
        fields are .env-only; changing them mid-session is a footgun.
        """
        return ThrottleConfig(
            max_per_day=self.cfg.signal_max_per_day,
            min_gap_s=self.cfg.signal_min_gap_s,
            cooldown_s=self.cfg.signal_cooldown_s,
            flip_guard_s=self.cfg.signal_flip_guard_s,
        )

    def _apply_sizing(self, resp: SignalResponse, symbol: str) -> None:
        """Suggest lots from a risk budget. Never guesses: with no configured
        capital the card simply carries no suggestion."""
        card = resp.signal
        if card is None:
            return
        capital = self.cfg.trading_capital
        meta = self.state.underlyings.get(symbol.upper())
        # Prefer THIS option contract's own lot size over the future's: NSE
        # applies lot revisions to newly listed series while live contracts
        # keep the old size, so the two disagree for weeks. The Kite basket
        # already resolves it this way, and the rupee figures the UI shows must
        # match the quantity Kite actually receives.
        lot = _option_lot_size(card.token) or (meta.lot_size if meta and meta.lot_size else 0)
        entry = card.ref_entry_premium or card.entry_high
        # Size against the stop that ACTUALLY ends the trade. With the index
        # invalidation primary, that is the disaster backstop — a wider stop is
        # more rupees per lot, so the same risk budget buys fewer lots. Sizing
        # off the narrower premium_sl would understate risk by ~2.5x.
        operative_sl = card.disaster_sl or card.premium_sl
        per_unit_risk = max(0.0, entry - operative_sl)

        # Contract/account facts are attached BEFORE the suggestion returns
        # early: the UI needs the lot size to show rupee outcomes even when no
        # capital is configured, and "no suggestion" must not mean "no numbers".
        card.lot_size = lot or None
        card.trading_capital = capital or None
        # daily_loss_limit is context the card carries only to relate a
        # position's loss to the account; the breaker that once acted on it is
        # gone (25-Jul), so it comes straight from .env now, not the live overlay.
        card.daily_loss_limit = self.cfg.signal_daily_loss_limit or None
        # Affordability prefill from the day's fund. Re-run against the live
        # premium on every poll (routes_signals); this is the issue-time value.
        limits = risk_limit_store.effective(self.cfg)
        apply_fund_sizing(card, limits.get("trading_fund", 0.0))

        # Scheduled-event caution: known macro windows halve the suggestion —
        # the documented conservative default. Zero score impact; the news
        # component handles the aftermath, this handles the calendar.
        import time as _time

        from app.market import events as _events

        # Window sized to the position's LIFETIME: an intraday MIS position is
        # squared off ~15:20 and can't reach an evening print (3h look-ahead);
        # a positional hold rides through anything on the calendar today (12h).
        window_h = 12.0 if card.mode.value == "positional" else 3.0
        card.event_note = _events.upcoming(int(_time.time()), window_h=window_h)

        if capital <= 0:
            card.sizing_note = "Set TRADING_CAPITAL in .env for a size suggestion"
            return
        if lot <= 0 or per_unit_risk <= 0:
            return
        budget = capital * (self.cfg.risk_per_trade_pct / 100.0)
        per_lot_risk = per_unit_risk * lot
        lots = int(budget // per_lot_risk)
        halved = bool(card.event_note and lots > 1)
        if halved:
            lots = lots // 2
        # EVENING-POSITIONAL GAP CUT (31-Aug): a positional entry at/after
        # 14:30 rides the overnight gap — the one risk no stop can act on
        # (18-Aug: 15:21 entry gapped to -50.8%). Scale the suggestion until
        # the overnight-hold ledger renders its verdict; 1-lot suggestions
        # cannot shrink, so the note carries the caution alone.
        evening = (card.mode.value == "positional"
                   and self.cfg.evening_positional_size_factor < 1.0
                   and ((int(_time.time()) + 19800) % 86400) // 60 >= 14 * 60 + 30)
        if evening and lots > 1:
            lots = max(1, int(lots * self.cfg.evening_positional_size_factor))
        card.suggested_lots = max(0, lots)
        card.sizing_note = (
            f"{lots} lot(s) risks ≈₹{lots * per_lot_risk:,.0f} "
            f"({self.cfg.risk_per_trade_pct:.1f}% of ₹{capital:,.0f}) if the stop is hit"
            + (f" · HALVED for {card.event_note}" if halved else "")
            + (" · EVENING entry: size cut — overnight gaps bypass stops"
               if evening else "")
        )

    def _frame_integrity_veto(self, df, now: int) -> str | None:
        """Data-integrity refusals shared by EVERY candidate source — the
        engine's cards and the setup detectors alike (review catch: a setup
        fired happily on yesterday's frame that _context_veto would have
        refused for the engine's own card).

        1. CURRENT-SESSION FRAME: the last closed candle must belong to
           today's session. The 22-Jul 09:15 card was scored 46/75 on
           YESTERDAY's candles through a seeding/pre-open window — a card must
           never describe a market from a previous day.
        2. POST-GAP QUIET: for a window after a tick-feed gap ends, the frame
           contains candles the engine never watched form (or a hole). The
           23-Jul reawakening minted a score-87 card 14s after a 77-minute
           blackout, five minutes after the top.
        """
        if df is not None and len(df):
            last_day = (int(df["ts"].iloc[-1]) + 19800) // 86400
            if last_day != (now + 19800) // 86400:
                return "Waiting for today's first closed candle — refusing to score yesterday's tape"
        quiet = self.cfg.signal_post_gap_quiet_s
        gap_end = getattr(self.state, "gap_ended_at", None)
        if quiet > 0 and gap_end is not None and now - gap_end < quiet:
            mins = int((quiet - (now - gap_end)) // 60) + 1
            return (f"Tick feed resumed after a gap — holding new cards ~{mins}m "
                    "while the tape re-establishes")
        return None

    def _context_veto(self, fresh: SignalResponse, df, now: int) -> str | None:
        """State-aware reasons an otherwise-issuable card must wait.

        Frame integrity (session-currentness, post-gap quiet) lives in
        _frame_integrity_veto so the setup detectors share it verbatim.
        """
        veto = self._frame_integrity_veto(df, now)
        if veto:
            return veto
        # 3. VOLUME DATA HEALTH (audit P0-1): "no volume data" used to score
        #    a neutral 7.0 — numerically equal to the participation floor, so a
        #    BLIND tape passed the floor while a real at-average tape (6 pts)
        #    was vetoed. Participation that cannot be verified fails closed,
        #    with its own reason — it is a data outage, not a measured-weak
        #    tape, so it does NOT go to the floor's counterfactual book.
        #    DELIBERATE consequence (review-flagged): a card whose volume is
        #    unavailable AND whose OI is sub-floor also skips the floor ledger
        #    — correct, because its outcome would be confounded by the blind
        #    volume; outage cards belong to no hypothesis's evidence.
        sig = fresh.signal
        if sig is not None and sig.score is not None:
            comp = next((c for c in sig.score.components
                         if c.name.startswith("Volume")), None)
            if comp is not None and any(
                    r.startswith("Volume unavailable") for r in comp.reasons):
                return ("Volume data unavailable — participation cannot be verified; "
                        "failing closed (a blind tape must not outrank a measured one)")
        return None

    def _late_cutoff_veto(self, fresh: SignalResponse, now: int) -> str | None:
        """LATE-ENTRY CUTOFF (intraday/scalp): a card born in the last hour
        runs into the 15:20 time exit with little runway — the audited week's
        14:27+ cards lost 7-for-7. BUT the 43-session replay says hour-15 is
        the BEST entry hour on the underlying (theta-blind), so this rule is
        now formally a HYPOTHESIS: the caller shadow-books what it vetoes
        (audit P0-2) and the paper book — which pays theta — will decide at
        30+ late-window fills. Positional is exempt: its thesis carries
        overnight."""
        cutoff = _parse_hhmm(self.cfg.signal_entry_cutoff_ist)
        if (cutoff is not None
                and fresh.mode.value in ("intraday", "scalp")
                and ((now + 19800) % 86400) // 60 >= cutoff):
            return (f"Past the {self.cfg.signal_entry_cutoff_ist} entry cutoff — a fresh "
                    "card now has little runway before the 15:20 close (live n=7 "
                    "said always-lose; the replay disagrees — the paper shadow "
                    "book is settling it).")
        return None

    def _refire_veto(self, card, now: int) -> str | None:
        """Refuse to re-issue a thesis the tape already rejected today.

        On 28-Jul the engine fired NIFTY 24000 PE three times — the first
        stopped out, and the two re-fires lost too (-Rs828 combined). The
        cadence throttle spaces cards in TIME but has no memory of OUTCOME:
        after a clean fill on the same underlying + direction (strike within
        2 steps) stops out or invalidates, the same thesis needs the guard
        window to pass — or a genuinely different setup — before it may speak
        again. Reads the paper book (always filled) and the live journal;
        hollow counterfactuals don't count (those cards were never offered).
        """
        window = self.cfg.signal_refire_guard_s
        if window <= 0 or card is None:
            return None
        try:
            for rej in self._recent_rejections(now):
                sym, direction, strike, exited_at, contract, label = rej
                if now - exited_at >= window:
                    continue
                # Per-symbol strike step (BANKNIFTY steps are 100, not 50) —
                # a hardcoded 50 would halve its adjacency window silently.
                from app.kite.instruments import UNDERLYING_CONFIG

                step = float(UNDERLYING_CONFIG.get(card.symbol.upper(), {}).get("step", 50))
                if (sym == card.symbol.upper()
                        and direction == card.direction.value
                        and abs(strike - (card.strike or 0)) <= 2 * step):
                    mins = int((window - (now - exited_at)) // 60) + 1
                    return (f"Re-fire guard: {contract} {label} "
                            f"{int((now - exited_at) // 60)}m ago — the same thesis "
                            f"waits ~{mins}m (or a different setup) before speaking again.")
        except Exception:  # a guard bug must never stop the engine
            log.debug("refire guard failed", exc_info=True)
        return None

    def _recent_rejections(self, now: int) -> list:
        """Today's thesis-rejecting closes from both books, cached ~15s.

        WHAT COUNTS AS REJECTION: plan stops/invalidations (paper closes this
        way), and any LOSING broker-flat or manual live close. The last two
        matter because a real live stop never carries reason "stop" — once the
        broker confirms a position, the reconciler closes it as "broker flat"
        whatever actually happened, so the reason string alone would make the
        guard blind to exactly the live losses it exists to remember. A losing
        close on the thesis is the signal; a winning broker-flat (target,
        profit take) must NOT arm it.

        CACHED because TradeStore.all() deep-copies every row and this runs on
        the per-mode evaluation cycle — one scan per ~15s bounds the cost no
        matter how many modes evaluate or how large the journals grow.
        """
        cached = getattr(self, "_refire_cache", None)
        if cached is not None and cached[0] > now:
            return cached[1]
        entries: list = []
        try:
            from app.paper.service import is_hollow_row
            from app.services import feed
            from app.trades.store import trade_store as live_store

            today = (now + 19800) // 86400
            books = list(live_store.all())
            paper = getattr(feed, "paper_store", None)
            if paper is not None:
                books += [t for t in paper.all() if not is_hollow_row(t)]
            for t in books:
                if t.status.value != "exited" or not t.exited_at:
                    continue
                if (t.exited_at + 19800) // 86400 != today:
                    continue
                reason = t.auto_close_reason
                loss = (t.realized_pnl or 0.0) < 0
                if reason in ("stop", "invalidation"):
                    label = f"{reason}ped out" if reason == "stop" else "invalidated"
                elif loss and (reason == "broker flat" or not t.auto_closed):
                    label = "closed at a loss"
                else:
                    continue
                entries.append((t.symbol.upper(), t.direction.value,
                                float(t.strike or 0), int(t.exited_at),
                                t.contract, label))
        except Exception:
            log.debug("refire scan failed", exc_info=True)
        self._refire_cache = (now + 15, entries)
        return entries

    def _scalp_friction_veto(self, card) -> str | None:
        """Refuse a scalp card whose target cannot pay its own costs.

        At scalp cadence the charges model is the game: brokerage + STT +
        levies on a +6%% move eat 20-30%% of the gross. A card whose estimated
        round-trip friction exceeds SCALP_MAX_FRICTION_PCT of the gross at
        Target 1 (at the suggested size, min 1 lot) is a donation, not a trade.
        """
        if card.mode.value != "scalp":
            return None
        limit = self.cfg.scalp_max_friction_pct
        lot = card.lot_size or 0
        entry = card.ref_entry_premium
        if limit <= 0 or lot <= 0 or not entry or card.target1 <= entry:
            return None
        try:
            from app.paper import charges as chg

            qty = max(1, card.suggested_lots or 1) * lot
            gross = (card.target1 - entry) * qty
            cost = chg.charges(entry, card.target1, qty, 2)
            if gross > 0 and cost / gross > limit:
                return (f"Scalp friction ₹{cost:,.0f} would eat {cost / gross * 100:.0f}% of the "
                        f"₹{gross:,.0f} move to T1 (limit {limit * 100:.0f}%) — costs win this trade")
        except Exception:  # a costing failure must not block other modes
            log.debug("scalp friction check failed", exc_info=True)
        return None

    def _hollow_veto(self, card) -> str | None:
        """Participation floor: volume and OI must each clear a minimum.

        These two components are the only ones that measure whether anyone is
        actually IN the move — the other four describe the chart, and a chart
        can look perfect on air. The 100-point total lets strong shape outvote
        a dead tape (24-Jul: every big paper loser carried volume 2-6/15 under
        a 25/25 price action; 23-Jul: same with OI). Below either floor the
        card is withheld with the reason spoken; the caller routes it to the
        shadow store so the paper book keeps grading the road not taken.
        """
        try:
            if card.score is None:
                return None
            floors = (("Volume", self.cfg.signal_min_volume_score),
                      ("Options", self.cfg.signal_min_oi_score))
            for prefix, floor in floors:
                if floor <= 0:
                    continue
                comp = next((c for c in card.score.components
                             if c.name.startswith(prefix)), None)
                if comp is not None and comp.points < floor:
                    return (f"{comp.name} {comp.points:.0f}/{comp.max:.0f} is below the "
                            f"{floor:.0f}-point floor — price is moving without "
                            "participation, and that is how exhaustion tails score. "
                            "The paper book tracks it as counter-evidence where its "
                            "own throttle allows.")
        except Exception:  # a floor bug must never stop the engine
            log.debug("hollow veto failed", exc_info=True)
        return None

    def _track_gate_runs(self, symbol: str, profile, df, fresh, now: int) -> None:
        """Per-bar persistence bookkeeping for the WATCH->CONFIRM gate: how
        many CONSECUTIVE closed bars each direction's score has held the
        mode's gate. Updated once per new closed bar (the 5s re-evals of the
        same bar must not inflate the run). In-memory only — a restart
        forgets runs, costing at most one extra confirm bar afterwards."""
        try:
            if df is None or not len(df):
                return
            bar = int(df["ts"].iloc[-1])
            secs = TIMEFRAME_SECONDS.get(profile.timeframe, 180)
            if bar + secs > now:
                return                    # forming bar — closed bars only
            runs = getattr(self, "_gate_runs", None)
            if runs is None:
                runs = self._gate_runs = {}
            key = (symbol.upper(), profile.mode.value)
            prev = runs.get(key)
            gate = profile.score_valid
            ce_pass = fresh.status.bull_score >= gate
            pe_pass = fresh.status.bear_score >= gate
            if prev is not None and prev["bar"] == bar:
                return                    # same bar re-eval: keep the record
            contiguous = prev is not None and prev["bar"] == bar - secs
            runs[key] = {
                "bar": bar,
                "CE": (prev["CE"] + 1 if contiguous and ce_pass and prev["CE"] > 0
                       else (1 if ce_pass else 0)),
                "PE": (prev["PE"] + 1 if contiguous and pe_pass and prev["PE"] > 0
                       else (1 if pe_pass else 0)),
            }
        except Exception:
            log.debug("gate-run tracking failed", exc_info=True)

    def _confirm_veto(self, fresh: SignalResponse) -> str | None:
        """WATCH->CONFIRM (audit P2-3, sized 05-Aug): a card whose direction
        has held the gate for fewer than SIGNAL_CONFIRM_BARS consecutive
        closed bars is a WATCH, not an offer. The 60d replay: one-bar
        flickers are 58% of crossings and lose -0.41R at 30% WR (the 05-Aug
        11:03 card's exact class); persistent episodes are the engine's only
        positive class. Intraday/scalp only — positional's 15m frame moves
        too slowly for flicker to be the failure mode."""
        need = int(getattr(self.cfg, "signal_confirm_bars", 0) or 0)
        if need <= 1 or fresh.signal is None:
            return None
        if fresh.mode.value not in ("intraday", "scalp"):
            return None
        runs = getattr(self, "_gate_runs", None) or {}
        rec = runs.get((fresh.symbol.upper(), fresh.mode.value))
        run = rec.get(fresh.signal.direction.value, 0) if rec else 0
        if run >= need:
            return None
        return (f"WATCH — first bar above the gate; needs {need} consecutive "
                f"closed bars to CONFIRM (has {max(run, 1)}). One-bar spikes "
                "lost -0.41R over 60 days; persistence is the only class "
                "that measured positive.")

    def _hypothesis_veto(self, fresh: SignalResponse, now: int) -> tuple:
        """Resolve the four MEASURED-HYPOTHESIS vetoes — participation floor,
        re-fire guard, late cutoff, WATCH->CONFIRM — into
        (veto_text, shadow_tag).

        Each of these gates is a live bet that refusing the card is the right
        call, and each keeps its own counterfactual ledger (floor / refire /
        late shadow classes) so the paper book can rule at 30+ fills. ALL
        THREE are computed every time (review finding, P0-2): a card failing
        two gates at once is confounded evidence for both hypotheses — it is
        vetoed (reason precedence: floor is the most fundamental refusal,
        then the re-fire guard, then the cutoff) and shadow-booked to
        NEITHER ledger.

        The re-fire guard joined this group on 03-Aug: it blocked a 24550 CE
        re-entry at 10:57 that ran +6-9% unmeasured — the 28-Jul evidence
        that created the guard (two re-fires, both lost) is n=2, today's
        counter-example is n=1, and neither decides a knob. Its blocks now
        leave a graded shadow instead of a vanished WAIT.
        """
        floor = self._hollow_veto(fresh.signal)
        refire = self._refire_veto(fresh.signal, now)
        late = self._late_cutoff_veto(fresh, now)
        confirm = self._confirm_veto(fresh)
        hits = [h for h in (floor, refire, late, confirm) if h]
        if not hits:
            return None, None
        if len(hits) > 1:
            lead = hits[0]
            extras = []
            if floor and refire:
                extras.append("under the re-fire guard")
            if late and lead is not late:
                extras.append("past the entry cutoff")
            if confirm and lead is not confirm:
                extras.append("unconfirmed (first gate bar)")
            return lead + " (also " + " and ".join(extras) + ")", None
        if floor:
            return floor, floor
        if refire:
            return refire, "refire: " + refire
        # Late alone: shadow-book only until 15:10 — after that there is
        # genuinely no runway left to measure the counterfactual.
        if late:
            if ((now + 19800) % 86400) // 60 <= 15 * 60 + 10:
                return late, "late: " + late
            return late, None
        return confirm, "confirm: " + confirm

    def _leadership_note(self, symbol: str) -> str | None:
        """BANKNIFTY-vs-NIFTY relative strength, as a displayed note only."""
        if symbol.upper() != "NIFTY":
            return None
        try:
            nifty = self.state.underlying_snapshot("NIFTY")
            bank = self.state.underlying_snapshot("BANKNIFTY")
            if not nifty or not bank or nifty.change_pct is None or bank.change_pct is None:
                return None
            gap = round(bank.change_pct - nifty.change_pct, 2)
            if abs(gap) < 0.35:
                return None
            side = "leading" if gap > 0 else "lagging"
            return f"BANKNIFTY {side} by {abs(gap):.2f}% — sector participation context"
        except Exception:  # display-only; never block an evaluation
            return None

    def evaluate_symbol(self, symbol: str, profile: ModeProfile) -> SignalResponse | None:
        engine = self.state.engine_for_symbol(symbol)
        if engine is None:
            return None
        now = int(time.time())

        # Session gate (round-2 audit): outside market hours the ticks/chain are
        # frozen leftovers — issuing a "fresh" card with a live countdown off
        # them is the most misleading thing an advisory tool can do. Reconcile a
        # market-closed no-trade instead (active cards still expire naturally).
        if not mcal.is_market_open():
            reason = mcal.market_closed_reason()
            status = MarketStatus(
                symbol=symbol, mode=profile.mode, regime=Regime.WARMING_UP,
                regime_label="Market Closed", bias=Bias.NEUTRAL,
                bull_score=0.0, bear_score=0.0, headline=reason,
                vix_status=None, news_label=None, news_net=None, notes=[reason],
            )
            closed = SignalResponse(
                symbol=symbol, mode=profile.mode, evaluated_at=now, status=status,
                action=Action.AVOID, signal=None, no_trade_reason=reason, score=None,
            )
            return self.store.reconcile(closed, now)
        df = engine.dataframe(profile.timeframe)
        # Evaluate on CLOSED candles only. The forming candle carries partial
        # volume (deflating volume_ratio ~10x early in a bucket) and flickering
        # patterns/structure; the chart still shows it via the REST candles API.
        secs = TIMEFRAME_SECONDS.get(profile.timeframe, 180)
        # `> 1`: never trim the ONLY candle away. Right after the 09:15 open (or
        # any day rollover) a single forming candle exists — trimming it yields
        # an empty frame that breaks downstream scoring. Keeping it is harmless
        # because the engine is still warming up (needs min_candles) and cannot
        # issue a signal from one bar.
        if len(df) > 1 and int(df["ts"].iloc[-1]) + secs > now:
            df = df.iloc[:-1]
        ind = compute_snapshot(df)
        snap = self.state.underlying_snapshot(symbol)
        chain = self.state.get_option_chain(chain_key(symbol, profile.expiry_key))
        vix = self.state.vix_snapshot()

        fresh = self.engine.evaluate(
            profile=profile,
            symbol=symbol,
            df=df,
            ind=ind,
            chain=chain,
            spot_ltp=snap.ltp if snap else None,
            fut_ltp=snap.fut_ltp if snap else None,
            prev_close=snap.prev_close if snap else None,
            day_high=snap.day_high if snap else None,
            day_low=snap.day_low if snap else None,
            vix_status=vix.status if vix else None,
            ticks=self.state.ticks,
            now=now,
            news=news_store.sentiment(symbol),
            vix_percentile=self.state.vix_percentile(vix.ltp if vix else None),
            rr1_override=calibration.intraday_rr1(profile, self.cfg),
        )

        # Cross-index leadership, surfaced as CONTEXT (zero score weight until
        # the paper book proves it deserves any): a CE thesis with BANKNIFTY
        # underperforming, or a PE with banks holding up, is worth an eyebrow.
        lead = self._leadership_note(symbol)
        if lead and fresh.status is not None:
            fresh.status.notes = [*fresh.status.notes, lead][:6]

        self._apply_sizing(fresh, symbol)

        # TAPE STATE + GOLDEN label (09-Aug weekend study). Display and
        # ledger material ONLY — no gate, no score input. Attached before the
        # veto block on purpose: vetoed copies land in the shadow stores with
        # their tags intact, so every ledger can later split by tape state.
        if fresh.signal is not None:
            fresh.signal.macd_aligned = self._macd_aligned(
                df, fresh.signal.direction.value)
            tape = self._tape_state(df, now)
            if tape is not None:
                t_state, t_pct, t_side = tape
                card = fresh.signal
                card.tape_state = t_state
                card.tape_resolved_pct = t_pct
                card.tape_aligned = (t_side == "up") == (card.direction.value == "CE")
                # GOLDEN = confirm-gated mode + developing tape + with the
                # day. The label is itself a hypothesis: the paper book grades
                # golden vs ordinary fills, verdict at 30+ golden fills.
                card.golden = bool(
                    t_state == "developing"
                    and card.tape_aligned
                    and card.mode.value in ("intraday", "scalp")
                    and int(getattr(self.cfg, "signal_confirm_bars", 0) or 0) >= 2
                )

        # Late-day positional cards carry the overnight-gap caution (advisory
        # text; the 28->29 Jul +230-point gap against the evening read is why).
        if fresh.signal is not None:
            gap_note = overnight_gap_note(
                fresh.signal.mode.value, (now + 19800) % 86400 // 60)
            if gap_note:
                fresh.signal.event_note = (
                    f"{fresh.signal.event_note} · {gap_note}"
                    if fresh.signal.event_note else gap_note)

        # Context vetoes that need STATE (or the sizing above). Each converts
        # an issued card into a WAIT with the reason shown — the score panel
        # stays live, only the offer is withheld.
        veto = shadow_tag = None
        # Persistence bookkeeping runs on EVERY evaluation (scores below the
        # gate must reset the run), before any veto looks at it.
        self._track_gate_runs(symbol, profile, df, fresh, now)
        # Whether the ENGINE formed a card at all, before any veto: the setup
        # detectors run only in the genuinely-empty case (review catch —
        # gating on the post-veto signal let a setup double-book the same bar
        # a floor/late shadow was already measuring, confounding both).
        engine_had_card = fresh.signal is not None
        if fresh.signal is not None:
            # Integrity vetoes first — session frame, volume-data health and
            # scalp friction are "this card is not viable" refusals, with no
            # hypothesis to measure. The re-fire guard deliberately moved OUT
            # of this group (03-Aug: it blocked a 24550 CE re-entry that ran
            # on without us) and into the measured-hypothesis resolution
            # below, where its blocks get shadow-booked and graded.
            veto = (self._context_veto(fresh, df, now)
                    or self._scalp_friction_veto(fresh.signal))
            if veto is None:
                veto, shadow_tag = self._hypothesis_veto(fresh, now)
            if shadow_tag:
                try:
                    shadow = fresh.model_copy(deep=True)
                    shadow.signal.hollow_reason = shadow_tag
                    # Same cadence throttle so the shadow slot stabilises one
                    # card per window, just like the live feed. Routed to the
                    # CLASS's own store (review catch): with one shared slot a
                    # squatting floor shadow silently dropped refire/late
                    # candidates and throttled their 30-fill verdicts.
                    shadow_store_for(shadow_tag).reconcile(
                        shadow, now, self._throttle())
                except Exception:
                    log.debug("shadow reconcile failed", exc_info=True)
            if veto:
                fresh.signal = None
                fresh.action = Action.WAIT
                fresh.no_trade_reason = veto
        # Setup detectors (audit P1-4): structural candidates the score cannot
        # see, shadow-booked into their OWN ledger. Only when the engine
        # formed NO card at all — a card that existed and was vetoed belongs
        # to that veto's ledger, and a second fill off the same bar would
        # confound both books.
        setup_name = None
        if not engine_had_card:
            setup_name = self._maybe_setup_shadow(symbol, profile, df, fresh, now)
        # Score trend, recorded from the PRE-throttle evaluation: the throttle
        # shapes what is OFFERED, not what the market scored. record() swallows
        # its own failures — the trend feature must never stop a signal.
        score_history.record(
            symbol, profile.mode.value, now,
            fresh.status.bull_score, fresh.status.bear_score,
            fresh.score.direction.value if fresh.score else None,
            {c.name: c.points for c in fresh.score.components} if fresh.score else None,
        )
        final = self.store.reconcile(fresh, now, self._throttle())
        self._trace_bar(symbol, profile, df, fresh, final, veto, shadow_tag, now,
                        setup=setup_name)
        return final

    def _maybe_setup_shadow(self, symbol, profile, df, fresh, now) -> str | None:
        """P1-4: run the registered setup detector and shadow-book its card.

        Fires only when the engine offered NO clean card, intraday only (the
        sized frame), never past the entry cutoff, one booking per bar and
        per-direction spacing per the registered DEDUPE_S. The card is built
        through the SAME strike-liquidity and premium-freshness gates as a
        real card — a bypassed liquidity guard or stale quote books nothing,
        so the ledger measures trades the system could actually have offered.
        Returns the setup name for the eval trace, or None. Never raises.
        """
        try:
            from app.signals import setups

            if profile.mode is not TradingMode.INTRADAY:
                return None
            if not getattr(self.cfg, "paper_trading", True):
                return None               # the ledger IS the product; no book, no point
            # The engine's own data-integrity refusals apply verbatim (review
            # catch: without this, a setup fired on yesterday's frame and
            # inside the post-gap quiet window the engine itself distrusts).
            if self._frame_integrity_veto(df, now):
                return None
            if self._late_cutoff_veto(fresh, now):
                return None
            if df is None or len(df) < 2:
                return None
            hit = setups.vwap_cross(df)
            if hit is None:
                return None
            fired = getattr(self, "_setup_fired", None)
            if fired is None:
                fired = self._setup_fired = {}
            key = (symbol.upper(), hit.name, hit.direction.value)
            last_bar, last_at = fired.get(key, (None, 0))
            if hit.bar_ts == last_bar or now - last_at < setups.DEDUPE_S:
                return None
            card = self._setup_card(symbol, profile, df, hit, now)
            if card is None:
                return None
            card.hollow_reason = f"setup: {hit.name} — {hit.note}"
            shadow = fresh.model_copy(deep=True)
            shadow.signal = card
            shadow.action = card.action
            # The store displaces an incumbent only on a bias flip, and the
            # engine's bias is exactly what setups must not inherit (third
            # door for the direction lockout): the setup's own direction IS
            # its bias claim, so an opposite setup can displace a stale one.
            shadow.status.bias = (Bias.BULLISH if card.direction is Direction.CE
                                  else Bias.BEARISH)
            # The detector's OWN dedupe is the cadence control — the live
            # feed's throttle must not gate this store (review catch: its
            # flip-guard would have blocked a PE setup within 30min of a CE
            # one, silently re-importing the direction lockout the detectors
            # exist to escape).
            # max_per_day is a >= check in the store — 0 would block EVERY
            # booking, so "effectively unlimited" is spelled as a big number.
            res = shadow_store_for("setup:").reconcile(
                shadow, now,
                ThrottleConfig(max_per_day=10000, min_gap_s=0, cooldown_s=0,
                               flip_guard_s=0))
            adopted = (res is not None and res.signal is not None
                       and res.signal.id == card.id)
            if not adopted:
                # Slot still held by a live prior setup card — retry next
                # cycle; the dedupe is NOT burned for a booking that never
                # happened (review catch: "booked" was logged either way).
                log.debug("setup candidate held (slot busy): %s", card.contract)
                return None
            fired[key] = (hit.bar_ts, now)
            log.info("setup shadow booked: %s %s %s", hit.name,
                     hit.direction.value, card.contract)
            return hit.name
        except Exception:
            log.debug("setup shadow failed", exc_info=True)
            return None

    def _setup_card(self, symbol, profile, df, hit, now):
        """A full SignalCard for a setup hit, through the real machinery:
        liquidity-guarded strike pick, freshness-gated premium, the mode's
        own ladder. Mirrors the engine's tradeable branch minus the score
        (a setup has none — confidence 0, empty breakdown, tagged title)."""
        from app.signals import risk as risk_mod
        from app.signals import strike as strike_mod
        from app.signals.engine import premium_quote
        from app.signals.models import ScoreBreakdown

        chain = self.state.get_option_chain(chain_key(symbol, profile.expiry_key))
        snap = self.state.underlying_snapshot(symbol)
        spot = float(snap.ltp) if snap and snap.ltp else None
        if chain is None or not spot:
            return None
        direction = hit.direction
        pick = strike_mod.select(
            symbol, direction, chain, spot, False,
            self.cfg.strike_min_oi, self.cfg.strike_max_spread_pct,
            self.state.ticks, strike_bias=profile.strike_bias,
        )
        if pick is None or pick.ltp is None:
            return None
        # A guards-bypassed ATM fallback is loud on a real card; for a SETUP
        # ledger it is disqualifying — participation is half the hypothesis.
        if getattr(pick, "guards_bypassed", False):
            return None
        entry_px, age = premium_quote(self.state.ticks, pick.token, pick.ltp, now)
        max_age = self.cfg.signal_max_premium_age_s
        if max_age > 0 and (age is None or age > max_age):
            return None
        price_fut = float(df["close"].iloc[-1])
        basis = ((snap.fut_ltp - snap.ltp)
                 if snap and snap.fut_ltp and snap.ltp else 0.0)
        ind = compute_snapshot(df)
        plan = risk_mod.build(
            direction, entry_px, df, ind, price_fut, symbol,
            profile.timeframe, profile.premium_sl_pct, profile.rr_target1,
            profile.rr_target2, basis,
            disaster_pct=(self.cfg.premium_disaster_pct
                          if self.cfg.stop_primary == "underlying"
                          and self.cfg.trading_capital > 0 else None),
            quick_pct=self.cfg.quick_target_pct or None,
        )
        bullish = direction is Direction.CE
        card = SignalCard(
            id=f"{symbol}-setup-{hit.name}-{now}-{direction.value}",
            symbol=symbol, mode=profile.mode,
            title=f"{symbol} VWAP {'RECLAIM' if bullish else 'REJECT'} (setup)",
            action=Action.BUY_CE if bullish else Action.BUY_PE,
            direction=direction, state=SignalState.ACTIVE,
            contract=pick.tradingsymbol or f"{symbol} {int(pick.strike)} {direction.value}",
            strike=pick.strike, token=pick.token,
            expiry=chain.expiry if chain else None,
            entry_low=plan.entry_low, entry_high=plan.entry_high,
            premium_sl=plan.premium_sl, disaster_sl=plan.disaster_sl,
            quick_target=plan.quick_target,
            target1=plan.target1, target2=plan.target2,
            trailing_sl_rule=plan.trailing_sl_rule,
            risk_reward=plan.risk_reward,
            confidence=0.0,
            underlying_invalidation=plan.underlying_invalidation,
            invalidation_note=plan.invalidation_note,
            invalidation_level=plan.invalidation_level,
            invalidation_dir=plan.invalidation_dir,
            reasons=[hit.note, *pick.rationale[:1]],
            created_at=now, valid_until=now + profile.validity_seconds,
            score=ScoreBreakdown(direction=direction, components=[], total=0.0),
            ref_spot=spot, ref_entry_premium=entry_px,
        )
        # Same fallback chain as _apply_sizing (review catch): a transient
        # 0/stale per-contract lot in the instrument dump must fall back to
        # the underlying's futures lot, not mint a permanently unfillable
        # card that silently starves the 30-fill ledger.
        meta = self.state.underlyings.get(symbol.upper()) \
            if hasattr(self.state, "underlyings") else None
        card.lot_size = (_option_lot_size(pick.token)
                         or (meta.lot_size if meta and meta.lot_size else 0)) or None
        return card

    @staticmethod
    def _macd_aligned(df, direction: str):
        """True when MACD(12,26,9) sits on the card's side of its signal line
        at issue. LOG-ONLY field (17-Sep sizing: the only MACD variant that
        passed all four walk-forward cells, ~0.70 removal efficiency even
        GIVEN ema-alignment — a momentum-deceleration read the 9/20 cross
        can't see). The 30-fill live readout decides whether it ever becomes
        a veto; until then it is a stamp, never a gate. None below 35 bars.
        Params 12/26/9 are frozen — tuning them would be fitting the label
        to the ledger it exists to test.
        """
        try:
            closes = [float(x) for x in df["close"].tolist()]
            if len(closes) < 35:
                return None

            def _ema(seq, span):
                k = 2.0 / (span + 1)
                s = seq[0]
                out = []
                for v in seq:
                    s = v * k + s * (1 - k)
                    out.append(s)
                return out

            e12, e26 = _ema(closes, 12), _ema(closes, 26)
            macd = [a - b for a, b in zip(e12, e26)]
            sig = _ema(macd, 9)
            hist = macd[-1] - sig[-1]
            return hist > 0 if direction == "CE" else hist < 0
        except Exception:
            return None

    @staticmethod
    def _tape_state(df, now: int):
        """(state, resolved_pct, side) of TODAY's session so far, or None
        while the day is too young to classify (<3 closed bars).

        resolved = |close - day open| / (day high - day low): how one-sided
        the session has been. The weekend study's split points (35/60) are
        FROZEN here — tuning them against outcomes would be fitting the
        label to the ledger it is supposed to test.
        """
        try:
            day = (int(now) + 19800) // 86400
            ts = df["ts"].astype(int)
            g = df[(ts + 19800) // 86400 == day]
            if len(g) < 3:
                return None
            # 30-minute session-age floor: the study that froze the 35/60
            # splits measured on 5-min bars — on the scalp 1m frame "3 bars"
            # is 09:18, when the ratio is coin-flip noise. Age gates DATA
            # QUALITY, not outcomes; it is not a tunable.
            if int(g["ts"].iloc[-1]) - int(g["ts"].iloc[0]) < 1800:
                return None
            o = float(g["open"].iloc[0])
            hi = float(g["high"].max())
            lo = float(g["low"].min())
            c = float(g["close"].iloc[-1])
            rng = hi - lo
            if rng <= 0:
                return None
            sf = abs(c - o) / rng
            state = ("developing" if 0.35 < sf < 0.60
                     else "stretched" if sf >= 0.60 else "two-way")
            return state, round(sf * 100.0, 1), ("up" if c >= o else "down")
        except Exception:
            return None

    def _trace_bar(self, symbol, profile, df, fresh, final, veto, shadow_tag, now,
                   setup=None) -> None:
        """Blind-spot instrumentation (audit P1-2): one trace line per closed
        bar with BOTH directions' scores, the regime vote, and whatever
        stopped a card — engine reason, service veto, or (visible by
        comparing `card` to `held`) the cadence/slot layer in reconcile.
        The audit could not attribute 27 of 41 missed moves because exactly
        this record did not exist. Must never stop a signal — swallows all.
        """
        try:
            if self.cfg.eval_trace_days <= 0 or df is None or not len(df):
                return
            bar = int(df["ts"].iloc[-1])
            # The single-candle frame right after the open is the FORMING bar
            # (the trim above deliberately keeps it). Recording it would burn
            # the dedupe key on partial-bar scores and the closed bar's real
            # line would never land — wait until the bar has actually closed.
            if bar + TIMEFRAME_SECONDS.get(profile.timeframe, 180) > now:
                return
            eval_trace.record({
                "ts": now,
                "bar": bar,
                "symbol": symbol,
                "mode": profile.mode.value,
                "regime": fresh.status.regime.value,
                "bias": fresh.status.bias.value,
                "bull": fresh.status.bull_score,
                "bear": fresh.status.bear_score,
                "close": round(float(df["close"].iloc[-1]), 2),
                # Post-veto outcome of THIS evaluation...
                "action": fresh.action.value,
                "reason": fresh.no_trade_reason,
                "veto": veto,
                "shadow": (("late" if shadow_tag.startswith("late:")
                            else "refire" if shadow_tag.startswith("refire:")
                            else "confirm" if shadow_tag.startswith("confirm:")
                            else "floor")
                           if shadow_tag else None),
                "card": fresh.signal.id if fresh.signal else None,
                # ...and what the slot is actually SERVING after the cadence
                # rules: card set but held different/absent = throttled away.
                "held": (final.signal.id
                         if final is not None and final.signal is not None else None),
                # P1-4: which setup detector (if any) fired on this bar.
                "setup": setup,
                # 09-Aug: tape state per bar — lets future audits split every
                # veto/outcome by developing/stretched/two-way without replay.
                "tape": (self._tape_state(df, now) or (None,))[0],
            })
        except Exception:
            log.debug("eval trace hook failed", exc_info=True)

    def evaluate_all(self) -> None:
        for symbol in self.cfg.signal_symbols:
            for profile in self.profiles.values():
                try:
                    self.evaluate_symbol(symbol, profile)
                except Exception as exc:  # pragma: no cover
                    log.warning("signal eval failed for %s/%s: %s", symbol, profile.mode.value, exc)
