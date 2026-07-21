"use client";

import { useMemo, useState } from "react";
import { SignalCard } from "@/lib/api";
import { axisPositions, economics } from "@/lib/tradeMath";
import { fmt, istToday } from "@/lib/format";

/**
 * What this trade can cost you and make you, in rupees, before you place it.
 *
 * THE AXIS IS ANCHORED AT ₹0, not at the stop-loss. That choice is the whole
 * design. A bar scaled to the stop shrinks when you tighten the stop, which
 * flatters the trade even though the money at risk has not changed by a rupee
 * — and on an expiry-day contract a tighter stop is *more* likely to be gapped
 * through, not less. Anchoring at zero makes the loss side depend only on the
 * premium you paid, so tightening the stop visibly moves width OUT of the
 * "planned" band and INTO the "no stop order exists" band, which is what
 * actually happens.
 *
 * The bar is a rupee scale, never a probability. Nothing here says how likely
 * any outcome is, because nothing in this app knows that.
 */

const inr = (n: number) =>
  `₹${Math.abs(n).toLocaleString("en-IN", { maximumFractionDigits: 0 })}`;

/** Diagonal stripes = "Tradewell places no order at this boundary." */
const HATCH =
  "repeating-linear-gradient(45deg, rgba(234,57,67,0.55) 0 4px, rgba(234,57,67,0.16) 4px 8px)";

export function RiskVisualizer({
  signal,
  lots,
  onLots,
  entryOverride,
  className = "",
}: {
  signal: SignalCard;
  lots: number;
  onLots?: (n: number) => void;
  /** A fill the user typed, if any — beats the plan's reference price. */
  entryOverride?: number | null;
  className?: string;
}) {
  const [showCosts, setShowCosts] = useState(false);

  // Price off entry_high, the TOP of the zone: that is literally the LIMIT
  // price the "Trade in Kite" basket sends, so it is the expected fill, not a
  // pessimistic edge case. A typed fill overrides it.
  const basis = entryOverride && entryOverride > 0 ? entryOverride : signal.entry_high;
  const basisLabel =
    entryOverride && entryOverride > 0 ? "your fill" : "top of entry zone";

  // The stop that ACTUALLY ends the trade. With the index invalidation primary,
  // that is the disaster backstop, not premium_sl — and it is ~2.5x wider. A
  // panel that kept using premium_sl would understate the planned loss by that
  // factor (Rs 101 vs Rs 757 on the 21-Jul card), which is the one thing this
  // block exists to prevent.
  const operativeStop = signal.disaster_sl ?? signal.premium_sl;

  const eco = useMemo(
    () =>
      economics({
        entry: basis,
        stop: operativeStop,
        target1: signal.target1,
        target2: signal.target2,
        lotSize: signal.lot_size,
        lots,
      }),
    [basis, operativeStop, signal.target1, signal.target2, signal.lot_size, lots],
  );

  const ax = useMemo(
    () =>
      axisPositions({
        entry: basis,
        stop: operativeStop,
        target1: signal.target1,
        target2: signal.target2,
      }),
    [basis, operativeStop, signal.target1, signal.target2],
  );

  // No lot size ⇒ no rupee figure. Guessing a multiplier here would be worse
  // than showing nothing: every number below would be wrong by a whole factor.
  if (!eco || !ax) {
    return (
      <div className={`rounded-md border border-edge bg-panel2 px-3 py-2 ${className}`}>
        <div className="text-[10px] uppercase tracking-wide text-muted">Max loss / gain</div>
        <div className="mt-0.5 text-[11px] text-muted">
          {signal.lot_size
            ? "Enter a whole number of lots to see rupee amounts."
            : "Lot size unavailable — rupee amounts need a live instrument load."}
        </div>
      </div>
    );
  }

  const zeroDte = signal.expiry === istToday();
  const overPlan = signal.suggested_lots != null && signal.suggested_lots > 0 && lots > signal.suggested_lots;
  // Costs eating >10% of the risk leg means the contract is too cheap to trade
  // profitably regardless of direction.
  const costHeavy = eco.costDragPct > 10;
  const capital = signal.trading_capital;
  const pctOfCapital = capital && capital > 0 ? (eco.maxLoss / capital) * 100 : null;

  const seg = (from: number, to: number) => ({
    left: `${from}%`,
    width: `${Math.max(0, to - from)}%`,
  });

  return (
    <div className={`rounded-md border border-edge bg-panel2 px-2.5 py-2 ${className}`}>
      {/* header — size lives here so the bar is live before the entry form opens */}
      <div className="flex items-center gap-2">
        <span className="text-[10px] uppercase tracking-wide text-muted">Max loss / gain</span>
        {onLots && (
          <span className="flex items-center gap-1">
            <button
              onClick={() => onLots(Math.max(1, lots - 1))}
              className="h-4 w-4 rounded bg-panel text-[11px] leading-none text-muted hover:text-white"
              aria-label="one lot fewer"
            >
              −
            </button>
            <span className="font-mono text-[11px] text-white">{lots}L</span>
            <button
              onClick={() => onLots(lots + 1)}
              className="h-4 w-4 rounded bg-panel text-[11px] leading-none text-muted hover:text-white"
              aria-label="one lot more"
            >
              +
            </button>
          </span>
        )}
        <span
          className="ml-auto truncate font-mono text-[9px] text-muted"
          title={`Priced at ₹${fmt(basis)} — ${basisLabel}. ${eco.qty} qty = ${lots} × ${signal.lot_size}.`}
        >
          @₹{fmt(basis)} · {eco.qty} qty
        </span>
        {overPlan && (
          <span
            className="tag bg-yellow-500/15 text-[9px] text-yellow-400"
            title={`Your risk settings imply ${signal.suggested_lots} lot(s)`}
          >
            over plan
          </span>
        )}
      </div>

      {/* the bar — one linear premium axis, ₹0 on the left, Target 2 on the right */}
      <div
        className="relative mt-1.5 h-4 w-full overflow-hidden rounded-sm bg-panel"
        title="Scale is rupees, not probability. Nothing here says how likely an outcome is."
      >
        {/* ₹0 → stop: no stop order exists here. Hatched, and the widest band. */}
        <div className="absolute inset-y-0" style={{ ...seg(0, ax.stop), background: HATCH }} />
        {/* stop → entry: the loss you planned for */}
        <div
          className="absolute inset-y-0 bg-bear/30"
          style={seg(ax.stop, ax.entry)}
        />
        {/* entry → breakeven: pure costs */}
        <div
          className="absolute inset-y-0 bg-yellow-500/40"
          style={seg(ax.entry, Math.min(ax.t1, (eco.breakeven / ax.top) * 100))}
        />
        {/* breakeven → T1 → T2 */}
        <div
          className="absolute inset-y-0 bg-bull/25"
          style={seg((eco.breakeven / ax.top) * 100, ax.t1)}
        />
        <div className="absolute inset-y-0 bg-bull/45" style={seg(ax.t1, ax.t2)} />

        {/* markers */}
        <div className="absolute inset-y-0 w-0.5 bg-white" style={{ left: `${ax.entry}%` }} />
        <div className="absolute inset-y-0 w-px bg-bear" style={{ left: `${ax.stop}%` }} />
      </div>

      {/* tick strip */}
      <div className="relative mt-0.5 h-3 w-full font-mono text-[9px] text-muted">
        <span className="absolute left-0">₹0</span>
        <span className="absolute -translate-x-1/2 text-bear" style={{ left: `${ax.stop}%` }}>
          {fmt(operativeStop)}
        </span>
        <span className="absolute -translate-x-1/2 text-white" style={{ left: `${ax.entry}%` }}>
          {fmt(basis)}
        </span>
        <span className="absolute right-0 text-bull">{fmt(signal.target2)}</span>
      </div>

      {/* the headline: the number every naive version of this chart omits */}
      <div className="mt-1.5 flex items-baseline gap-2 border-l-2 border-bear bg-bear/10 px-2 py-1">
        <span className="text-[10px] uppercase tracking-wide text-bear">If it goes to ₹0</span>
        <span className="ml-auto font-mono text-base font-semibold text-bear">
          −{inr(eco.maxLoss)}
        </span>
      </div>
      <div className="mt-0.5 px-0.5 text-[10px] text-muted">
        100% of premium — no stop order is placed.{" "}
        <span className="text-yellow-400">
          Your ₹{fmt(operativeStop)} stop covers only {Math.round(eco.coveragePct)}%
        </span>{" "}
        of that (−{inr(eco.plannedLoss)}).
        {pctOfCapital != null && ` That is ${pctOfCapital.toFixed(1)}% of capital.`}
      </div>

      {/* upside, deliberately the smallest type in the block */}
      <div className="mt-1 flex flex-wrap items-center gap-x-2 font-mono text-[10px]">
        <span className="text-bull">T1 +{inr(eco.gainT1)}</span>
        <span className="text-bull/80">T2 +{inr(eco.gainT2)}</span>
        <span className="text-muted">· net R:R 1:{eco.rrT1.toFixed(2)}</span>
        <button
          onClick={() => setShowCosts((v) => !v)}
          className="ml-auto text-muted underline decoration-dotted hover:text-white"
          title="Estimated Zerodha charges — verify against your contract note"
        >
          {showCosts ? "hide" : "costs"}
        </button>
      </div>

      {showCosts && (
        <div className="mt-1 rounded bg-panel px-2 py-1 font-mono text-[9px] leading-relaxed text-muted">
          breakeven ₹{fmt(eco.breakeven)} (premium must rise {((eco.breakeven / basis - 1) * 100).toFixed(2)}% just to
          cover charges) · charges are {eco.costDragPct.toFixed(1)}% of your risk leg · est. only, rates change
        </div>
      )}

      {costHeavy && (
        <div className="mt-1 rounded bg-bear/10 px-2 py-1 text-[10px] text-bear">
          ⚠ Charges are {eco.costDragPct.toFixed(0)}% of the risk on this contract — the premium is
          too small to clear costs. Net R:R is 1:{eco.rrT1.toFixed(2)}, not 1:{signal.risk_reward}.
        </div>
      )}

      {zeroDte && (
        <div className="mt-1 rounded bg-yellow-500/10 px-2 py-1 text-[10px] text-yellow-400">
          ⏱ Expires today — decay can reach ₹0 within hours and the stop may never fill. If left to
          expire in-the-money, exercise STT is charged on intrinsic value, not premium.
        </div>
      )}
    </div>
  );
}
