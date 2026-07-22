"use client";

/**
 * Alert tone packs, synthesized with WebAudio — no asset files, so nothing to
 * download and no CSP/host concerns.
 *
 * Each pack defines two sequences:
 *   good   — a new tradeable signal appeared
 *   urgent — a stop-loss / invalidation / time-exit fired on an open position
 *
 * The good/urgent split is not decoration: a trader must be able to tell "an
 * opportunity" from "get out" without looking at the screen. So in EVERY pack
 * `urgent` is lower and/or falling and/or repeated, and `good` rises. Adding a
 * pack whose two tones sound alike would defeat the point.
 */

export type ChimeKind = "good" | "urgent";

interface Note {
  freq: number;
  at: number;        // seconds from sequence start
  dur: number;
  type?: OscillatorType;
  gain?: number;     // peak gain, default 0.22
}

export interface TonePack {
  id: string;
  label: string;
  hint: string;
  good: Note[];
  urgent: Note[];
}

// Descending, repeated, or low = "urgent" across the board — see the module doc.
export const TONE_PACKS: TonePack[] = [
  {
    id: "bells",
    label: "Bells",
    hint: "The original — bright rising sine bells",
    good: [
      { freq: 880, at: 0, dur: 0.25 },
      { freq: 1318.5, at: 0.22, dur: 0.35 },
    ],
    urgent: [
      { freq: 523.25, at: 0, dur: 0.18 },
      { freq: 415.3, at: 0.2, dur: 0.18 },
      { freq: 349.23, at: 0.4, dur: 0.32 },
    ],
  },
  {
    id: "rise",
    label: "Rise",
    hint: "A quick three-note arpeggio — distinctive, hard to miss",
    good: [
      { freq: 587.33, at: 0, dur: 0.14, type: "triangle" },   // D5
      { freq: 783.99, at: 0.12, dur: 0.14, type: "triangle" }, // G5
      { freq: 1046.5, at: 0.24, dur: 0.3, type: "triangle" },  // C6
    ],
    urgent: [
      { freq: 440, at: 0, dur: 0.16, type: "triangle" },
      { freq: 349.23, at: 0.18, dur: 0.16, type: "triangle" },
      { freq: 261.63, at: 0.36, dur: 0.34, type: "triangle" }, // C4, low
    ],
  },
  {
    id: "marimba",
    label: "Marimba",
    hint: "Warm wooden triad — softer, less piercing",
    good: [
      { freq: 523.25, at: 0, dur: 0.16, type: "triangle" },
      { freq: 659.25, at: 0.1, dur: 0.16, type: "triangle" },
      { freq: 783.99, at: 0.2, dur: 0.3, type: "triangle" },
    ],
    urgent: [
      { freq: 392, at: 0, dur: 0.22, type: "triangle" },
      { freq: 293.66, at: 0.24, dur: 0.22, type: "triangle" },
      { freq: 392, at: 0.5, dur: 0.28, type: "triangle" },     // repeat = insistent
    ],
  },
  {
    id: "ping",
    label: "Ping",
    hint: "One clean ping with an echo — minimal",
    good: [
      { freq: 1244.5, at: 0, dur: 0.22 },
      { freq: 1244.5, at: 0.26, dur: 0.3, gain: 0.1 },         // echo
    ],
    urgent: [
      { freq: 466.16, at: 0, dur: 0.22 },
      { freq: 466.16, at: 0.26, dur: 0.22, gain: 0.14 },
      { freq: 466.16, at: 0.52, dur: 0.28, gain: 0.1 },        // low, triple = alarm
    ],
  },
];

const _byId = Object.fromEntries(TONE_PACKS.map((p) => [p.id, p]));
const STORE_KEY = "tradewell.alerts.tone";
const DEFAULT_ID = "rise";     // a NEW signature, so the change is audible at once

export function getToneId(): string {
  try {
    const id = localStorage.getItem(STORE_KEY);
    return id && _byId[id] ? id : DEFAULT_ID;
  } catch {
    return DEFAULT_ID;
  }
}

export function setToneId(id: string): void {
  if (_byId[id]) {
    try {
      localStorage.setItem(STORE_KEY, id);
    } catch {
      /* storage unavailable — falls back to default next load */
    }
  }
}

function playSequence(notes: Note[]) {
  try {
    const Ctx =
      window.AudioContext ??
      (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext;
    const ctx = new Ctx();
    let end = 0;
    for (const n of notes) {
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = n.type ?? "sine";
      osc.frequency.value = n.freq;
      const peak = n.gain ?? 0.22;
      gain.gain.setValueAtTime(0.0001, ctx.currentTime + n.at);
      gain.gain.exponentialRampToValueAtTime(peak, ctx.currentTime + n.at + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + n.at + n.dur);
      osc.connect(gain).connect(ctx.destination);
      osc.start(ctx.currentTime + n.at);
      osc.stop(ctx.currentTime + n.at + n.dur + 0.05);
      end = Math.max(end, n.at + n.dur);
    }
    setTimeout(() => ctx.close(), (end + 0.4) * 1000);
  } catch {
    /* audio unavailable (no user gesture yet) — the desktop notification still fires */
  }
}

/** Play the CURRENTLY SELECTED pack's tone for `kind`. */
export function chime(kind: ChimeKind = "good") {
  const pack = _byId[getToneId()] ?? _byId[DEFAULT_ID];
  playSequence(kind === "urgent" ? pack.urgent : pack.good);
}

/** Preview a SPECIFIC pack (for the picker), without changing the selection. */
export function previewTone(id: string, kind: ChimeKind = "good") {
  const pack = _byId[id];
  if (pack) playSequence(kind === "urgent" ? pack.urgent : pack.good);
}
