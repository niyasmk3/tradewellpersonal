"use client";

import { useEffect, useMemo, useState } from "react";
import { API_BASE } from "@/lib/api";

/**
 * The loss guards, editable live. These HALT SIGNALS — they place, modify or
 * close no order. That line is repeated on the panel itself, not just here,
 * because "I set a loss limit" must never be misread as "I set a stop-loss".
 */
interface Field {
  key: string;
  label: string;
  unit: "rupees" | "trades" | "positions";
  value: number;
  min: number;
  max: number;
  step: number;
  zero_disables: boolean;
  disabled: boolean;
  source: "env" | "override";
  env_default: number;
  description: string;
  example: string;
}

interface View {
  fields: Field[];
  updated_at: number | null;
  note: string;
}

const fmtVal = (f: Field) =>
  f.unit === "rupees" ? `₹${f.value.toLocaleString("en-IN")}` : String(f.value);

export function RiskSettings({ onClose }: { onClose: () => void }) {
  const [view, setView] = useState<View | null>(null);
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    fetch(`${API_BASE}/settings/risk-limits`, { cache: "no-store" })
      .then((r) => r.json())
      .then((v: View) => {
        setView(v);
        setDraft(Object.fromEntries(v.fields.map((f) => [f.key, String(f.value)])));
      })
      .catch(() => setErr("Could not load risk limits — is the backend running?"));
  }, []);

  const changed = useMemo(() => {
    if (!view) return {};
    const out: Record<string, number> = {};
    for (const f of view.fields) {
      const raw = (draft[f.key] ?? "").trim();
      if (raw === "") continue;
      const n = Number(raw.replace(/,/g, ""));
      if (Number.isFinite(n) && n !== f.value) out[f.key] = n;
    }
    return out;
  }, [draft, view]);

  const hasChanges = Object.keys(changed).length > 0;

  const save = async () => {
    setBusy(true);
    setErr(null);
    try {
      const res = await fetch(`${API_BASE}/settings/risk-limits`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(changed),
      });
      const body = await res.json();
      if (!res.ok) throw new Error(body?.detail ?? "Save failed");
      setView(body);
      setDraft(Object.fromEntries((body as View).fields.map((f) => [f.key, String(f.value)])));
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Save failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/60 p-4 sm:p-8"
      onClick={onClose}
    >
      <div
        className="card w-full max-w-lg border border-edge"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-edge px-4 py-2.5">
          <h2 className="text-sm font-semibold">Risk limits</h2>
          <button onClick={onClose} className="text-muted hover:text-white" aria-label="close">
            ✕
          </button>
        </div>

        {/* The load-bearing caveat, first thing on the panel. */}
        <div className="border-b border-bear/30 bg-bear/10 px-4 py-2 text-[11px] text-bear/90">
          These <b>halt new signals</b> — Tradewell places, moves or closes no order. They
          stop the engine offering more trades once the day turns against you; your actual
          loss is still capped only by the stop you act on yourself.
        </div>

        {err && <div className="bg-bear/15 px-4 py-1.5 text-[11px] text-bear">{err}</div>}

        {!view ? (
          <div className="p-4 text-xs text-muted">Loading…</div>
        ) : (
          <div className="max-h-[60vh] space-y-3 overflow-y-auto scroll-thin p-4">
            {view.fields.map((f) => (
              <div key={f.key} className="rounded-md border border-edge bg-panel2 p-3">
                <div className="flex items-center gap-2">
                  <label className="text-sm font-medium">{f.label}</label>
                  {f.source === "override" ? (
                    <span className="tag bg-accent/15 text-[9px] text-accent">custom</span>
                  ) : (
                    <span className="tag bg-panel text-[9px] text-muted">default</span>
                  )}
                  {f.disabled && (
                    <span className="tag bg-panel text-[9px] text-muted" title="Zero disables this guard">
                      off
                    </span>
                  )}
                  <div className="ml-auto flex items-center gap-1">
                    {f.unit === "rupees" && <span className="text-muted">₹</span>}
                    <input
                      value={draft[f.key] ?? ""}
                      onChange={(e) => setDraft((d) => ({ ...d, [f.key]: e.target.value }))}
                      inputMode="decimal"
                      className="w-28 rounded border border-edge bg-panel px-2 py-1 text-right font-mono text-xs outline-none focus:border-accent"
                    />
                    <span className="w-16 text-[10px] text-muted">{f.unit}</span>
                  </div>
                </div>
                <p className="mt-1.5 text-[11px] leading-relaxed text-white/75">{f.description}</p>
                <p className="mt-1 text-[10px] italic text-muted">
                  {f.example}
                  {f.source === "override" && f.env_default !== f.value && (
                    <>
                      {" · "}
                      <button
                        onClick={() => setDraft((d) => ({ ...d, [f.key]: String(f.env_default) }))}
                        className="underline decoration-dotted hover:text-white"
                      >
                        reset to default ({f.unit === "rupees" ? `₹${f.env_default.toLocaleString("en-IN")}` : f.env_default})
                      </button>
                    </>
                  )}
                </p>
              </div>
            ))}
          </div>
        )}

        <div className="flex items-center gap-2 border-t border-edge px-4 py-2.5">
          <span className="text-[10px] text-muted">
            {hasChanges ? `${Object.keys(changed).length} unsaved change(s)` : "Applies on the next signal cycle — no restart"}
          </span>
          <button onClick={onClose} className="ml-auto rounded bg-panel2 px-3 py-1 text-xs text-muted hover:text-white">
            Close
          </button>
          <button
            onClick={save}
            disabled={busy || !hasChanges}
            className="rounded bg-accent px-3 py-1 text-xs font-medium text-white disabled:opacity-40"
          >
            {busy ? "Saving…" : "Save"}
          </button>
        </div>
      </div>
    </div>
  );
}
