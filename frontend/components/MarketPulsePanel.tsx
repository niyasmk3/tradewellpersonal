"use client";

import { useRef, useState } from "react";
import { MarketPulse, api } from "@/lib/api";

type ChatMsg = { role: "user" | "assistant"; content: string };

/** The ask-anything box: questions go to Claude WITH the live dashboard
 *  context attached server-side, so answers are about this screen right now.
 *  Explains only — the engine's gates stay the only signal source. */
function PulseChat() {
  const [open, setOpen] = useState(false);
  const [msgs, setMsgs] = useState<ChatMsg[]>([]);
  const [q, setQ] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const listRef = useRef<HTMLDivElement>(null);

  const send = async () => {
    const question = q.trim();
    if (!question || busy) return;
    setBusy(true);
    setErr(null);
    setQ("");
    const next: ChatMsg[] = [...msgs, { role: "user", content: question }];
    setMsgs(next);
    try {
      const r = await api.marketChat(question, msgs.slice(-16));
      setMsgs([...next, { role: "assistant", content: r.answer }]);
      requestAnimationFrame(() =>
        listRef.current?.scrollTo({ top: listRef.current.scrollHeight }));
    } catch (e) {
      const m = e instanceof Error ? e.message : String(e);
      setErr(/404/.test(m)
        ? "The chat endpoint arrives with the next backend restart (⟳ button)."
        : m);
      setMsgs(msgs);                    // roll the unanswered question back
      setQ(question);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="mt-2 border-t border-edge/60 pt-1.5">
      <button
        onClick={() => setOpen((v) => !v)}
        className="text-[10px] uppercase tracking-wide text-muted transition hover:text-white"
        title="Ask anything about what's on screen — the answer is grounded in the live dashboard state. Explains only; never a trade signal."
      >
        {open ? "▾ Ask about this market" : "▸ Ask about this market"}
      </button>
      {open && (
        <div className="mt-1.5">
          {msgs.length > 0 && (
            <div ref={listRef} className="mb-1.5 flex max-h-48 flex-col gap-1.5 overflow-y-auto pr-1">
              {msgs.map((m, i) => (
                <div
                  key={i}
                  className={`rounded px-2 py-1 text-[11px] leading-relaxed ${
                    m.role === "user"
                      ? "self-end bg-accent/15 text-white/90"
                      : "self-start bg-panel2 text-white/80"
                  }`}
                >
                  {m.content}
                </div>
              ))}
              {busy && <div className="self-start px-2 text-[11px] text-muted">thinking…</div>}
            </div>
          )}
          {err && <p className="mb-1 text-[10px] text-yellow-400">{err}</p>}
          <div className="flex gap-1.5">
            <input
              value={q}
              onChange={(e) => setQ(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && send()}
              placeholder="e.g. why is the chart above the header price?"
              className="min-w-0 flex-1 rounded border border-edge bg-panel2 px-2 py-1 text-[11px] text-white placeholder:text-muted focus:border-accent/60 focus:outline-none"
            />
            <button
              onClick={send}
              disabled={busy || !q.trim()}
              className="rounded bg-accent/20 px-2.5 py-1 text-[11px] font-medium text-accent transition hover:bg-accent/30 disabled:opacity-40"
            >
              Ask
            </button>
          </div>
          <p className="mt-1 text-[9px] leading-snug text-muted">
            Grounded in the live screen · explains, never signals · not investment advice
          </p>
        </div>
      )}
    </div>
  );
}

/**
 * Live tape analytics, sitting directly under the score card. The score says
 * what the engine thinks of the SETUP; this says what the market is DOING —
 * stretch, participation, range consumption, positioning drift, fear. Every
 * number is honest about its baseline (tooltips) and renders "—" while its
 * inputs warm up rather than inventing a value.
 */
function Item({ label, value, tone, title }: {
  label: string; value: string; tone?: string; title: string;
}) {
  return (
    <div className="min-w-0" title={title}>
      <div className="truncate text-[9px] uppercase tracking-wide text-muted">{label}</div>
      <div className={`truncate font-mono text-[11px] ${tone ?? "text-white/85"}`}>{value}</div>
    </div>
  );
}

export function MarketPulsePanel({ data, error }: { data: MarketPulse | null; error?: string | null }) {
  if (error) {
    const stale = /404|not found/i.test(error);
    return (
      <div className="card px-3 py-1.5 text-[10px] text-muted">
        {stale
          ? "Market pulse arrives after the next backend restart (⟳ button)."
          : `Market pulse unavailable — ${error}`}
      </div>
    );
  }
  const d = data;
  const pos = d?.range_pos_pct;
  const stretch = d?.vwap_dist_atr;
  const rr = d?.vol_run_rate;
  const fmt = (v: number | undefined | null, dp = 1, suffix = "") =>
    v === undefined || v === null ? "—" : `${v.toFixed(dp)}${suffix}`;

  return (
    <div className="card px-3 py-2">
      <div className="mb-1.5 flex items-center justify-between">
        <span className="text-[10px] uppercase tracking-wide text-muted">Market pulse · futures</span>
        {pos !== undefined && (
          // Day-range strip: where price sits between today's low and high.
          <span className="relative h-1.5 w-24 rounded-full bg-edge" title={`Day range ₹${fmt(d?.day_low, 1)}–₹${fmt(d?.day_high, 1)} — trading at ${fmt(pos, 0, "%")} of it`}>
            <span
              className="absolute top-1/2 h-2.5 w-1 -translate-y-1/2 rounded-sm bg-accent"
              style={{ left: `${Math.min(97, Math.max(0, pos))}%` }}
            />
          </span>
        )}
      </div>
      <div className="grid grid-cols-3 gap-x-3 gap-y-1.5">
        <Item
          label="Range pos"
          value={fmt(pos, 0, "%")}
          tone={pos === undefined ? undefined : pos >= 65 ? "text-bull" : pos <= 35 ? "text-bear" : undefined}
          title="Where the future trades inside today's high–low. Extremes with high range-used often mean the move already happened."
        />
        <Item
          label="VWAP stretch"
          value={stretch === undefined ? "—" : `${stretch > 0 ? "+" : ""}${stretch.toFixed(2)} ATR`}
          tone={stretch === undefined ? undefined : Math.abs(stretch) >= 2 ? "text-yellow-400" : undefined}
          title="Distance from session VWAP in ATR units. Beyond ±2 ATR is stretched — chasing territory (the engine's extension gate vetoes at 3.5)."
        />
        <Item
          label="Volume"
          value={rr === undefined ? "—" : `${rr.toFixed(2)}×`}
          tone={rr === undefined ? undefined : rr >= 1.3 ? "text-bull" : rr <= 0.7 ? "text-muted" : undefined}
          title="Mean 15-minute volume today vs recent sessions. Above 1× = participation; thin tape makes every other signal less trustworthy."
        />
        <Item
          label="Range used"
          value={fmt(d?.range_vs_typical_pct, 0, "%")}
          title="Today's high–low as a share of the typical recent daily range. Near 100% early = the day's travel may be mostly spent."
        />
        <Item
          label="PCR"
          value={d?.pcr === undefined ? "—" : `${d.pcr.toFixed(2)}${d.pcr_shift !== undefined && d.pcr_shift !== 0 ? ` (${d.pcr_shift > 0 ? "+" : ""}${d.pcr_shift.toFixed(2)})` : ""}`}
          tone={d?.pcr_shift === undefined ? undefined : d.pcr_shift > 0.05 ? "text-bull" : d.pcr_shift < -0.05 ? "text-bear" : undefined}
          title="Put/call OI ratio, with drift since Tradewell first observed it today (not the exchange open). Rising = put writers building support under the market."
        />
        <Item
          label="VIX"
          value={d?.vix === undefined ? "—" : `${d.vix.toFixed(2)}${d.vix_chg_pct !== undefined ? ` (${d.vix_chg_pct > 0 ? "+" : ""}${d.vix_chg_pct.toFixed(1)}%)` : ""}`}
          tone={d?.vix_chg_pct === undefined ? undefined : d.vix_chg_pct >= 3 ? "text-bear" : undefined}
          title="India VIX vs its last daily close. Spiking fear inflates every premium you buy — and the one you're already holding."
        />
      </div>
      {/* The same numbers in plain language — composed by fixed rules on the
          backend from the fields above, so it can never say what they don't. */}
      {d?.story && (
        <p className="mt-2 border-t border-edge/60 pt-1.5 text-[11px] leading-relaxed text-white/70">
          {d.story}
        </p>
      )}
      <PulseChat />
    </div>
  );
}
