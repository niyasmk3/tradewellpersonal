"use client";

import { useState } from "react";
import { MarketMood, MarketSnapshot, NewsResponse, UnderlyingSnapshot, api } from "@/lib/api";
import { fmt, signed, istTime } from "@/lib/format";
import { Logo } from "./Logo";

function zoneTone(zone: string): string {
  switch (zone) {
    case "Extreme Fear": return "text-red-400";
    case "Fear": return "text-orange-400";
    case "Greed": return "text-teal-300";
    case "Extreme Greed": return "text-emerald-400";
    default: return "text-muted";
  }
}

/** Market Mood Index — contrarian Fear/Greed gauge, always visible. */
function MoodChip({ mood }: { mood: MarketMood | null | undefined }) {
  if (!mood) return null;
  const pct = Math.max(0, Math.min(100, mood.value));
  return (
    <span
      className="flex items-center gap-1.5"
      title={`Market Mood Index ${mood.value} — ${mood.zone}. Contrarian gauge (VIX, momentum, FII, skew). Context only — not used in signal scoring. Source: ${mood.source}`}
    >
      <span className="text-muted">MMI</span>
      <span className={`font-mono ${zoneTone(mood.zone)}`}>{mood.value.toFixed(1)}</span>
      <span className={`hidden lg:inline ${zoneTone(mood.zone)}`}>{mood.zone}</span>
      <span
        className="relative h-1.5 w-9 rounded-full"
        style={{ background: "linear-gradient(90deg,#ef4444 0%,#f59e0b 33%,#14b8a6 66%,#10b981 100%)" }}
      >
        <span
          className="absolute -top-[3px] h-[9px] w-[2px] rounded bg-white"
          style={{ left: `calc(${pct}% - 1px)` }}
        />
      </span>
    </span>
  );
}

/** Claude news sentiment for the SELECTED index. */
function NewsChip({ news, symbol }: { news: NewsResponse | null | undefined; symbol: string }) {
  if (!news?.enabled) return null;
  const s = news.sentiments.find((x) => x.symbol === symbol) ?? news.sentiments[0];
  if (!s) return null;

  const tone =
    s.label === "bullish" ? "text-bull"
    : s.label === "bearish" ? "text-bear"
    : s.label === "volatile" ? "text-yellow-400"
    : "text-muted";
  const quiet = s.items_considered === 0;

  return (
    <span
      className="flex items-center gap-1.5"
      title={
        quiet
          ? "No market-moving news in the lookback window — news scores neutral"
          : `${s.items_considered} market-moving headline(s) for ${s.symbol}: ${s.label} (net ${s.net_score >= 0 ? "+" : ""}${s.net_score.toFixed(0)})\n\n${s.top_headlines.slice(0, 3).join("\n")}`
      }
    >
      <span className="text-muted">News</span>
      {quiet ? (
        <span className="text-muted">quiet</span>
      ) : (
        <>
          <span className={`font-mono ${tone}`}>
            {s.net_score >= 0 ? "+" : ""}
            {s.net_score.toFixed(0)}
          </span>
          <span className={`hidden lg:inline ${tone}`}>{s.label}</span>
          <span className="text-muted">({s.items_considered})</span>
        </>
      )}
    </span>
  );
}

function UnderlyingChip({
  u,
  active,
  onClick,
}: {
  u: UnderlyingSnapshot;
  active: boolean;
  onClick: () => void;
}) {
  const up = (u.change ?? 0) >= 0;
  // One line instead of three: the vertical chip spent ~56px saying what fits
  // in a single row, and the header is fixed chrome in the single-screen shell.
  return (
    <button
      onClick={onClick}
      title={`${u.symbol} ${fmt(u.ltp)} (${signed(u.change_pct)}%)`}
      className={`flex items-baseline gap-1.5 whitespace-nowrap rounded-md border px-2 py-1 transition ${
        active ? "border-accent bg-panel2" : "border-edge bg-panel hover:border-muted/50"
      }`}
    >
      <span className="text-[10px] uppercase tracking-wide text-muted">{u.symbol}</span>
      <span className="font-mono text-xs">{fmt(u.ltp)}</span>
      <span className={`font-mono text-[10px] ${up ? "text-bull" : "text-bear"}`}>
        {signed(u.change_pct)}%
      </span>
    </button>
  );
}

export function MarketStatusBar({
  snapshot,
  wsStatus,
  selected,
  onSelect,
  mood,
  news,
}: {
  snapshot: MarketSnapshot | null;
  wsStatus: string;
  selected: string;
  onSelect: (s: string) => void;
  mood?: MarketMood | null;
  news?: NewsResponse | null;
}) {
  const open = snapshot?.market_open;
  const vix = snapshot?.vix;
  const [restarting, setRestarting] = useState(false);

  const restartFeed = async () => {
    setRestarting(true);
    try {
      await api.restartFeed();
    } catch (e) {
      alert(e instanceof Error ? e.message : "Feed restart failed");
    } finally {
      setRestarting(false);
    }
  };

  return (
    <header className="shrink-0 border-b border-edge bg-[#0a0e13]/95 backdrop-blur">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 px-3 py-1.5">
        <div className="flex items-center gap-2">
          <Logo size={22} />
          <span className="text-base font-semibold tracking-tight">
            Trade<span className="text-[#2dd4bf]">well</span>
          </span>
        </div>

        {/* index selectors inline — the second header row is gone */}
        {(snapshot?.underlyings ?? []).map((u) => (
          <UnderlyingChip key={u.symbol} u={u} active={u.symbol === selected} onClick={() => onSelect(u.symbol)} />
        ))}
        {(!snapshot || snapshot.underlyings.length === 0) && (
          <span className="text-xs text-muted">Waiting for market data…</span>
        )}

        <span
          className={`tag ${
            open ? "bg-bull/15 text-bull" : "bg-bear/15 text-bear"
          }`}
        >
          <span className={`mr-1 h-1.5 w-1.5 rounded-full ${open ? "bg-bull" : "bg-bear"}`} />
          {open ? "Market Open" : "Market Closed"}
        </span>

        <div className="ml-auto flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-muted">
          {vix?.ltp != null && (
            <span className="font-mono">
              India VIX <span className="text-white">{fmt(vix.ltp)}</span>{" "}
              <span className={(vix.change_pct ?? 0) >= 0 ? "text-bear" : "text-bull"}>
                {signed(vix.change_pct)}%
              </span>{" "}
              <span className="text-muted">· {vix.status}</span>
            </span>
          )}
          <MoodChip mood={mood} />
          <NewsChip news={news} symbol={selected} />
          <span className="font-mono">{istTime(snapshot?.server_time)} IST</span>
          {/* "live" requires BOTH the WS connection AND fresh backend ticks —
              a connected socket to a dead feed must read as stale, not live. */}
          {wsStatus === "open" && snapshot?.feed_stale ? (
            <span className="flex items-center gap-1">
              <span
                className="tag bg-bear/15 text-bear"
                title={`No ticks from Kite${snapshot?.last_tick_age != null ? ` for ${snapshot.last_tick_age}s` : ""}`}
              >
                feed stale
              </span>
              <button
                onClick={restartFeed}
                disabled={restarting}
                title="Rebuild the Kite ticker on the current session (no re-login needed)"
                className="rounded border border-edge bg-panel2 px-1.5 py-0.5 text-[10px] text-white hover:border-accent disabled:opacity-50"
              >
                {restarting ? "restarting…" : "restart feed"}
              </button>
            </span>
          ) : (
            <span
              className={`tag ${
                wsStatus === "open" ? "bg-bull/15 text-bull" : "bg-yellow-500/15 text-yellow-400"
              }`}
            >
              {wsStatus === "open" ? "live" : wsStatus}
            </span>
          )}
        </div>
      </div>
    </header>
  );
}
