"use client";

import { useState } from "react";
import { API_BASE, MarketStatus, TradingMode, api } from "@/lib/api";
import { signed } from "@/lib/format";
import { AlertToneMenu } from "./AlertToneMenu";

/**
 * Full backend PROCESS restart from the dashboard — the recovery for a stale
 * process (old code after a deploy, e.g. Monday open still on Friday's
 * snapshot) or a dead ticker socket, neither of which "restart feed" can fix.
 * Confirms first, then polls until the fresh process answers and reloads the
 * page so every panel resumes from clean state.
 */
function RestartBackendButton() {
  const [state, setState] = useState<"idle" | "waiting">("idle");
  const click = async () => {
    if (
      !window.confirm(
        "Restart the backend process?\n\nThe tick feed drops for ~10-20 seconds while it " +
          "relaunches on the current code. If today's Kite login hasn't been done yet, " +
          "the login gate appears. Tradewell places no orders either way.",
      )
    )
      return;
    setState("waiting");
    try {
      await api.restartBackend();
    } catch (e) {
      // Two very different failures: the old process dying before the response
      // finishes (success — proceed to poll), vs a clean 404 because the
      // RUNNING backend predates this endpoint (this code hot-reloads into the
      // dev frontend before the backend has been restarted onto it once).
      if (e instanceof Error && /404|not found/i.test(e.message)) {
        setState("idle");
        alert(
          "The running backend predates this button. Restart it once from the " +
            "terminal (./start.sh, after market close) — from then on the button works.",
        );
        return;
      }
    }
    const deadline = Date.now() + 60_000;
    // First give the OLD process time to actually exit, or an immediate poll
    // hits it and we reload straight back into the stale backend.
    await new Promise((r) => setTimeout(r, 3000));
    while (Date.now() < deadline) {
      try {
        const res = await fetch(`${API_BASE}/auth/status`, { cache: "no-store" });
        if (res.ok) {
          window.location.reload();
          return;
        }
      } catch {
        /* still rebinding */
      }
      await new Promise((r) => setTimeout(r, 2000));
    }
    setState("idle");
    alert("Backend did not come back within 60s — check the terminal running start.sh.");
  };
  return (
    <button
      onClick={click}
      disabled={state === "waiting"}
      title="Restart the backend process — picks up deployed code and revives a dead ticker (restart feed cannot). ~10-20s of feed downtime."
      className="rounded-md border border-edge bg-panel px-2 py-0.5 text-xs text-muted transition hover:text-white disabled:opacity-50"
    >
      {state === "waiting" ? "restarting…" : "⟳ backend"}
    </button>
  );
}

function ScoreMeter({ label, value, tone }: { label: string; value: number; tone: "bull" | "bear" }) {
  const bar = tone === "bull" ? "bg-bull" : "bg-bear";
  const text = tone === "bull" ? "text-bull" : "text-bear";
  return (
    <span className="flex items-center gap-1.5">
      <span className="text-[10px] uppercase text-muted">{label}</span>
      <span className="h-1.5 w-12 overflow-hidden rounded-full bg-edge">
        <span
          className={`block h-full ${bar}`}
          style={{ width: `${Math.min(100, Math.max(0, value))}%` }}
        />
      </span>
      <span className={`w-6 text-right font-mono text-xs ${text}`}>{value.toFixed(0)}</span>
    </span>
  );
}

/**
 * One 36px control rail: regime + bull/bear, the mode & timeframe switches, the
 * alert toggle and today's realized P&L. Consolidates what used to be three
 * separate stacked rows (regime card, mode row, timeframe row) so the chart and
 * signal card get that vertical space back.
 */
export function CommandStrip({
  status,
  showRegime,
  mode,
  available,
  onMode,
  tf,
  timeframes,
  onTf,
  chartError,
  alerts,
  realizedToday,
  realizedTotal,
  onRiskSettings,
  className = "",
}: {
  status: MarketStatus | null;
  showRegime: boolean;
  mode: TradingMode;
  available: { key: TradingMode; label: string }[];
  onMode: (m: TradingMode) => void;
  tf: string;
  timeframes: string[];
  onTf: (t: any) => void;
  chartError?: string | null;
  alerts: { muted: boolean; toggle: () => void };
  realizedToday: number;
  realizedTotal: number;
  onRiskSettings?: () => void;
  className?: string;
}) {
  const [toneMenu, setToneMenu] = useState(false);
  const biasTone =
    status?.bias === "bullish" ? "text-bull" : status?.bias === "bearish" ? "text-bear" : "text-muted";

  return (
    <div className={`card flex flex-wrap items-center gap-x-4 gap-y-1.5 px-3 py-1.5 ${className}`}>
      {showRegime && (
        <>
          <span className="flex min-w-0 items-center gap-2">
            <span className="text-[10px] uppercase tracking-wide text-muted">Regime</span>
            <span className={`text-sm font-semibold ${biasTone}`}>
              {status?.regime_label ?? "—"}
            </span>
            {status?.headline && (
              <span className="hidden max-w-[22ch] truncate text-xs text-white/70 2xl:inline">
                {status.headline}
              </span>
            )}
          </span>
          {status && (
            <span className="flex items-center gap-3">
              <ScoreMeter label="Bull" value={status.bull_score} tone="bull" />
              <ScoreMeter label="Bear" value={status.bear_score} tone="bear" />
            </span>
          )}

          <span className="inline-flex rounded-md border border-edge bg-panel p-0.5">
            {available.map((m) => (
              <button
                key={m.key}
                onClick={() => onMode(m.key)}
                title={
                  m.key === "intraday"
                    ? "same-day · weekly options"
                    : m.key === "scalp"
                      ? "minutes-scale · paper-only until the book earns it"
                      : "multi-day swing · monthly options"
                }
                className={`rounded px-2 py-0.5 text-xs font-medium transition ${
                  m.key === mode ? "bg-accent text-white" : "text-muted hover:text-white"
                }`}
              >
                {m.label}
              </button>
            ))}
          </span>
        </>
      )}

      <span className="inline-flex items-center gap-0.5">
        {timeframes.map((t) => (
          <button
            key={t}
            onClick={() => onTf(t)}
            className={`rounded px-2 py-0.5 text-xs font-medium ${
              t === tf ? "bg-accent text-white" : "bg-panel2 text-muted hover:text-white"
            }`}
          >
            {t}
          </button>
        ))}
        {chartError && <span className="ml-1 text-[10px] text-bear">chart: {chartError}</span>}
      </span>

      <span className="ml-auto flex items-center gap-3">
        <span className="whitespace-nowrap text-[11px]" title="Realized P&L — booked today (lifetime in brackets)">
          <span className="text-muted">P&L </span>
          <span className={`font-mono ${realizedToday >= 0 ? "text-bull" : "text-bear"}`}>
            ₹{signed(realizedToday, 0)}
          </span>
          {realizedTotal !== realizedToday && (
            <span className="text-muted"> ({signed(realizedTotal, 0)})</span>
          )}
        </span>
        <button
          onClick={alerts.toggle}
          title={
            alerts.muted
              ? "Alerts off — click for a chime + desktop notification on each new signal"
              : "Alerts on — click to mute"
          }
          className={`rounded-md border px-2 py-0.5 text-xs transition ${
            alerts.muted
              ? "border-edge bg-panel text-muted hover:text-white"
              : "border-accent/60 bg-accent/15 text-accent"
          }`}
        >
          {alerts.muted ? "🔕" : "🔔"}
        </button>
        {/* Tone picker — its own control so it doesn't fight the mute toggle. */}
        <span className="relative">
          <button
            onClick={() => setToneMenu((v) => !v)}
            title="Choose the alert tone"
            className="rounded-md border border-edge bg-panel px-1.5 py-0.5 text-[10px] text-muted transition hover:text-white"
          >
            ▾
          </button>
          {toneMenu && <AlertToneMenu onClose={() => setToneMenu(false)} />}
        </span>
        {onRiskSettings && (
          <button
            onClick={onRiskSettings}
            title="Trading settings — today's fund and the paper-simulator position cap"
            className="rounded-md border border-edge bg-panel px-2 py-0.5 text-xs text-muted transition hover:text-white"
          >
            ⚙
          </button>
        )}
        <RestartBackendButton />
      </span>
    </div>
  );
}
