"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { SignalCard } from "./api";
import { chime, notify } from "./alerts";

const MUTE_KEY = "tradewell.alerts.muted";

/**
 * Alerts (sound + desktop notification) whenever a NEW tradeable signal card
 * appears in ANY of the watched slots (both trading modes — alerts must fire
 * even while you're viewing another symbol or mode).
 */
export function useSignalAlert(signals: Array<SignalCard | null | undefined>) {
  const [muted, setMuted] = useState(true); // safe default until localStorage loads
  const alerted = useRef<Set<string>>(new Set());

  useEffect(() => {
    setMuted(localStorage.getItem(MUTE_KEY) === "1" ? true : localStorage.getItem(MUTE_KEY) === "0" ? false : true);
  }, []);

  const toggle = useCallback(() => {
    setMuted((m) => {
      const next = !m;
      localStorage.setItem(MUTE_KEY, next ? "1" : "0");
      // Permission prompt must come from a user gesture — the toggle click is one.
      try {
        if (!next && "Notification" in window && Notification.permission === "default") {
          Notification.requestPermission();
        }
      } catch {
        /* notification API unavailable — sound-only */
      }
      return next;
    });
  }, []);

  // Re-run when any watched card's identity/state changes, not on every render.
  const idsKey = signals.map((s) => (s ? `${s.id}:${s.state}:${s.action}` : "-")).join("|");

  useEffect(() => {
    if (muted) return;
    for (const signal of signals) {
      if (!signal) continue;
      if (signal.action !== "buy_ce" && signal.action !== "buy_pe") continue;
      if (signal.state !== "active") continue;
      if (alerted.current.has(signal.id)) continue;
      alerted.current.add(signal.id);
      chime("good");
      notify(
        `Tradewell — ${signal.title}`,
        `${signal.contract} · entry ₹${signal.entry_low}–${signal.entry_high} · SL ₹${signal.premium_sl} · T1 ₹${signal.target1} · score ${signal.confidence}`,
        signal.id,
      );
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [idsKey, muted]);

  return { muted, toggle };
}
