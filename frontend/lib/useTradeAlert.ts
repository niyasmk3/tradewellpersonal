"use client";

import { useEffect, useRef } from "react";
import { Trade, TradeAction } from "./api";
import { chime, notify } from "./alerts";
import { fmt, signed } from "./format";

/** Recommendations worth interrupting the user for, and how they sound. */
const URGENT: Partial<Record<TradeAction, { label: string; kind: "good" | "urgent" }>> = {
  stop_loss_hit: { label: "Stop-loss hit — exit", kind: "urgent" },
  invalidated: { label: "Invalidated — exit", kind: "urgent" },
  time_exit: { label: "Time exit — close position", kind: "urgent" },
  target2_reached: { label: "Target 2 reached — book remaining", kind: "good" },
  book_partial: { label: "Target 1 — book partial, trail rest", kind: "good" },
};

const isOpen = (t: Trade) => t.status === "entered" || t.status === "partial";

/**
 * Alerts on TRADE events, not signals.
 *
 * Tradewell never exits a position — the monitor only changes its advice. Without
 * this, a stop-loss or target hit while the trader is away from the screen was
 * completely silent, which is the worst time to be silent.
 *
 * Fires once per (trade, transition). The first payload after load only primes
 * the seen-set, so reloading the page doesn't replay old states as fresh alerts.
 */
export function useTradeAlert(trades: Trade[] | null | undefined, muted: boolean) {
  const seen = useRef<Set<string>>(new Set());
  const primed = useRef(false);

  useEffect(() => {
    if (!trades) return;

    const events: { key: string; kind: "good" | "urgent"; title: string; body: string }[] = [];

    for (const t of trades.filter(isOpen)) {
      // Target 1: the monitor flips t1_hit and moves the stop to breakeven.
      // Tracked separately because a single-lot trade shows TRAIL_SL (which
      // persists) rather than a one-off recommendation.
      if (t.t1_hit) {
        const key = `${t.id}:t1`;
        if (!seen.current.has(key)) {
          seen.current.add(key);
          events.push({
            key,
            kind: "good",
            title: `Target 1 hit — ${t.contract}`,
            body: `LTP ₹${fmt(t.current_premium)} · P&L ₹${signed(t.pnl ?? 0, 0)} · stop moved to entry ₹${fmt(t.entry_premium)}`,
          });
        }
      }

      const rec = URGENT[t.recommendation];
      if (rec) {
        const key = `${t.id}:${t.recommendation}`;
        if (!seen.current.has(key)) {
          seen.current.add(key);
          events.push({
            key,
            kind: rec.kind,
            title: `${rec.label} — ${t.contract}`,
            body: `LTP ₹${fmt(t.current_premium)} · P&L ₹${signed(t.pnl ?? 0, 0)} · ${t.recommendation_note ?? ""}`.trim(),
          });
        }
      }
    }

    // First payload: record current state without alerting.
    if (!primed.current) {
      primed.current = true;
      return;
    }
    if (muted) return;

    for (const e of events) {
      chime(e.kind);
      notify(`Tradewell — ${e.title}`, e.body, e.key);
    }
  }, [trades, muted]);
}
