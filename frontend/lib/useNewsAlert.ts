"use client";

import { useEffect, useRef } from "react";
import { NewsResponse } from "./api";
import { chime, notify } from "./alerts";

/**
 * Alerts on BREAKING market-moving headlines (impact >= NEWS_BREAKING_IMPACT).
 *
 * Exists because a US-Iran story moved NIFTY while the dashboard said nothing:
 * every configured feed was a *markets* desk, and news is only 10 of the 100
 * score points, so it could never surface through the signal alone. This gives
 * the headline its own channel — a heads-up, deliberately not a trade call.
 *
 * Uses the "urgent" tone: a geopolitical shock is a reason to look at the
 * screen, not a reason to celebrate.
 */
export function useNewsAlert(news: NewsResponse | null | undefined, muted: boolean) {
  const seen = useRef<Set<string>>(new Set());
  const primed = useRef(false);

  useEffect(() => {
    const items = news?.breaking ?? [];
    if (!news?.enabled) return;

    const fresh = items.filter((n) => !seen.current.has(n.id));
    fresh.forEach((n) => seen.current.add(n.id));

    // First payload only primes the set, so a reload doesn't replay old news.
    if (!primed.current) {
      primed.current = true;
      return;
    }
    if (muted) return;

    for (const n of fresh) {
      chime("urgent");
      notify(
        `Breaking — ${n.affected_market} ${n.sentiment}`,
        `${n.title}\n\nimpact ${n.impact_score} · ${n.event_type} · ${n.source}`,
        n.id,
      );
    }
  }, [news, muted]);
}
