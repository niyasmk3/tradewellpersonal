"use client";

import { useEffect, useRef } from "react";
import {
  createChart,
  ColorType,
  CandlestickSeries,
  LineSeries,
  IChartApi,
  ISeriesApi,
  UTCTimestamp,
} from "lightweight-charts";
import { Candle } from "@/lib/api";

const IST_OFFSET = 19800; // +5:30 so axis labels read IST

function ema(values: number[], period: number): (number | undefined)[] {
  const k = 2 / (period + 1);
  const out: (number | undefined)[] = [];
  let prev: number | undefined;
  for (let i = 0; i < values.length; i++) {
    prev = prev === undefined ? values[i] : values[i] * k + prev * (1 - k);
    out.push(i >= period - 1 ? prev : undefined);
  }
  return out;
}

function vwap(candles: Candle[]): (number | undefined)[] {
  let cumPV = 0;
  let cumV = 0;
  return candles.map((c) => {
    const tp = (c.high + c.low + c.close) / 3;
    cumPV += tp * c.volume;
    cumV += c.volume;
    return cumV > 0 ? cumPV / cumV : undefined;
  });
}

type LineData = { time: UTCTimestamp; value: number }[];

function line(candles: Candle[], series: (number | undefined)[]): LineData {
  const out: LineData = [];
  candles.forEach((c, i) => {
    const v = series[i];
    if (v !== undefined) out.push({ time: (c.ts + IST_OFFSET) as UTCTimestamp, value: v });
  });
  return out;
}

export function PriceChart({
  candles,
  title,
  fill = false,
}: {
  candles: Candle[];
  title: string;
  /** fill=true: take whatever height the parent gives (single-screen layout).
   *  The chart itself is autoSize, so it follows the container. */
  fill?: boolean;
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const vwapRef = useRef<ISeriesApi<"Line"> | null>(null);
  const ema9Ref = useRef<ISeriesApi<"Line"> | null>(null);
  const ema20Ref = useRef<ISeriesApi<"Line"> | null>(null);

  // Create the chart once.
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;

    const chart = createChart(el, {
      layout: {
        background: { type: ColorType.Solid, color: "#0f141b" },
        textColor: "#8b98a9",
        fontFamily: "ui-monospace, monospace",
      },
      grid: {
        vertLines: { color: "#1a2029" },
        horzLines: { color: "#1a2029" },
      },
      timeScale: { timeVisible: true, secondsVisible: false, borderColor: "#1f2937" },
      rightPriceScale: { borderColor: "#1f2937" },
      crosshair: { mode: 0 },
      autoSize: true,
    });

    candleRef.current = chart.addSeries(CandlestickSeries, {
      upColor: "#16c784",
      downColor: "#ea3943",
      borderVisible: false,
      wickUpColor: "#16c784",
      wickDownColor: "#ea3943",
    });
    vwapRef.current = chart.addSeries(LineSeries, { color: "#3b82f6", lineWidth: 2, priceLineVisible: false, lastValueVisible: false });
    ema9Ref.current = chart.addSeries(LineSeries, { color: "#f5a623", lineWidth: 1, priceLineVisible: false, lastValueVisible: false });
    ema20Ref.current = chart.addSeries(LineSeries, { color: "#a78bfa", lineWidth: 1, priceLineVisible: false, lastValueVisible: false });

    chartRef.current = chart;
    return () => {
      chart.remove();
      chartRef.current = null;
    };
  }, []);

  // Push data on candle updates — but skip identical payloads: off-hours every
  // 2s poll returns the same 240 bars, and a full setData() would repaint all
  // series and jitter the crosshair/pan while the trader inspects candles.
  const sigRef = useRef<string>("");

  useEffect(() => {
    if (!candleRef.current || candles.length === 0) return;
    const last = candles[candles.length - 1];
    const sig = `${candles.length}:${last.ts}:${last.close}:${last.high}:${last.low}:${last.volume}`;
    if (sig === sigRef.current) return;
    sigRef.current = sig;
    const closes = candles.map((c) => c.close);

    candleRef.current.setData(
      candles.map((c) => ({
        time: (c.ts + IST_OFFSET) as UTCTimestamp,
        open: c.open,
        high: c.high,
        low: c.low,
        close: c.close,
      })),
    );
    vwapRef.current?.setData(line(candles, vwap(candles)));
    ema9Ref.current?.setData(line(candles, ema(closes, 9)));
    ema20Ref.current?.setData(line(candles, ema(closes, 20)));
  }, [candles]);

  return (
    <div className={`card flex flex-col overflow-hidden ${fill ? "h-full min-h-0" : ""}`}>
      <div className="flex shrink-0 items-center justify-between border-b border-edge px-3 py-1.5">
        <h3 className="text-sm font-medium">{title}</h3>
        <div className="flex items-center gap-3 text-[10px]">
          <span className="text-accent">VWAP</span>
          <span className="text-[#f5a623]">EMA9</span>
          <span className="text-[#a78bfa]">EMA20</span>
        </div>
      </div>
      <div ref={containerRef} className={fill ? "min-h-0 w-full flex-1" : "h-[420px] w-full"} />
    </div>
  );
}
