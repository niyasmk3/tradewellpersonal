"use client";

/** Shared alert primitives for signal + trade notifications. */

export type ChimeKind = "good" | "urgent";

/** WebAudio chime — no asset file. "urgent" is deliberately lower and
 *  repeated so a stop-loss doesn't sound like a target. */
export function chime(kind: ChimeKind = "good") {
  try {
    const Ctx =
      window.AudioContext ??
      (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext;
    const ctx = new Ctx();
    const play = (freq: number, at: number, dur: number) => {
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = "sine";
      osc.frequency.value = freq;
      gain.gain.setValueAtTime(0.0001, ctx.currentTime + at);
      gain.gain.exponentialRampToValueAtTime(0.22, ctx.currentTime + at + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + at + dur);
      osc.connect(gain).connect(ctx.destination);
      osc.start(ctx.currentTime + at);
      osc.stop(ctx.currentTime + at + dur + 0.05);
    };
    if (kind === "urgent") {
      play(523.25, 0, 0.18);
      play(415.3, 0.2, 0.18);
      play(349.23, 0.4, 0.32);
    } else {
      play(880, 0, 0.25);
      play(1318.5, 0.22, 0.35);
    }
    setTimeout(() => ctx.close(), 1600);
  } catch {
    /* audio unavailable (no user gesture yet) — the notification still fires */
  }
}

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
