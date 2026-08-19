"use client";

// Overnight — the confirmed variant of the Closing Day strategy, on its own
// surface so re-running one never rewrites the other's evidence.
//
// The rule: at 15:00, today's body (vs open) AND the last hour (vs 14:00)
// must agree. Confirmed green -> ATM CE, confirmed red -> ATM PE, sold at the
// next 09:50. Disagreement nights are stood aside — the tab shows their P&L
// anyway, because a filter is only believable next to what it removed.
//
// Same evidence discipline as ClosingLab: measured index numbers and modelled
// option numbers never share a card, and the model's error brief rides along.

import { useCallback, useEffect, useState } from "react";
import {
  ClosingMonthRow,
  ClosingTrade,
  ClosingWindow,
  OvernightFilters,
  OvernightLadderRung,
  OvernightResults,
  api,
} from "@/lib/api";
import { fmt, fmtInt, signed } from "@/lib/format";
import { usePolling } from "@/lib/usePolling";
import { ModuleSwitcher } from "./ModuleSwitcher";

const rs = (n: number | null | undefined) =>
  n == null ? "—" : `${n < 0 ? "−" : ""}₹${Math.abs(n).toLocaleString("en-IN", {
    maximumFractionDigits: 0,
  })}`;

const pct = (n: number | null | undefined, dp = 1) =>
  n == null ? "—" : `${n > 0 ? "+" : ""}${n.toFixed(dp)}%`;

const signCls = (v: number | null | undefined) =>
  v == null ? "text-muted" : v > 0 ? "text-bull" : v < 0 ? "text-bear" : "text-muted";

function ciVerdict(ci: [number, number] | null | undefined): string | null {
  if (!ci) return null;
  if (ci[0] > 0) return "above zero";
  if (ci[1] < 0) return "below zero";
  return "spans zero";
}

function Stat({ label, value, cls, sub }: {
  label: string; value: string; cls?: string; sub?: string;
}) {
  return (
    <div className="rounded border border-edge/60 bg-panel2 px-3 py-2">
      <p className="text-[10px] uppercase tracking-wide text-muted">{label}</p>
      <p className={`font-mono text-lg leading-tight ${cls ?? ""}`}>{value}</p>
      {sub && <p className="mt-0.5 text-[10px] text-muted">{sub}</p>}
    </div>
  );
}

function VerdictCards({ w }: { w: ClosingWindow }) {
  const u = w.underlying;
  const o = w.option;
  if (!u || !o) return null;
  return (
    <div className="grid gap-2 md:grid-cols-2">
      <div className="rounded border border-bull/30 bg-bull/5 p-3">
        <p className="mb-2 text-[10px] uppercase tracking-wide text-bull">
          Index · measured · confirmed nights
        </p>
        <div className="grid grid-cols-3 gap-2">
          <Stat label="continued" value={`${u.continued_pct}%`}
                cls={u.continued_pct > 50 ? "text-bull" : "text-bear"}
                sub={`n=${u.n} nights`} />
          <Stat label="median move" value={signed(u.median_signed_pts, 1)}
                cls={signCls(u.median_signed_pts)} sub="points, signed" />
          <Stat label="mean move" value={signed(u.mean_signed_pts, 1)}
                cls={signCls(u.mean_signed_pts)}
                sub={u.mean_signed_ci95
                  ? `95% CI ${u.mean_signed_ci95[0]} to ${u.mean_signed_ci95[1]} · ${ciVerdict(u.mean_signed_ci95)}`
                  : undefined} />
        </div>
      </div>
      <div className="rounded border border-accent/30 bg-accent/5 p-3">
        <p className="mb-2 text-[10px] uppercase tracking-wide text-accent">
          Option · modelled · confirmed nights
        </p>
        <div className="grid grid-cols-3 gap-2">
          <Stat label="win rate" value={`${o.win_rate_pct ?? 0}%`}
                cls={(o.win_rate_pct ?? 0) > 50 ? "text-bull" : "text-bear"}
                sub={`n=${o.n} trades`} />
          <Stat label="mean / trade" value={pct(o.mean_pct)} cls={signCls(o.mean_pct)}
                sub={o.mean_net_pct_ci95
                  ? `95% CI ${o.mean_net_pct_ci95[0]} to ${o.mean_net_pct_ci95[1]} · ${ciVerdict(o.mean_net_pct_ci95)}`
                  : undefined} />
          <Stat label="total" value={rs(o.total_net_rs)} cls={signCls(o.total_net_rs)}
                sub={`${w.lots ?? 1} lot · max DD ${rs(o.max_drawdown_rs)}`} />
        </div>
      </div>
    </div>
  );
}

/** The reason this tab exists, as one table: what the filter kept, what it
 *  removed, and what taking every night would have done. */
function FilterEvidence({ label, w }: { label: string; w: OvernightResults["primary"] }) {
  const rows: { name: string; o?: { total_net_rs?: number; win_rate_pct?: number; mean_pct?: number; median_pct?: number; n?: number } }[] = [
    { name: "unfiltered (Closing Day baseline)", o: { ...w.unfiltered?.option, n: w.nights_considered } },
    { name: "confirmed nights — THE STRATEGY", o: w.traded?.option },
    { name: "disagreement nights — stood aside", o: w.skipped?.option },
  ];
  return (
    <div>
      <p className="mb-1 text-[10px] uppercase tracking-wide text-muted">{label}</p>
      <table className="w-full text-[11px]">
        <thead className="text-[10px] uppercase text-muted">
          <tr className="border-b border-edge/60">
            <th className="py-1 text-left font-normal">nights</th>
            <th className="py-1 text-right font-normal">n</th>
            <th className="py-1 text-right font-normal">win</th>
            <th className="py-1 text-right font-normal">mean %</th>
            <th className="py-1 text-right font-normal">median %</th>
            <th className="py-1 text-right font-normal">total</th>
          </tr>
        </thead>
        <tbody className="font-mono">
          {rows.map((r) => (
            <tr key={r.name}
                className={`border-b border-edge/30 ${r.name.includes("STRATEGY") ? "bg-accent/10" : ""}`}>
              <td className="py-1 text-left font-sans">{r.name}</td>
              <td className="py-1 text-right text-muted">{r.o?.n ?? "—"}</td>
              <td className="py-1 text-right">{r.o?.win_rate_pct != null ? `${r.o.win_rate_pct}%` : "—"}</td>
              <td className={`py-1 text-right ${signCls(r.o?.mean_pct)}`}>{pct(r.o?.mean_pct)}</td>
              <td className={`py-1 text-right ${signCls(r.o?.median_pct)}`}>{pct(r.o?.median_pct)}</td>
              <td className={`py-1 text-right ${signCls(r.o?.total_net_rs)}`}>{rs(r.o?.total_net_rs)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {(w.unconfirmable?.n ?? 0) > 0 && (
        <p className="mt-1 text-[10px] text-muted">
          {w.unconfirmable?.n} night(s) unconfirmable (flat body, flat last hour, or no 14:00 bar).
        </p>
      )}
    </div>
  );
}

function MonthTable({ rows }: { rows: ClosingMonthRow[] }) {
  return (
    <table className="w-full text-[11px]">
      <thead className="text-[10px] uppercase text-muted">
        <tr className="border-b border-edge/60">
          <th className="py-1 text-left font-normal">month</th>
          <th className="py-1 text-right font-normal">trades</th>
          <th className="py-1 text-right font-normal">net</th>
          <th className="py-1 text-right font-normal">mean %</th>
          <th className="py-1 text-right font-normal">win</th>
          <th className="py-1 text-right font-normal">best</th>
          <th className="py-1 text-right font-normal">worst</th>
        </tr>
      </thead>
      <tbody className="font-mono">
        {rows.map((m) => (
          <tr key={m.month} className="border-b border-edge/30">
            <td className="py-1 text-left font-sans">{m.month}</td>
            <td className="py-1 text-right text-muted">{m.trades}</td>
            <td className={`py-1 text-right ${signCls(m.net_rs)}`}>{rs(m.net_rs)}</td>
            <td className={`py-1 text-right ${signCls(m.mean_net_pct)}`}>{pct(m.mean_net_pct)}</td>
            <td className="py-1 text-right">{m.win_rate_pct}%</td>
            <td className="py-1 text-right text-bull">{rs(m.best_rs)}</td>
            <td className="py-1 text-right text-bear">{rs(m.worst_rs)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function Ledger({ trades, note }: { trades: ClosingTrade[]; note: string }) {
  return (
    <div className="max-h-[32rem] overflow-auto">
      <table className="w-full text-[11px]">
        <thead className="sticky top-0 bg-panel text-[10px] uppercase text-muted">
          <tr className="border-b border-edge/60">
            <th className="py-1 text-left font-normal">date</th>
            <th className="py-1 text-left font-normal">dir</th>
            <th className="py-1 text-right font-normal" title="today's 09:15 open">open</th>
            <th className="py-1 text-right font-normal" title="the 14:00 print — the confirmation read">14:00</th>
            <th className="py-1 text-right font-normal" title="the 15:00 print — the signal">15:00</th>
            <th className="py-1 text-right font-normal" title="the next session's 09:50 print — the exit">next 09:50</th>
            <th className="py-1 text-center font-normal"
                title="pre-registered flags: V = vol expansion, R = range position outside the middle band. Green = pass, grey = fail, faint = unknown.">
              flags
            </th>
            <th className="py-1 text-right font-normal">strike</th>
            <th className="py-1 text-right font-normal">dte</th>
            <th className="py-1 text-right font-normal">move</th>
            <th className="py-1 text-right font-normal">in</th>
            <th className="py-1 text-right font-normal">out</th>
            <th className="py-1 text-right font-normal">net %</th>
            <th className="py-1 text-right font-normal">net</th>
          </tr>
        </thead>
        <tbody className="font-mono">
          {trades.map((t) => (
            <tr key={t.date} className="border-b border-edge/30">
              <td className="py-1 text-left">{t.date}</td>
              <td className={`py-1 text-left ${t.direction === "CE" ? "text-bull" : "text-bear"}`}>
                {t.direction}
                {t.extrapolated && <span title="entry VIX outside the calibrated range"
                                         className="ml-1 text-muted">*</span>}
              </td>
              <td className="py-1 text-right text-muted">{fmt(t.day_open, 0)}</td>
              <td className="py-1 text-right text-muted">{fmt(t.p1400, 0)}</td>
              <td className="py-1 text-right text-muted">{fmt(t.signal_price, 0)}</td>
              <td className={`py-1 text-right ${signCls(t.signed_move_pts)}`}>{fmt(t.exit_spot, 0)}</td>
              <td className="py-1 text-center"><FlagChips t={t} /></td>
              <td className="py-1 text-right text-muted">{fmt(t.strike, 0)}</td>
              <td className="py-1 text-right text-muted">{t.dte_entry.toFixed(0)}</td>
              <td className={`py-1 text-right ${signCls(t.signed_move_pts)}`}>{signed(t.signed_move_pts, 0)}</td>
              <td className="py-1 text-right text-muted">{fmt(t.fill_in, 1)}</td>
              <td className="py-1 text-right text-muted">{fmt(t.fill_out, 1)}</td>
              <td className={`py-1 text-right ${signCls(t.net_pct)}`}>{pct(t.net_pct, 0)}</td>
              <td className={`py-1 text-right ${signCls(t.net_rs)}`}>{rs(t.net_rs)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="mt-1 text-[10px] text-muted">{note}</p>
    </div>
  );
}


/** Compact chips for the two pre-registered flags on a ledger row. */
function FlagChips({ t }: { t: ClosingTrade }) {
  const chip = (label: string, v: boolean | null | undefined, title: string) => (
    <span title={title}
          className={`rounded px-1 text-[9px] font-mono ${
            v === true ? "bg-bull/20 text-bull" : v === false ? "bg-panel2 text-muted" : "text-muted/40"}`}>
      {label}
    </span>
  );
  return (
    <span className="inline-flex gap-0.5">
      {chip("V", t.f_vol_expand, `vol expansion: VIX vs prev ${t.vix_vs_prev ?? "?"} / day ${t.vix_day_chg ?? "?"}`)}
      {chip("R", t.f_midrange, `range position ${t.range_pos ?? "?"} (mid-range 0.35–0.65 fails)`)}
    </span>
  );
}

function LadderTable({ rungs, label }: { rungs: OvernightLadderRung[]; label: string }) {
  return (
    <div>
      <p className="mb-1 text-[10px] uppercase tracking-wide text-muted">{label}</p>
      <table className="w-full text-[11px]">
        <thead className="text-[10px] uppercase text-muted">
          <tr className="border-b border-edge/60">
            <th className="py-1 text-left font-normal">rung</th>
            <th className="py-1 text-right font-normal">n</th>
            <th className="py-1 text-right font-normal">win</th>
            <th className="py-1 text-right font-normal">mean %</th>
            <th className="py-1 text-right font-normal">median %</th>
            <th className="py-1 text-right font-normal">total</th>
            <th className="py-1 text-right font-normal">max DD</th>
            <th className="py-1 text-right font-normal" title="what this rung's added filter removed">removed</th>
          </tr>
        </thead>
        <tbody className="font-mono">
          {rungs.map((rg, i) => (
            <tr key={rg.label} className={`border-b border-edge/30 ${i === rungs.length - 1 ? "bg-accent/10" : ""}`}>
              <td className="py-1 text-left font-sans">{rg.label}</td>
              <td className="py-1 text-right text-muted">{rg.kept.n}</td>
              <td className="py-1 text-right">{rg.kept.win_pct != null ? `${rg.kept.win_pct}%` : "—"}</td>
              <td className={`py-1 text-right ${signCls(rg.kept.mean_pct)}`}>{pct(rg.kept.mean_pct)}</td>
              <td className={`py-1 text-right ${signCls(rg.kept.median_pct)}`}>{pct(rg.kept.median_pct)}</td>
              <td className={`py-1 text-right ${signCls(rg.kept.total_rs)}`}>{rs(rg.kept.total_rs)}</td>
              <td className="py-1 text-right text-bear">{rs(rg.kept.max_dd_rs)}</td>
              <td className="py-1 text-right text-muted">
                {rg.removed ? `${rg.removed.n} @ ${pct(rg.removed.mean_pct)}` : "—"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** The pre-registered filter panel: definitions, backtest ladder, and the
 *  live scoreboard that is the only test the Round-4 numbers have left. */
function FiltersPanel({ f, primary, threeYear }: {
  f: OvernightFilters;
  primary?: OvernightLadderRung[];
  threeYear?: OvernightLadderRung[];
}) {
  const live = f.live;
  if (!live) return null; // stale results file from before the filters block
  return (
    <section className="card grid gap-3 px-4 py-3">
      <div className="flex flex-wrap items-baseline gap-3">
        <h2 className="text-xs font-semibold uppercase tracking-wide text-muted">
          Pre-registered filters
        </h2>
        <span className="tag bg-panel2 text-[10px] text-muted">
          registered {f.registered_on} · flags, not gates
        </span>
      </div>

      <div className="grid gap-2 md:grid-cols-2">
        {f.definitions.map((d) => (
          <div key={d.key} className="rounded border border-edge/60 bg-panel2 px-3 py-2">
            <p className="text-[11px]">
              <span className="font-semibold">{d.label}</span>{" "}
              <span className={`tag ${d.status === "verified" ? "bg-bull/15 text-bull" : "bg-accent/15 text-accent"} text-[9px]`}>
                {d.status}
              </span>
            </p>
            <p className="mt-1 text-[11px]">{d.rule}</p>
            <p className="mt-1 text-[10px] leading-relaxed text-muted">{d.evidence}</p>
          </div>
        ))}
      </div>

      {primary && <LadderTable rungs={primary} label="Backtest ladder · holdout 1 year · modelled" />}
      {threeYear && <LadderTable rungs={threeYear} label="Backtest ladder · 3 years · modelled" />}

      <div className="rounded border border-accent/30 bg-accent/5 px-3 py-2">
        <p className="text-[10px] uppercase tracking-wide text-accent">
          Live scoreboard — the test that counts
        </p>
        <div className="mt-1 grid grid-cols-2 gap-2 md:grid-cols-4">
          <Stat label="live nights" value={String(live.live_nights)}
                sub={`after ${live.registered_on}`} />
          <Stat label="filtered nights" value={String(live.filtered_nights)}
                sub={`${live.verdict_due} more until a verdict (${live.min_sample} rule)`} />
          <Stat label="filters pass" value={live.filters_pass.n ? pct(live.filters_pass.mean_pct) : "—"}
                cls={signCls(live.filters_pass.mean_pct)}
                sub={live.filters_pass.n ? `n=${live.filters_pass.n} · ${rs(live.filters_pass.total_rs)}` : "no nights yet"} />
          <Stat label="filters fail" value={live.filters_fail.n ? pct(live.filters_fail.mean_pct) : "—"}
                cls={signCls(live.filters_fail.mean_pct)}
                sub={live.filters_fail.n ? `n=${live.filters_fail.n} · ${rs(live.filters_fail.total_rs)}` : "no nights yet"} />
        </div>
        <p className="mt-1 text-[10px] leading-relaxed text-muted">{live.note}</p>
      </div>

      <p className="text-[10px] leading-relaxed text-muted">{f.note}</p>
    </section>
  );
}

function errHint(err: string): string {
  if (err.includes("404")) {
    return "The backend hasn't loaded the /overnight endpoints yet — they arrive with the next backend restart.";
  }
  return `${err} — is the backend running?`;
}

export function OvernightLab() {
  const status = usePolling(() => api.overnightStatus(), 20000, []);
  const [r, setR] = useState<OvernightResults | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState<"sync" | "analyze" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [ledger, setLedger] = useState<"none" | "traded" | "skipped">("none");
  const [tradedRows, setTradedRows] = useState<ClosingTrade[]>([]);
  const [skippedRows, setSkippedRows] = useState<ClosingTrade[]>([]);

  const load = useCallback(async () => {
    try {
      setR(await api.overnightResults());
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

  useEffect(() => {
    if (ledger === "traded" && !tradedRows.length) {
      void api.overnightTrades("traded").then((t) => setTradedRows(t.rows)).catch(() => undefined);
    }
    if (ledger === "skipped" && !skippedRows.length) {
      void api.overnightTrades("skipped").then((t) => setSkippedRows(t.rows)).catch(() => undefined);
    }
  }, [ledger, tradedRows.length, skippedRows.length]);

  const run = async (mode: "sync" | "analyze") => {
    setBusy(mode);
    setError(null);
    try {
      if (mode === "sync") await api.overnightSync();
      setR(await api.overnightAnalyze());
      setTradedRows([]); setSkippedRows([]);
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      setError(msg.includes("401") || msg.toLowerCase().includes("login")
        ? "Kite login required — authenticate on the dashboard first, then retry."
        : msg);
    } finally {
      setBusy(null);
    }
  };

  const p = r?.primary;
  const vb = r?.validation_brief;

  return (
    <div className="mx-auto flex min-h-screen max-w-6xl flex-col gap-3 p-3">
      <header className="card flex flex-wrap items-center gap-3 px-4 py-3">
        <a href="/" className="text-xs text-muted hover:text-white">← Dashboard</a>
        <ModuleSwitcher />
        <h1 className="text-sm font-semibold">Overnight · NIFTY 50</h1>
        <span className="tag bg-accent/15 text-[10px] text-accent"
              title="day body + last-hour agreement, held 15:05 → next 09:50">
          confirmed hold
        </span>
        {p?.traded && (
          <span className="text-[11px] text-muted">
            {p.traded.n} traded / {p.skipped.n} stood aside · {p.traded.from} → {p.traded.to}
          </span>
        )}
        <div className="ml-auto flex items-center gap-2">
          {status.data && (
            <span className="text-[10px] text-muted">
              {fmtInt(status.data.index_bars)} index · {fmtInt(status.data.vix_bars)} vix bars
            </span>
          )}
          <button onClick={() => run("sync")} disabled={busy !== null}
            className="rounded bg-accent/20 px-3 py-1 text-xs font-medium text-accent hover:bg-accent/30 disabled:opacity-50">
            {busy === "sync" ? "Syncing…" : "Sync + Analyze"}
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

      {loaded && !r && !error && (
        <div className="card px-4 py-6 text-center text-xs text-muted">
          No analysis yet — click <span className="text-accent">Sync + Analyze</span>.
          (Shares its data with the Closing Day tab; only the analysis differs.)
        </div>
      )}

      {r && p && (
        <>
          <section className="card px-4 py-3">
            <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted">
              The rule
            </h2>
            <dl className="grid gap-x-6 gap-y-1 text-[11px] md:grid-cols-2">
              {Object.entries(r.rule).map(([k, v]) => (
                <div key={k} className="flex gap-2">
                  <dt className="w-14 shrink-0 text-muted">{k}</dt>
                  <dd>{v}</dd>
                </div>
              ))}
            </dl>
            <p className="mt-2 rounded border border-edge/60 bg-panel2 px-2 py-1.5 text-[10px] leading-relaxed text-muted">
              {r.disclaimer}
            </p>
          </section>

          <VerdictCards w={p.traded} />

          <section className="card grid gap-4 px-4 py-3">
            <h2 className="text-xs font-semibold uppercase tracking-wide text-muted">
              What the filter kept vs removed · modelled
            </h2>
            <FilterEvidence label="1 year" w={p} />
            {r.three_year && <FilterEvidence label="3 years" w={r.three_year} />}
            {vb && (
              <p className="rounded border border-edge/60 bg-panel2 px-2 py-1.5 text-[10px] leading-relaxed text-muted">
                Model error: {vb.oos_level_err_pct ?? "—"}% out-of-sample on premium level,{" "}
                {vb.overnight_err_pp ?? "—"}pp on the overnight change (n={vb.overnight_n_pairs}),
                bias {vb.overnight_bias_pp ?? "—"}pp. {vb.note}
              </p>
            )}
          </section>

          {r.filters && (
            <FiltersPanel f={r.filters} primary={p.filter_ladder} threeYear={r.three_year?.filter_ladder} />
          )}

          {p.traded?.per_month && (
            <section className="card px-4 py-3">
              <div className="mb-2 flex flex-wrap items-baseline gap-3">
                <h2 className="text-xs font-semibold uppercase tracking-wide text-muted">
                  Month by month · confirmed nights · modelled
                </h2>
                <span className="text-[11px] text-muted">
                  mean{" "}
                  <span className={signCls(p.traded.per_month.mean_net_rs)}>
                    {rs(p.traded.per_month.mean_net_rs)}
                  </span>{" "}
                  · median{" "}
                  <span className={signCls(p.traded.per_month.median_net_rs)}>
                    {rs(p.traded.per_month.median_net_rs)}
                  </span>{" "}
                  · {p.traded.per_month.positive_months}/{p.traded.per_month.months} months positive
                  · {p.traded.per_month.mean_trades} trades/month
                </span>
              </div>
              <MonthTable rows={p.traded.per_month.rows} />
            </section>
          )}

          <section className="card px-4 py-3">
            <div className="flex items-center gap-3">
              <h2 className="text-xs font-semibold uppercase tracking-wide text-muted">Ledgers</h2>
              {(["traded", "skipped"] as const).map((w) => (
                <button key={w}
                        onClick={() => setLedger(ledger === w ? "none" : w)}
                        className={`rounded px-2 py-0.5 text-[11px] ${
                          ledger === w ? "bg-accent/20 text-accent" : "bg-panel2 text-muted hover:text-white"}`}>
                  {w === "traded"
                    ? `confirmed (${p.traded?.n ?? 0})`
                    : `stood aside (${p.skipped?.n ?? 0})`}
                </button>
              ))}
            </div>
            {ledger === "traded" && (
              <div className="mt-2">
                <Ledger trades={tradedRows}
                        note="Confirmed nights: 15:00 on the same side of both the open and the 14:00 print. * = entry VIX outside the calibrated range. `move` is signed from the 15:05 fill." />
              </div>
            )}
            {ledger === "skipped" && (
              <div className="mt-2">
                <Ledger trades={skippedRows}
                        note="Disagreement nights the strategy stood aside from — what buying anyway (per the day body) would have done. These are hypothetical fills shown as evidence, not trades." />
              </div>
            )}
          </section>
        </>
      )}
    </div>
  );
}
