"use client";

import { SignalHistoryRow } from "@/lib/api";
import { istDateTime, istDayKey, istTime } from "@/lib/format";

/**
 * Every card the engine issued lately — the record of what came and went.
 *
 * Exists because a signal you never saw is otherwise invisible: the card
 * expires, the slot clears, and the day's best setup leaves no trace on the
 * dashboard (22-Jul: the highest-scoring card ever, issued and expired in 8
 * minutes while nobody watched). This tab is the trace. Each row shows what
 * the CARD did (expired / cancelled / invalidated) and — separately — whether
 * YOU acted on it, so "missed" is a visible state rather than a silence.
 */

const STATE_STYLE: Record<string, string> = {
  active: "bg-bull/15 text-bull",
  expired: "bg-panel text-muted",
  cancelled: "bg-yellow-500/15 text-yellow-400",
  invalidated: "bg-bear/15 text-bear",
};

const TAKEN_STYLE: Record<string, string> = {
  live: "bg-accent/15 text-accent",
  paper: "bg-panel2 text-white/70",
  both: "bg-accent/15 text-accent",
};

export function SignalHistoryPanel({
  data,
  error,
}: {
  data: { rows: SignalHistoryRow[]; count: number } | null;
  error?: string | null;
}) {
  if (error) {
    return (
      <div className="py-3 text-center text-xs text-muted">
        Signal history unavailable — {error}.
        <div className="mt-1 text-[10px]">
          If you updated Tradewell just now, restart the backend to enable this tab.
        </div>
      </div>
    );
  }
  if (!data) return <div className="py-3 text-center text-xs text-muted">Loading…</div>;
  if (data.rows.length === 0) {
    return (
      <div className="py-3 text-center text-xs text-muted">
        No cards issued yet — every signal the engine offers will be listed here,
        including the ones that expire while you are away.
      </div>
    );
  }

  const missed = data.rows.filter((r) => r.state !== "active" && r.taken === null).length;

  return (
    <div className="text-xs">
      <div className="mb-2 flex items-center gap-2 text-[10px] text-muted">
        <span>{data.rows.length} card(s) on record</span>
        {missed > 0 && (
          <span className="tag bg-yellow-500/10 text-yellow-400">
            {missed} came &amp; went untaken
          </span>
        )}
      </div>
      <div className="space-y-1">
        {data.rows.map((r) => (
          <div key={r.id} className="rounded border border-edge/60 bg-panel2 px-2 py-1">
            <div className="flex items-center gap-2">
              <span className={`tag ${r.direction === "CE" ? "bg-bull/15 text-bull" : "bg-bear/15 text-bear"}`}>
                {r.direction}
              </span>
              <span className="truncate font-mono text-muted">{r.contract}</span>
              <span className="tag bg-panel text-[9px] text-muted">{r.mode}</span>
              <span className="ml-auto font-mono text-white/80">{r.score.toFixed(0)}</span>
              <span className={`tag text-[9px] ${STATE_STYLE[r.state] ?? "bg-panel text-muted"}`}>
                {r.state}
              </span>
              {r.taken ? (
                <span className={`tag text-[9px] ${TAKEN_STYLE[r.taken]}`}>{r.taken}</span>
              ) : (
                r.state !== "active" && (
                  <span className="tag bg-panel text-[9px] text-muted/70">not taken</span>
                )
              )}
            </div>
            <div className="mt-0.5 flex flex-wrap gap-x-2 font-mono text-[10px] text-muted">
              <span>{istDateTime(r.created_at)}</span>
              <span>
                valid till{" "}
                {istDayKey(r.valid_until) !== istDayKey(r.created_at)
                  ? istDateTime(r.valid_until)
                  : istTime(r.valid_until)}
              </span>
              <span className="text-white/70">
                zone ₹{r.entry_low}–{r.entry_high}
              </span>
              <span>SL ₹{r.premium_sl}</span>
              <span>T1 ₹{r.target1}</span>
            </div>
          </div>
        ))}
      </div>
      <div className="mt-2 text-[10px] text-muted">
        Last 50 cards per mode. &ldquo;not taken&rdquo; means neither the journal nor the
        paper book has a trade on that card.
      </div>
    </div>
  );
}
