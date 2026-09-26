"use client";

// Today's GOLDM 3m tape with the paper cards PINNED TO THEIR CANDLES: entry
// arrows on the bar that filled them, exit dots on the bar that closed them,
// target/stop lines while a card is open, and the previous session's close as
// the dotted H1 reference. The candles come from the live payload itself —
// the chart draws exactly what simulate_day scored, never a different fetch.
//
// Same lightweight-charts v5 conventions as PatternsTapeChart (markers must
// be time-sorted or the plugin drops them; price lines removed before re-add).

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
import { GoldLive } from "@/lib/api";

const IST_OFFSET = 19800; // axis labels read IST

const BULL = "#16c784";
const BEAR = "#ea3943";

export function GoldChart({ data }: { data: GoldLive }) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const markersRef = useRef<ISeriesMarkersPluginApi<Time> | null>(null);
  const linesRef = useRef<ReturnType<ISeriesApi<"Candlestick">["createPriceLine"]>[]>([]);

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
      upColor: BULL,
      downColor: BEAR,
      borderVisible: false,
      wickUpColor: BULL,
      wickDownColor: BEAR,
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

  useEffect(() => {
    const series = seriesRef.current;
    if (!series) return;
    const candles = data.candles ?? [];
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
    const inRange = (ts: number | null): ts is number =>
      ts != null && ts >= firstTs && ts <= lastTs;

    // Entry/exit markers on the exact bars simulate_day used.
    const markers: SeriesMarker<Time>[] = [];
    for (const c of data.cards) {
      const long = c.direction === "LONG";
      if (inRange(c.entry_ts)) {
        markers.push({
          time: (c.entry_ts + IST_OFFSET) as Time,
          position: long ? "belowBar" : "aboveBar",
          shape: long ? "arrowUp" : "arrowDown",
          color: long ? BULL : BEAR,
          text: `${c.rule} ${long ? "long" : "short"}`,
          size: 2,
        });
      }
      if (c.state === "closed" && inRange(c.exit_ts)) {
        const won = (c.net_rs ?? 0) > 0;
        markers.push({
          time: (c.exit_ts + IST_OFFSET) as Time,
          position: long ? "aboveBar" : "belowBar",
          shape: "circle",
          color: won ? BULL : BEAR,
          text: `${c.rule} ${c.exit_reason ?? "exit"}`,
        });
      }
    }
    markers.sort((a, b) => (a.time as number) - (b.time as number));
    markersRef.current?.setMarkers(markers);

    // Price lines: prev close always (H1's reference), target/stop while open.
    linesRef.current.forEach((l) => {
      try {
        series.removePriceLine(l);
      } catch {
        /* already gone */
      }
    });
    linesRef.current = [];
    const addLine = (price: number, color: string, title: string) =>
      linesRef.current.push(series.createPriceLine({
        price, color, lineWidth: 1, lineStyle: 3, axisLabelVisible: false, title,
      }));
    if (data.prev_close != null) addLine(data.prev_close, "#4b5563", "prev close");
    for (const c of data.cards) {
      if (c.state !== "open") continue;
      if (c.target_px != null) addLine(c.target_px, BULL, `${c.rule} target`);
      if (c.stop_px != null) addLine(c.stop_px, BEAR, `${c.rule} stop`);
    }
  }, [data]);

  return <div ref={containerRef} className="h-80 w-full" />;
}
