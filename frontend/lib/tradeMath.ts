/**
 * Rupee outcomes for a signal, computed BEFORE the trade is placed.
 *
 * Pure functions only — no React, no formatting. Kept separate from the
 * component so the arithmetic can be tested directly (`npm run test:math`).
 * Money shown to a trader is the one thing in this app that must not be
 * wrong-by-a-factor, so it does not live inline in JSX.
 *
 * THE ONE DOMAIN FACT THAT DRIVES ALL OF THIS: every Tradewell signal is a
 * BOUGHT option — long CE or long PE. A PE signal is still a BUY. So profit is
 * always "premium went up" and loss is always "premium went down", for both
 * directions. There is no sign flip between CE and PE, and adding one would be
 * a bug. What differs between CE and PE is which way the INDEX must move, and
 * that is already expressed in the option's own premium.
 *
 * Consequence: the true worst case is the premium going to ZERO — losing 100%
 * of what you paid — not the stop-loss level. Tradewell places no stop order,
 * so the stop is only a plan, honoured only if you act. Any visualiser that
 * presents the stop as "max loss" understates the real risk, which is why
 * `maxLoss` and `plannedLoss` are separate fields here.
 */

export interface EconomicsInput {
  /** Premium the plan was computed from (ref_entry_premium ?? entry_high). */
  entry: number;
  /** Premium stop-loss level. */
  stop: number;
  target1: number;
  target2: number;
  /** Contract multiplier (NIFTY = 75). Null/0 ⇒ no rupee figures at all. */
  lotSize: number | null;
  lots: number;
}

export interface Economics {
  qty: number;
  /** Capital deployed = entry × qty (gross premium debit). */
  cost: number;
  /**
   * Total loss if the option expires worthless — premium PLUS the entry costs
   * you paid anyway. Strictly greater than 100% of premium.
   */
  maxLoss: number;
  /** Loss if the stop is honoured, net of a round trip. Positive number. */
  plannedLoss: number;
  gainT1: number;
  gainT2: number;
  /** Net reward:risk actually achieved at this fill, after costs. */
  rrT1: number;
  rrT2: number;
  /** maxLoss / plannedLoss, e.g. 5.0×. */
  maxLossMultiple: number;
  /**
   * plannedLoss / maxLoss, as a percentage. THE headline honesty number: how
   * much of your real exposure the stop actually covers. At the default 18%
   * premium stop this is only ~20%.
   */
  coveragePct: number;
  /** Premium needed just to break even after charges. */
  breakeven: number;
  /** Round-trip charges as a % of gross risk. >10% ⇒ costs dominate. */
  costDragPct: number;
}

const round2 = (n: number) => Math.round(n * 100) / 100;

/**
 * Returns null when the inputs cannot produce a truthful rupee figure — a
 * missing lot size, a non-positive size, or an incoherent plan. Callers must
 * render nothing rather than fall back to a guessed multiplier: a confidently
 * wrong rupee amount is worse than no rupee amount.
 */
export function economics(i: EconomicsInput): Economics | null {
  const { entry, stop, target1, target2, lotSize, lots } = i;
  if (!lotSize || lotSize <= 0) return null;
  if (!Number.isFinite(lots) || lots <= 0 || !Number.isInteger(lots)) return null;
  if (!Number.isFinite(entry) || entry <= 0) return null;
  // A stop at or above entry, or at/below zero, means the plan is malformed.
  if (!Number.isFinite(stop) || stop <= 0 || stop >= entry) return null;

  const qty = lotSize * lots;
  const cost = round2(entry * qty);

  // Worst case: the option lapses worthless. One leg, no STT — but the entry
  // costs were still paid, so this exceeds the premium itself.
  const lapse = estimateCharges(entry, 0, qty, 1)!;
  const maxLoss = round2(cost + lapse.total);

  // Every other outcome is a round trip: entry cost + exit cost.
  const cStop = estimateCharges(entry, stop, qty, 2)!;
  const cT1 = estimateCharges(entry, target1, qty, 2)!;
  const cT2 = estimateCharges(entry, target2, qty, 2)!;

  const grossRisk = (entry - stop) * qty;
  const plannedLoss = round2(grossRisk + cStop.total);
  // Targets below entry would be nonsense for a long option; clamp to 0 rather
  // than rendering a negative "gain".
  const gainT1 = round2(Math.max(0, Math.max(0, target1 - entry) * qty - cT1.total));
  const gainT2 = round2(Math.max(0, Math.max(0, target2 - entry) * qty - cT2.total));

  return {
    qty,
    cost,
    maxLoss,
    plannedLoss,
    gainT1,
    gainT2,
    rrT1: plannedLoss > 0 ? round2(gainT1 / plannedLoss) : 0,
    rrT2: plannedLoss > 0 ? round2(gainT2 / plannedLoss) : 0,
    maxLossMultiple: plannedLoss > 0 ? round2(maxLoss / plannedLoss) : 0,
    coveragePct: maxLoss > 0 ? round2((plannedLoss / maxLoss) * 100) : 0,
    breakeven: breakevenPremium(entry, qty),
    costDragPct: grossRisk > 0 ? round2((cStop.total / grossRisk) * 100) : 0,
  };
}

/**
 * Positions of the plan's premium levels on a linear 0→max axis, as
 * percentages. The axis deliberately STARTS AT ZERO so the distance from entry
 * down to total loss is drawn to the same scale as the distance down to the
 * stop — that visual gap is the whole point of the chart.
 */
export function axisPositions(i: Pick<EconomicsInput, "entry" | "stop" | "target1" | "target2">) {
  const top = Math.max(i.target2, i.target1, i.entry);
  if (!Number.isFinite(top) || top <= 0) return null;
  const pos = (v: number) => Math.max(0, Math.min(100, (v / top) * 100));
  return {
    top,
    zero: 0,
    stop: pos(i.stop),
    entry: pos(i.entry),
    t1: pos(i.target1),
    t2: pos(i.target2),
  };
}

/**
 * Zerodha NSE F&O option-BUY charge schedule. RATES CHANGE — these are
 * point-in-time values, shown in the UI as an estimate and never folded
 * silently into a headline. Verify against your own contract note.
 *
 * GST applies to brokerage + exchange + SEBI + IPFT only — never to STT or
 * stamp duty. Stamp duty is buy-side only; STT is sell-side only.
 */
export const CHARGE_RATES = {
  brokeragePerOrder: 20,      // flat, per executed order
  sttSellPct: 0.001,          // 0.10% of SELL premium turnover (w.e.f. 01-Oct-2024)
  exchangeTxnPct: 0.0003503,  // NSE options, premium turnover, both sides
  ipftPct: 0.000005,          // NSE investor protection fund, ₹50/crore
  sebiPct: 0.000001,          // ₹10 per crore
  gstPct: 0.18,
  stampDutyBuyPct: 0.00003,   // 0.003%, buy side only
};

export interface Charges {
  total: number;
  /** Premium the position must reach just to cover costs — the real breakeven. */
  breakevenPremium: number;
}

/**
 * `legs` is 1 for the LAPSE path and 2 for a round trip.
 *
 * An option that expires worthless is never sold: there is no second order, so
 * no second brokerage and no STT (STT is levied on the sell side). Charging a
 * round trip there overstates the loss; charging a single leg on a real exit
 * understates it. That is why the path is an explicit argument rather than a
 * constant — the two must not drift apart.
 */
export function estimateCharges(
  entry: number,
  exitPremium: number,
  qty: number,
  legs: 1 | 2 = 2,
): Charges | null {
  if (!(qty > 0) || !(entry > 0) || !(exitPremium >= 0)) return null;
  const r = CHARGE_RATES;
  const buyTurnover = entry * qty;
  // A lapsed option is never sold, so it contributes no sell-side turnover.
  const sellTurnover = legs === 2 ? exitPremium * qty : 0;
  const turnover = buyTurnover + sellTurnover;

  const brokerage = r.brokeragePerOrder * legs;
  const stt = sellTurnover * r.sttSellPct;
  const exch = turnover * r.exchangeTxnPct;
  const ipft = turnover * r.ipftPct;
  const sebi = turnover * r.sebiPct;
  const gst = (brokerage + exch + ipft + sebi) * r.gstPct;
  const stamp = buyTurnover * r.stampDutyBuyPct;

  return {
    total: round2(brokerage + stt + exch + ipft + sebi + gst + stamp),
    breakevenPremium: round2(breakevenPremium(entry, qty)),
  };
}

/**
 * Closed-form breakeven: the premium P at which (P−E)·qty exactly equals the
 * round-trip charges on a buy at E and a sell at P. Solving for P rather than
 * iterating keeps it exact.
 */
export function breakevenPremium(entry: number, qty: number): number {
  const r = CHARGE_RATES;
  const varBoth = r.exchangeTxnPct + r.ipftPct + r.sebiPct;      // both sides
  const buyRate = 1 + varBoth * (1 + r.gstPct) + r.stampDutyBuyPct;
  const sellRate = 1 - varBoth * (1 + r.gstPct) - r.sttSellPct;
  const fixed = (r.brokeragePerOrder * 2 * (1 + r.gstPct)) / qty;
  return round2((entry * buyRate + fixed) / sellRate);
}
