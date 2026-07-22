"use client";

import { useEffect, useRef, useState } from "react";
import { TONE_PACKS, getToneId, setToneId, previewTone } from "@/lib/tones";

/**
 * Small popover for choosing the alert tone. Sound can't be described, so every
 * option previews on click — pick by ear. Selecting also previews, so a choice
 * is always confirmed audibly.
 *
 * Two preview buttons per pack: the signal tone (▶) and the stop tone (⛔),
 * because the whole reason the packs keep those distinct is that a trader must
 * hear the difference. Letting them compare here is the point.
 */
export function AlertToneMenu({ onClose }: { onClose: () => void }) {
  const [selected, setSelected] = useState(getToneId());
  const ref = useRef<HTMLDivElement>(null);

  // Close on outside click or Escape.
  useEffect(() => {
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose();
    };
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [onClose]);

  const choose = (id: string) => {
    setToneId(id);
    setSelected(id);
    previewTone(id, "good"); // confirm the choice by ear
  };

  return (
    <div
      ref={ref}
      className="absolute right-0 top-full z-50 mt-1 w-72 rounded-md border border-edge bg-panel shadow-xl"
    >
      <div className="border-b border-edge px-3 py-1.5 text-[11px] font-medium text-white">
        Alert tone
      </div>
      <div className="space-y-1 p-2">
        {TONE_PACKS.map((p) => (
          <div
            key={p.id}
            className={`flex items-center gap-2 rounded border px-2 py-1.5 ${
              selected === p.id ? "border-accent/60 bg-accent/10" : "border-edge bg-panel2"
            }`}
          >
            <button onClick={() => choose(p.id)} className="min-w-0 flex-1 text-left">
              <div className="flex items-center gap-1.5">
                <span className="text-xs font-medium text-white">{p.label}</span>
                {selected === p.id && <span className="text-[9px] text-accent">✓ selected</span>}
              </div>
              <div className="truncate text-[10px] text-muted">{p.hint}</div>
            </button>
            <button
              onClick={() => previewTone(p.id, "good")}
              title="Preview the signal tone"
              className="rounded bg-panel px-1.5 py-0.5 text-[11px] text-bull hover:bg-edge"
            >
              ▶
            </button>
            <button
              onClick={() => previewTone(p.id, "urgent")}
              title="Preview the stop-loss tone"
              className="rounded bg-panel px-1.5 py-0.5 text-[11px] text-bear hover:bg-edge"
            >
              ⛔
            </button>
          </div>
        ))}
      </div>
      <div className="border-t border-edge px-3 py-1.5 text-[10px] text-muted">
        ▶ signal · ⛔ stop-loss. Saved on this device.
      </div>
    </div>
  );
}
