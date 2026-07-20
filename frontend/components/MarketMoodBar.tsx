"use client";

import { MarketMood } from "@/lib/api";
import { istDate } from "@/lib/format";

// Zone → accent colour. Fear side warns of a nervous tape; greed side warns of
// froth (contrarian). Kept as context only — the signal engine ignores this.
function zoneTone(zone: string): string {
  switch (zone) {
    case "Extreme Fear": return "text-red-400";
    case "Fear": return "text-orange-400";
    case "Greed": return "text-teal-300";
    case "Extreme Greed": return "text-emerald-400";
    default: return "text-muted";
  }
}

export function MarketMoodBar({ mood }: { mood: MarketMood | null | undefined }) {
  if (!mood) return null;
  const pct = Math.max(0, Math.min(100, mood.value));
  return (
    <div className="card p-3">
      <div className="mb-2 flex items-center justify-between">
        <h3 className="text-sm font-medium">Market Mood</h3>
        <span className="text-[10px] text-muted">{mood.source}</span>
      </div>

      <div className="flex items-baseline gap-2">
        <span className={`font-mono text-2xl font-semibold ${zoneTone(mood.zone)}`}>
          {mood.value.toFixed(1)}
        </span>
        <span className={`text-sm font-medium ${zoneTone(mood.zone)}`}>{mood.zone}</span>
        <span className="ml-auto text-[10px] text-muted">
          {mood.date ? `as of ${istDate(Math.floor(new Date(mood.date).getTime() / 1000))}` : ""}
        </span>
      </div>

      {/* 0-100 fear→greed gauge with a marker at the current value */}
      <div className="relative mt-2 h-2 rounded-full"
        style={{ background: "linear-gradient(90deg,#ef4444 0%,#f59e0b 33%,#14b8a6 66%,#10b981 100%)" }}>
        <div className="absolute -top-1 h-4 w-0.5 rounded bg-white shadow"
          style={{ left: `calc(${pct}% - 1px)` }} />
      </div>
      <div className="mt-1 flex justify-between text-[9px] uppercase tracking-wide text-muted">
        <span>Extreme Fear</span>
        <span>Extreme Greed</span>
      </div>
      <p className="mt-2 text-[10px] leading-relaxed text-muted">
        Contrarian sentiment gauge (VIX, momentum, FII, skew). Context only — not used in signals.
      </p>
    </div>
  );
}
