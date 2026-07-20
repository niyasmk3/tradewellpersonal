"use client";

import { IndicatorSnapshot, UnderlyingSnapshot } from "@/lib/api";
import { fmt } from "@/lib/format";

/** One compact inline reading: LABEL value [state]. */
function Pill({
  label,
  value,
  tone,
  state,
  title,
}: {
  label: string;
  value: string;
  tone?: "bull" | "bear" | "neutral";
  state?: string;
  title?: string;
}) {
  const color = tone === "bull" ? "text-bull" : tone === "bear" ? "text-bear" : "text-white";
  return (
    <span className="flex items-baseline gap-1 whitespace-nowrap" title={title}>
      <span className="text-[10px] uppercase tracking-wide text-muted">{label}</span>
      <span className={`font-mono text-xs ${color}`}>{value}</span>
      {state && <span className={`text-[10px] ${color}`}>{state}</span>}
    </span>
  );
}

export function IndicatorPanel({
  ind,
  underlying,
  className = "",
}: {
  ind: IndicatorSnapshot | null;
  underlying: UnderlyingSnapshot | null;
  className?: string;
}) {
  const price = underlying?.fut_ltp ?? underlying?.ltp ?? null;

  // Trend read from EMA stack + Supertrend + price vs VWAP.
  const aboveVwap = price != null && ind?.vwap != null ? price > ind.vwap : null;
  const emaBull = ind?.ema9 != null && ind?.ema20 != null ? ind.ema9 > ind.ema20 : null;
  const rsiTone: "bull" | "bear" | "neutral" =
    ind?.rsi == null ? "neutral" : ind.rsi >= 60 ? "bull" : ind.rsi <= 40 ? "bear" : "neutral";
  const trending = ind?.adx != null && ind.adx >= 25;

  return (
    <div className={`card flex items-center gap-x-4 gap-y-1 overflow-x-auto scroll-thin px-3 py-2 ${className}`}>
      <Pill
        label="VWAP"
        value={fmt(ind?.vwap)}
        tone={aboveVwap == null ? "neutral" : aboveVwap ? "bull" : "bear"}
        state={aboveVwap == null ? undefined : aboveVwap ? "↑" : "↓"}
        title={aboveVwap == null ? "VWAP" : `Price ${aboveVwap ? "above" : "below"} VWAP`}
      />
      <Pill label="EMA9" value={fmt(ind?.ema9)} tone={emaBull == null ? "neutral" : emaBull ? "bull" : "bear"} />
      <Pill label="EMA20" value={fmt(ind?.ema20)} />
      <Pill label="EMA50" value={fmt(ind?.ema50)} />
      <Pill label="RSI" value={fmt(ind?.rsi, 1)} tone={rsiTone} />
      <Pill label="ATR" value={fmt(ind?.atr, 1)} title="Average True Range — volatility" />
      <Pill
        label="ADX"
        value={fmt(ind?.adx, 1)}
        tone={trending ? "bull" : "neutral"}
        state={ind?.adx == null ? undefined : trending ? "trending" : "weak"}
      />
      <Pill
        label="ST"
        value={ind?.supertrend_dir ? ind.supertrend_dir.toUpperCase() : "—"}
        tone={ind?.supertrend_dir === "up" ? "bull" : ind?.supertrend_dir === "down" ? "bear" : "neutral"}
        title={`Supertrend ${fmt(ind?.supertrend)}`}
      />
      <span className="ml-auto whitespace-nowrap text-[10px] text-muted">near-month fut</span>
    </div>
  );
}
