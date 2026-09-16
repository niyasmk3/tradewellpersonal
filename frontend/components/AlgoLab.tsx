"use client";

// Algo — the execution module's control surface.
//
// This screen is built for situational awareness under stress, not for
// analytics: the loudest element is the arm state, the KILL button is always
// visible and needs no token, and every number a decision rests on comes from
// the backend's frozen contract and append-only ledger — nothing here is
// computed in the browser. Today only the dry-run broker exists, so the tab
// is a rehearsal room: intents are gated, logged and acknowledged
// synthetically, and the ledger is the evidence that the gate set is
// calibrated before any rung above A1 is even discussed.
//
// Plan and rollout ladder: docs/algo-tab-plan-2026-09-16.md

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  AlgoLedgerRow,
  AlgoMode,
  AlgoStatus,
  api,
} from "@/lib/api";
import { fmt, istTime } from "@/lib/format";
import { usePolling } from "@/lib/usePolling";
import { ModuleSwitcher } from "./ModuleSwitcher";

const TOKEN_KEY = "tradewell.algoToken";

const hhmm = (min: number) =>
  `${String(Math.floor(min / 60)).padStart(2, "0")}:${String(min % 60).padStart(2, "0")}`;

const mmss = (s: number) =>
  `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(Math.floor(s % 60)).padStart(2, "0")}`;

const rs = (n: number | null | undefined) =>
  n == null ? "—" : `₹${n.toLocaleString("en-IN", { maximumFractionDigits: 0 })}`;

function loadToken(): string {
  try {
    return sessionStorage.getItem(TOKEN_KEY) ?? "";
  } catch {
    return "";
  }
}

function saveToken(t: string) {
  try {
    if (t) sessionStorage.setItem(TOKEN_KEY, t);
    else sessionStorage.removeItem(TOKEN_KEY);
  } catch {
    /* per-viewer convenience only */
  }
}

/** The arm banner: the one element that must be readable from across the room. */
function ArmBanner({ s }: { s: AlgoStatus }) {
  if (s.kill.tripped) {
    return (
      <div className="card border-bear bg-bear/15 px-4 py-3">
        <div className="text-lg font-semibold text-bear">KILL SWITCH TRIPPED · {s.kill.code}</div>
        <div className="mt-0.5 text-xs text-bear/90">
          {s.kill.detail || "no detail"} — the runner emits nothing, exits included. Any open
          position is yours to manage in Kite. Only an explicit Clear (token) re-enables arming.
        </div>
      </div>
    );
  }
  if (!s.arm) {
    return (
      <div className="card bg-panel2 px-4 py-3">
        <div className="text-lg font-semibold text-muted">DISARMED</div>
        <div className="mt-0.5 text-xs text-muted">
          The runner idles. Arms never persist — a restart always comes up here.
        </div>
      </div>
    );
  }
  const cls =
    s.arm.mode === "live"
      ? "border-bear bg-bear/15 text-bear animate-pulse"
      : s.arm.mode === "paper"
        ? "border-accent bg-accent/15 text-accent"
        : "border-accent/50 bg-accent/10 text-accent";
  return (
    <div className={`card px-4 py-3 ${cls}`}>
      <div className="text-lg font-semibold">
        {s.arm.mode.toUpperCase()}
        {s.arm.mode === "dry" ? "-RUN" : ""} · armed · {s.arm.strategy} · expires in{" "}
        {mmss(s.arm.remaining_s)}
      </div>
      <div className="mt-0.5 text-xs opacity-80">
        {s.arm.mode === "dry"
          ? "Intents are gated and logged; the broker acks synthetically. No order can be placed by this process."
          : "Real orders. Stay at the screen."}
      </div>
    </div>
  );
}

function Preflight({ s }: { s: AlgoStatus }) {
  return (
    <section className="card px-4 py-3">
      <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted">Preflight</h2>
      <div className="grid grid-cols-1 gap-x-4 gap-y-1 text-[11px] sm:grid-cols-2 lg:grid-cols-3">
        {s.preflight.map((p) => (
          <div key={p.key} className="flex items-baseline gap-2">
            <span
              className={`tag w-12 justify-center ${
                p.ok === null
                  ? "bg-panel2 text-muted"
                  : p.ok
                    ? "bg-bull/20 text-bull"
                    : "bg-bear/20 text-bear"
              }`}
            >
              {p.ok === null ? "n/a" : p.ok ? "ok" : "BLOCK"}
            </span>
            <span className="font-mono text-muted">{p.key}</span>
            <span className="truncate" title={p.detail}>{p.detail}</span>
          </div>
        ))}
      </div>
    </section>
  );
}

function ClosingCard({ s }: { s: AlgoStatus }) {
  const a = s.adapters.closing;
  const c = a.card;
  return (
    <section className="card px-4 py-3">
      <div className="mb-2 flex flex-wrap items-baseline gap-3">
        <h2 className="text-xs font-semibold uppercase tracking-wide text-muted">
          Closing Day · adapter
        </h2>
        <span className="text-[11px] text-muted">
          policy <span className="font-mono text-white">{a.entry_policy}</span> · entry{" "}
          {hhmm(a.entry_window_min[0])}–{hhmm(a.entry_window_min[1])} · exit{" "}
          {hhmm(a.exit_window_min[0])}–{hhmm(a.exit_window_min[1])} · {a.lots} lot ·{" "}
          {a.product} · limit ±{(a.limit_buffer_pct * 100).toFixed(1)}%
        </span>
      </div>
      {c ? (
        <div className="flex flex-wrap items-center gap-3 text-xs">
          <span
            className={`tag ${
              c.verdict === "CLEAN"
                ? "bg-bull/20 text-bull"
                : c.verdict === "FLAGGED"
                  ? "bg-bear/20 text-bear"
                  : "bg-panel2 text-muted"
            }`}
          >
            {c.verdict}
          </span>
          <span className="font-mono">
            {c.direction ?? "—"} {c.strike ?? "—"} · exp {c.expiry ?? "—"} · exit{" "}
            {c.next_trading_day ?? "—"}
          </span>
          <span className="text-muted">logged {c.as_of.slice(11, 19)}</span>
          {c.red.length > 0 && (
            <span className="text-bear">red: {c.red.join(", ")}</span>
          )}
        </div>
      ) : (
        <div className="text-xs text-muted">No settled 15:05 card logged for {s.day} yet.</div>
      )}
      <div className={`mt-2 text-xs ${a.would_fire ? "text-bull" : "text-muted"}`}>
        {a.would_fire ? "would fire → " : "would not fire → "}
        {a.decision}
      </div>
    </section>
  );
}

function Rails({ s }: { s: AlgoStatus }) {
  const k = s.contract;
  const Row = ({ label, used, cap }: { label: string; used: number; cap: number }) => (
    <div className="flex items-baseline justify-between gap-2 font-mono text-[11px]">
      <span className="text-muted">{label}</span>
      <span className={used >= cap ? "text-bear" : ""}>
        {used} / {cap}
      </span>
    </div>
  );
  return (
    <section className="card px-4 py-3">
      <div className="mb-2 flex items-baseline gap-2">
        <h2 className="text-xs font-semibold uppercase tracking-wide text-muted">Rails</h2>
        <span className="text-[10px] text-muted">frozen {k.freeze_date} · constants, not settings</span>
      </div>
      <div className="grid grid-cols-1 gap-x-6 gap-y-1 sm:grid-cols-2">
        <Row label="orders today (all)" used={s.today.orders} cap={k.max_orders_per_day} />
        <Row label="entries today" used={s.today.open_orders} cap={k.max_open_orders_per_day} />
        <Row label="open positions" used={s.positions.length} cap={k.max_open_positions} />
        <div className="flex items-baseline justify-between gap-2 font-mono text-[11px]">
          <span className="text-muted">exit reserve</span>
          <span>{k.exit_reserve_orders} orders</span>
        </div>
        <div className="flex items-baseline justify-between gap-2 font-mono text-[11px]">
          <span className="text-muted">rate</span>
          <span>{k.max_orders_per_sec}/s (regulatory 10/s)</span>
        </div>
        <div className="flex items-baseline justify-between gap-2 font-mono text-[11px]">
          <span className="text-muted">lots / order</span>
          <span>{k.max_lots_per_order}</span>
        </div>
        <div className="flex items-baseline justify-between gap-2 font-mono text-[11px]">
          <span className="text-muted">opens window</span>
          <span>
            {hhmm(k.entry_window_min[0])}–{hhmm(k.entry_window_min[1])} · none after{" "}
            {hhmm(k.hard_flatten_min)}
          </span>
        </div>
        <div className="flex items-baseline justify-between gap-2 font-mono text-[11px]">
          <span className="text-muted">day loss cap</span>
          <span className={k.daily_loss_cap_rs > 0 ? "" : "text-bear"}>
            {k.daily_loss_cap_rs > 0 ? rs(k.daily_loss_cap_rs) : "UNSET → live blocked"}
          </span>
        </div>
        <div className="flex items-baseline justify-between gap-2 font-mono text-[11px]">
          <span className="text-muted">notional / order</span>
          <span className={k.max_notional_per_order_rs > 0 ? "" : "text-bear"}>
            {k.max_notional_per_order_rs > 0 ? rs(k.max_notional_per_order_rs) : "UNSET → live blocked"}
          </span>
        </div>
        <div className="flex items-baseline justify-between gap-2 font-mono text-[11px]">
          <span className="text-muted">arm TTL</span>
          <span>{Math.round(k.arm_ttl_s / 60)} min</span>
        </div>
        <div className="flex items-baseline justify-between gap-2 font-mono text-[11px]">
          <span className="text-muted">kill after</span>
          <span>{k.consecutive_rejects_kill} rejects in a row</span>
        </div>
      </div>
      <details className="mt-2 text-[10px] text-muted">
        <summary className="cursor-pointer">
          {s.checks.length} guard checks ({s.checks.filter((c) => c.scope !== "open").length} reach exits)
        </summary>
        <div className="mt-1 flex flex-wrap gap-1 font-mono">
          {s.checks.map((c) => (
            <span
              key={c.code}
              className={`tag ${c.scope === "open" ? "bg-panel2" : "bg-accent/10 text-accent"}`}
              title={c.scope === "open" ? "opens only" : c.scope === "exit" ? "exits only" : "every order"}
            >
              {c.code}
            </span>
          ))}
        </div>
      </details>
    </section>
  );
}

const TYPE_CLS: Record<AlgoLedgerRow["type"], string> = {
  intent: "text-muted",
  verdict: "text-white",
  order: "text-accent",
  result: "text-accent",
  skip: "text-muted",
  kill: "text-bear",
  clear: "text-bull",
  arm: "text-accent",
  disarm: "text-muted",
  reconcile: "text-muted",
};

function describe(r: AlgoLedgerRow): string {
  switch (r.type) {
    case "intent":
      return `${r.purpose} ${r.side} ${r.quantity} ${r.tradingsymbol} @ ${fmt(r.price ?? null)} — ${r.note ?? ""}`;
    case "verdict":
      return r.allowed
        ? `ALLOWED (${r.mode})`
        : `BLOCKED ${r.code}: ${r.reason}${r.blocks && r.blocks.length > 1 ? ` [+${r.blocks.length - 1}: ${r.blocks.slice(1).join(", ")}]` : ""}`;
    case "order":
      return `SENT ${r.mode} ${r.side} ${r.quantity} ${r.tradingsymbol} @ ${fmt(r.price ?? null)}${r.closes ? " (closes " + r.closes + ")" : ""}`;
    case "result":
      return `${r.ok ? "ACK" : "REJECTED"} ${r.status ?? ""} ${r.broker_order_id ?? ""} ${r.message ?? ""}`;
    case "skip":
      return `skip — ${r.reason ?? ""}`;
    case "kill":
      return `KILL ${r.code ?? ""} ${r.detail ?? r.reason ?? ""}${r.streak ? ` (streak ${r.streak})` : ""}`;
    case "clear":
      return `cleared by ${r.by ?? "?"} ${r.reason ?? ""}`;
    case "arm":
      return `armed ${r.strategy} ${r.mode} by ${r.by ?? "?"}`;
    case "disarm":
      return `disarmed by ${r.by ?? "?"} ${r.reason ?? ""}`;
    default:
      return JSON.stringify(r);
  }
}

function Ledger({ rows, day }: { rows: AlgoLedgerRow[]; day: string }) {
  return (
    <section className="card px-4 py-3">
      <div className="mb-2 flex items-baseline gap-2">
        <h2 className="text-xs font-semibold uppercase tracking-wide text-muted">Ledger</h2>
        <span className="text-[10px] text-muted">
          append-only · newest first · {rows.length} rows · today {day}
        </span>
      </div>
      {rows.length === 0 ? (
        <div className="text-xs text-muted">
          Nothing yet. Arm the closing adapter (dry) before 15:05 on a trading day and the
          first rows land as the card is read.
        </div>
      ) : (
        <div className="scroll-thin max-h-[28rem] overflow-auto">
          <table className="w-full text-left font-mono text-[11px]">
            <tbody>
              {rows.map((r, i) => (
                <tr key={`${r.ts}-${i}`} className="border-t border-edge/40 align-top">
                  <td className="whitespace-nowrap py-0.5 pr-2 text-muted">{r.day.slice(5)} {istTime(r.ts)}</td>
                  <td className={`whitespace-nowrap py-0.5 pr-2 ${TYPE_CLS[r.type] ?? ""}`}>{r.type}</td>
                  <td className="py-0.5 pr-2 text-muted">{r.key?.split(":").slice(1).join(":") ?? ""}</td>
                  <td className={`py-0.5 ${r.type === "verdict" && !r.allowed ? "text-bear" : ""}`}>
                    {describe(r)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

export function AlgoLab() {
  const status = usePolling(() => api.algoStatus(), 5000, []);
  const ledger = usePolling(() => api.algoLedger(300), 10000, []);
  const [s, setS] = useState<AlgoStatus | null>(null);
  const [rows, setRows] = useState<AlgoLedgerRow[]>([]);
  const [token, setToken] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const strategy = "closing";
  const mode: AlgoMode = "dry";

  useEffect(() => setToken(loadToken()), []);
  useEffect(() => { if (status.data) setS(status.data); }, [status.data]);
  useEffect(() => { if (ledger.data) setRows(ledger.data.rows); }, [ledger.data]);

  const phrase = `ARM ${strategy.toUpperCase()} ${mode.toUpperCase()}`;
  // Only the two things the ARM ROUTE itself refuses on gate the button. The
  // rest (stale tick, skew, holiday) are per-intent guard concerns: arming at
  // 09:16 for the 09:50 exit is fine even though yesterday's ticks are stale.
  const preflightBlocks = useMemo(
    () => (s ? s.preflight.filter((p) => p.ok === false && (p.key === "control_token" || p.key === "kill_switch")) : []),
    [s],
  );
  const preflightWarnings = useMemo(
    () => (s ? s.preflight.filter((p) => p.ok === false && !["control_token", "kill_switch", "live_caps", "static_ip"].includes(p.key)) : []),
    [s],
  );

  const act = useCallback(async (what: string, fn: () => Promise<AlgoStatus>) => {
    setBusy(what);
    setError(null);
    try {
      setS(await fn());
      if (what === "arm") setConfirm("");
      setRows((await api.algoLedger(300)).rows);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }, []);

  const onToken = (t: string) => { setToken(t); saveToken(t); };

  return (
    <div className="mx-auto flex min-h-screen max-w-6xl flex-col gap-3 p-3">
      <header className="card flex flex-wrap items-center gap-3 px-4 py-3">
        <a href="/" className="text-xs text-muted hover:text-white">← Dashboard</a>
        <ModuleSwitcher />
        <h1 className="text-sm font-semibold">Algo · execution</h1>
        <span className="tag bg-accent/15 text-[10px] text-accent"
              title="Only the dry-run broker exists. Nothing in this process can place an order.">
          dry-run only · rung A0
        </span>
        {s && <span className="text-[11px] text-muted">{s.now.slice(11, 19)} IST</span>}
        <div className="ml-auto flex items-center gap-2">
          <button
            onClick={() => act("kill", () => api.algoKill("kill button"))}
            disabled={busy !== null || (s?.kill.tripped ?? false)}
            title="Trips the kill switch and disarms. Needs no token. The runner then emits nothing, exits included, until cleared."
            className="rounded bg-bear px-4 py-1.5 text-xs font-bold text-white hover:bg-bear/80 disabled:opacity-40"
          >
            KILL
          </button>
        </div>
      </header>

      {status.error && (
        <div className="card border-bear/50 bg-bear/10 px-4 py-2 text-xs text-bear">
          Backend unreachable: {status.error}
        </div>
      )}
      {error && (
        <div className="card border-bear/50 bg-bear/10 px-4 py-2 text-xs text-bear">{error}</div>
      )}

      {s && (
        <>
          <ArmBanner s={s} />

          <section className="card px-4 py-3">
            <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted">Arm</h2>
            <div className="flex flex-wrap items-end gap-3 text-xs">
              <label className="flex flex-col gap-1">
                <span className="text-[10px] text-muted">strategy</span>
                <select value={strategy} disabled className="rounded border border-edge bg-panel2 px-2 py-1 font-mono">
                  {s.strategies.map((x) => <option key={x} value={x}>{x}</option>)}
                </select>
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-[10px] text-muted">mode</span>
                <select value={mode} disabled className="rounded border border-edge bg-panel2 px-2 py-1 font-mono">
                  {s.contract.modes.map((m) => (
                    <option key={m} value={m} disabled={!s.contract.reachable_modes.includes(m)}>
                      {m}{s.contract.reachable_modes.includes(m) ? "" : " (not reachable)"}
                    </option>
                  ))}
                </select>
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-[10px] text-muted">control token (ALGO_CONTROL_TOKEN)</span>
                <input
                  type="password"
                  value={token}
                  onChange={(e) => onToken(e.target.value)}
                  placeholder="from backend/.env"
                  className="w-56 rounded border border-edge bg-panel2 px-2 py-1 font-mono"
                />
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-[10px] text-muted">type exactly: <span className="font-mono text-white">{phrase}</span></span>
                <input
                  value={confirm}
                  onChange={(e) => setConfirm(e.target.value)}
                  placeholder={phrase}
                  disabled={!!s.arm || s.kill.tripped}
                  className="w-56 rounded border border-edge bg-panel2 px-2 py-1 font-mono"
                />
              </label>
              {s.arm ? (
                <button
                  onClick={() => act("disarm", () => api.algoDisarm(token, "ui"))}
                  disabled={busy !== null || !token}
                  title="Full stop, same as kill but voluntary and un-paged. Any open position becomes yours to manage in Kite."
                  className="rounded bg-panel2 px-3 py-1.5 text-xs font-medium text-white hover:bg-edge disabled:opacity-40"
                >
                  {busy === "disarm" ? "Disarming…" : "Disarm"}
                </button>
              ) : (
                <button
                  onClick={() => act("arm", () => api.algoArm(token, strategy, mode, confirm))}
                  disabled={busy !== null || !token || confirm !== phrase || s.kill.tripped || preflightBlocks.length > 0}
                  title={preflightBlocks.length > 0 ? `preflight blocks: ${preflightBlocks.map((p) => p.key).join(", ")}` : `arms ${strategy} in ${mode} for ${Math.round(s.contract.arm_ttl_s / 60)} minutes`}
                  className="rounded bg-accent px-3 py-1.5 text-xs font-medium text-white hover:bg-accent/80 disabled:opacity-40"
                >
                  {busy === "arm" ? "Arming…" : `Arm ${mode}`}
                </button>
              )}
              {s.kill.tripped && (
                <button
                  onClick={() => act("clear", () => api.algoClear(token, "ui"))}
                  disabled={busy !== null || !token}
                  title="Un-latches the kill switch. Requires the control token; logged."
                  className="rounded bg-bull/20 px-3 py-1.5 text-xs font-medium text-bull hover:bg-bull/30 disabled:opacity-40"
                >
                  {busy === "clear" ? "Clearing…" : "Clear kill switch"}
                </button>
              )}
            </div>
            {preflightBlocks.length > 0 && !s.arm && (
              <div className="mt-2 text-[11px] text-bear">
                Arming is blocked: {preflightBlocks.map((p) => `${p.key} (${p.detail})`).join(" · ")}
              </div>
            )}
            {preflightWarnings.length > 0 && (
              <div className="mt-2 text-[11px] text-muted">
                The guard will refuse opens while: {preflightWarnings.map((p) => `${p.key} (${p.detail})`).join(" · ")}
              </div>
            )}
          </section>

          <Preflight s={s} />
          <ClosingCard s={s} />

          <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
            <Rails s={s} />
            <section className="card px-4 py-3">
              <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted">
                Positions · runner&apos;s belief
              </h2>
              {s.positions.length === 0 ? (
                <div className="text-xs text-muted">Flat. (Not yet reconciled against Kite — build step 9.)</div>
              ) : (
                <table className="w-full text-left font-mono text-[11px]">
                  <thead className="text-muted">
                    <tr><th className="pr-2">opened</th><th className="pr-2">mode</th><th className="pr-2">contract</th><th className="pr-2">qty</th><th>price</th></tr>
                  </thead>
                  <tbody>
                    {s.positions.map((p) => (
                      <tr key={p.key} className="border-t border-edge/40">
                        <td className="pr-2">{p.day}</td>
                        <td className="pr-2 text-accent">{p.mode}</td>
                        <td className="pr-2">{p.tradingsymbol}</td>
                        <td className="pr-2">{p.quantity}</td>
                        <td>{fmt(p.price ?? null)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
              <div className="mt-3 border-t border-edge/40 pt-2 text-[11px] text-muted">
                <span className="font-semibold text-white">Honesty box.</span> {s.note} Nothing in this
                repo has cleared its own live-money bar: the closing card has ~16 logged nights, gold
                ~2 weeks forward, and the engine&apos;s only positive class is +0.08R. This tab
                rehearses the machinery while the evidence ladders keep running on their own clocks;
                arming live is a separate, per-strategy decision the plan does not authorise.
              </div>
            </section>
          </div>

          <Ledger rows={rows} day={s.day} />
        </>
      )}
    </div>
  );
}
