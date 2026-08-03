"use client";

// Today's NIFTY 5-minute tape with the analysis PINNED TO ITS CANDLE:
// candlestick-pattern markers on the bar that printed them, level-touch
// callouts (BUY/CEILING) on the bar where they fired, and the strong S/R
// ladder as horizontal lines. Index-space throughout — the ladder and the
// candles share a scale, so a "touch" here is exactly what the watcher saw.
//
// Lives in the Patterns Lab ONLY (user rule: trading screens stay untouched).

import { useEffect, useRef } from "react";
import {
  createChart,
  createSeriesMarkers,
  ColorType,
  CandlestickSeries,
  IChartApi,
  ISeriesApi,
  ISeriesMarkersPluginApi,
  SeriesMarker,
  Time,
  UTCTimestamp,
} from "lightweight-charts";
import { LevelAlert, LevelRow, PatternsLiveRead } from "@/lib/api";

const IST_OFFSET = 19800; // axis labels read IST

// Compact marker labels — a chart marker carries a word, the table below
// carries the stats.
function short(pattern: string): string {
  return pattern
    .replace("bull_", "")
    .replace("bear_", "")
    .replace(/_/g, " ");
}

export function PatternsTapeChart({
  read,
  levels,
  alerts,
}: {
  read: PatternsLiveRead;
  levels: LevelRow[];
  alerts: LevelAlert[];
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const markersRef = useRef<ISeriesMarkersPluginApi<Time> | null>(null);
  const levelLinesRef = useRef<ReturnType<ISeriesApi<"Candlestick">["createPriceLine"]>[]>([]);

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const chart = createChart(el, {
      layout: {
        background: { type: ColorType.Solid, color: "#0f141b" },
        textColor: "#8b98a9",
        fontFamily: "ui-monospace, monospace",
      },
      grid: { vertLines: { color: "#1a2029" }, horzLines: { color: "#1a2029" } },
      timeScale: { timeVisible: true, secondsVisible: false, borderColor: "#1f2937" },
      rightPriceScale: { borderColor: "#1f2937" },
      crosshair: { mode: 0 },
      autoSize: true,
    });
    seriesRef.current = chart.addSeries(CandlestickSeries, {
      upColor: "#16c784",
      downColor: "#ea3943",
      borderVisible: false,
      wickUpColor: "#16c784",
      wickDownColor: "#ea3943",
    });
    markersRef.current = createSeriesMarkers(seriesRef.current, []);
    chartRef.current = chart;
    return () => {
      chart.remove();
      chartRef.current = null;
      seriesRef.current = null;
      markersRef.current = null;
    };
  }, []);

  // Candles + markers. Markers must be time-sorted or the plugin drops them.
  useEffect(() => {
    const series = seriesRef.current;
    if (!series) return;
    const candles = read.candles ?? [];
    if (candles.length === 0) return;
    series.setData(
      candles.map((c) => ({
        time: (c.ts + IST_OFFSET) as UTCTimestamp,
        open: c.open,
        high: c.high,
        low: c.low,
        close: c.close,
      })),
    );

    const firstTs = candles[0].ts;
    const lastTs = candles[candles.length - 1].ts;
    const snap = (ts: number) => Math.min(lastTs, Math.max(firstTs, ts - (ts % 300)));

    const markers: SeriesMarker<Time>[] = [];
    for (const p of read.patterns ?? []) {
      if (p.bar_ts == null) continue;
      const bullish = p.direction === "bullish";
      markers.push({
        time: (p.bar_ts + IST_OFFSET) as Time,
        position: bullish ? "belowBar" : "aboveBar",
        shape: bullish ? "arrowUp" : "arrowDown",
        color: bullish ? "#16c784" : "#ea3943",
        text: short(p.pattern),
      });
    }
    for (const a of alerts) {
      // Belt over the backend's braces: an alert outside today's candle range
      // must be DROPPED, not clamped onto a boundary candle it never touched.
      // (+300s keeps the forming-bar case, whose candle isn't closed yet.)
      if (a.ts < firstTs - 300 || a.ts > lastTs + 600) continue;
      // A callout fires mid-bar; pin it to the bar that was forming then.
      markers.push({
        time: (snap(a.ts) + IST_OFFSET) as Time,
        position: a.side === "buy" ? "belowBar" : "aboveBar",
        shape: a.side === "buy" ? "arrowUp" : "arrowDown",
        color: a.side === "buy" ? "#22d3ee" : "#facc15",
        text: a.side === "buy" ? `BUY ${a.strike}CE` : `BOOK @ ${a.level.toFixed(0)}`,
        size: 2,
      });
    }
    markers.sort((a, b) => (a.time as number) - (b.time as number));
    markersRef.current?.setMarkers(markers);
  }, [read, alerts]);

  // The strong S/R ladder as dotted lines — same index scale as the candles.
  useEffect(() => {
    const series = seriesRef.current;
    if (!series) return;
    levelLinesRef.current.forEach((l) => {
      try {
        series.removePriceLine(l);
      } catch {
        /* already gone */
      }
    });
    levelLinesRef.current = levels.map((l) =>
      series.createPriceLine({
        price: l.level,
        color: l.days_touched >= 10 ? "#8b5cf6" : "#4b5563",
        lineWidth: 1,
        lineStyle: 3,
        axisLabelVisible: false,
        title: `${l.level.toFixed(0)} · ${Math.round(l.hold_rate * 100)}%/${l.days_touched}d`,
      }),
    );
  }, [levels]);

  return <div ref={containerRef} className="h-72 w-full" />;
}
