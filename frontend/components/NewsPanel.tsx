"use client";

import { AnalyzedNews, NewsResponse } from "@/lib/api";
import { istTime } from "@/lib/format";

function sentTone(s: string): string {
  if (s === "positive") return "text-bull";
  if (s === "negative") return "text-bear";
  return "text-muted";
}

function sentBg(s: string): string {
  if (s === "positive") return "bg-bull/15 text-bull";
  if (s === "negative") return "bg-bear/15 text-bear";
  if (s === "volatile") return "bg-yellow-500/15 text-yellow-400";
  return "bg-panel2 text-muted";
}

function NewsRow({ n }: { n: AnalyzedNews }) {
  return (
    <a
      href={n.link || undefined}
      target="_blank"
      rel="noreferrer"
      className="block rounded-md border border-edge/60 bg-panel2 px-3 py-2 hover:border-muted/50"
    >
      <div className="flex items-start gap-2">
        <span className={`tag ${sentBg(n.sentiment)} mt-0.5 shrink-0`}>{n.sentiment}</span>
        <div className="min-w-0">
          <div className="truncate text-xs text-white/90">{n.title}</div>
          <div className="mt-0.5 flex flex-wrap items-center gap-x-2 text-[10px] text-muted">
            <span className="text-white/70">{n.affected_market}</span>
            <span>· impact {n.impact_score}</span>
            <span>· {n.event_type}</span>
            {n.is_market_moving && <span className="text-yellow-400">· market-moving</span>}
            <span>· {n.source}</span>
            <span>· {istTime(n.published)}</span>
          </div>
        </div>
      </div>
    </a>
  );
}

export function NewsPanel({
  data,
  error,
  bare = false,
}: {
  data: NewsResponse | null;
  error?: string | null;
  bare?: boolean;
}) {
  // bare: inside ContextRail (card chrome + title come from the tab).
  const box = bare ? "p-3" : "card p-3";
  if (error && !data) {
    return <div className={`${box} text-xs text-bear`}>News feed unavailable — {error}</div>;
  }
  if (data && !data.enabled) {
    return (
      <div className={box}>
        {!bare && <h3 className="mb-1 text-sm font-medium">News Intelligence</h3>}
        <p className="text-xs text-muted">{data.note}</p>
      </div>
    );
  }
  if (!data) return null;

  return (
    <div className={box}>
      <div className="mb-2 flex items-center justify-between">
        {!bare && <h3 className="text-sm font-medium">News Intelligence</h3>}
        <span className={`text-[10px] text-muted ${bare ? "ml-auto" : ""}`}>Claude sentiment</span>
      </div>

      {/* per-index sentiment chips */}
      <div className="mb-2 flex flex-wrap gap-2">
        {data.sentiments.map((s) => (
          <div key={s.symbol} className="flex items-center gap-1.5 rounded-md border border-edge bg-panel2 px-2 py-1">
            <span className="text-[10px] uppercase text-muted">{s.symbol}</span>
            <span className={`tag ${sentBg(s.label)}`}>{s.label}</span>
            {s.items_considered > 0 && (
              <span className={`font-mono text-[11px] ${sentTone(s.net_score >= 0 ? "positive" : "negative")}`}>
                {s.net_score >= 0 ? "+" : ""}
                {s.net_score.toFixed(0)}
              </span>
            )}
          </div>
        ))}
      </div>

      <div className={`space-y-1.5 ${bare ? "" : "max-h-[280px] overflow-y-auto scroll-thin"}`}>
        {data.items.length === 0 ? (
          <div className="py-4 text-center text-xs text-muted">Scanning feeds…</div>
        ) : (
          data.items.map((n) => <NewsRow key={n.id} n={n} />)
        )}
      </div>
    </div>
  );
}
