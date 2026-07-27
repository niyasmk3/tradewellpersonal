"use client";

import { useEffect, useMemo, useState } from "react";
import dynamic from "next/dynamic";
import { Timeframe, TradingMode, api } from "@/lib/api";
import { istTime } from "@/lib/format";
import { useLiveData } from "@/lib/useLiveData";
import { usePolling } from "@/lib/usePolling";
import { SignalHistoryPanel } from "./SignalHistoryPanel";
import { useSignalAlert } from "@/lib/useSignalAlert";
import { useTradeAlert } from "@/lib/useTradeAlert";
import { useNewsAlert } from "@/lib/useNewsAlert";
import { MarketStatusBar } from "./MarketStatusBar";
import { CommandStrip } from "./CommandStrip";
import { ContextRail } from "./ContextRail";
import { IndicatorPanel } from "./IndicatorPanel";
import { OptionChainTable } from "./OptionChainTable";
import { SignalPanel } from "./SignalPanel";
import { MarketPulsePanel } from "./MarketPulsePanel";
import { TradesPanel, TradeJournal, realizedSummary } from "./TradesPanel";
import { NewsPanel } from "./NewsPanel";
import { BacktestPanel } from "./BacktestPanel";
import { QtyCalculator } from "./QtyCalculator";
import { RiskSettings } from "./RiskSettings";
import { PaperPanel } from "./PaperPanel";

// lightweight-charts touches the DOM — load the chart client-side only.
const PriceChart = dynamic(() => import("./PriceChart").then((m) => m.PriceChart), {
  ssr: false,
  loading: () => <div className="card min-h-0 flex-1 animate-pulse bg-panel" />,
});

const TIMEFRAMES: Timeframe[] = ["1m", "3m", "5m", "15m"];
const MODES: { key: TradingMode; label: string }[] = [
  { key: "intraday", label: "Intraday" },
  // Appears only when SIGNAL_MODES includes it; cards are paper-only either way.
  { key: "scalp", label: "Scalp" },
  { key: "positional", label: "Positional" },
];

export function Dashboard() {
  const { snapshot, status } = useLiveData();
  const [symbol, setSymbol] = useState("NIFTY");
  const [tf, setTf] = useState<Timeframe>("3m");
  const [mode, setMode] = useState<TradingMode>("intraday");

  const candles = usePolling(() => api.candles(symbol, tf), 2000, [symbol, tf]);
  const indicators = usePolling(() => api.indicators(symbol, tf), 2000, [symbol, tf]);
  const chain = usePolling(() => api.optionChain(symbol), 3000, [symbol]);
  const enabledModes = usePolling(() => api.modes(), 30000, []);

  // Signals run on NIFTY only. Both modes are polled ALWAYS — regardless of the
  // viewed symbol/mode — so alerts can fire while you watch another chart.
  const modesOn = enabledModes.data?.modes ?? ["intraday", "positional"];
  // Bumped after a re-price so the refreshed levels appear immediately rather
  // than on the next 3s poll.
  const [sigTick, setSigTick] = useState(0);
  const intradaySig = usePolling(
    () => api.signal("NIFTY", "intraday"), 3000, [sigTick], modesOn.includes("intraday"),
  );
  const positionalSig = usePolling(
    () => api.signal("NIFTY", "positional"), 3000, [sigTick], modesOn.includes("positional"),
  );
  // Scalp cards live 180s — if this poll didn't exist, a scalp card could be
  // born, pushed to the phone and expire without the dashboard ever showing it
  // (and the Scalp toggle would silently render the INTRADAY card instead).
  const scalpSig = usePolling(
    () => api.signal("NIFTY", "scalp"), 3000, [sigTick], modesOn.includes("scalp"),
  );
  const signalRes =
    mode === "positional" ? positionalSig : mode === "scalp" ? scalpSig : intradaySig;
  const news = usePolling(() => api.news(), 15000, []);
  const mood = usePolling(() => api.marketMood(), 60000, []);
  // Simulated book — polled slowly; it only changes when a signal fires.
  const paper = usePolling(() => api.paperSummary(), 15000, []);
  const signalHist = usePolling(() => api.signalHistory(symbol), 15000, [symbol]);
  // Live tape analytics under the score card — cheap, so polled at chart cadence.
  const marketPulse = usePolling(() => api.pulse(symbol), 3000, [symbol]);

  // Trades are global (not per-symbol). `tradesTick` forces an immediate refetch after an action.
  const [tradesTick, setTradesTick] = useState(0);
  const trades = usePolling(() => api.trades(), 2000, [tradesTick]);
  const refreshTrades = () => setTradesTick((n) => n + 1);

  // Locally dismiss ("Ignore") the current signal until a new one is issued.
  const [dismissed, setDismissed] = useState<string | null>(null);

  // Exit straight from the chart overlay. Records the exit in the journal at
  // the live premium — Tradewell still places no orders, you exit in Kite.
  const [exitingId, setExitingId] = useState<string | null>(null);
  const exitPosition = async (t: { id: string; contract: string }) => {
    if (!window.confirm(`Record ${t.contract} as exited at the live premium?`)) return;
    setExitingId(t.id);
    try {
      await api.exitTrade(t.id);
      refreshTrades();
    } catch (e) {
      alert(e instanceof Error ? e.message : "could not record the exit");
    } finally {
      setExitingId(null);
    }
  };

  // Only show toggles the backend actually serves; fall back to the default pair.
  const available = useMemo(() => {
    const enabled = enabledModes.data?.modes ?? ["intraday", "positional"];
    return MODES.filter((m) => enabled.includes(m.key));
  }, [enabledModes.data]);

  // If the selected mode gets disabled, snap to the first available one.
  useEffect(() => {
    if (available.length > 0 && !available.some((m) => m.key === mode)) {
      setMode(available[0].key);
    }
  }, [available, mode]);

  const underlying = useMemo(
    () => snapshot?.underlyings.find((u) => u.symbol === symbol) ?? null,
    [snapshot, symbol],
  );

  // Apply the local "Ignore" dismissal to the current signal.
  const signalData = useMemo(() => {
    const d = signalRes.data;
    if (d?.signal && d.signal.id === dismissed) {
      return { ...d, signal: null, action: "avoid" as const, no_trade_reason: "Signal ignored" };
    }
    return d;
  }, [signalRes.data, dismissed]);

  // Sound + desktop notification when a new tradeable signal appears in EITHER
  // mode — fed from the always-on polls, not the view-scoped one.
  const alerts = useSignalAlert([
    intradaySig.data?.signal,
    positionalSig.data?.signal,
    scalpSig.data?.signal,
  ]);
  // Tradewell never exits a position — so a stop-loss/target/invalidation hit
  // while the trader is away from the screen must make a noise.
  useTradeAlert(trades.data, alerts.muted);
  // Breaking macro/geopolitical headlines get their own channel: news is only
  // 10 of the 100 score points, so it can never surface via the signal alone.
  useNewsAlert(news.data, alerts.muted);

  const chartTitle = `${symbol} Futures · ${tf}`;

  // Context rail: tabs stay user-controlled (never auto-switch — the layout must
  // not move under the cursor mid-session).
  const [ctxTab, setCtxTab] = useState("chain");
  const [riskOpen, setRiskOpen] = useState(false);
  const allTrades = trades.data ?? [];
  const openTrades = allTrades.filter((t) => t.status === "entered" || t.status === "partial");
  const hasOpen = openTrades.length > 0;
  const realized = realizedSummary(allTrades);

  const ctxTabs = [
    { key: "chain", label: "Chain", node: <OptionChainTable chain={chain.data} bare /> },
    {
      key: "news",
      label: "News",
      node: <NewsPanel data={news.data} error={news.error} bare />,
      badge: (news.data?.items ?? []).some((n) => n.is_market_moving),
    },
    { key: "journal", label: "Journal", node: <TradeJournal trades={allTrades} onChange={refreshTrades} /> },
    {
      key: "paper",
      label: "Paper",
      node: <PaperPanel data={paper.data} error={paper.error} />,
      badge: (paper.data?.open ?? 0) > 0,
    },
    {
      key: "signals",
      label: "Signals",
      node: <SignalHistoryPanel data={signalHist.data} error={signalHist.error} />,
      // A card that retired UNTAKEN in the last hour is exactly the "did I
      // miss something?" case this tab exists for — surface it as a dot.
      badge: (signalHist.data?.rows ?? []).some(
        (r) =>
          r.state !== "active" &&
          r.taken === null &&
          Date.now() / 1000 - r.created_at < 3600,
      ),
    },
    ...(symbol === "NIFTY"
      ? [{ key: "backtest", label: "Backtest", node: <BacktestPanel symbol={symbol} /> }]
      : []),
    // Lot/qty affordability calculator (the "tradewell Qty.xlsx" sheet, live).
    { key: "calc", label: "Calc", node: <QtyCalculator /> },
  ];

  return (
    // Fixed application shell on desktop: nothing scrolls the page, panels
    // scroll internally. Below lg it degrades to a normal scrolling column.
    <div className="flex min-h-screen flex-col bg-[#0a0e13] lg:h-screen lg:overflow-hidden">
      <MarketStatusBar
        snapshot={snapshot}
        wsStatus={status}
        selected={symbol}
        onSelect={setSymbol}
        mood={mood.data}
        news={news.data}
      />

      <CommandStrip
        status={symbol === "NIFTY" ? (signalRes.data?.status ?? null) : null}
        showRegime={symbol === "NIFTY"}
        mode={mode}
        available={available}
        onMode={setMode}
        tf={tf}
        timeframes={TIMEFRAMES}
        onTf={setTf}
        chartError={candles.error}
        alerts={alerts}
        realizedToday={realized.today}
        realizedTotal={realized.total}
        onRiskSettings={() => setRiskOpen(true)}
        className="mx-2 mt-2 shrink-0"
      />

      {/* Breaking macro headline — a chime is missable, a red bar is not. */}
      {(news.data?.breaking ?? []).length > 0 && (
        <a
          href={news.data!.breaking[0].link || undefined}
          target="_blank"
          rel="noreferrer"
          title={`${news.data!.breaking[0].summary}\n\nimpact ${news.data!.breaking[0].impact_score} · ${news.data!.breaking[0].event_type} · ${news.data!.breaking[0].source}`}
          className="mx-2 mt-2 flex shrink-0 items-center gap-2 rounded-lg border border-bear/50 bg-bear/10 px-3 py-1.5 hover:border-bear"
        >
          <span className="tag bg-bear/20 text-bear">BREAKING</span>
          <span className="tag bg-panel2 text-muted">{news.data!.breaking[0].affected_market}</span>
          <span className="truncate text-xs text-white/90">{news.data!.breaking[0].title}</span>
          <span className="ml-auto whitespace-nowrap text-[10px] text-muted">
            impact {news.data!.breaking[0].impact_score} · {istTime(news.data!.breaking[0].published)}
          </span>
        </a>
      )}

      {/* Three rails: DECISION | MARKET | CONTEXT.
          <lg  : single scrolling column (accepted mobile/tablet degradation)
          lg   : 2 cols + context as a 240px full-width bottom dock
          xl+  : 3 rails, no page scroll */}
      <main
        className="grid min-h-0 flex-1 grid-cols-1 gap-2 overflow-y-auto p-2
                   lg:grid-cols-[minmax(320px,360px)_minmax(0,1fr)] lg:grid-rows-[minmax(0,1fr)_240px] lg:overflow-hidden
                   xl:grid-cols-[380px_minmax(0,1fr)_400px] xl:grid-rows-1
                   2xl:grid-cols-[420px_minmax(0,1fr)_440px]"
      >
        {/* RAIL 1 — DECISION. Open positions take the top slot whenever money
            is at risk; the signal card keeps the rest and is never removed. */}
        <section className="flex min-h-0 min-w-0 flex-col gap-2">
          {(hasOpen || trades.error) && (
            <TradesPanel
              trades={allTrades}
              error={trades.error}
              onChange={refreshTrades}
              className="max-h-[45%] shrink-0"
            />
          )}
          <SignalPanel
            data={signalData}
            symbol={symbol}
            mode={mode}
            error={signalRes.error}
            onEntered={refreshTrades}
            onIgnore={(id) => setDismissed(id)}
            onReprice={() => setSigTick((n) => n + 1)}
            className="min-h-0 flex-1"
          />
          {/* What the market is DOING while you read the score above. */}
          <MarketPulsePanel data={marketPulse.data} error={marketPulse.error} />
        </section>

        {/* RAIL 2 — MARKET. Chart absorbs all leftover height (~700px vs the
            old fixed 420px), indicators as one strip directly beneath it. */}
        <section className="flex min-h-0 min-w-0 flex-col gap-2">
          <PriceChart
            candles={candles.data ?? []}
            title={chartTitle}
            fill
            positions={openTrades}
            busyId={exitingId}
            onExit={exitPosition}
          />
          <IndicatorPanel ind={indicators.data} underlying={underlying} className="shrink-0" />
        </section>

        {/* RAIL 3 — CONTEXT. Mutually-exclusive reference panels as tabs. */}
        <section className="flex min-h-0 min-w-0 flex-col lg:col-span-2 xl:col-span-1">
          <ContextRail tabs={ctxTabs} tab={ctxTab} onTab={setCtxTab} className="min-h-0 flex-1" />
        </section>
      </main>

      {riskOpen && <RiskSettings onClose={() => setRiskOpen(false)} />}
    </div>
  );
}
