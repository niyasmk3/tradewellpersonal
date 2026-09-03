"use client";

// Gold — the second evidence stream beside NIFTY: pre-registered candle rules
// on MCX GOLDM, graded shadow-first, with XAUUSD as the independent
// climatology cross-check. Advisory nothing, paper everything.
//
// The evidence discipline is the whole tab: rule parameters are FROZEN in the
// backend (deliberately not configurable), backtest and forward rows never
// share a table, and only forward samples — blind days after the freeze —
// count toward any verdict. Early forward results promote or kill nothing.

import { useCallback, useEffect, useState } from "react";
import {
  GoldCard,
  GoldPhaseBlock,
  GoldResults,
  GoldSummary,
  GoldTradesResponse,
  GoldVenueClimatology,
  api,
} from "@/lib/api";
import { fmt, fmtInt, signed } from "@/lib/format";
import { usePolling } from "@/lib/usePolling";
import { ModuleSwitcher } from "./ModuleSwitcher";

const rs = (n: number | null | undefined) =>
  n == null ? "—" : `${n < 0 ? "−" : ""}₹${Math.abs(n).toLocaleString("en-IN", {
    maximumFractionDigits: 0,
  })}`;

const signCls = (v: number | null | undefined) =>
  v == null ? "text-muted" : v > 0 ? "text-bull" : v < 0 ? "text-bear" : "text-muted";

const STATE_CLS: Record<GoldCard["state"], string> = {
  no_data: "bg-panel2 text-muted",
  pending: "bg-accent/15 text-accent",
  no_setup: "bg-panel2 text-muted",
  open: "bg-bull/20 text-bull",
  closed: "bg-accent/20 text-accent",
};

const hhmm = (ts: number | null) =>
  ts == null ? "—" : new Date(ts * 1000).toLocaleTimeString("en-IN", {
    hour: "2-digit", minute: "2-digit", hour12: false, timeZone: "Asia/Kolkata",
  });

/** Today's paper card for one rule — the live loop watches these same states. */
function LiveCard({ c }: { c: GoldCard }) {
  return (
    <div className="rounded border border-edge/60 bg-panel2 px-3 py-2">
      <div className="flex items-baseline gap-2">
        <span className="font-mono text-xs font-semibold">{c.rule}</span>
        <span className="text-[11px] text-muted">{c.name}</span>
        <span className={`ml-auto rounded px-1.5 py-0.5 text-[9px] uppercase ${STATE_CLS[c.state]}`}>
          {c.state.replace("_", " ")}
        </span>
      </div>
      <div className="mt-1 grid grid-cols-2 gap-x-3 gap-y-0.5 font-mono text-[11px]">
        <span className="text-muted">signal</span>
        <span className={signCls(c.signal_pct)}>{c.signal_pct != null ? `${signed(c.signal_pct, 2)}%` : "—"}</span>
        {c.entry_px != null && (
          <>
            <span className="text-muted">{c.direction?.toLowerCase()} @ {hhmm(c.entry_ts)}</span>
            <span>{fmt(c.entry_px, 0)}</span>
            <span className="text-muted">target / stop</span>
            <span>{fmt(c.target_px, 0)} / {fmt(c.stop_px, 0)}</span>
          </>
        )}
        {c.state === "open" && (
          <>
            <span className="text-muted">paper P&L</span>
            <span className={signCls(c.net_rs)}>{rs(c.net_rs)}</span>
          </>
        )}
        {c.state === "closed" && (
          <>
            <span className="text-muted">exit ({c.exit_reason} @ {hhmm(c.exit_ts)})</span>
            <span className={signCls(c.net_rs)}>{fmt(c.exit_px, 0)} · {rs(c.net_rs)}</span>
          </>
        )}
      </div>
      {c.note && <p className="mt-1 text-[10px] leading-relaxed text-muted">{c.note}</p>}
    </div>
  );
}

/** One phase's per-rule scoreboard. Backtest and forward NEVER share a table. */
function PhaseTable({ block, gates }: {
  block: GoldPhaseBlock;
  gates?: Record<string, { forward_n: number; verdict_due: number }>;
}) {
  const rules = Object.keys(block.per_rule);
  const row = (key: string, s: GoldSummary, label?: string) => (
    <tr key={key} className={`border-b border-edge/30 ${label ? "bg-panel2/50" : ""}`}>
      <td className="py-1 text-left font-sans">{label ?? key}</td>
      <td className="py-1 text-right text-muted">{s.n}</td>
      <td className="py-1 text-right">{s.win_rate_pct != null ? `${s.win_rate_pct}%` : "—"}</td>
      <td className={`py-1 text-right ${signCls(s.avg_net_rs)}`}>{rs(s.avg_net_rs)}</td>
      <td className="py-1 text-right text-muted">{s.profit_factor ?? "—"}</td>
      <td className="py-1 text-right text-muted">
        {s.exit_mix ? `${s.exit_mix.target}/${s.exit_mix.stop}/${s.exit_mix.time}` : "—"}
      </td>
      <td className={`py-1 text-right ${signCls(s.net_rs)}`}>{rs(s.net_rs)}</td>
      <td className="py-1 text-right text-muted">
        {gates ? (gates[key]
          ? (gates[key].verdict_due > 0 ? `${gates[key].verdict_due} to go` : "matured")
          : "—") : "—"}
      </td>
    </tr>
  );
  return (
    <table className="w-full text-[11px]">
      <thead className="text-[10px] uppercase text-muted">
        <tr className="border-b border-edge/60">
          <th className="py-1 text-left font-normal">rule</th>
          <th className="py-1 text-right font-normal">n</th>
          <th className="py-1 text-right font-normal">win</th>
          <th className="py-1 text-right font-normal">avg/trade</th>
          <th className="py-1 text-right font-normal" title="profit factor">pf</th>
          <th className="py-1 text-right font-normal" title="exits: target/stop/time">t/s/t</th>
          <th className="py-1 text-right font-normal">net</th>
          <th className="py-1 text-right font-normal" title="closed forward trades still needed before the 30-sample keep/kill verdict">verdict</th>
        </tr>
      </thead>
      <tbody className="font-mono">
        {rules.map((k) => row(k, block.per_rule[k]))}
        {row("__all__", block.total, "all rules")}
      </tbody>
    </table>
  );
}

/** Hour-of-day travel share as tiny bars — the venue's clock at a glance. */
function HourBars({ v, busiest }: { v: GoldVenueClimatology; busiest: number[] }) {
  const max = Math.max(...v.hourly_travel.map((h) => h.share_pct), 1);
  return (
    <div className="flex h-14 items-end gap-px">
      {v.hourly_travel.map((h) => (
        <div key={h.hour} className="flex flex-1 flex-col items-center gap-0.5"
             title={`${String(h.hour).padStart(2, "0")}:00 IST — ${h.share_pct}% of travel`}>
          <div className={busiest.includes(h.hour) ? "w-full bg-accent" : "w-full bg-edge"}
               style={{ height: `${Math.max(2, (h.share_pct / max) * 40)}px` }} />
          <span className="text-[8px] text-muted">{h.hour}</span>
        </div>
      ))}
    </div>
  );
}

function VenuePanel({ label, v, tag }: { label: string; v: GoldVenueClimatology; tag: string }) {
  return (
    <div className="rounded border border-edge/60 bg-panel2 px-3 py-2">
      <p className="text-[10px] uppercase tracking-wide text-muted">
        {label} <span className="normal-case">· {tag}</span>
      </p>
      <HourBars v={v} busiest={v.busiest_hours_ist} />
      <div className="mt-1 grid grid-cols-2 gap-x-3 gap-y-0.5 font-mono text-[10px] text-muted">
        <span>busiest hours (IST)</span>
        <span className="text-accent">{v.busiest_hours_ist.map((h) => `${h}:00`).join(" · ")}</span>
        <span>17:00–21:00 share</span>
        <span>{v.evening_share_pct != null ? `${v.evening_share_pct}%` : "—"}</span>
        {v.avg_day_range_pct != null && (
          <>
            <span>avg day range</span>
            <span>{v.avg_day_range_pct}%</span>
          </>
        )}
        {v.avg_abs_gap_pct != null && (
          <>
            <span>avg |overnight gap|</span>
            <span>{v.avg_abs_gap_pct}%</span>
          </>
        )}
        {v.gap_over_half_pct != null && (
          <>
            <span>days gapping ≥0.5%</span>
            <span>{v.gap_over_half_pct}%</span>
          </>
        )}
      </div>
    </div>
  );
}

function Ledger({ rows }: { rows: GoldCard[] }) {
  return (
    <div className="max-h-[28rem] overflow-auto">
      <table className="w-full text-[11px]">
        <thead className="sticky top-0 bg-panel text-[10px] uppercase text-muted">
          <tr className="border-b border-edge/60">
            <th className="py-1 text-left font-normal">date</th>
            <th className="py-1 text-left font-normal">rule</th>
            <th className="py-1 text-left font-normal">phase</th>
            <th className="py-1 text-left font-normal">dir</th>
            <th className="py-1 text-right font-normal">signal</th>
            <th className="py-1 text-right font-normal">in</th>
            <th className="py-1 text-right font-normal">target</th>
            <th className="py-1 text-right font-normal">stop</th>
            <th className="py-1 text-right font-normal">out</th>
            <th className="py-1 text-left font-normal">exit</th>
            <th className="py-1 text-right font-normal">pts</th>
            <th className="py-1 text-right font-normal">net</th>
          </tr>
        </thead>
        <tbody className="font-mono">
          {rows.map((t) => (
            <tr key={`${t.date}-${t.rule}`} className="border-b border-edge/30">
              <td className="py-1 text-left">{t.date}</td>
              <td className="py-1 text-left">{t.rule}</td>
              <td className={`py-1 text-left ${t.phase === "forward" ? "text-accent" : "text-muted"}`}>
                {t.phase}
              </td>
              <td className={`py-1 text-left ${t.direction === "LONG" ? "text-bull" : "text-bear"}`}>
                {t.direction === "LONG" ? "L" : "S"}
              </td>
              <td className={`py-1 text-right ${signCls(t.signal_pct)}`}>{signed(t.signal_pct, 2)}%</td>
              <td className="py-1 text-right text-muted">{fmt(t.entry_px, 0)}</td>
              <td className="py-1 text-right text-muted">{fmt(t.target_px, 0)}</td>
              <td className="py-1 text-right text-muted">{fmt(t.stop_px, 0)}</td>
              <td className="py-1 text-right text-muted">{fmt(t.exit_px, 0)}</td>
              <td className="py-1 text-left text-muted">{t.exit_reason}</td>
              <td className={`py-1 text-right ${signCls(t.points)}`}>{signed(t.points, 0)}</td>
              <td className={`py-1 text-right ${signCls(t.net_rs)}`}>{rs(t.net_rs)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="mt-1 text-[10px] text-muted">
        GOLDM mini, 1 lot: ₹10/point of the per-10g quote, ₹250 round-trip charges.
        Exits idealised at the level — no slippage model yet. Newest first.
      </p>
    </div>
  );
}

function errHint(err: string): string {
  if (err.includes("404")) {
    return "The backend hasn't loaded the /gold endpoints yet — they arrive with the next backend restart.";
  }
  return `${err} — is the backend running?`;
}

export function GoldLab() {
  const status = usePolling(() => api.goldStatus(), 20000, []);
  const live = usePolling(() => api.goldLive(), 45000, []);
  const [r, setR] = useState<GoldResults | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState<"sync" | "xau" | "analyze" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [ledger, setLedger] = useState<"none" | "forward" | "backtest">("none");
  // One slot per phase: each response lands in its own key, so a slow
  // "forward" reply can never render under the "backtest" toggle or block
  // the right refetch (review catch).
  const [rows, setRows] = useState<Partial<Record<"forward" | "backtest", GoldTradesResponse>>>({});
  const [ledgerErr, setLedgerErr] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setR(await api.goldResults());
      setError(null);
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      if (!msg.startsWith("404")) setError(errHint(msg));
      setR(null);
    } finally {
      setLoaded(true);
    }
  }, []);
  useEffect(() => { void load(); }, [load]);

  // A server-side re-analysis shows up as a newer generated_at on the status
  // poll — reload then (strictly-newer compare, same rules as the other labs).
  const generatedAt = status.data?.results_generated_at;
  const shownGeneratedAt = r?.generated_at;
  useEffect(() => {
    if (!loaded || !generatedAt) return;
    if (!shownGeneratedAt || generatedAt > shownGeneratedAt) {
      setRows({});
      void load();
    }
  }, [generatedAt, shownGeneratedAt, loaded, load]);

  useEffect(() => {
    if (ledger === "none" || rows[ledger]) return;
    const which = ledger;
    setLedgerErr(null);
    void api.goldTrades("all", which, 2000)
      .then((t) => setRows((r) => ({ ...r, [which]: t })))
      .catch((e) => setLedgerErr(
        `Ledger fetch failed: ${e instanceof Error ? e.message : String(e)}`));
  }, [ledger, rows]);

  const run = async (mode: "sync" | "xau" | "analyze") => {
    setBusy(mode);
    setError(null);
    setNote(null);
    try {
      if (mode === "sync") await api.goldSync();
      if (mode === "xau") {
        const s = await api.goldSyncXau();
        setNote(`XAUUSD backfill: ${JSON.stringify(s)}`);
      }
      setR(await api.goldAnalyze());
      setRows({});
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      setError(msg.includes("401") || msg.toLowerCase().includes("login")
        ? "Kite login required — authenticate on the dashboard first, then retry."
        : msg);
    } finally {
      setBusy(null);
    }
  };

  const clim = r?.climatology;
  const liveLoginNeeded = live.error?.includes("401");

  return (
    <div className="mx-auto flex min-h-screen max-w-6xl flex-col gap-3 p-3">
      <header className="card flex flex-wrap items-center gap-3 px-4 py-3">
        <a href="/" className="text-xs text-muted hover:text-white">← Dashboard</a>
        <ModuleSwitcher />
        <h1 className="text-sm font-semibold">Gold · MCX + XAUUSD</h1>
        <span className="tag bg-accent/15 text-[10px] text-accent"
              title="pre-registered candle rules, graded shadow-first; only forward samples count">
          shadow lab · paper only
        </span>
        {status.data && (
          <span className="text-[11px] text-muted">
            {status.data.contract ?? status.data.symbol} · frozen {status.data.freeze_date}
          </span>
        )}
        <div className="ml-auto flex items-center gap-2">
          {status.data && (
            <span className="text-[10px] text-muted">
              {fmtInt(status.data.m3_bars)} 3m · {fmtInt(status.data.day_bars)} day ·{" "}
              {fmtInt(status.data.xau_bars)} xau bars
            </span>
          )}
          <button onClick={() => run("sync")} disabled={busy !== null}
            className="rounded bg-accent/20 px-3 py-1 text-xs font-medium text-accent hover:bg-accent/30 disabled:opacity-50">
            {busy === "sync" ? "Syncing…" : "Sync MCX + Analyze"}
          </button>
          <button onClick={() => run("xau")} disabled={busy !== null}
            title="Dukascopy 1-min backfill — first run fetches ~3 years of day files (a few minutes); re-runs only fetch what's missing"
            className="rounded bg-panel2 px-3 py-1 text-xs text-muted hover:text-white disabled:opacity-50">
            {busy === "xau" ? "Backfilling…" : "Backfill XAUUSD"}
          </button>
          <button onClick={() => run("analyze")} disabled={busy !== null}
            className="rounded bg-panel2 px-3 py-1 text-xs text-muted hover:text-white disabled:opacity-50">
            {busy === "analyze" ? "Analyzing…" : "Re-analyze"}
          </button>
        </div>
      </header>

      {error && (
        <div className="card border-bear/50 bg-bear/10 px-4 py-2 text-xs text-bear">{error}</div>
      )}
      {note && (
        <div className="card px-4 py-2 font-mono text-[10px] text-muted">{note}</div>
      )}

      {loaded && !r && !error && (
        <div className="card px-4 py-6 text-center text-xs text-muted">
          No analysis yet — click <span className="text-accent">Sync MCX + Analyze</span> (needs a
          Kite login). <span className="text-accent">Backfill XAUUSD</span> is optional and free —
          it powers the cross-venue climatology check.
        </div>
      )}

      {/* Today's paper cards — the same states the (opt-in) alert loop watches. */}
      <section className="card px-4 py-3">
        <div className="mb-2 flex flex-wrap items-baseline gap-3">
          <h2 className="text-xs font-semibold uppercase tracking-wide text-muted">
            Today · paper cards · live
          </h2>
          {live.data && (
            <span className="text-[11px] text-muted">
              {live.data.contract.tradingsymbol} · prev close {fmt(live.data.prev_close, 0)}
              {!live.data.market_hours && " · outside MCX hours (Mon–Fri 09:00–23:30 IST)"}
            </span>
          )}
          {status.data && !status.data.live_enabled && (
            <span className="tag bg-panel2 text-[10px] text-muted"
                  title="GOLD_LIVE_ENABLED=false — the tab still reads live cards on demand; only the push-alert loop is off">
              alert loop off
            </span>
          )}
          {live.data && live.error && (
            <span className="tag bg-bear/15 text-[10px] text-bear" title={live.error}>
              live read failing — showing the {hhmm(live.data.fetched_at)} snapshot
            </span>
          )}
        </div>
        {live.data ? (
          <div className="grid gap-2 md:grid-cols-3">
            {live.data.cards.map((c) => <LiveCard key={c.rule} c={c} />)}
          </div>
        ) : (
          <p className="text-[11px] text-muted">
            {liveLoginNeeded
              ? "Kite login required for the live read — authenticate on the dashboard."
              : live.error
                ? errHint(live.error)
                : "Loading today's tape…"}
          </p>
        )}
      </section>

      {r && (
        <>
          <section className="card px-4 py-3">
            <div className="mb-2 flex items-baseline gap-3">
              <h2 className="text-xs font-semibold uppercase tracking-wide text-muted">
                The rules · frozen {r.freeze_date}
              </h2>
              <span className="text-[10px] text-muted">
                parameters are constants in the backend on purpose — editing one after grading kills
                the experiment; a new idea gets a new rule name
              </span>
            </div>
            <div className="grid gap-2 md:grid-cols-3">
              {r.rules.map((rule) => (
                <div key={rule.key} className="rounded border border-edge/60 bg-panel2 px-3 py-2">
                  <p className="text-[11px]">
                    <span className="font-mono font-semibold">{rule.key}</span>{" "}
                    <span className="font-semibold">{rule.name}</span>
                  </p>
                  <p className="mt-1 text-[10px] leading-relaxed text-muted">{rule.blurb}</p>
                </div>
              ))}
            </div>
            <p className="mt-2 rounded border border-edge/60 bg-panel2 px-2 py-1.5 text-[10px] leading-relaxed text-muted">
              {r.disclaimer}
            </p>
          </section>

          <section className="card grid gap-3 px-4 py-3">
            <h2 className="text-xs font-semibold uppercase tracking-wide text-muted">
              Forward — the blind exam · days after the freeze
            </h2>
            <PhaseTable block={r.forward} gates={r.gates.per_rule} />
            <p className="text-[10px] leading-relaxed text-muted">
              Verdicts at {r.gates.min_forward_samples}+ forward samples per rule. The live-money
              bar is far higher: {r.gates.live_money_bar}
            </p>
          </section>

          <section className="card grid gap-3 px-4 py-3">
            <h2 className="text-xs font-semibold uppercase tracking-wide text-muted">
              Backtest — hindsight-risked homework · days before the freeze
            </h2>
            <PhaseTable block={r.backtest} />
            <p className="text-[10px] leading-relaxed text-muted">
              Displayed for calibration, not authority — the reference study&apos;s first forward
              days inverted its backtest ranking, which is precisely why the sample gate exists.
            </p>
          </section>

          {clim && (
            <section className="card grid gap-3 px-4 py-3">
              <div className="flex flex-wrap items-baseline gap-3">
                <h2 className="text-xs font-semibold uppercase tracking-wide text-muted">
                  Climatology — the priors, published before the rules
                </h2>
                {clim.clock_agreement && (
                  <span className={`tag text-[10px] ${
                    clim.clock_agreement.verdict === "same clock"
                      ? "bg-bull/15 text-bull" : "bg-bear/15 text-bear"}`}>
                    {clim.clock_agreement.verdict}
                    {" · "}shared busy hours: {clim.clock_agreement.shared.map((h) => `${h}:00`).join(", ") || "none"}
                  </span>
                )}
              </div>
              <div className="grid gap-2 md:grid-cols-2">
                {clim.mcx && (
                  <VenuePanel label="MCX GOLDM" tag={`${clim.mcx.sessions} sessions · Kite`} v={clim.mcx} />
                )}
                {clim.xauusd ? (
                  <VenuePanel label="XAUUSD spot" tag={`${clim.xauusd.days} days · Dukascopy 1-min`} v={clim.xauusd} />
                ) : (
                  <div className="rounded border border-edge/60 bg-panel2 px-3 py-2 text-[11px] text-muted">
                    No XAUUSD bars yet — click Backfill XAUUSD to add the independent cross-venue
                    check (free, no login).
                  </div>
                )}
              </div>
              <p className="text-[10px] leading-relaxed text-muted">{clim.note}</p>
            </section>
          )}

          <section className="card px-4 py-3">
            <div className="flex items-center gap-3">
              <h2 className="text-xs font-semibold uppercase tracking-wide text-muted">Ledger</h2>
              {(["forward", "backtest"] as const).map((w) => (
                <button key={w}
                        onClick={() => setLedger(ledger === w ? "none" : w)}
                        className={`rounded px-2 py-0.5 text-[11px] ${
                          ledger === w ? "bg-accent/20 text-accent" : "bg-panel2 text-muted hover:text-white"}`}>
                  {w} ({(w === "forward" ? r.forward : r.backtest).total.n ?? 0})
                </button>
              ))}
            </div>
            {ledgerErr && <p className="mt-1 text-[10px] text-bear">{ledgerErr}</p>}
            {ledger !== "none" && rows[ledger] && (
              <div className="mt-2">
                {rows[ledger]!.count < rows[ledger]!.total && (
                  <p className="text-[10px] text-muted">
                    showing the newest {rows[ledger]!.count} of {rows[ledger]!.total} rows
                  </p>
                )}
                <Ledger rows={rows[ledger]!.rows} />
              </div>
            )}
          </section>

          <section className="card px-4 py-3">
            <h2 className="mb-1 text-xs font-semibold uppercase tracking-wide text-muted">
              Compliance
            </h2>
            <ul className="list-disc pl-4 text-[10px] leading-relaxed text-muted">
              {r.compliance.map((c) => <li key={c}>{c}</li>)}
            </ul>
          </section>
        </>
      )}
    </div>
  );
}
