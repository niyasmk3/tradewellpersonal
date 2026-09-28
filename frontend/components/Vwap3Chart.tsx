"use client";

// One session of NIFTY 5m candles with B09's three principal lines — session
// VWAP and the ±kσ bands — plus touch markers and any engine trades that day
// (entry arrow, exit dot). Same lightweight-charts v5 conventions as
// GoldChart (markers time-sorted; IST axis offset).

import { useEffect, useRef } from "react";
import {
  createChart,
  createSeriesMarkers,
  ColorType,
  CandlestickSeries,
  LineSeries,
  IChartApi,
  ISeriesApi,
  ISeriesMarkersPluginApi,
  SeriesMarker,
  Time,
  UTCTimestamp,
} from "lightweight-charts";
import { LabVwap3Day } from "@/lib/api";

const IST_OFFSET = 19800;
const BULL = "#16c784";
const BEAR = "#ea3943";
const ACCENT = "#3b82f6";

export function Vwap3Chart({ data }: { data: LabVwap3Day }) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const vwapRef = useRef<ISeriesApi<"Line"> | null>(null);
  const upRef = useRef<ISeriesApi<"Line"> | null>(null);
  const dnRef = useRef<ISeriesApi<"Line"> | null>(null);
  const markersRef = useRef<ISeriesMarkersPluginApi<Time> | null>(null);

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
    candleRef.current = chart.addSeries(CandlestickSeries, {
      upColor: BULL, downColor: BEAR, borderVisible: false,
      wickUpColor: BULL, wickDownColor: BEAR,
    });
    vwapRef.current = chart.addSeries(LineSeries, {
      color: ACCENT, lineWidth: 2, priceLineVisible: false,
      lastValueVisible: false,
    });
    upRef.current = chart.addSeries(LineSeries, {
      color: BEAR, lineWidth: 1, lineStyle: 2, priceLineVisible: false,
      lastValueVisible: false,
    });
    dnRef.current = chart.addSeries(LineSeries, {
      color: BULL, lineWidth: 1, lineStyle: 2, priceLineVisible: false,
      lastValueVisible: false,
    });
    markersRef.current = createSeriesMarkers(candleRef.current, []);
    chartRef.current = chart;
    return () => {
      chart.remove();
      chartRef.current = null;
      candleRef.current = null;
      vwapRef.current = null;
      upRef.current = null;
      dnRef.current = null;
      markersRef.current = null;
    };
  }, []);

  useEffect(() => {
    if (!candleRef.current || data.candles.length === 0) return;
    const t = (ts: number) => (ts + IST_OFFSET) as UTCTimestamp;
    candleRef.current.setData(data.candles.map((c) => ({
      time: t(c.ts), open: c.open, high: c.high, low: c.low, close: c.close,
    })));
    vwapRef.current?.setData(data.vwap.map((p) => ({ time: t(p.ts), value: p.v })));
    upRef.current?.setData(data.upper.map((p) => ({ time: t(p.ts), value: p.v })));
    dnRef.current?.setData(data.lower.map((p) => ({ time: t(p.ts), value: p.v })));

    const markers: SeriesMarker<Time>[] = [];
    for (const tc of data.touches) {
      const upperTouch = tc.side === "upper";
      markers.push({
        time: t(tc.ts) as Time,
        position: upperTouch ? "aboveBar" : "belowBar",
        shape: upperTouch ? "arrowDown" : "arrowUp",
        color: tc.returned ? (upperTouch ? BEAR : BULL) : "#eab308",
        text: `${upperTouch ? "▲σ" : "▼σ"} #${tc.ordinal} ${tc.returned ? "→vwap" : tc.outcome}`,
        size: 1,
      });
    }
    for (const tr of data.trades) {
      const pe = tr.legs[0]?.right === "PE";
      markers.push({
        time: t(tr.entry_ts) as Time,
        position: pe ? "aboveBar" : "belowBar",
        shape: pe ? "arrowDown" : "arrowUp",
        color: ACCENT,
        text: `BUY ${pe ? "PE" : "CE"} ${tr.tag}`,
        size: 2,
      });
      markers.push({
        time: t(tr.exit_ts) as Time,
        position: pe ? "belowBar" : "aboveBar",
        shape: "circle",
        color: (tr.pnl_net ?? 0) > 0 ? BULL : BEAR,
        text: `${tr.exit_reason} ${tr.pnl_net != null ? `₹${Math.round(tr.pnl_net)}` : ""}`,
      });
    }
    markers.sort((a, b) => (a.time as number) - (b.time as number));
    markersRef.current?.setMarkers(markers);
    chartRef.current?.timeScale().fitContent();
  }, [data]);

  return <div ref={containerRef} className="h-80 w-full" />;
}
