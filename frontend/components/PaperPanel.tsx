"use client";

import { PaperSummary } from "@/lib/api";
import { istTime, signed } from "@/lib/format";

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
                <span className={`ml-auto font-mono ${r.net_pnl >= 0 ? "text-bull" : "text-bear"}`}>
                  ₹{signed(r.net_pnl, 0)}
                  <span className="ml-1 text-[10px] opacity-80">{signed(r.return_pct, 1)}%</span>
                </span>
              </div>
              <div className="mt-0.5 flex flex-wrap gap-x-2 font-mono text-[10px] text-muted">
                <span>{istTime(r.entered_at)}</span>
                <span className="text-white/80">₹{r.entry}</span>
                <span>→</span>
                <span>{istTime(r.exited_at)}</span>
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
