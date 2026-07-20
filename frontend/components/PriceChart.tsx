"use client";

import { useEffect, useRef, useState } from "react";
import {
  createChart,
  ColorType,
  CandlestickSeries,
  HistogramSeries,
  LineSeries,
  IChartApi,
  ISeriesApi,
  UTCTimestamp,
} from "lightweight-charts";
import { Candle, Trade } from "@/lib/api";
import { fmt, istTime, signed } from "@/lib/format";

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

/**
 * MACD(12,26,9) on the FUTURE's closes.
 *   macd   = EMA12 − EMA26        (fast vs slow momentum)
 *   signal = EMA9 of macd
 *   hist   = macd − signal        (shrinks before the lines cross)
 * Deliberately computed on the underlying future, never on option premium —
 * theta decay and IV shifts corrupt moving averages of a premium series.
 */
function macd(values: number[], fast = 12, slow = 26, sig = 9) {
  const ef = ema(values, fast);
  const es = ema(values, slow);
  const line: (number | undefined)[] = values.map((_, i) =>
    ef[i] !== undefined && es[i] !== undefined ? (ef[i] as number) - (es[i] as number) : undefined,
  );
  // EMA of the defined slice only, then re-aligned to the original indices.
  const firstIdx = line.findIndex((v) => v !== undefined);
  const signal: (number | undefined)[] = new Array(values.length).fill(undefined);
  if (firstIdx >= 0) {
    const dense = line.slice(firstIdx) as number[];
    ema(dense, sig).forEach((v, i) => (signal[firstIdx + i] = v));
  }
  const hist = line.map((v, i) =>
    v !== undefined && signal[i] !== undefined ? v - (signal[i] as number) : undefined,
  );
  return { line, signal, hist };
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
  positions = [],
  onExit,
  busyId,
}: {
  candles: Candle[];
  title: string;
  /** fill=true: take whatever height the parent gives (single-screen layout).
   *  The chart itself is autoSize, so it follows the container. */
  fill?: boolean;
  /** Open positions, drawn as floating cards over the chart. */
  positions?: Trade[];
  onExit?: (t: Trade) => void;
  busyId?: string | null;
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const vwapRef = useRef<ISeriesApi<"Line"> | null>(null);
  const ema9Ref = useRef<ISeriesApi<"Line"> | null>(null);
  const ema20Ref = useRef<ISeriesApi<"Line"> | null>(null);
  // MACD lives in a second pane; off by default so it never silently steals
  // chart height in the fixed single-screen layout.
  const [showMacd, setShowMacd] = useState(false);
  const macdRef = useRef<ISeriesApi<"Line"> | null>(null);
  const macdSigRef = useRef<ISeriesApi<"Line"> | null>(null);
  const macdHistRef = useRef<ISeriesApi<"Histogram"> | null>(null);
  // Signature of the last payload pushed, so identical polls don't repaint.
  // Declared here (not beside the data effect) because chart creation must be
  // able to CLEAR it — see the reset in the creation effect below.
  const sigRef = useRef<string>("");

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
    // The series above are brand new and EMPTY. Clear the payload signature so
    // the data effect actually repopulates them — otherwise a chart recreated
    // while `candles` is unchanged (HMR, remount) stays permanently blank.
    sigRef.current = "";
    return () => {
      chart.remove();
      chartRef.current = null;
    };
  }, []);

  // Create/destroy the MACD pane on toggle. Series live in pane index 1, so
  // the candles keep pane 0 and simply give up some height.
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    if (showMacd) {
      macdHistRef.current = chart.addSeries(
        HistogramSeries,
        { priceLineVisible: false, lastValueVisible: false, priceFormat: { type: "price", precision: 2, minMove: 0.01 } },
        1,
      );
      macdRef.current = chart.addSeries(
        LineSeries,
        { color: "#e6edf3", lineWidth: 1, priceLineVisible: false, lastValueVisible: false },
        1,
      );
      macdSigRef.current = chart.addSeries(
        LineSeries,
        { color: "#ea3943", lineWidth: 1, priceLineVisible: false, lastValueVisible: false },
        1,
      );
      sigRef.current = "";           // force the next data push to repaint
    } else {
      for (const r of [macdRef, macdSigRef, macdHistRef]) {
        if (r.current) {
          try {
            chart.removeSeries(r.current as ISeriesApi<"Line">);
          } catch {
            /* pane already gone */
          }
          r.current = null;
        }
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [showMacd]);

  // Underlying invalidation levels as price lines. These are the ONLY position
  // levels in index points — the premium stop/targets belong to a different
  // scale entirely and would be meaningless drawn on a futures chart.
  const priceLinesRef = useRef<ReturnType<ISeriesApi<"Candlestick">["createPriceLine"]>[]>([]);
  useEffect(() => {
    const series = candleRef.current;
    if (!series) return;
    priceLinesRef.current.forEach((l) => {
      try {
        series.removePriceLine(l);
      } catch {
        /* already gone */
      }
    });
    priceLinesRef.current = positions
      .filter((p) => p.invalidation_level != null)
      .map((p) =>
        series.createPriceLine({
          price: p.invalidation_level as number,
          color: p.direction === "CE" ? "#16c784" : "#ea3943",
          lineWidth: 1,
          lineStyle: 2,
          axisLabelVisible: true,
          title: `${p.direction} invalidation`,
        }),
      );
  }, [positions]);

  // Push data on candle updates — but skip identical payloads: off-hours every
  // 2s poll returns the same 240 bars, and a full setData() would repaint all
  // series and jitter the crosshair/pan while the trader inspects candles.
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

    if (macdRef.current) {
      const m = macd(closes);
      macdRef.current.setData(line(candles, m.line));
      macdSigRef.current?.setData(line(candles, m.signal));
      // Green above zero / red below — momentum turning shows up here first.
      macdHistRef.current?.setData(
        candles
          .map((c, i) =>
            m.hist[i] === undefined
              ? null
              : {
                  time: (c.ts + IST_OFFSET) as UTCTimestamp,
                  value: m.hist[i] as number,
                  color: (m.hist[i] as number) >= 0 ? "#16c78488" : "#ea394388",
                },
          )
          .filter(Boolean) as { time: UTCTimestamp; value: number; color: string }[],
      );
    }
  }, [candles, showMacd]);

  return (
    <div className={`card relative flex flex-col overflow-hidden ${fill ? "h-full min-h-0" : ""}`}>
      <div className="flex shrink-0 items-center justify-between border-b border-edge px-3 py-1.5">
        <h3 className="text-sm font-medium">{title}</h3>
        <div className="flex items-center gap-3 text-[10px]">
          <span className="text-accent">VWAP</span>
          <span className="text-[#f5a623]">EMA9</span>
          <span className="text-[#a78bfa]">EMA20</span>
          <button
            onClick={() => setShowMacd((v) => !v)}
            title="MACD(12,26,9) on the near-month future — display only, not part of the signal score"
            className={`rounded border px-1.5 py-0.5 transition ${
              showMacd
                ? "border-accent/60 bg-accent/15 text-accent"
                : "border-edge bg-panel2 text-muted hover:text-white"
            }`}
          >
            MACD
          </button>
        </div>
      </div>
      {/* The chart container stays a DIRECT flex child sized by flex-1 — an
          extra positioned wrapper broke sizing and rendered the chart blank.
          The overlay is anchored to the card instead (see `relative` above). */}
      <div ref={containerRef} className={fill ? "min-h-0 w-full flex-1" : "h-[420px] w-full"} />

      {/* Floating position cards — live P&L and a one-click exit without
          leaving the chart. An overlay rather than a price-anchored label,
          because the premium levels aren't on this chart's scale. */}
      {positions.length > 0 && (
          <div className="pointer-events-none absolute right-3 top-10 z-10 flex flex-col gap-1.5">
            {positions.map((p) => {
              const pnl = p.pnl ?? 0;
              const up = pnl >= 0;
              return (
                <div
                  key={p.id}
                  className={`pointer-events-auto flex items-center gap-2 rounded-md border px-2.5 py-1.5 backdrop-blur ${
                    up ? "border-bull/50 bg-bull/10" : "border-bear/50 bg-bear/10"
                  }`}
                  title={`Entered ${istTime(p.entered_at)} @ ₹${fmt(p.entry_premium)} · SL ₹${fmt(p.trailing_sl)} · T1 ₹${fmt(p.target1)}`}
                >
                  <div className="leading-tight">
                    <div className="font-mono text-[11px] text-white/90">
                      {p.contract}
                      <span className="text-muted"> · {p.lots} lot{p.lots > 1 ? "s" : ""}</span>
                    </div>
                    <div className="flex items-center gap-1.5 font-mono text-[10px]">
                      <span className={up ? "text-bull" : "text-bear"}>₹{signed(pnl, 0)}</span>
                      <span className={up ? "text-bull" : "text-bear"}>({signed(p.pnl_pct, 1)}%)</span>
                      <span className="text-muted">in {istTime(p.entered_at)}</span>
                    </div>
                  </div>
                  {onExit && (
                    <button
                      onClick={() => onExit(p)}
                      disabled={busyId === p.id}
                      title="Record this position as exited (you exit in Kite — Tradewell places no orders)"
                      className="rounded bg-bear/80 px-2 py-0.5 text-[11px] font-medium text-white hover:bg-bear disabled:opacity-40"
                    >
                      {busyId === p.id ? "…" : "Exit"}
                    </button>
                  )}
                </div>
              );
            })}
        </div>
      )}
    </div>
  );
}
