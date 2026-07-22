"use client";

/** Shared alert primitives for signal + trade notifications.
 *
 * The chime is now a selectable tone pack (see ./tones). This module keeps
 * `chime` / `ChimeKind` re-exported so the alert hooks import from one place. */

export type { ChimeKind } from "./tones";
export { chime } from "./tones";

/** Desktop notification. Guarded: on Chrome for Android the page-context
 *  Notification constructor throws even when permission is granted, and an
 *  uncaught throw inside a React effect would unmount the dashboard. */
export function notify(title: string, body: string, tag: string) {
  try {
    if (!("Notification" in window) || Notification.permission !== "granted") return;
    const n = new Notification(title, { body, tag });
    n.onclick = () => window.focus();
  } catch {
    /* sound-only fallback */
  }
}
