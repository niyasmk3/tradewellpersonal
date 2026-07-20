"use client";

import { MarketStatus } from "@/lib/api";

function ScoreMeter({ label, value, tone }: { label: string; value: number; tone: "bull" | "bear" }) {
  const color = tone === "bull" ? "bg-bull" : "bg-bear";
  const text = tone === "bull" ? "text-bull" : "text-bear";
  return (
    <div className="flex min-w-[130px] items-center gap-2">
      <span className="text-[10px] uppercase text-muted">{label}</span>
      <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-edge">
        <div className={`h-full ${color}`} style={{ width: `${Math.min(100, Math.max(0, value))}%` }} />
      </div>
      <span className={`w-7 text-right font-mono text-xs ${text}`}>{value.toFixed(0)}</span>
    </div>
  );
}

export function RegimeBar({ status }: { status: MarketStatus | null }) {
  if (!status) {
    return (
      <div className="card px-4 py-2.5 text-sm text-muted">Signal engine warming up…</div>
    );
  }
  const biasTone =
    status.bias === "bullish" ? "text-bull" : status.bias === "bearish" ? "text-bear" : "text-muted";

  return (
    <div className="card flex flex-wrap items-center gap-x-5 gap-y-2 px-4 py-2.5">
      <div className="flex items-center gap-2">
        <span className="text-[10px] uppercase tracking-wide text-muted">Regime</span>
        <span className={`text-sm font-semibold ${biasTone}`}>{status.regime_label}</span>
      </div>
      <div className="text-sm text-white/90">{status.headline}</div>
      {status.news_label && (
        <span
          className={`tag ${
            status.news_label === "positive"
              ? "bg-bull/15 text-bull"
              : status.news_label === "negative"
                ? "bg-bear/15 text-bear"
                : status.news_label === "volatile"
                  ? "bg-yellow-500/15 text-yellow-400"
                  : "bg-panel2 text-muted"
          }`}
        >
          News {status.news_label}
          {status.news_net != null && status.news_label !== "neutral"
            ? ` ${status.news_net >= 0 ? "+" : ""}${status.news_net.toFixed(0)}`
            : ""}
        </span>
      )}
      <div className="ml-auto flex flex-wrap items-center gap-4">
        <ScoreMeter label="Bull" value={status.bull_score} tone="bull" />
        <ScoreMeter label="Bear" value={status.bear_score} tone="bear" />
      </div>
    </div>
  );
}
