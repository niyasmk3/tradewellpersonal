"use client";

import { PaperSummary } from "@/lib/api";
import { istDateTime, istDayKey, istTime, signed } from "@/lib/format";

/**
 * Forward-test results: every issued signal taken in simulation and exited by
 * the plan, net of slippage and the full charge schedule.
 *
 * This is the only evidence in the product that is not a backtest. Backtests
 * here grade the underlying future and exclude theta, IV and premium spreads;
 * these are real option premiums at real signal times, with no look-ahead. The
 * numbers are therefore worth more per trade — and there will be very few of
 * them, so the panel leads with the count rather than the P&L.
 */
export function PaperPanel({ data, error }: { data: PaperSummary | null; error?: string | null }) {
  if (error) {
    return (
      <div className="p-3 text-xs text-muted">
        {error.includes("off") ? (
          <>
            Paper trading is off. Set <span className="font-mono text-white">PAPER_TRADING=true</span>{" "}
            in <span className="font-mono">backend/.env</span> and restart the backend.
          </>
        ) : (
          error
        )}
      </div>
    );
  }
  if (!data) return <div className="p-3 text-xs text-muted">Loading…</div>;

  const net = data.net_pnl;
  const tone = net >= 0 ? "text-bull" : "text-bear";
  // Below ~30 closed trades nothing here is statistically meaningful, and
  // saying so on the panel is cheaper than saying it after a decision.
  const thin = data.trades < 30;

  return (
    <div className="p-2 text-[11px]">
      <div className="mb-2 flex items-center gap-2 rounded border border-edge bg-panel2 px-2 py-1.5">
        <span className="tag bg-accent/15 text-accent">SIMULATED</span>
        <span className="text-muted">no orders placed</span>
        <span className="ml-auto font-mono text-muted">
          {data.lots} lot · {(data.slippage_pct * 100).toFixed(2)}% slip
        </span>
      </div>

      <div className="grid grid-cols-3 gap-1.5">
        <Stat label="Closed" value={String(data.trades)} sub={data.open ? `${data.open} open` : undefined} />
        <Stat label="Win rate" value={`${data.win_rate}%`} sub={`${data.wins}W / ${data.losses}L`} />
        <Stat label="Net P&L" value={`₹${signed(net, 0)}`} tone={tone} sub={`after ₹${Math.round(data.charges)} charges`} />
        <Stat label="Expectancy" value={`₹${signed(data.expectancy, 0)}`} tone={data.expectancy >= 0 ? "text-bull" : "text-bear"} sub="per trade" />
        <Stat label="Avg win" value={`₹${signed(data.avg_win, 0)}`} tone="text-bull" />
        <Stat label="Avg loss" value={`₹${signed(data.avg_loss, 0)}`} tone="text-bear" />
      </div>

      {/* The split that decides gates: which MODE earns and which bleeds.
          (29-Jul: book +2.3k overall while intraday/scalp ran negative and
          positional carried everything — this line makes that visible.) */}
      {data.by_mode && Object.keys(data.by_mode).length > 0 && (
        <div className="mt-2 flex flex-wrap gap-1.5 text-[10px]">
          {Object.entries(data.by_mode).map(([m, s]) => (
            <span
              key={m}
              className={`tag ${s.expectancy > 0 ? "bg-bull/15 text-bull" : "bg-bear/15 text-bear"}`}
              title={`${m}: ${s.trades} closed, net ₹${s.net_pnl.toLocaleString("en-IN")}, ${s.win_rate}% wins — expectancy per trade net of charges`}
            >
              {m} ₹{signed(s.expectancy, 0)}/trade ({s.trades})
            </span>
          ))}
        </div>
      )}

      {/* The overnight-hold ledger: positional rows that held through the
          close. 29-Jul's lesson — the gap is the one move no intraday exit
          can touch; this line shows whether being on the bus when it leaves
          actually pays, and whether the deliberate evening entries pay more. */}
      {data.overnight && (
        <div className="mt-2 flex flex-wrap items-center gap-1.5 text-[10px]">
          <span className="text-muted">overnight:</span>
          <span
            className={`tag ${data.overnight.expectancy > 0 ? "bg-bull/15 text-bull" : "bg-bear/15 text-bear"}`}
            title={data.overnight.rows
              .map(
                (r) =>
                  `${r.contract} ${istDayKey(r.entered_at)}→${istDayKey(r.exited_at)}` +
                  `${r.score != null ? ` score ${r.score}` : ""}` +
                  `${r.overnight_move_pct != null ? ` gap ${signed(r.overnight_move_pct, 1)}%` : ""}` +
                  ` net ₹${signed(r.net_pnl, 0)}${r.evening ? " (evening)" : ""}`,
              )
              .join("\n")}
          >
            held ₹{signed(data.overnight.expectancy, 0)}/trade ({data.overnight.trades},{" "}
            {data.overnight.win_rate}% wins)
          </span>
          {data.overnight.evening && (
            <span
              className={`tag ${data.overnight.evening.expectancy > 0 ? "bg-bull/15 text-bull" : "bg-bear/15 text-bear"}`}
              title="Entries at/after 14:30 IST — the deliberate hold-into-the-close gap bets"
            >
              evening ₹{signed(data.overnight.evening.expectancy, 0)}/trade (
              {data.overnight.evening.trades})
            </span>
          )}
        </div>
      )}

      {Object.keys(data.by_reason).length > 0 && (
        <div className="mt-2 flex flex-wrap gap-1.5 text-[10px] text-muted">
          <span>exits:</span>
          {Object.entries(data.by_reason).map(([r, n]) => (
            <span key={r} className="tag bg-panel2">
              {r} {n}
            </span>
          ))}
        </div>
      )}

      {thin && data.trades > 0 && (
        <div className="mt-2 rounded bg-yellow-500/10 px-2 py-1 text-[10px] text-yellow-400">
          {data.trades} trade{data.trades === 1 ? "" : "s"} — far too few to conclude anything. A
          run of wins or losses this short is what luck looks like; ~30+ before reading into it.
        </div>
      )}

      {/* The participation floor's own scoreboard: what the vetoed cards would
          have made. Negative = the floor is earning its keep. */}
      {data.hollow && (
        <div className="mt-2 rounded bg-purple-500/10 px-2 py-1 text-[10px] text-purple-300">
          Vetoed-card counterfactual: {data.hollow.trades} closed fill
          {data.hollow.trades === 1 ? "" : "s"}
          {data.hollow.open ? ` (+${data.hollow.open} open)` : ""}, net ₹
          {signed(data.hollow.net_pnl, 0)} (₹{signed(data.hollow.expectancy, 0)}/trade,{" "}
          {data.hollow.win_rate}% wins).{" "}
          {data.hollow.trades === 0
            ? "No closed fills yet — verdict pending."
            : data.hollow.expectancy < 0
              ? "The volume/OI floor is earning its keep."
              : "If this stays positive over 30+ fills, lower the floors."}
        </div>
      )}

      {/* The 14:15-cutoff hypothesis: live n=7 said late cards always lose;
          the 43-session replay said hour-15 is the best hour (theta-blind).
          These fills pay real theta — they settle it. */}
      {data.late_shadow && (
        <div className="mt-2 rounded bg-orange-500/10 px-2 py-1 text-[10px] text-orange-300">
          Late-window counterfactual (14:15–15:10 vetoed cards): {data.late_shadow.trades} closed
          {data.late_shadow.open ? ` (+${data.late_shadow.open} open)` : ""}, net ₹
          {signed(data.late_shadow.net_pnl, 0)} (₹{signed(data.late_shadow.expectancy, 0)}/trade,{" "}
          {data.late_shadow.win_rate}% wins).{" "}
          {data.late_shadow.trades < 30
            ? `Verdict at 30+ fills — ${30 - data.late_shadow.trades} to go.`
            : data.late_shadow.expectancy > 0
              ? "Positive at sample size: the cutoff is costing money — consider retiring it."
              : "Negative at sample size: the cutoff is earning its keep."}
        </div>
      )}

      {/* The re-fire guard's scoreboard: what the cards it refused would have
          made. Born from n=2 losing re-fires; blocked a winner 03-Aug. */}
      {data.refire_shadow && (
        <div className="mt-2 rounded bg-rose-500/10 px-2 py-1 text-[10px] text-rose-300">
          Re-fire-guard counterfactual: {data.refire_shadow.trades} closed
          {data.refire_shadow.open ? ` (+${data.refire_shadow.open} open)` : ""}, net ₹
          {signed(data.refire_shadow.net_pnl, 0)} (₹{signed(data.refire_shadow.expectancy, 0)}/trade,{" "}
          {data.refire_shadow.win_rate}% wins).{" "}
          {data.refire_shadow.trades < 30
            ? `Verdict at 30+ fills — ${30 - data.refire_shadow.trades} to go.`
            : data.refire_shadow.expectancy > 0
              ? "Positive at sample size: the guard is costing money — shorten or retire it."
              : "Negative at sample size: the guard is earning its keep."}
        </div>
      )}

      {/* Exit-policy A/B: the same recorded fills replayed under "bank the
          whole 1-lot position at the quick target" vs the live ratchet. */}
      {data.exit_ab && (
        <div className="mt-2 rounded bg-sky-500/10 px-2 py-1 text-[10px] text-sky-300">
          {data.exit_ab.policy_live === "quick_bank" ? (
            <>Exit policy: banking at the quick target is LIVE. {data.exit_ab.note}</>
          ) : (
            <>
              Exit A/B (1-lot, {data.exit_ab.n} fills, {data.exit_ab.n_diverged} diverged):
              ratchet ₹{signed(data.exit_ab.ratchet?.expectancy ?? 0, 0)}/trade vs
              bank-at-QT ₹{signed(data.exit_ab.quick_bank?.expectancy ?? 0, 0)}/trade
              (Δ ₹{signed(data.exit_ab.delta_net ?? 0, 0)} total). {data.exit_ab.verdict}
            </>
          )}
        </div>
      )}

      {/* Stop-basis A/B: every clean fill's twin runs the OTHER stop basis
          (premium stop vs underlying invalidation + disaster backstop).
          STOP_PRIMARY was flipped twice on n=1 — this settles it at 30. */}
      {data.stop_ab && (
        <div className="mt-2 rounded bg-teal-500/10 px-2 py-1 text-[10px] text-teal-300">
          Stop-basis A/B ({data.stop_ab.n} pair{data.stop_ab.n === 1 ? "" : "s"},{" "}
          {data.stop_ab.n_diverged} diverged
          {data.stop_ab.pending ? `, ${data.stop_ab.pending} still open` : ""}):
          premium stop ₹{signed(data.stop_ab.premium_stop?.expectancy ?? 0, 0)}/trade vs
          underlying stop ₹{signed(data.stop_ab.underlying_stop?.expectancy ?? 0, 0)}/trade
          (Δ ₹{signed(data.stop_ab.delta_net ?? 0, 0)} total). {data.stop_ab.verdict}
        </div>
      )}

      <div className="mt-2 space-y-1">
        {data.rows
          .slice()
          .sort((a, b) => (b.exited_at ?? 0) - (a.exited_at ?? 0))
          .map((r) => (
            <div key={r.id} className="rounded border border-edge/60 bg-panel2 px-2 py-1">
              <div className="flex items-center gap-2">
                <span className={`tag ${r.direction === "CE" ? "bg-bull/15 text-bull" : "bg-bear/15 text-bear"}`}>
                  {r.direction}
                </span>
                <span className="truncate font-mono text-muted">{r.contract}</span>
                <span className="tag bg-panel text-[9px] text-muted">{r.reason}</span>
                {r.hollow && (
                  <span
                    className={`tag text-[9px] ${
                      r.shadow_class === "late"
                        ? "bg-orange-500/15 text-orange-300"
                        : r.shadow_class === "refire"
                          ? "bg-rose-500/15 text-rose-300"
                          : r.shadow_class === "stopb"
                            ? "bg-teal-500/15 text-teal-300"
                            : "bg-purple-500/15 text-purple-300"
                    }`}
                    title={
                      r.shadow_class === "late"
                        ? "Counterfactual: this card was vetoed by the 14:15 entry cutoff — paper takes it anyway so the cutoff hypothesis stays auditable. Not counted in any aggregate above."
                        : r.shadow_class === "refire"
                          ? "Counterfactual: this card was vetoed by the re-fire guard (same thesis stopped/invalidated within 2h) — paper takes it anyway so the guard stays auditable. Not counted in any aggregate above."
                          : r.shadow_class === "stopb"
                            ? "Stop-basis A/B twin: the same fill run under the OTHER stop basis. Graded only against its paired clean row — not counted in any aggregate above."
                            : "Counterfactual: this card was vetoed by the volume/OI participation floor — paper takes it anyway so the floor stays auditable. Not counted in any aggregate above."
                    }
                  >
                    {r.shadow_class === "late"
                      ? "late"
                      : r.shadow_class === "refire"
                        ? "refire"
                        : r.shadow_class === "stopb"
                          ? "stop A/B"
                          : "hollow"}
                  </span>
                )}
                {r.era?.startsWith("inflated") && (
                  <span
                    className="tag bg-yellow-500/15 text-[9px] text-yellow-400"
                    title="Filled before the honest-fill fix (23-Jul) — excluded from every aggregate above"
                  >
                    inflated
                  </span>
                )}
                <span className={`ml-auto font-mono ${r.net_pnl >= 0 ? "text-bull" : "text-bear"}`}>
                  ₹{signed(r.net_pnl, 0)}
                  <span className="ml-1 text-[10px] opacity-80">{signed(r.return_pct, 1)}%</span>
                </span>
              </div>
              <div className="mt-0.5 flex flex-wrap gap-x-2 font-mono text-[10px] text-muted">
                <span>{istDateTime(r.entered_at)}</span>
                <span className="text-white/80">₹{r.entry}</span>
                <span>→</span>
                {/* Repeat the date on exit only when it differs — a positional
                    round trip can close days later, and "13:19 → 10:28" with no
                    dates reads as time travel. Same-day exits stay compact. */}
                <span>
                  {istDayKey(r.exited_at) !== istDayKey(r.entered_at)
                    ? istDateTime(r.exited_at)
                    : istTime(r.exited_at)}
                </span>
                <span className="text-white/80">₹{r.exit}</span>
                <span>· {r.quantity} qty</span>
                <span title="Charges deducted from the gross move">· ₹{Math.round(r.charges)} chg</span>
              </div>
            </div>
          ))}
        {data.trades === 0 && (
          <div className="py-2 text-center text-xs text-muted">
            No simulated trades yet — the engine takes one when it issues a signal.
          </div>
        )}
      </div>

      <p className="mt-2 text-[10px] leading-relaxed text-muted">{data.note}</p>
    </div>
  );
}

function Stat({ label, value, sub, tone }: { label: string; value: string; sub?: string; tone?: string }) {
  return (
    <div className="rounded border border-edge bg-panel2 px-2 py-1">
      <div className="text-[9px] uppercase tracking-wide text-muted">{label}</div>
      <div className={`font-mono text-sm ${tone ?? "text-white"}`}>{value}</div>
      {sub && <div className="text-[9px] text-muted">{sub}</div>}
    </div>
  );
}
