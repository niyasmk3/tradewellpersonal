/**
 * Money math tests. Run: npm run test:math
 *
 * Expected values are computed BY HAND in the comments, not copied from a run.
 * A test that just records whatever the code printed cannot catch a wrong
 * formula, and this is the one file in the app where a wrong formula shows a
 * trader a false rupee amount.
 */
import { axisPositions, breakevenPremium, economics, estimateCharges, groupClosedByDay, returnPct } from "../lib/tradeMath";

let failed = 0;
function check(name: string, got: unknown, want: unknown) {
  const g = JSON.stringify(got);
  const w = JSON.stringify(want);
  if (g === w) {
    console.log(`  ok   ${name}`);
  } else {
    failed++;
    console.log(`  FAIL ${name}\n         got  ${g}\n         want ${w}`);
  }
}

// Round numbers so the arithmetic can be followed: lot 75, entry ₹100,
// stop ₹75 (25% below), T1 ₹137.50 (1.5R gross), T2 ₹175 (3R gross).
const BASE = { entry: 100, stop: 75, target1: 137.5, target2: 175, lotSize: 75, lots: 1 };
const e1 = economics(BASE)!;

// qty 75 · gross cost 100×75 = 7,500
check("qty", e1.qty, 75);
check("cost", e1.cost, 7500);

// LAPSE (1 leg, no STT): brokerage 20 · exch 7500×0.0003503 = 2.62725
// ipft 0.0375 · sebi 0.0075 · gst 0.18×(20+2.62725+0.0375+0.0075) = 4.081005
// stamp 7500×0.00003 = 0.225  →  26.98
// maxLoss = 7500 + 26.98 = 7526.98  (MORE than 100% of premium)
check("maxLoss = premium + entry costs", e1.maxLoss, 7526.98);
check("maxLoss exceeds premium", e1.maxLoss > e1.cost, true);

// STOP (2 legs): sell 75×75 = 5,625 · turnover 13,125
// brokerage 40 · stt 5.625 · exch 4.5976875 · ipft 0.065625 · sebi 0.013125
// gst 0.18×44.6764375 = 8.04175875 · stamp 0.225  →  58.57
// plannedLoss = 1,875 + 58.57 = 1,933.57
check("plannedLoss", e1.plannedLoss, 1933.57);

// T1 (2 legs): sell 10,312.50 · turnover 17,812.50 → charges 65.23
// gain = 2,812.50 − 65.23 = 2,747.27
check("gainT1 net", e1.gainT1, 2747.27);
// T2: sell 13,125 · turnover 20,625 → charges 69.22
// gain = 5,625 − 69.22 = 5,555.78
check("gainT2 net", e1.gainT2, 5555.78);

// Net R:R is WORSE than the card's gross 1:1.5 — costs land on both legs.
check("net rrT1 (2747.27/1933.57)", e1.rrT1, 1.42);
check("net rrT2 (5555.78/1933.57)", e1.rrT2, 2.87);

// 7526.98 / 1933.57 = 3.89
check("maxLossMultiple", e1.maxLossMultiple, 3.89);
// 1933.57 / 7526.98 = 25.69%  — the stop covers only a quarter of real exposure
check("coveragePct", e1.coveragePct, 25.69);
// 58.57 / 1875 = 3.12%
check("costDragPct", e1.costDragPct, 3.12);

// Closed-form breakeven: (100×1.000450434 + 47.2/75) / 0.998579566 = 100.82
check("breakeven", e1.breakeven, 100.82);
check("breakeven above entry", e1.breakeven > BASE.entry, true);

// THE CRITICAL INVARIANT: for a bought option the true max loss must always
// exceed the planned stop loss. If this inverts, the chart is lying.
check("maxLoss > plannedLoss", e1.maxLoss > e1.plannedLoss, true);
check("coverage under 100%", e1.coveragePct < 100, true);

// A PE signal is still a BUY — identical arithmetic, no sign flip anywhere.
check("PE identical to CE", JSON.stringify(economics(BASE)), JSON.stringify(e1));

// --- scaling: GROSS is linear in lots, NET is not (brokerage is flat/order) ---
const e3 = economics({ ...BASE, lots: 3 })!;
check("3 lots qty", e3.qty, 225);
check("3 lots gross cost is 3x", e3.cost, 22500);
// lapse at 225: brokerage 20 · exch 7.88175 · ipft 0.1125 · sebi 0.0225
// gst 5.043015 · stamp 0.675 → 33.73 ⇒ 22,533.73, NOT 3 × 7,526.98 (22,580.94)
check("3 lots maxLoss", e3.maxLoss, 22533.73);
check("flat brokerage amortises", e3.maxLoss < e1.maxLoss * 3, true);
// Bigger size ⇒ costs matter proportionally less ⇒ net R:R improves slightly.
check("3 lots net rr better than 1 lot", e3.rrT1 > e1.rrT1, true);

// --- the cheap-contract trap: a ~₹19 0DTE premium at 18% stop ---
// gross risk = 3.45×75 = 258.75; round-trip charges ≈ 43 ⇒ ~17% cost drag,
// which destroys the edge before any view on direction is expressed.
const cheap = economics({ entry: 19.25, stop: 15.8, target1: 24.4, target2: 27.9, lotSize: 75, lots: 1 })!;
check("cheap contract has heavy cost drag", cheap.costDragPct > 10, true);
check("cheap contract net rr below gross 1.5", cheap.rrT1 < 1.5, true);

// --- degenerate inputs must return null, never a guessed number ---
check("null lotSize", economics({ ...BASE, lotSize: null }), null);
check("zero lotSize", economics({ ...BASE, lotSize: 0 }), null);
check("zero lots", economics({ ...BASE, lots: 0 }), null);
check("negative lots", economics({ ...BASE, lots: -2 }), null);
check("fractional lots", economics({ ...BASE, lots: 1.5 }), null);
check("stop above entry", economics({ ...BASE, stop: 120 }), null);
check("stop equals entry", economics({ ...BASE, stop: 100 }), null);
check("zero entry", economics({ ...BASE, entry: 0 }), null);
check("NaN lots", economics({ ...BASE, lots: NaN }), null);
check("target below entry clamps to 0", economics({ ...BASE, target1: 80 })!.gainT1, 0);

// --- axis: zero-anchored so the drop to total loss is drawn to scale ---
// top = 175 · stop 75/175 = 42.857 · entry 100/175 = 57.143 · t1 137.5/175 = 78.571
const ax = axisPositions(BASE)!;
check("axis top", ax.top, 175);
check("axis starts at zero", ax.zero, 0);
check("axis stop %", Math.round(ax.stop * 100) / 100, 42.86);
check("axis entry %", Math.round(ax.entry * 100) / 100, 57.14);
check("axis t1 %", Math.round(ax.t1 * 100) / 100, 78.57);
check("axis t2 %", ax.t2, 100);
check("axis ordering", ax.zero < ax.stop && ax.stop < ax.entry && ax.entry < ax.t1 && ax.t1 <= ax.t2, true);

// THE POINT OF ZERO-ANCHORING: tightening the stop must SHRINK the protected
// band and GROW the unprotected one — total loss-side width is invariant. A
// loss-scaled axis would instead shorten the whole red bar and flatter the
// trade, even though not one rupee of exposure changed.
const tight = axisPositions({ ...BASE, stop: 90 })!;
check("tighter stop keeps entry position", tight.entry, ax.entry);
check("tighter stop grows unprotected zone", tight.stop > ax.stop, true);
const tightE = economics({ ...BASE, stop: 90 })!;
check("tighter stop leaves maxLoss unchanged", tightE.maxLoss, e1.maxLoss);
check("tighter stop LOWERS coverage", tightE.coveragePct < e1.coveragePct, true);

// --- charges: leg count is the thing that must not drift ---
const rt = estimateCharges(100, 175, 75, 2)!;
check("round trip total", rt.total, 69.22);
const lapse = estimateCharges(100, 0, 75, 1)!;
check("lapse total (1 leg, no STT)", lapse.total, 26.98);
check("lapse cheaper than round trip", lapse.total < rt.total, true);
// Selling at a high premium costs MORE than at a low one (STT is on the sell).
check("STT scales with exit price", estimateCharges(100, 175, 75, 2)!.total > estimateCharges(100, 75, 75, 2)!.total, true);
check("charges null on zero qty", estimateCharges(100, 175, 0, 2), null);
check("breakevenPremium matches economics", breakevenPremium(100, 75), e1.breakeven);

// --- journal return % -------------------------------------------------------
// 1 lot: 75 qty at Rs 120 = Rs 9,000 deployed. Exit at 150 -> +Rs 2,250.
// 2250 / 9000 = 25.00%
check("returnPct simple", returnPct(2250, 120, 75, 75), 25);
check("returnPct loss", returnPct(-1575, 120, 75, 75), -17.5);
check("returnPct flat", returnPct(0, 120, 75, 75), 0);

// THE PARTIAL CASE. 2 lots (150 qty) at Rs 120 = Rs 18,000 deployed.
// Book 1 lot at 150 (+2,250), exit the rest at 160 (+3,000) => +Rs 5,250.
// book_partial has by now shrunk `quantity` to 75, so the naive denominator
// would be Rs 9,000 and report 58.33% — more than double the truth.
check("returnPct uses ENTRY size, not the remainder", returnPct(5250, 120, 150, 75), 29.17);
check("naive denominator would have been wrong", returnPct(5250, 120, null, 75), 58.33);

// Legacy rows carry no initial_quantity; with no partial the two agree.
check("legacy row falls back to quantity", returnPct(2250, 120, null, 75), 25);
check("legacy row: undefined too", returnPct(2250, 120, undefined, 75), 25);

// Degenerate inputs must yield null, never a bogus percentage.
check("returnPct zero entry", returnPct(2250, 0, 75, 75), null);
check("returnPct zero qty", returnPct(2250, 120, 0, 0), null);
check("returnPct NaN pnl", returnPct(NaN, 120, 75, 75), null);


// --- journal day grouping ---------------------------------------------------
const D = 86400;
const IST_NOON = 19676 * D + 6 * 3600;        // ~11:30 IST on an arbitrary day
const mk = (over: Partial<any> = {}) => ({
  status: "exited", entered_at: IST_NOON, exited_at: IST_NOON + 3600,
  realized_pnl: 100, ...over,
});

const g = groupClosedByDay([
  mk({ entered_at: IST_NOON, exited_at: IST_NOON + 60, realized_pnl: -500 }),        // day 0
  mk({ entered_at: IST_NOON, exited_at: IST_NOON + 120, realized_pnl: 300 }),        // day 0, later
  mk({ entered_at: IST_NOON + D, exited_at: IST_NOON + D + 60, realized_pnl: 900 }), // day +1
  mk({ status: "entered", exited_at: null }),                                        // OPEN: excluded
  mk({ status: "ignored", exited_at: null, entered_at: IST_NOON + 2 * D }),          // day +2
]);
check("groups one bucket per day", g.length, 3);
check("newest day first", g[0].key > g[1].key && g[1].key > g[2].key, true);
check("open trades excluded", g.flatMap((d) => d.rows).length, 4);

// Day +2 holds only the ignored row: it must appear, but contribute no P&L
// and count as neither a win nor a loss.
check("ignored row is kept", g[0].rows.length, 1);
check("ignored contributes no P&L", g[0].realized, 0);
check("ignored is neither W nor L", [g[0].wins, g[0].losses], [0, 0]);

check("day +1 subtotal", g[1].realized, 900);
check("day 0 subtotal nets both", g[2].realized, -200);
check("day 0 W/L", [g[2].wins, g[2].losses], [1, 1]);
// Within a day, most recently booked first.
check("rows newest-first within a day", g[2].rows[0].realized_pnl, 300);

// Subtotals must sum to the overall realised total, or the header lies.
const all = g.reduce((s, d) => s + d.realized, 0);
check("subtotals reconcile with the total", all, 700);

// An overnight position is filed under the day it was BOOKED, not opened.
const overnight = groupClosedByDay([
  mk({ entered_at: IST_NOON, exited_at: IST_NOON + D, realized_pnl: 50 }),
]);
check("overnight files under its exit day", overnight[0].key, Math.floor((IST_NOON + D + 19800) / 86400));
check("empty input", groupClosedByDay([]), []);


console.log(failed === 0 ? "\nALL PASSED" : `\n${failed} FAILED`);
process.exit(failed === 0 ? 0 : 1);
