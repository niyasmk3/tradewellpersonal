"use client";

import { useEffect, useRef, useState } from "react";

/**
 * Polls an async fetcher on an interval and returns the latest value.
 * Re-subscribes whenever `deps` change (e.g. selected symbol / timeframe).
 * `enabled=false` pauses polling (used while unauthenticated).
 */
export function usePolling<T>(
  fetcher: () => Promise<T>,
  intervalMs: number,
  deps: unknown[],
  enabled = true,
) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;
  // Generation counter: bumped per tick AND per re-subscribe. A slow response
  // from an older generation must never overwrite newer data (out-of-order
  // resolutions), and a response from a previous deps-key (old symbol/tf) must
  // never render under the new key.
  const genRef = useRef(0);

  useEffect(() => {
    if (!enabled) return;
    let alive = true;
    // New subscription (deps changed): show loading state, not the previous
    // key's data masquerading as current.
    setData(null);
    setError(null);

    const tick = async () => {
      const gen = ++genRef.current;
      try {
        const v = await fetcherRef.current();
        if (alive && gen === genRef.current) {
          setData(v);
          setError(null);
        }
      } catch (e) {
        if (alive && gen === genRef.current) {
          setError(e instanceof Error ? e.message : String(e));
        }
      }
    };

    tick();
    const id = setInterval(tick, intervalMs);
    return () => {
      alive = false;
      genRef.current++; // invalidate any in-flight response from this subscription
      clearInterval(id);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [intervalMs, enabled, ...deps]);

  return { data, error };
}
