"use client";

import { useEffect, useState } from "react";
import { ApiError, ScoreBreakdown, ScoreHistoryPoint, SignalResponse, TradingMode, api } from "@/lib/api";
import { fetchKiteBasket, submitKiteBasket } from "@/lib/kiteBasket";
import { fmt, istToday, parseNum, pctFrom } from "@/lib/format";
import { usePolling } from "@/lib/usePolling";
import { RiskVisualizer } from "./RiskVisualizer";

function useCountdown(validUntil: number | undefined) {
  const [now, setNow] = useState(() => Math.floor(Date.now() / 1000));
  useEffect(() => {
    const id = setInterval(() => setNow(Math.floor(Date.now() / 1000)), 1000);
    return () => clearInterval(id);
  }, []);
  if (!validUntil) return null;
  const left = validUntil - now;
  if (left <= 0) return "expired";
  const m = Math.floor(left / 60);
  const s = left % 60;
  return `${m}:${s.toString().padStart(2, "0")}`;
}

function Stat({
  label,
  value,
  tone,
  pct,
}: {
  label: string;
  value: string;
  tone?: string;
  /** % move from entry — this is what Kite's GTT stop-loss / target boxes want. */
  pct?: string;
}) {
  return (
    <div className="rounded-md border border-edge bg-panel2 px-3 py-2">
      <div className="text-[10px] uppercase tracking-wide text-muted">{label}</div>
      <div className={`font-mono text-sm ${tone ?? "text-white"}`}>{value}</div>
      {pct && <div className={`font-mono text-[10px] ${tone ?? "text-muted"}`}>{pct} · for GTT</div>}
    </div>
  );
}

/** Inline sparkline for a hover popover. Pure SVG, no library. */
function Spark({ values, max, color = "#4a9eff", gate }: {
  values: number[];
  max: number;
  color?: string;
  /** Dashed reference line; defaults to `max` ("full points"). */
  gate?: number;
}) {
  const W = 196;
  const H = 42;
  if (values.length < 2) {
    return <div className="py-2 text-center text-[10px] text-muted">collecting history…</div>;
  }
  const x = (i: number) => (i / (values.length - 1)) * (W - 6) + 3;
  const y = (v: number) => H - 4 - (Math.max(0, Math.min(v, max)) / max) * (H - 8);
  const pts = values.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  const last = values[values.length - 1];
  return (
    <svg width={W} height={H} className="block">
      <line x1="3" y1={y(gate ?? max)} x2={W - 3} y2={y(gate ?? max)} stroke="#3a4356" strokeDasharray="3 3" />
      <line x1="3" y1={y(0)} x2={W - 3} y2={y(0)} stroke="#252b38" />
      <polyline points={pts} fill="none" stroke={color} strokeWidth="1.5" />
      <circle cx={x(values.length - 1)} cy={y(last)} r="2.5" fill={color} />
    </svg>
  );
}

function ScoreBars({
  score,
  history,
}: {
  score: ScoreBreakdown;
  /** Same-day evaluation trail; null until the poll lands (or old backend). */
  history: ScoreHistoryPoint[] | null;
}) {
  const [hover, setHover] = useState<string | null>(null);
  return (
    <div className="grid grid-cols-2 gap-x-4 gap-y-1.5 sm:grid-cols-3">
      {score.components.map((c) => {
        const pct = c.max > 0 ? (c.points / c.max) * 100 : 0;
        const series = (history ?? [])
          .map((p) => p.components?.[c.name])
          .filter((v): v is number => typeof v === "number");
        return (
          // Roll-over target: mouse, keyboard focus, and tap all open the
          // trend popover — a hover-only affordance is invisible on touch.
          <div
            key={c.name}
            className="relative min-w-0 cursor-help"
            tabIndex={0}
            onMouseEnter={() => setHover(c.name)}
            onMouseLeave={() => setHover((h) => (h === c.name ? null : h))}
            onFocus={() => setHover(c.name)}
            onBlur={() => setHover((h) => (h === c.name ? null : h))}
            onClick={() => setHover((h) => (h === c.name ? null : c.name))}
          >
            <div className="flex justify-between text-[10px] text-muted">
              <span className="truncate">{c.name}</span>
              <span className="font-mono">
                {c.points}/{c.max}
              </span>
            </div>
            <div className="mt-0.5 h-1 overflow-hidden rounded-full bg-edge">
              <div className="h-full bg-accent" style={{ width: `${pct}%` }} />
            </div>
            {hover === c.name && (
              <div className="absolute left-0 top-full z-20 mt-1 rounded-md border border-edge bg-panel px-2 pb-1 pt-1.5 shadow-lg">
                <div className="mb-0.5 flex items-baseline justify-between gap-3 text-[9px] text-muted">
                  <span className="truncate">{c.name} — today</span>
                  <span className="font-mono">
                    now {c.points}/{c.max}
                  </span>
                </div>
                <Spark values={series} max={c.max} />
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

export function SignalPanel({
  data,
  symbol,
  mode,
  error,
  onEntered,
  onIgnore,
  onReprice,
  className = "",
}: {
  data: SignalResponse | null;
  symbol: string;
  mode: TradingMode;
  error?: string | null;
  onEntered: () => void;
  onIgnore: (signalId: string) => void;
  onReprice?: () => void;
  className?: string;
}) {
  const signal = data?.signal ?? null;
  const countdown = useCountdown(signal?.valid_until);
  // Same-day score trail for the hover sparklines. Cheap (decimated server-
  // side) and shared by every component row, so one poll for the panel.
  const scoreHist = usePolling(() => api.scoreHistory(symbol, mode), 30000, [symbol, mode]);
  const [entering, setEntering] = useState(false);
  const [lots, setLots] = useState("1");
  const [entryPx, setEntryPx] = useState("");
  const [busy, setBusy] = useState(false);

  // Bind the form to the card it was opened for: if the active signal changes
  // (bias flip swaps CE→PE), a still-open form must not book the new contract
  // at the old premium. Reset everything whenever the card id changes.
  //
  // Lots prefill: the CONSERVATIVE of the two counts. fund_lots caps outlay,
  // suggested_lots caps loss — and they can differ by 100x (₹10L fund vs a 1%
  // risk budget). Defaulting to the bigger one would open every Kite basket
  // at maximum size and fire the oversize-entry warning on every journal
  // entry, training the exact click-through habit that warning exists to
  // break. The fund count stays one tap away on the ⛁ line below — sizing up
  // to the whole fund must be a decision, not a default. Prefill happens ONLY
  // here, on a new card: while the form is open the field belongs to the
  // user, and a poll that reprices fund_lots must not overwrite what they
  // typed.
  useEffect(() => {
    setEntering(false);
    setEntryPx("");
    const conservative = Math.min(
      signal?.fund_lots || Infinity,
      signal?.suggested_lots || Infinity,
    );
    setLots(String(Number.isFinite(conservative) && conservative > 0 ? conservative : 1));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [signal?.id]);

  const expired = countdown === "expired" || (signal ? signal.state !== "active" : false);

  // How long ago the card was PRICED. The Kite hand-off refuses anything older
  // than 15 min because its entry zone was priced off a stale premium; a
  // positional card stays "active" far longer, so it can look orderable while
  // the hand-off rejects it. Surface that, with a one-click re-price.
  const [nowSec, setNowSec] = useState(() => Math.floor(Date.now() / 1000));
  useEffect(() => {
    const id = setInterval(() => setNowSec(Math.floor(Date.now() / 1000)), 5000);
    return () => clearInterval(id);
  }, []);
  const [repricing, setRepricing] = useState(false);
  const pricedAgoMin = signal ? Math.max(0, Math.floor((nowSec - signal.created_at) / 60)) : 0;
  const STALE_MIN = 15; // must match _MAX_CARD_AGE_S on the backend

  const reprice = async () => {
    if (!signal) return;
    setRepricing(true);
    try {
      const res = await api.repriceSignal(symbol, mode);
      // Refresh re-validates the live score first: if the setup no longer
      // qualifies the signal is CLOSED, not re-priced. Say so — a silently
      // vanishing card would be worse than the stale one.
      if (res.status === "closed") {
        alert(res.reason ?? "Setup no longer valid — signal closed.");
      }
      onReprice?.(); // bump the parent poll so the fresh (or now-absent) card shows
    } catch (e) {
      alert(e instanceof Error ? e.message : "could not refresh the price");
    } finally {
      setRepricing(false);
    }
  };

  /**
   * Hand the order to Kite pre-filled. Tradewell places nothing — this opens
   * Zerodha's basket screen in a new tab where YOU review and confirm.
   * Confirms the quantity first, because a mis-set lots field is the one
   * mistake this button makes faster.
   */
  const tradeInKite = async () => {
    if (!signal || expired) return;
    const lotsN = parseNum(lots);
    if (lotsN === null || lotsN < 1 || !Number.isInteger(lotsN)) {
      alert("Enter a valid whole lot count (1 or more)");
      return;
    }
    // Confirm BEFORE fetching. Confirming after would let the dialog sit open
    // indefinitely while the card expires or the bias flips behind it — the
    // backend guards would have passed minutes earlier, against a card that no
    // longer exists. This way validation runs immediately before the POST.
    const id = signal.id;
    if (
      !window.confirm(
        `Open a Kite order for ${signal.contract}?\n\n` +
          `${lotsN} lot(s) · BUY · LIMIT ₹${fmt(signal.entry_high)}\n\n` +
          `Tradewell places nothing — you review and confirm in Kite.`,
      )
    ) {
      return;
    }
    setBusy(true);
    try {
      const payload = await fetchKiteBasket(symbol, mode, lotsN, id);
      submitKiteBasket(payload);
    } catch (e) {
      alert(e instanceof Error ? e.message : "could not prepare the Kite order");
    } finally {
      setBusy(false);
    }
  };

  const enter = async () => {
    if (!signal || expired) return;
    const lotsN = parseNum(lots);
    if (lotsN === null || lotsN < 1 || !Number.isInteger(lotsN)) {
      alert("Enter a valid whole lot count (1 or more)");
      return;
    }
    let premium: number | undefined;
    if (entryPx.trim()) {
      const p = parseNum(entryPx);
      if (p === null || p <= 0) {
        alert("Enter a valid entry premium");
        return;
      }
      premium = p;
    }
    // No typed premium → send none, so the backend books your LIVE fill
    // (typed > live > signal reference).
    setBusy(true);
    try {
      try {
        await api.enterTrade(symbol, mode, lotsN, premium, signal.id);
      } catch (e) {
        // Size above the card's suggestion is refused ONCE, with the rupee risk
        // spelled out. It is a speed bump, not a wall: the position is already
        // open at the broker, and refusing outright would only leave it
        // untracked. Confirm, and the same request goes back acknowledged.
        if (e instanceof ApiError && e.code === "oversize_lots") {
          if (!confirm(`${e.message}\n\nJournal it anyway?`)) return;
          await api.enterTrade(symbol, mode, lotsN, premium, signal.id, true);
        } else {
          throw e;
        }
      }
      setEntering(false);
      setEntryPx("");
      onEntered();
    } catch (e) {
      alert(e instanceof Error ? e.message : "could not enter trade");
    } finally {
      setBusy(false);
    }
  };

  if (symbol !== "NIFTY") {
    return (
      <div className={`card p-4 text-sm text-muted ${className}`}>
        Signals run on <span className="text-white">NIFTY</span> only in Phase 2. Switch to NIFTY to see setups.
      </div>
    );
  }

  // No active signal → no-trade / wait state.
  if (!signal) {
    const action = data?.action ?? "avoid";
    const wait = action === "wait";
    return (
      <div className={`card overflow-y-auto scroll-thin p-4 ${className}`}>
        {error && !data && (
          <div className="mb-2 text-xs text-bear">Signal feed unavailable — {error}</div>
        )}
        <div className="flex items-center gap-2">
          <span className={`tag ${wait ? "bg-yellow-500/15 text-yellow-400" : "bg-panel2 text-muted"}`}>
            {wait ? "WAIT FOR CONFIRMATION" : "NO TRADE"}
          </span>
          {data?.status && <span className="text-sm text-muted">{data.status.regime_label}</span>}
        </div>
        <p className="mt-2 text-sm text-white/80">
          {data?.no_trade_reason ?? "No valid setup right now."}
        </p>
        {data?.score && (
          <div className="mt-3">
            <div className="mb-1 text-[10px] uppercase text-muted">
              Best-direction score ({data.score.direction}) · {data.score.total.toFixed(0)}/100
            </div>
            <ScoreBars score={data.score} history={scoreHist.data?.points ?? null} />
            {/* The build-up that matters most while waiting: both totals
                against the 78 issue gate, so "bear grinding toward a card"
                is visible without hovering anything. */}
            {(scoreHist.data?.points?.length ?? 0) >= 2 && (
              <div className="mt-2 rounded-md border border-edge/60 bg-panel2 px-2 pb-1 pt-1.5">
                <div className="flex justify-between text-[9px] text-muted">
                  <span>
                    score trend · <span className="text-bull">CE {scoreHist.data!.points.at(-1)!.bull.toFixed(0)}</span>{" "}
                    <span className="text-bear">PE {scoreHist.data!.points.at(-1)!.bear.toFixed(0)}</span>
                  </span>
                  <span>dotted = 78 issue gate</span>
                </div>
                <div className="flex gap-3">
                  <Spark values={scoreHist.data!.points.map((p) => p.bull)} max={100} color="#16c784" gate={78} />
                  <Spark values={scoreHist.data!.points.map((p) => p.bear)} max={100} color="#ea3943" gate={78} />
                </div>
              </div>
            )}
          </div>
        )}
      </div>
    );
  }

  // The premium the SL/targets were computed from — the base for the GTT %.
  const entryRef = signal.ref_entry_premium ?? signal.entry_high;
  const ce = signal.direction === "CE";
  const dirColor = ce ? "text-bull" : "text-bear";
  const dirBg = ce ? "bg-bull/15 text-bull" : "bg-bear/15 text-bear";
  const conf = signal.confidence;
  const confLabel = conf >= 80 ? "strong" : conf >= 70 ? "valid" : "wait";
  const border = ce ? "border-bull/40" : "border-bear/40";

  return (
    // Pinned shell: header, contract, key numbers and the action row never
    // scroll away — only the rationale (invalidation / reasons / score) does.
    // That guarantees entry zone, SL, targets and the Enter button are on
    // screen whatever the engine emits.
    <div className={`card flex flex-col overflow-hidden border ${border} ${className}`}>
      {/* header */}
      <div className="flex shrink-0 flex-wrap items-center gap-2 px-3 pt-3">
        <span className={`tag ${dirBg} text-xs`}>{ce ? "BUY CE" : "BUY PE"}</span>
        <span className="tag bg-panel2 text-muted">{signal.mode === "positional" ? "Positional" : "Intraday"}</span>
        <span className="text-sm font-semibold">{signal.title}</span>
        <div className="ml-auto flex items-center gap-3">
          <div className="text-right">
            <div className={`font-mono text-lg leading-none ${dirColor}`}>{conf.toFixed(0)}</div>
            <div className="text-[9px] uppercase text-muted">{confLabel}</div>
          </div>
          <div className="text-right">
            <div className="font-mono text-sm leading-none">{countdown ?? "—"}</div>
            <div className="text-[9px] uppercase text-muted">valid</div>
          </div>
        </div>
      </div>

      <div className="mt-2 flex shrink-0 items-center gap-2 px-3 text-sm">
        <span className="font-mono font-medium">{signal.contract}</span>
        {signal.expiry && <span className="text-[11px] text-muted">exp {signal.expiry}</span>}
        {signal.expiry === istToday() && (
          <span
            className="tag bg-yellow-500/15 text-yellow-400"
            title="This contract expires TODAY — gamma and theta are extreme; premiums move much faster than usual"
          >
            0DTE
          </span>
        )}
        {/* Pricing freshness + one-click re-price, both modes. Available from 2m
            (intraday cards only live 8m, so a 5m gate barely appeared). Amber —
            "worth refreshing" — when the price has drifted out of the entry
            zone, or when it is past the Kite hand-off's 15-min cutoff. */}
        {!expired &&
          (() => {
            const ltp = signal.live_premium;
            const outOfZone = ltp != null && (ltp < signal.entry_low || ltp > signal.entry_high);
            const attention = pricedAgoMin >= STALE_MIN || outOfZone;
            if (pricedAgoMin < 2 && !outOfZone) return null;
            const why = outOfZone
              ? "Price has drifted out of the entry zone — refresh to re-price and re-validate."
              : pricedAgoMin >= STALE_MIN
                ? `Priced ${pricedAgoMin}m ago — too old to place in Kite. Refresh to re-price against the current premium.`
                : `Priced ${pricedAgoMin}m ago`;
            return (
              <span className="ml-auto flex items-center gap-1.5">
                <span className={`text-[10px] ${attention ? "text-yellow-400" : "text-muted"}`} title={why}>
                  priced {pricedAgoMin}m ago
                </span>
                <button
                  onClick={reprice}
                  disabled={repricing}
                  title="Re-validate the score and re-price against the current premium — same strike, today's price. Closes the signal if it no longer qualifies."
                  className={`rounded border px-1.5 py-0.5 text-[10px] transition disabled:opacity-40 ${
                    attention
                      ? "border-yellow-500/60 bg-yellow-500/15 text-yellow-400 hover:bg-yellow-500/25"
                      : "border-edge bg-panel text-muted hover:text-white"
                  }`}
                >
                  {repricing ? "…" : "↻ Refresh"}
                </button>
              </span>
            );
          })()}
      </div>

      {/* LIVE price of this contract, against the entry zone — the "is it
          buyable at this price right now" read. Ticks with each 3s poll. */}
      {(() => {
        const ltp = signal.live_premium;
        if (ltp == null) {
          return (
            <div className="mt-1.5 px-3 text-[11px] text-muted">
              LTP <span className="font-mono">—</span> · no live tick for this strike
            </div>
          );
        }
        const inZone = ltp >= signal.entry_low && ltp <= signal.entry_high;
        const below = ltp < signal.entry_low;
        const zoneLabel = inZone ? "in entry zone" : below ? "below zone — cheaper than plan" : "above zone — chasing";
        const zoneTone = inZone ? "text-bull" : below ? "text-accent" : "text-yellow-400";
        const ref = signal.ref_entry_premium ?? signal.entry_high;
        const pct = ref ? ((ltp / ref - 1) * 100) : null;
        return (
          <div className="mt-1.5 flex items-center gap-2 px-3">
            <span className="text-[10px] uppercase tracking-wide text-muted">LTP</span>
            <span className={`font-mono text-lg leading-none ${zoneTone}`}>₹{fmt(ltp)}</span>
            {pct != null && (
              <span className="font-mono text-[11px] text-muted">
                {pct >= 0 ? "+" : "−"}{Math.abs(pct).toFixed(1)}% vs plan
              </span>
            )}
            <span className={`tag ml-auto ${
              inZone ? "bg-bull/15 text-bull" : below ? "bg-accent/15 text-accent" : "bg-yellow-500/15 text-yellow-400"
            }`}>
              {zoneLabel}
            </span>
          </div>
        );
      })()}

      {/* key numbers — pinned */}
      <div className="mt-2 grid shrink-0 grid-cols-3 gap-1.5 px-3">
        <Stat label="Entry zone" value={`₹${fmt(signal.entry_low)}–₹${fmt(signal.entry_high)}`} />
        <Stat
          // "planned" is load-bearing: no stop order is placed, so this is a
          // level you must act on, not a floor the market guarantees.
          label="Stop-loss (planned)"
          value={`₹${fmt(signal.premium_sl)}`}
          tone="text-bear"
          pct={pctFrom(signal.premium_sl, entryRef)}
        />
        <Stat label="Risk:Reward" value={`1:${signal.risk_reward}`} />
        {signal.quick_target && (
          <Stat
            // The level that turns a spike-and-fade into a scratch instead of a
            // loss: book half, stop to entry, let the rest run to T1/T2.
            label="Book ½ at"
            value={`₹${fmt(signal.quick_target)}`}
            tone="text-accent"
            pct={pctFrom(signal.quick_target, entryRef)}
          />
        )}
        <Stat
          label="Target 1"
          value={`₹${fmt(signal.target1)}`}
          tone="text-bull"
          pct={pctFrom(signal.target1, entryRef)}
        />
        <Stat
          label="Target 2"
          value={`₹${fmt(signal.target2)}`}
          tone="text-bull"
          pct={pctFrom(signal.target2, entryRef)}
        />
        {/* Strike is already in the contract line above ("NIFTY 24800 CE") —
            the tile is spent here on rupee outcomes instead. */}
      </div>

      {/* Rupees at stake, pinned. A risk warning that can scroll away is not a
          warning, so this sits in the shrink-0 shell with the action row. */}
      <div className="mt-2 shrink-0 px-3">
        <RiskVisualizer
          signal={signal}
          lots={Math.max(1, Math.floor(parseNum(lots) ?? 1))}
          onLots={(n) => setLots(String(n))}
          entryOverride={parseNum(entryPx)}
        />
        {/* Affordability line: what today's fund buys at the live premium.
            Clickable when the form has drifted from it, so getting back to the
            prefilled quantity is one tap, not mental division. */}
        {signal.fund_note && (
          <button
            type="button"
            onClick={() =>
              signal.fund_lots && signal.fund_lots > 0 && setLots(String(signal.fund_lots))
            }
            className="mt-1 block w-full truncate text-left font-mono text-[10px] text-muted hover:text-white"
            title="Fund-affordable size — click to use it as the lots"
          >
            ⛁ {signal.fund_note}
          </button>
        )}
      </div>

      {/* ---- everything below scrolls ---- */}
      <div className="mt-2 min-h-0 flex-1 overflow-y-auto scroll-thin px-3">
      {/* invalidation */}
      <div className="rounded-md border border-edge bg-panel2 px-3 py-2">
        <div className="text-[10px] uppercase tracking-wide text-muted">Underlying confirmation</div>
        <div className="text-sm">{signal.underlying_invalidation}</div>
        <div className="mt-0.5 text-[11px] text-muted">{signal.invalidation_note}</div>
        <div className="mt-1 text-[11px] text-muted">↳ {signal.trailing_sl_rule}</div>
      </div>

      {/* reasons */}
      {signal.reasons.length > 0 && (
        <ul className="mt-3 space-y-0.5">
          {signal.reasons.map((r, i) => (
            <li key={i} className="flex gap-1.5 text-[12px] text-white/80">
              <span className={dirColor}>•</span>
              <span>{r}</span>
            </li>
          ))}
        </ul>
      )}

      {/* score */}
      <div className="mt-3 border-t border-edge pt-3">
        <div className="mb-1.5 text-[10px] uppercase text-muted">Score breakdown · {conf.toFixed(0)}/100</div>
        <ScoreBars score={signal.score} history={scoreHist.data?.points ?? null} />
      </div>
      </div>
      {/* ---- end scroll region ---- */}

      {/* manual tracking — pinned to the bottom */}
      <div className="shrink-0 border-t border-edge px-3 py-2">
        {expired ? (
          <div className="text-xs text-muted">
            Entry window closed — wait for the next signal.
          </div>
        ) : !entering ? (
          <div className="flex items-center gap-2">
            <button
              onClick={() => setEntering(true)}
              className="rounded-md bg-bull px-3 py-1.5 text-xs font-medium text-black hover:opacity-90"
            >
              Mark as Entered
            </button>
            <button
              onClick={() => onIgnore(signal.id)}
              className="rounded-md bg-panel2 px-3 py-1.5 text-xs text-muted hover:text-white"
            >
              Ignore
            </button>
            <span className="text-[10px] text-muted">tracks your manual fill — no order is placed</span>
          </div>
        ) : (
          <div className="flex flex-wrap items-end gap-2">
            <label className="text-[10px] text-muted">
              Lots
              <input
                value={lots}
                onChange={(e) => setLots(e.target.value)}
                inputMode="numeric"
                className="mt-0.5 block w-16 rounded border border-edge bg-panel2 px-2 py-1 font-mono text-xs outline-none focus:border-accent"
              />
            </label>
            <label className="text-[10px] text-muted">
              Entry ₹
              {/* Deliberately NOT prefilled: an untouched field books your LIVE
                  premium; typing a price overrides it with your actual fill. */}
              <input
                value={entryPx}
                onChange={(e) => setEntryPx(e.target.value)}
                inputMode="decimal"
                placeholder="live fill"
                title={`Leave blank to record the live premium (zone ₹${fmt(signal.entry_low)}–₹${fmt(signal.entry_high)})`}
                className="mt-0.5 block w-24 rounded border border-edge bg-panel2 px-2 py-1 font-mono text-xs outline-none placeholder:text-muted/60 focus:border-accent"
              />
            </label>
            <button
              onClick={enter}
              disabled={busy}
              className="rounded-md bg-bull px-3 py-1.5 text-xs font-medium text-black disabled:opacity-40"
            >
              {busy ? "…" : "Confirm"}
            </button>
            {/* Hands the order to Kite pre-filled — you still confirm there. */}
            <button
              onClick={tradeInKite}
              disabled={busy}
              title="Open this order pre-filled in Kite — you review and confirm there. Tradewell never places orders."
              className="rounded-md border border-accent/60 bg-accent/15 px-3 py-1.5 text-xs font-medium text-accent disabled:opacity-40"
            >
              Trade in Kite ↗
            </button>
            <button onClick={() => setEntering(false)} className="rounded-md bg-panel2 px-3 py-1.5 text-xs text-muted">
              Cancel
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
