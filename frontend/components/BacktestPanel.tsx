"use client";

import { useState } from "react";
import { BacktestResult, BacktestTrade, TradingMode, api } from "@/lib/api";
import { fmt, signed, istDate, istTime } from "@/lib/format";

const DAY_OPTIONS = [10, 20, 30, 60];

function Metric({ label, value, tone }: { label: string; value: string; tone?: string }) {
  return (
    <div className="rounded-md border border-edge bg-panel2 px-2 py-1.5">
      <div className="text-[10px] uppercase tracking-wide text-muted">{label}</div>
      <div className={`font-mono text-sm ${tone ?? "text-white"}`}>{value}</div>
    </div>
  );
}

function EquityCurve({ points }: { points: number[] }) {
  if (points.length < 2) return null;
  const w = 100;
  const h = 32;
  const min = Math.min(0, ...points);
  const max = Math.max(0, ...points);
  const span = max - min || 1;
  const x = (i: number) => (i / (points.length - 1)) * w;
  const y = (v: number) => h - ((v - min) / span) * h;
  const path = points.map((v, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(2)},${y(v).toFixed(2)}`).join(" ");
  const zeroY = y(0);
  const up = points[points.length - 1] >= 0;
  return (
    <svg viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" className="h-16 w-full">
      <line x1="0" y1={zeroY} x2={w} y2={zeroY} stroke="currentColor" className="text-edge" strokeWidth="0.5" />
      <path d={path} fill="none" strokeWidth="1.2" className={up ? "text-bull" : "text-bear"} stroke="currentColor" />
    </svg>
  );
}

function outcomeTone(o: string): string {
  if (o === "target") return "bg-bull/15 text-bull";
  if (o === "stop") return "bg-bear/15 text-bear";
  return "bg-panel2 text-muted";
}

function TradeRow({ t }: { t: BacktestTrade }) {
  const win = t.r_multiple > 0;
  return (
    <tr className="border-t border-edge/50">
      <td className="py-1 pr-2 text-muted">{istDate(t.entry_ts)} {istTime(t.entry_ts)}</td>
      <td className="py-1 pr-2">
        <span className={t.direction === "CE" ? "text-bull" : "text-bear"}>{t.direction}</span>
      </td>
      <td className="py-1 pr-2 text-right font-mono">{fmt(t.entry)}</td>
      <td className="py-1 pr-2 text-right font-mono">{fmt(t.exit)}</td>
      <td className={`py-1 pr-2 text-right font-mono ${win ? "text-bull" : "text-bear"}`}>{signed(t.r_multiple, 2)}R</td>
      <td className="py-1 text-right">
        <span className={`tag ${outcomeTone(t.outcome)}`}>{t.outcome}</span>
      </td>
    </tr>
  );
}

export function BacktestPanel({ symbol }: { symbol: string }) {
  const [mode, setMode] = useState<TradingMode>("intraday");
  const [days, setDays] = useState(20);
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<BacktestResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function run() {
    setRunning(true);
    setError(null);
    try {
      const res = await api.backtest(symbol, mode, days);
      setResult(res);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setResult(null);
    } finally {
      setRunning(false);
    }
  }

  const posTone = (n: number) => (n > 0 ? "text-bull" : n < 0 ? "text-bear" : "text-white");

  return (
    <div className="card p-3">
      <div className="mb-2 flex items-center justify-between">
        <h3 className="text-sm font-medium">Backtest</h3>
        <span className="text-[10px] text-muted">Directional · signal-engine replay</span>
      </div>

      {/* controls */}
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <div className="inline-flex rounded-md border border-edge bg-panel p-0.5">
          {(["intraday", "positional"] as TradingMode[]).map((m) => (
            <button
              key={m}
              onClick={() => setMode(m)}
              disabled={running}
              className={`rounded px-2.5 py-1 text-xs font-medium capitalize transition ${
                m === mode ? "bg-accent text-white" : "text-muted hover:text-white"
              }`}
            >
              {m}
            </button>
          ))}
        </div>
        <div className="inline-flex rounded-md border border-edge bg-panel p-0.5">
          {DAY_OPTIONS.map((d) => (
            <button
              key={d}
              onClick={() => setDays(d)}
              disabled={running}
              className={`rounded px-2 py-1 text-xs font-medium transition ${
                d === days ? "bg-accent text-white" : "text-muted hover:text-white"
              }`}
            >
              {d}d
            </button>
          ))}
        </div>
        <button
          onClick={run}
          disabled={running}
          className="rounded-md bg-accent px-3 py-1 text-xs font-semibold text-white disabled:opacity-50"
        >
          {running ? "Running…" : "Run backtest"}
        </button>
      </div>

      {error && <div className="mb-2 rounded-md bg-bear/10 px-3 py-2 text-xs text-bear">{error}</div>}

      {running && !result && (
        <div className="py-6 text-center text-xs text-muted">Fetching history &amp; replaying the engine…</div>
      )}

      {!result && !running && !error && (
        <p className="py-4 text-center text-xs text-muted">
          Replays the {symbol} signal engine over historical futures candles and grades each signal on the
          underlying move. Pick a mode &amp; window, then run.
        </p>
      )}

      {result && (
        <div className="space-y-3">
          <div className="text-[10px] text-muted">
            {result.trades_total} trades · {result.bars} bars · {istDate(result.from_ts)} → {istDate(result.to_ts)} · {result.timeframe}
          </div>

          <div className="grid grid-cols-3 gap-1.5 sm:grid-cols-4">
            <Metric label="Trades" value={String(result.trades_total)} />
            <Metric label="Win rate" value={`${result.win_rate}%`} tone={result.win_rate >= 50 ? "text-bull" : "text-bear"} />
            <Metric label="Expectancy" value={`${signed(result.expectancy_r, 2)}R`} tone={posTone(result.expectancy_r)} />
            <Metric label="Total" value={`${signed(result.total_r, 1)}R`} tone={posTone(result.total_r)} />
            <Metric label="Profit factor" value={result.profit_factor === null ? "∞" : fmt(result.profit_factor, 2)} />
            <Metric label="Avg win" value={`${signed(result.avg_win_r, 2)}R`} tone="text-bull" />
            <Metric label="Avg loss" value={`${signed(result.avg_loss_r, 2)}R`} tone="text-bear" />
            <Metric label="Max DD" value={`-${fmt(result.max_drawdown_r, 1)}R`} tone="text-bear" />
          </div>

          {result.trades_total > 0 && (
            <>
              <div className="flex items-center gap-4 text-[11px] text-muted">
                <span>CE: {result.ce_trades} @ <span className="text-white">{result.ce_win_rate}%</span></span>
                <span>PE: {result.pe_trades} @ <span className="text-white">{result.pe_win_rate}%</span></span>
              </div>

              <div>
                <div className="mb-1 text-[10px] uppercase tracking-wide text-muted">Equity (cumulative R)</div>
                <EquityCurve points={result.equity_curve} />
              </div>

              <div className="max-h-[240px] overflow-y-auto scroll-thin">
                <table className="w-full text-[11px]">
                  <thead className="sticky top-0 bg-panel text-[10px] uppercase text-muted">
                    <tr className="text-left">
                      <th className="py-1 pr-2 font-normal">Entry</th>
                      <th className="py-1 pr-2 font-normal">Dir</th>
                      <th className="py-1 pr-2 text-right font-normal">In</th>
                      <th className="py-1 pr-2 text-right font-normal">Out</th>
                      <th className="py-1 pr-2 text-right font-normal">R</th>
                      <th className="py-1 text-right font-normal">Exit</th>
                    </tr>
                  </thead>
                  <tbody>
                    {result.trades.slice().reverse().map((t, i) => <TradeRow key={i} t={t} />)}
                  </tbody>
                </table>
              </div>
            </>
          )}

          <p className="text-[10px] leading-relaxed text-muted">{result.note}</p>
        </div>
      )}
    </div>
  );
}
