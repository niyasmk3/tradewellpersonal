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
  // Starts muted only so the first render matches the server's, then resolves
  // from storage below. It is NOT the preference default — see the effect.
  const [muted, setMuted] = useState(true);
  const alerted = useRef<Set<string>>(new Set());

  useEffect(() => {
    // ALERTS ARE ON UNTIL YOU TURN THEM OFF. They used to default to muted, so
    // an untouched install was silent forever — which is how the 92.9 card on
    // 22-Jul expired unseen. Only an explicit "1" mutes now.
    setMuted(localStorage.getItem(MUTE_KEY) === "1");
  }, []);

  // Notification permission can only be requested from a user gesture, so a
  // default-on alert cannot ask for it at load. Piggyback on the first click or
  // keypress anywhere in the page — sound still works without it.
  useEffect(() => {
    if (muted) return;
    if (!("Notification" in window) || Notification.permission !== "default") return;
    const ask = () => {
      try {
        Notification.requestPermission();
      } catch {
        /* notification API unavailable — sound-only */
      }
    };
    window.addEventListener("pointerdown", ask, { once: true });
    window.addEventListener("keydown", ask, { once: true });
    return () => {
      window.removeEventListener("pointerdown", ask);
      window.removeEventListener("keydown", ask);
    };
  }, [muted]);

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
