"use client";

import { MarketStatus, TradingMode } from "@/lib/api";
import { signed } from "@/lib/format";

function ScoreMeter({ label, value, tone }: { label: string; value: number; tone: "bull" | "bear" }) {
  const bar = tone === "bull" ? "bg-bull" : "bg-bear";
  const text = tone === "bull" ? "text-bull" : "text-bear";
  return (
    <span className="flex items-center gap-1.5">
      <span className="text-[10px] uppercase text-muted">{label}</span>
      <span className="h-1.5 w-12 overflow-hidden rounded-full bg-edge">
        <span
          className={`block h-full ${bar}`}
          style={{ width: `${Math.min(100, Math.max(0, value))}%` }}
        />
      </span>
      <span className={`w-6 text-right font-mono text-xs ${text}`}>{value.toFixed(0)}</span>
    </span>
  );
}

/**
 * One 36px control rail: regime + bull/bear, the mode & timeframe switches, the
 * alert toggle and today's realized P&L. Consolidates what used to be three
 * separate stacked rows (regime card, mode row, timeframe row) so the chart and
 * signal card get that vertical space back.
 */
export function CommandStrip({
  status,
  showRegime,
  mode,
  available,
  onMode,
  tf,
  timeframes,
  onTf,
  chartError,
  alerts,
  realizedToday,
  realizedTotal,
  className = "",
}: {
  status: MarketStatus | null;
  showRegime: boolean;
  mode: TradingMode;
  available: { key: TradingMode; label: string }[];
  onMode: (m: TradingMode) => void;
  tf: string;
  timeframes: string[];
  onTf: (t: any) => void;
  chartError?: string | null;
  alerts: { muted: boolean; toggle: () => void };
  realizedToday: number;
  realizedTotal: number;
  className?: string;
}) {
  const biasTone =
    status?.bias === "bullish" ? "text-bull" : status?.bias === "bearish" ? "text-bear" : "text-muted";

  return (
    <div className={`card flex flex-wrap items-center gap-x-4 gap-y-1.5 px-3 py-1.5 ${className}`}>
      {showRegime && (
        <>
          <span className="flex min-w-0 items-center gap-2">
            <span className="text-[10px] uppercase tracking-wide text-muted">Regime</span>
            <span className={`text-sm font-semibold ${biasTone}`}>
              {status?.regime_label ?? "—"}
            </span>
            {status?.headline && (
              <span className="hidden max-w-[22ch] truncate text-xs text-white/70 2xl:inline">
                {status.headline}
              </span>
            )}
          </span>
          {status && (
            <span className="flex items-center gap-3">
              <ScoreMeter label="Bull" value={status.bull_score} tone="bull" />
              <ScoreMeter label="Bear" value={status.bear_score} tone="bear" />
            </span>
          )}

          <span className="inline-flex rounded-md border border-edge bg-panel p-0.5">
            {available.map((m) => (
              <button
                key={m.key}
                onClick={() => onMode(m.key)}
                title={m.key === "intraday" ? "same-day · weekly options" : "multi-day swing · monthly options"}
                className={`rounded px-2 py-0.5 text-xs font-medium transition ${
                  m.key === mode ? "bg-accent text-white" : "text-muted hover:text-white"
                }`}
              >
                {m.label}
              </button>
            ))}
          </span>
        </>
      )}

      <span className="inline-flex items-center gap-0.5">
        {timeframes.map((t) => (
          <button
            key={t}
            onClick={() => onTf(t)}
            className={`rounded px-2 py-0.5 text-xs font-medium ${
              t === tf ? "bg-accent text-white" : "bg-panel2 text-muted hover:text-white"
            }`}
          >
            {t}
          </button>
        ))}
        {chartError && <span className="ml-1 text-[10px] text-bear">chart: {chartError}</span>}
      </span>

      <span className="ml-auto flex items-center gap-3">
        <span className="whitespace-nowrap text-[11px]" title="Realized P&L — booked today (lifetime in brackets)">
          <span className="text-muted">P&L </span>
          <span className={`font-mono ${realizedToday >= 0 ? "text-bull" : "text-bear"}`}>
            ₹{signed(realizedToday, 0)}
          </span>
          {realizedTotal !== realizedToday && (
            <span className="text-muted"> ({signed(realizedTotal, 0)})</span>
          )}
        </span>
        <button
          onClick={alerts.toggle}
          title={
            alerts.muted
              ? "Alerts off — click for a chime + desktop notification on each new signal"
              : "Alerts on — click to mute"
          }
          className={`rounded-md border px-2 py-0.5 text-xs transition ${
            alerts.muted
              ? "border-edge bg-panel text-muted hover:text-white"
              : "border-accent/60 bg-accent/15 text-accent"
          }`}
        >
          {alerts.muted ? "🔕" : "🔔"}
        </button>
      </span>
    </div>
  );
}
