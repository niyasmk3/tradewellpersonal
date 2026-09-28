"use client";

/**
 * Strategy Lab — 40-strategy NIFTY F&O research + automated paper trading.
 *
 * Eight sections behind one sub-nav: Command (overview), Library (the 40),
 * Backtest, Paper, Analytics (run comparison), Learning (champion/challenger),
 * Risk, Data. Everything is simulation; evidence classes (backtest /
 * walk-forward / paper) are labelled and never blended.
 */

import { useCallback, useEffect, useMemo, useState } from "react";

import {
  api,
  LabBucket,
  LabExperiment,
  LabOverview,
  LabParamSpec,
  LabPosition,
  LabRun,
  LabRunSummary,
  LabStrategy,
  LabVwap3Bucket,
  LabVwap3Cell,
  LabVwap3Day,
  LabVwap3Result,
  LabVwap3Summary,
} from "@/lib/api";
import { usePolling } from "@/lib/usePolling";
import { ModuleSwitcher } from "@/components/ModuleSwitcher";
import { Vwap3Chart } from "@/components/Vwap3Chart";

// ---- tiny helpers (GoldLab idioms) ----------------------------------------

function rs(v: number | null | undefined, dp = 0): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  const s = Math.abs(v).toLocaleString("en-IN", {
    minimumFractionDigits: dp, maximumFractionDigits: dp,
  });
  return v < 0 ? `−₹${s}` : `₹${s}`;
}

function num(v: number | null | undefined, dp = 2): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return v.toLocaleString("en-IN", { maximumFractionDigits: dp });
}

function signCls(v: number | null | undefined): string {
  if (v === null || v === undefined || v === 0) return "text-muted";
  return v > 0 ? "text-bull" : "text-bear";
}

function istTime(ts: number): string {
  return new Date(ts * 1000).toLocaleTimeString("en-IN", {
    timeZone: "Asia/Kolkata", hour: "2-digit", minute: "2-digit",
    hour12: false,
  });
}

const READY_CLS: Record<string, string> = {
  ready: "bg-bull/15 text-bull",
  window_limited: "bg-accent/15 text-accent",
  awaiting_sync: "bg-yellow-500/15 text-yellow-400",
  blocked: "bg-bear/15 text-bear",
  missing: "bg-bear/15 text-bear",
  thin: "bg-yellow-500/15 text-yellow-400",
};

const READY_LABEL: Record<string, string> = {
  ready: "ready",
  window_limited: "window-limited",
  awaiting_sync: "awaiting sync",
  blocked: "data-blocked",
};

function Tile({ label, value, cls = "", hint }: {
  label: string; value: string; cls?: string; hint?: string;
}) {
  return (
    <div className="rounded border border-edge/60 bg-panel2 px-3 py-2" title={hint}>
      <div className="text-[10px] uppercase tracking-wide text-muted">{label}</div>
      <div className={`font-mono text-sm ${cls}`}>{value}</div>
    </div>
  );
}

function EquityCurve({ points, label }: { points: number[]; label: string }) {
  if (points.length < 2) {
    return <div className="text-[11px] text-muted">Not enough days for a curve yet.</div>;
  }
  const min = Math.min(0, ...points);
  const max = Math.max(0, ...points);
  const span = max - min || 1;
  const pts = points.map((v, i) => {
    const x = (i / (points.length - 1)) * 100;
    const y = 30 - ((v - min) / span) * 28;
    return `${i === 0 ? "M" : "L"}${x.toFixed(2)},${y.toFixed(2)}`;
  });
  const zeroY = 30 - ((0 - min) / span) * 28;
  const last = points[points.length - 1];
  return (
    <div>
      <div className="text-[10px] uppercase tracking-wide text-muted">{label}</div>
      <svg viewBox="0 0 100 32" preserveAspectRatio="none" className="h-16 w-full">
        <line x1="0" y1={zeroY} x2="100" y2={zeroY} stroke="currentColor" className="text-edge" strokeWidth="0.5" />
        <path d={pts.join(" ")} fill="none" strokeWidth="1"
          stroke="currentColor" className={last >= 0 ? "text-bull" : "text-bear"} />
      </svg>
    </div>
  );
}

function BucketTable({ title, buckets }: { title: string; buckets: Record<string, LabBucket> }) {
  const keys = Object.keys(buckets);
  if (!keys.length) return null;
  return (
    <div className="rounded border border-edge/60 bg-panel2 px-3 py-2">
      <div className="mb-1 text-[10px] uppercase tracking-wide text-muted">{title}</div>
      <table className="w-full text-[11px]">
        <thead className="text-[10px] uppercase text-muted">
          <tr><th className="text-left font-normal">bucket</th>
            <th className="text-right font-normal">n</th>
            <th className="text-right font-normal">win%</th>
            <th className="text-right font-normal">net</th>
            <th className="text-right font-normal">/trade</th></tr>
        </thead>
        <tbody className="font-mono">
          {keys.map((k) => {
            const b = buckets[k];
            return (
              <tr key={k} className="border-t border-edge/30">
                <td className="py-0.5">{k}{b.low_sample ? <span className="text-yellow-400" title="fewer than 30 trades — anecdote, not evidence"> ⚠</span> : null}</td>
                <td className="text-right">{b.n}</td>
                <td className="text-right">{num(b.win_rate, 1)}</td>
                <td className={`text-right ${signCls(b.net)}`}>{rs(b.net)}</td>
                <td className={`text-right ${signCls(b.expectancy)}`}>{rs(b.expectancy)}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function SummaryTiles({ s }: { s: LabRunSummary }) {
  return (
    <div className="grid gap-2 md:grid-cols-4">
      <Tile label="Trades" value={String(s.trades)} />
      <Tile label="Win rate" value={s.win_rate === null ? "—" : `${num(s.win_rate, 1)}%`}
        hint="Win rate is NOT profitability — read it next to expectancy and PF" />
      <Tile label="Expectancy /trade" value={rs(s.expectancy)} cls={signCls(s.expectancy)} />
      <Tile label="Net P&L" value={rs(s.net_pnl)} cls={signCls(s.net_pnl)} />
      <Tile label="Profit factor" value={num(s.profit_factor)} />
      <Tile label="Max drawdown" value={rs(s.max_drawdown)} cls={signCls(s.max_drawdown)} />
      <Tile label="Avg win / loss" value={`${rs(s.avg_win)} / ${rs(s.avg_loss)}`} />
      <Tile label="Longest loss streak" value={String(s.longest_loss_streak)} />
      <Tile label="Sharpe (daily)" value={num(s.sharpe)} hint="withheld under 20 trading days" />
      <Tile label="Time in market" value={s.time_in_market_pct === null ? "—" : `${num(s.time_in_market_pct, 1)}%`} />
      <Tile label="Peak margin (est.)" value={rs(s.peak_margin_rs)} hint={s.margin_note} />
      <Tile label="Return on capital" value={s.return_on_capital_pct === null ? "—" : `${num(s.return_on_capital_pct, 2)}%`} />
    </div>
  );
}

// ---- sections --------------------------------------------------------------

function CommandCenter() {
  const o = usePolling<LabOverview>(() => api.labOverview(), 15000, []);
  const [study, setStudy] = useState<LabVwap3Result | null>(null);
  useEffect(() => {
    api.labVwap3Latest().then((r) => setStudy(r.result)).catch(() => {});
  }, []);
  if (o.error) return <div className="card border-bear/50 bg-bear/10 px-4 py-2 text-xs text-bear">{o.error}</div>;
  if (!o.data) return <div className="card px-4 py-6 text-center text-xs text-muted">Loading…</div>;
  const d = o.data;
  return (
    <>
      {study && <Vwap3ProbCard r={study} compact />}
      <section className="card px-4 py-3">
        <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted">Paper portfolio — live simulation</h2>
        <div className="grid gap-2 md:grid-cols-4">
          <Tile label="Today P&L" value={rs(d.today_pnl)} cls={signCls(d.today_pnl)} />
          <Tile label="Trades today" value={String(d.today_trades)} />
          <Tile label="Open positions" value={String(d.open_positions)} />
          <Tile label="Exposure" value={rs(d.exposure_rs)} />
          <Tile label="Cumulative P&L" value={rs(d.cumulative_pnl)} cls={signCls(d.cumulative_pnl)}
            hint={`${d.paper_day_count} paper day-ledgers`} />
          <Tile label="Day win rate" value={d.win_rate_days === null ? "—" : `${num(d.win_rate_days, 1)}%`} />
          <Tile label="Day profit factor" value={num(d.profit_factor_days)} />
          <Tile label="Max drawdown" value={rs(d.max_drawdown)} cls={signCls(d.max_drawdown)} />
        </div>
        <div className="mt-3"><EquityCurve points={d.equity_curve.map((p) => p.cum)} label="Paper equity (cumulative ₹, day ledgers)" /></div>
      </section>
      <section className="card px-4 py-3">
        <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted">Engine & data</h2>
        <div className="grid gap-2 md:grid-cols-4">
          <Tile label="Strategies" value={`${d.strategies_total}`} />
          <Tile label="Paper-enabled" value={String(d.paper_enabled_count)} />
          <Tile label="Experimental arms" value={String(d.experimental_count)} />
          <Tile label="Suspended" value={String(d.suspended_count)} cls={d.suspended_count ? "text-yellow-400" : ""} />
          <Tile label="Paper engine" value={d.paper_engine_on ? "ARMED" : "off (LAB_PAPER_ENABLED)"}
            cls={d.paper_engine_on ? "text-bull" : "text-muted"} />
          <Tile label="Session" value={d.session.status ?? "—"} hint={d.session.day} />
          <Tile label="Emergency stop" value={d.emergency_stop ? "TRIPPED" : "clear"}
            cls={d.emergency_stop ? "text-bear" : "text-bull"} />
          <Tile label="Options data era" value={`${d.chain_era.from ?? "—"} →`}
            hint="Real option quotes exist only from this date; Kite deletes expired contracts" />
        </div>
        <div className="mt-2 grid gap-2 md:grid-cols-4">
          {Object.entries(d.data_readiness).map(([k, v]) => (
            <Tile key={k} label={`Strategies ${READY_LABEL[k] ?? k}`} value={String(v)} />
          ))}
        </div>
        <p className="mt-2 text-[10px] text-muted">{d.evidence_note}</p>
      </section>
    </>
  );
}

function ReadinessBadge({ r }: { r: { status: string; notes: string[] } }) {
  return (
    <span className={`tag ${READY_CLS[r.status] ?? "bg-panel2 text-muted"}`}
      title={r.notes.join("\n") || "all required data available"}>
      {READY_LABEL[r.status] ?? r.status}
    </span>
  );
}

function Library({ onBacktest }: { onBacktest: (sid: string) => void }) {
  const [rows, setRows] = useState<LabStrategy[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [q, setQ] = useState("");
  const [cat, setCat] = useState("all");
  const [open, setOpen] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(() => {
    api.labLibrary().then(setRows).catch((e) => setError(String(e.message ?? e)));
  }, []);
  useEffect(() => { load(); }, [load]);

  const cats = useMemo(() => rows ? Array.from(new Set(rows.map((r) => r.category_label))) : [], [rows]);
  const filtered = useMemo(() => (rows ?? []).filter((r) =>
    (cat === "all" || r.category_label === cat) &&
    (q === "" || `${r.sid} ${r.name} ${r.hypothesis}`.toLowerCase().includes(q.toLowerCase()))
  ), [rows, q, cat]);

  const togglePaper = async (r: LabStrategy) => {
    setBusy(r.sid);
    try {
      await api.labPaperToggle(r.sid, !r.paper_enabled);
      load();
    } catch (e) { setError(String((e as Error).message ?? e)); }
    setBusy(null);
  };

  if (error) return <div className="card border-bear/50 bg-bear/10 px-4 py-2 text-xs text-bear">{error}</div>;
  if (!rows) return <div className="card px-4 py-6 text-center text-xs text-muted">Loading catalog…</div>;
  return (
    <section className="card px-4 py-3">
      <div className="mb-2 flex flex-wrap items-baseline gap-3">
        <h2 className="text-xs font-semibold uppercase tracking-wide text-muted">Strategy library — {filtered.length}/{rows.length}</h2>
        <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="search…"
          className="rounded border border-edge bg-panel2 px-2 py-0.5 text-xs outline-none" />
        <select value={cat} onChange={(e) => setCat(e.target.value)}
          className="rounded border border-edge bg-panel2 px-2 py-0.5 text-xs">
          <option value="all">all categories</option>
          {cats.map((c) => <option key={c} value={c}>{c}</option>)}
        </select>
      </div>
      <div className="max-h-[34rem] overflow-auto scroll-thin">
        <table className="w-full text-[11px]">
          <thead className="sticky top-0 bg-panel text-[10px] uppercase text-muted">
            <tr className="border-b border-edge/60">
              <th className="py-1 text-left font-normal">id</th>
              <th className="text-left font-normal">strategy</th>
              <th className="text-left font-normal">inst</th>
              <th className="text-left font-normal">data</th>
              <th className="text-right font-normal" title="latest in-sample backtest">bt /trade</th>
              <th className="text-right font-normal">bt n</th>
              <th className="text-right font-normal">paper ₹</th>
              <th className="text-left font-normal">ver</th>
              <th className="text-right font-normal">actions</th>
            </tr>
          </thead>
          <tbody className="font-mono">
            {filtered.map((r) => {
              const run = r.latest_runs.insample ?? r.latest_runs.walkforward ?? r.latest_runs.oos;
              return (
                <>
                  <tr key={r.sid} className="cursor-pointer border-b border-edge/30 hover:bg-panel2/50"
                    onClick={() => setOpen(open === r.sid ? null : r.sid)}>
                    <td className="py-1">{r.sid}</td>
                    <td className="max-w-[16rem] truncate" title={r.hypothesis}>{r.name}</td>
                    <td>{r.instrument} {r.timeframe}</td>
                    <td><ReadinessBadge r={r.readiness} /></td>
                    <td className={`text-right ${signCls(run?.expectancy)}`}>{run ? rs(run.expectancy) : "—"}</td>
                    <td className="text-right">{run?.trades ?? "—"}{run && (run.trades ?? 0) < 30 ? <span className="text-yellow-400" title="under 30 trades"> ⚠</span> : null}</td>
                    <td className={`text-right ${signCls(r.paper?.net)}`}>{r.paper ? rs(r.paper.net) : "—"}</td>
                    <td>v{r.champion_version}{r.experimental_version ? ` +exp v${r.experimental_version}` : ""}</td>
                    <td className="text-right">
                      <button className="rounded bg-accent/20 px-2 py-0.5 text-accent hover:bg-accent/30"
                        onClick={(e) => { e.stopPropagation(); onBacktest(r.sid); }}>backtest</button>{" "}
                      <button disabled={busy === r.sid || r.readiness.status === "blocked" || r.readiness.status === "awaiting_sync"}
                        title={r.readiness.status === "awaiting_sync" ? "data not synced yet" : ""}
                        className={`rounded px-2 py-0.5 disabled:opacity-40 ${r.paper_enabled ? "bg-bull/20 text-bull" : "bg-panel2 text-muted hover:text-white"}`}
                        onClick={(e) => { e.stopPropagation(); togglePaper(r); }}>
                        {r.paper_enabled ? "paper ON" : "paper off"}
                      </button>
                    </td>
                  </tr>
                  {open === r.sid && (
                    <tr key={`${r.sid}-d`} className="border-b border-edge/30 bg-panel2/40">
                      <td colSpan={9} className="px-3 py-2 font-sans">
                        <div className="grid gap-2 md:grid-cols-2">
                          <div>
                            <div className="text-[10px] uppercase text-muted">Hypothesis</div>
                            <p className="text-[11px]">{r.hypothesis}</p>
                            <div className="mt-1 text-[10px] uppercase text-muted">Signal logic</div>
                            <p className="text-[11px]">{r.logic}</p>
                            {r.suspended_reason && <p className="mt-1 text-[11px] text-bear">suspended: {r.suspended_reason}</p>}
                          </div>
                          <div>
                            <div className="text-[10px] uppercase text-muted">Data needs · usable span</div>
                            <p className="font-mono text-[11px]">{r.data_needs.join(", ")}</p>
                            <p className="font-mono text-[11px]">{r.readiness.from ?? "—"} → {r.readiness.to ?? "—"}</p>
                            {r.readiness.notes.map((n, i) => <p key={i} className="text-[10px] text-yellow-400">{n}</p>)}
                            <div className="mt-1 text-[10px] uppercase text-muted">Parameters ({r.params.length}; defaults)</div>
                            <p className="font-mono text-[10px] text-muted">
                              {r.params.map((p) => `${p.key}=${String(p.default)}`).join("  ")}
                            </p>
                          </div>
                        </div>
                      </td>
                    </tr>
                  )}
                </>
              );
            })}
          </tbody>
        </table>
      </div>
      <p className="mt-1 text-[10px] text-muted">
        ⚠ = under 30 observations. Options strategies carry real quotes only inside the chain-snapshot era; earlier spans are refused, not modelled.
      </p>
    </section>
  );
}

function BacktestLab({ sid, setSid }: { sid: string; setSid: (s: string) => void }) {
  const [lib, setLib] = useState<LabStrategy[] | null>(null);
  const [overrides, setOverrides] = useState<Record<string, string>>({});
  const [fromDay, setFromDay] = useState("");
  const [toDay, setToDay] = useState("");
  const [capital, setCapital] = useState("500000");
  const [kind, setKind] = useState("insample");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<LabRun | null>(null);

  useEffect(() => { api.labLibrary().then(setLib).catch((e) => setError(String(e.message ?? e))); }, []);
  const strat = useMemo(() => lib?.find((r) => r.sid === sid) ?? null, [lib, sid]);
  useEffect(() => {
    setOverrides({});
    if (strat?.readiness.from) setFromDay(strat.readiness.from);
    if (strat?.readiness.to) setToDay(strat.readiness.to);
  }, [strat?.sid]);

  const run = async () => {
    if (!strat) return;
    setBusy(true); setError(null); setResult(null);
    try {
      const params: Record<string, unknown> = {};
      for (const [k, v] of Object.entries(overrides)) {
        if (v === "") continue;
        const spec = strat.params.find((p) => p.key === k);
        params[k] = spec?.kind === "choice" ? v : spec?.kind === "bool" ? v === "true" : Number(v);
      }
      const res = await api.labBacktest({
        sid, params: Object.keys(params).length ? params : null,
        from_day: fromDay || null, to_day: toDay || null,
        capital_rs: Number(capital) || 500000, kind,
      });
      if (res.status !== "done") {
        setError(`${res.status}: ${res.note ?? ""}`);
      } else {
        const full = await api.labRun(res.run_id, 500);
        setResult(full);
      }
    } catch (e) { setError(String((e as Error).message ?? e)); }
    setBusy(false);
  };

  const s = result?.summary as LabRunSummary | undefined;
  const curve = useMemo(() => {
    if (!s?.daily_pnl) return [];
    let c = 0; return s.daily_pnl.map((d) => { c += d.net; return c; });
  }, [s]);

  return (
    <>
      <section className="card px-4 py-3">
        <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted">Backtest lab</h2>
        <div className="flex flex-wrap items-center gap-2 text-xs">
          <select value={sid} onChange={(e) => setSid(e.target.value)}
            className="rounded border border-edge bg-panel2 px-2 py-1">
            {(lib ?? []).map((r) => (
              <option key={r.sid} value={r.sid}
                disabled={r.readiness.status === "blocked" || r.readiness.status === "awaiting_sync"}>
                {r.sid} · {r.name}{r.readiness.status !== "ready" ? ` (${READY_LABEL[r.readiness.status] ?? r.readiness.status})` : ""}
              </option>
            ))}
          </select>
          <label className="text-muted">from <input type="date" value={fromDay} onChange={(e) => setFromDay(e.target.value)}
            className="rounded border border-edge bg-panel2 px-2 py-1" /></label>
          <label className="text-muted">to <input type="date" value={toDay} onChange={(e) => setToDay(e.target.value)}
            className="rounded border border-edge bg-panel2 px-2 py-1" /></label>
          <label className="text-muted">capital ₹<input value={capital} onChange={(e) => setCapital(e.target.value)}
            className="w-24 rounded border border-edge bg-panel2 px-2 py-1 font-mono" /></label>
          <select value={kind} onChange={(e) => setKind(e.target.value)}
            className="rounded border border-edge bg-panel2 px-2 py-1">
            <option value="insample">in-sample</option>
            <option value="walkforward">walk-forward (DEV)</option>
            <option value="oos">out-of-sample</option>
          </select>
          <button onClick={run} disabled={busy || !strat}
            className="rounded bg-accent/20 px-3 py-1 font-medium text-accent hover:bg-accent/30 disabled:opacity-50">
            {busy ? "Running…" : "Run backtest"}
          </button>
        </div>
        {strat && (
          <div className="mt-2">
            <div className="text-[10px] uppercase text-muted">
              Usable span {strat.readiness.from ?? "—"} → {strat.readiness.to ?? "—"} · parameters (blank = default)
            </div>
            <div className="mt-1 grid gap-1 md:grid-cols-4">
              {strat.params.map((p: LabParamSpec) => (
                <label key={p.key} className="flex items-center justify-between gap-1 rounded border border-edge/50 bg-panel2 px-2 py-0.5 text-[10px]"
                  title={`${p.help}${p.lo !== null ? ` [${p.lo}..${p.hi}]` : ""}${p.tunable ? " · tunable by learning" : ""}`}>
                  <span className="truncate text-muted">{p.label}</span>
                  {p.kind === "choice" ? (
                    <select value={overrides[p.key] ?? ""} onChange={(e) => setOverrides({ ...overrides, [p.key]: e.target.value })}
                      className="w-24 bg-panel px-1 font-mono">
                      <option value="">{String(p.default)}</option>
                      {p.choices.map((c) => <option key={c} value={c}>{c}</option>)}
                    </select>
                  ) : (
                    <input value={overrides[p.key] ?? ""} placeholder={String(p.default)}
                      onChange={(e) => setOverrides({ ...overrides, [p.key]: e.target.value })}
                      className="w-16 rounded bg-panel px-1 text-right font-mono" />
                  )}
                </label>
              ))}
            </div>
          </div>
        )}
        {error && <div className="mt-2 rounded border border-bear/50 bg-bear/10 px-3 py-1.5 text-xs text-bear">{error}</div>}
      </section>
      {s && result && (
        <section className="card grid gap-3 px-4 py-3">
          <div className="flex flex-wrap items-baseline gap-3">
            <h2 className="text-xs font-semibold uppercase tracking-wide text-muted">
              Run #{result.run_id} — {result.sid} v{result.version} · {s.kind}
            </h2>
            {result.data_note && <span className="tag bg-yellow-500/15 text-yellow-400">{result.data_note}</span>}
          </div>
          {s.warnings?.length > 0 && (
            <div className="rounded border border-yellow-500/40 bg-yellow-500/10 px-3 py-1.5 text-[11px] text-yellow-400">
              {s.warnings.map((w, i) => <div key={i}>⚠ {w}</div>)}
            </div>
          )}
          <SummaryTiles s={s} />
          <EquityCurve points={curve} label="Equity (cumulative net ₹ by day)" />
          <div className="grid gap-2 md:grid-cols-2">
            <BucketTable title="By exit reason" buckets={s.by_exit_reason} />
            <BucketTable title="By regime" buckets={s.by_regime} />
            <BucketTable title="By session" buckets={s.by_session} />
            <BucketTable title="Expiry vs normal days" buckets={s.by_expiry_day} />
          </div>
          {Object.keys(s.rejected_signals ?? {}).length > 0 && (
            <div className="rounded border border-edge/60 bg-panel2 px-3 py-2">
              <div className="text-[10px] uppercase tracking-wide text-muted">Signals that did NOT become trades</div>
              <p className="font-mono text-[11px] text-muted">
                {Object.entries(s.rejected_signals).map(([k, v]) => `${k}:${v}`).join("  ")}
              </p>
            </div>
          )}
          {result.trades && result.trades.length > 0 && (
            <div className="max-h-80 overflow-auto scroll-thin">
              <table className="w-full text-[11px]">
                <thead className="sticky top-0 bg-panel text-[10px] uppercase text-muted">
                  <tr className="border-b border-edge/60">
                    <th className="py-1 text-left font-normal">entry</th>
                    <th className="text-left font-normal">dir/tag</th>
                    <th className="text-left font-normal">legs</th>
                    <th className="text-right font-normal">gross</th>
                    <th className="text-right font-normal">costs</th>
                    <th className="text-right font-normal">net</th>
                    <th className="text-left font-normal">exit</th>
                  </tr>
                </thead>
                <tbody className="font-mono">
                  {result.trades.map((t) => (
                    <tr key={t.seq} className="border-b border-edge/30">
                      <td className="py-0.5">{new Date(t.entry_ts * 1000).toLocaleDateString("en-IN", { timeZone: "Asia/Kolkata", day: "2-digit", month: "short" })} {istTime(t.entry_ts)}</td>
                      <td>{t.direction} {t.tag}</td>
                      <td className="max-w-[18rem] truncate" title={t.legs.map((l) => `${l.side} ${l.symbol} @${l.entry}→${l.exit ?? "?"}`).join("\n")}>
                        {t.legs.map((l) => `${l.side === "BUY" ? "+" : "−"}${l.symbol || l.right}`).join(" ")}
                      </td>
                      <td className={`text-right ${signCls(t.pnl_gross)}`}>{rs(t.pnl_gross)}</td>
                      <td className="text-right text-muted">{rs(t.costs)}</td>
                      <td className={`text-right ${signCls(t.pnl_net)}`}>{rs(t.pnl_net)}</td>
                      <td>{t.exit_reason}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
      )}
    </>
  );
}

function PositionsTable({ rows, closed }: { rows: LabPosition[]; closed: boolean }) {
  if (!rows.length) return <div className="text-[11px] text-muted">{closed ? "No closed trades today." : "No open positions."}</div>;
  return (
    <table className="w-full text-[11px]">
      <thead className="text-[10px] uppercase text-muted">
        <tr className="border-b border-edge/60">
          <th className="py-1 text-left font-normal">sid</th>
          <th className="text-left font-normal">opened</th>
          <th className="text-left font-normal">legs</th>
          {closed ? <>
            <th className="text-right font-normal">net</th>
            <th className="text-left font-normal">exit</th>
          </> : <th className="text-right font-normal">MAE/MFE</th>}
        </tr>
      </thead>
      <tbody className="font-mono">
        {rows.map((p) => (
          <tr key={p.id} className="border-b border-edge/30">
            <td className="py-0.5">{p.sid} v{p.version}{p.experimental ? " (exp)" : ""}</td>
            <td>{istTime(p.opened_ts)}</td>
            <td className="max-w-[20rem] truncate" title={p.legs.map((l) => `${l.side} ${l.tradingsymbol} ×${l.qty} @${l.price}`).join("\n")}>
              {p.legs.map((l) => `${l.side === "BUY" ? "+" : "−"}${l.tradingsymbol || l.right}`).join(" ")}
            </td>
            {closed ? <>
              <td className={`text-right ${signCls(p.pnl_net)}`}>{rs(p.pnl_net)}</td>
              <td>{p.exit_reason}</td>
            </> : (
              <td className="text-right text-muted">
                {rs((p.state as { mae?: number }).mae ?? 0)} / {rs((p.state as { mfe?: number }).mfe ?? 0)}
              </td>
            )}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function PaperTerminal() {
  const st = usePolling(() => api.labPaper(), 10000, []);
  const days = usePolling(() => api.labPaperDays(undefined, 60), 60000, []);
  if (st.error) return <div className="card border-bear/50 bg-bear/10 px-4 py-2 text-xs text-bear">{st.error}</div>;
  if (!st.data) return <div className="card px-4 py-6 text-center text-xs text-muted">Loading…</div>;
  const d = st.data;
  const session = (d.session ?? {}) as { status?: string };
  return (
    <>
      <section className="card px-4 py-3">
        <div className="mb-2 flex flex-wrap items-baseline gap-3">
          <h2 className="text-xs font-semibold uppercase tracking-wide text-muted">Paper terminal — {d.day}</h2>
          <span className={`tag ${session.status === "live" ? "bg-bull/15 text-bull" : "bg-panel2 text-muted"}`}>
            session {session.status ?? "not started"}
          </span>
          {Boolean(d.risk.emergency_stop) && <span className="tag bg-bear/15 text-bear">EMERGENCY STOP</span>}
        </div>
        <div className="grid gap-3 md:grid-cols-2">
          <div>
            <div className="mb-1 text-[10px] uppercase tracking-wide text-muted">Open positions</div>
            <PositionsTable rows={d.open_positions} closed={false} />
            {d.pending_orders.length > 0 && (
              <p className="mt-1 font-mono text-[10px] text-yellow-400">{d.pending_orders.length} order(s) awaiting a fill observation</p>
            )}
          </div>
          <div>
            <div className="mb-1 text-[10px] uppercase tracking-wide text-muted">Closed today</div>
            <PositionsTable rows={d.closed_today} closed />
          </div>
        </div>
      </section>
      <section className="card px-4 py-3">
        <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted">Signals today (fills, blocks, lapses)</h2>
        {d.signals.length === 0 ? <div className="text-[11px] text-muted">No signals yet today.</div> : (
          <div className="max-h-60 overflow-auto scroll-thin">
            <table className="w-full text-[11px]">
              <thead className="sticky top-0 bg-panel text-[10px] uppercase text-muted">
                <tr className="border-b border-edge/60">
                  <th className="py-1 text-left font-normal">time</th>
                  <th className="text-left font-normal">sid</th>
                  <th className="text-left font-normal">what</th>
                  <th className="text-left font-normal">outcome</th>
                  <th className="text-left font-normal">reason</th>
                </tr>
              </thead>
              <tbody className="font-mono">
                {d.signals.map((s) => (
                  <tr key={s.id} className="border-b border-edge/30">
                    <td className="py-0.5">{istTime(s.ts)}</td>
                    <td>{s.sid}{s.experimental ? " (exp)" : ""}</td>
                    <td>{String((s.payload as { direction?: string }).direction ?? "")} {String((s.payload as { tag?: string }).tag ?? "")}</td>
                    <td className={s.outcome === "ordered" ? "text-bull" : "text-muted"}>{s.outcome}</td>
                    <td className="text-muted">{s.reason}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
      <section className="card px-4 py-3">
        <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted">Day ledgers (immutable)</h2>
        {!days.data || days.data.length === 0 ? <div className="text-[11px] text-muted">No paper day-ledgers yet — they are cut after each session close.</div> : (
          <div className="max-h-60 overflow-auto scroll-thin">
            <table className="w-full text-[11px]">
              <thead className="sticky top-0 bg-panel text-[10px] uppercase text-muted">
                <tr className="border-b border-edge/60">
                  <th className="py-1 text-left font-normal">day</th>
                  <th className="text-left font-normal">sid</th>
                  <th className="text-right font-normal">trades</th>
                  <th className="text-right font-normal">net</th>
                </tr>
              </thead>
              <tbody className="font-mono">
                {days.data.map((r, i) => (
                  <tr key={i} className="border-b border-edge/30">
                    <td className="py-0.5">{r.day}</td>
                    <td>{r.sid} v{r.version}</td>
                    <td className="text-right">{r.trades}</td>
                    <td className={`text-right ${signCls(r.pnl_net)}`}>{rs(r.pnl_net)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </>
  );
}

function Analytics() {
  const [runs, setRuns] = useState<LabRun[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [a, setA] = useState<number | null>(null);
  const [b, setB] = useState<number | null>(null);
  useEffect(() => { api.labRuns(undefined, 100).then(setRuns).catch((e) => setError(String(e.message ?? e))); }, []);
  const pick = (id: number) => {
    if (a === id) setA(null); else if (b === id) setB(null);
    else if (a === null) setA(id); else setB(id);
  };
  const ra = runs?.find((r) => r.run_id === a);
  const rb = runs?.find((r) => r.run_id === b);
  const fields: [string, (s: LabRunSummary) => string][] = [
    ["Evidence class", (s) => s.kind],
    ["Trades", (s) => String(s.trades)],
    ["Win rate", (s) => s.win_rate === null ? "—" : `${num(s.win_rate, 1)}%`],
    ["Expectancy", (s) => rs(s.expectancy)],
    ["Net P&L", (s) => rs(s.net_pnl)],
    ["Profit factor", (s) => num(s.profit_factor)],
    ["Max drawdown", (s) => rs(s.max_drawdown)],
    ["Sharpe", (s) => num(s.sharpe)],
  ];
  if (error) return <div className="card border-bear/50 bg-bear/10 px-4 py-2 text-xs text-bear">{error}</div>;
  return (
    <>
      <section className="card px-4 py-3">
        <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted">
          Run history — click two rows to compare
        </h2>
        {!runs ? <div className="text-xs text-muted">Loading…</div> : runs.length === 0 ? (
          <div className="text-[11px] text-muted">No runs recorded yet — run a backtest first.</div>
        ) : (
          <div className="max-h-80 overflow-auto scroll-thin">
            <table className="w-full text-[11px]">
              <thead className="sticky top-0 bg-panel text-[10px] uppercase text-muted">
                <tr className="border-b border-edge/60">
                  <th className="py-1 text-left font-normal">run</th>
                  <th className="text-left font-normal">sid</th>
                  <th className="text-left font-normal">ver</th>
                  <th className="text-left font-normal">class</th>
                  <th className="text-right font-normal">n</th>
                  <th className="text-right font-normal">/trade</th>
                  <th className="text-right font-normal">net</th>
                  <th className="text-left font-normal">status</th>
                </tr>
              </thead>
              <tbody className="font-mono">
                {runs.map((r) => {
                  const s = r.summary as LabRunSummary;
                  const sel = r.run_id === a || r.run_id === b;
                  return (
                    <tr key={r.run_id} onClick={() => pick(r.run_id)}
                      className={`cursor-pointer border-b border-edge/30 ${sel ? "bg-accent/10" : "hover:bg-panel2/50"}`}>
                      <td className="py-0.5">#{r.run_id}</td>
                      <td>{r.sid}</td>
                      <td>v{r.version}</td>
                      <td>{r.kind}</td>
                      <td className="text-right">{s.trades ?? "—"}</td>
                      <td className={`text-right ${signCls(s.expectancy)}`}>{s.expectancy !== undefined ? rs(s.expectancy) : "—"}</td>
                      <td className={`text-right ${signCls(s.net_pnl)}`}>{s.net_pnl !== undefined ? rs(s.net_pnl) : "—"}</td>
                      <td className={r.status === "done" ? "" : "text-yellow-400"}>{r.status}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>
      {ra && rb && (ra.summary as LabRunSummary).trades !== undefined && (rb.summary as LabRunSummary).trades !== undefined && (
        <section className="card px-4 py-3">
          <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted">
            #{ra.run_id} ({ra.sid} v{ra.version}) vs #{rb.run_id} ({rb.sid} v{rb.version})
          </h2>
          <table className="w-full text-[11px]">
            <tbody className="font-mono">
              {fields.map(([label, fn]) => (
                <tr key={label} className="border-b border-edge/30">
                  <td className="py-0.5 text-muted">{label}</td>
                  <td className="text-right">{fn(ra.summary as LabRunSummary)}</td>
                  <td className="text-right">{fn(rb.summary as LabRunSummary)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="mt-1 text-[10px] text-muted">Comparing across evidence classes (in-sample vs paper etc.) is apples-to-oranges — the class row is first for a reason.</p>
        </section>
      )}
    </>
  );
}

function LearningLab() {
  const exps = usePolling<LabExperiment[]>(() => api.labExperiments(), 30000, []);
  const [report, setReport] = useState<Record<string, unknown> | null>(null);
  const [reportErr, setReportErr] = useState<string | null>(null);
  const [open, setOpen] = useState<number | null>(null);
  useEffect(() => {
    api.labReport().then(setReport).catch((e) => setReportErr(String(e.message ?? e)));
  }, []);
  const STATUS_CLS: Record<string, string> = {
    proposed: "bg-accent/15 text-accent", validating: "bg-yellow-500/15 text-yellow-400",
    paper: "bg-accent/15 text-accent", accepted: "bg-bull/15 text-bull",
    rejected: "bg-panel2 text-muted",
  };
  return (
    <>
      <section className="card px-4 py-3">
        <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted">Champion / challenger experiments</h2>
        {!exps.data || exps.data.length === 0 ? (
          <div className="text-[11px] text-muted">
            No experiments yet. The learning engine proposes challengers after enough paper evidence accumulates (30+ trades), validates them walk-forward, and promotes only within paper.
          </div>
        ) : (
          <div className="max-h-96 overflow-auto scroll-thin">
            <table className="w-full text-[11px]">
              <thead className="sticky top-0 bg-panel text-[10px] uppercase text-muted">
                <tr className="border-b border-edge/60">
                  <th className="py-1 text-left font-normal">exp</th>
                  <th className="text-left font-normal">sid</th>
                  <th className="text-left font-normal">hypothesis</th>
                  <th className="text-left font-normal">status</th>
                  <th className="text-left font-normal">decision</th>
                </tr>
              </thead>
              <tbody className="font-mono">
                {exps.data.map((e) => (
                  <>
                    <tr key={e.exp_id} className="cursor-pointer border-b border-edge/30 hover:bg-panel2/50"
                      onClick={() => setOpen(open === e.exp_id ? null : e.exp_id)}>
                      <td className="py-0.5">#{e.exp_id}</td>
                      <td>{e.sid} v{e.champion}→v{e.challenger}</td>
                      <td className="max-w-[16rem] truncate font-sans">{e.hypothesis}</td>
                      <td><span className={`tag ${STATUS_CLS[e.status] ?? ""}`}>{e.status}</span></td>
                      <td className="max-w-[16rem] truncate font-sans text-muted" title={e.decision_reason}>{e.decision_reason}</td>
                    </tr>
                    {open === e.exp_id && (
                      <tr key={`${e.exp_id}-d`} className="border-b border-edge/30 bg-panel2/40">
                        <td colSpan={5} className="px-3 py-2">
                          <div className="grid gap-2 font-sans text-[11px] md:grid-cols-3">
                            <div>
                              <div className="text-[10px] uppercase text-muted">Evidence used</div>
                              <pre className="whitespace-pre-wrap font-mono text-[10px] text-muted">{JSON.stringify(e.evidence, null, 1)}</pre>
                            </div>
                            <div>
                              <div className="text-[10px] uppercase text-muted">Walk-forward (DEV / TEST expectancy)</div>
                              <pre className="whitespace-pre-wrap font-mono text-[10px] text-muted">
                                {JSON.stringify({
                                  dev_champion: ((e.dev_result as { champion?: { expectancy?: number } }).champion ?? {}).expectancy,
                                  dev_challenger: ((e.dev_result as { challenger?: { expectancy?: number } }).challenger ?? {}).expectancy,
                                  test_champion: ((e.test_result as { champion?: { expectancy?: number } }).champion ?? {}).expectancy,
                                  test_challenger: ((e.test_result as { challenger?: { expectancy?: number } }).challenger ?? {}).expectancy,
                                }, null, 1)}
                              </pre>
                            </div>
                            <div>
                              <div className="text-[10px] uppercase text-muted">Forward paper</div>
                              <pre className="whitespace-pre-wrap font-mono text-[10px] text-muted">{JSON.stringify(e.paper_result, null, 1)}</pre>
                            </div>
                          </div>
                        </td>
                      </tr>
                    )}
                  </>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
      <section className="card px-4 py-3">
        <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted">Latest daily learning report</h2>
        {reportErr && <div className="text-[11px] text-muted">No report yet — one is generated after each paper session. ({reportErr})</div>}
        {report && (
          <div className="grid gap-2 text-[11px] md:grid-cols-2">
            <div className="rounded border border-edge/60 bg-panel2 px-3 py-2">
              <div className="text-[10px] uppercase text-muted">Session {String(report.day)}</div>
              <p className="font-mono">net {rs(report.net_pnl as number)} over {String(report.trades)} trades</p>
              <p className="font-mono text-[10px] text-muted">signals: {JSON.stringify(report.signal_outcomes)}</p>
              <p className="font-mono text-[10px] text-muted">blocks: {JSON.stringify(report.block_reasons)}</p>
            </div>
            <div className="rounded border border-edge/60 bg-panel2 px-3 py-2">
              <div className="text-[10px] uppercase text-muted">Data quality / suspensions</div>
              <p className="font-mono text-[10px]">{String((report.data_quality as { issue_count?: number })?.issue_count ?? 0)} data issues (14d)</p>
              <pre className="whitespace-pre-wrap font-mono text-[10px] text-muted">{JSON.stringify(report.suspended)}</pre>
            </div>
            <div className="rounded border border-edge/60 bg-panel2 px-3 py-2 md:col-span-2">
              <div className="text-[10px] uppercase text-muted">Biggest losers / winners today</div>
              <pre className="max-h-40 overflow-auto whitespace-pre-wrap font-mono text-[10px] text-muted scroll-thin">
                {JSON.stringify({ losers: report.top_losers, winners: report.top_winners }, null, 1)}
              </pre>
            </div>
          </div>
        )}
      </section>
    </>
  );
}

function RiskControl() {
  const [view, setView] = useState<Awaited<ReturnType<typeof api.labRisk>> | null>(null);
  const [edits, setEdits] = useState<Record<string, string>>({});
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const load = useCallback(() => { api.labRisk().then(setView).catch((e) => setError(String(e.message ?? e))); }, []);
  useEffect(() => { load(); }, [load]);

  const save = async () => {
    setBusy(true); setError(null);
    try {
      const updates: Record<string, number> = {};
      for (const [k, v] of Object.entries(edits)) if (v !== "") updates[k] = Number(v);
      await api.labRiskSet(updates);
      setEdits({}); load();
    } catch (e) { setError(String((e as Error).message ?? e)); }
    setBusy(false);
  };
  const emergency = async (action: "trip" | "clear", flatten = false) => {
    if (action === "trip" && !window.confirm(flatten
      ? "Trip the emergency stop AND flatten all simulated positions at next quotes?"
      : "Trip the emergency stop? New paper orders stop immediately; open positions keep their normal exits.")) return;
    setBusy(true);
    try { await api.labEmergency(action, flatten, "ui"); load(); }
    catch (e) { setError(String((e as Error).message ?? e)); }
    setBusy(false);
  };

  if (error && !view) return <div className="card border-bear/50 bg-bear/10 px-4 py-2 text-xs text-bear">{error}</div>;
  if (!view) return <div className="card px-4 py-6 text-center text-xs text-muted">Loading…</div>;
  const tripped = Boolean(view.limits.emergency_stop);
  return (
    <>
      <section className="card px-4 py-3">
        <div className="mb-2 flex flex-wrap items-center gap-3">
          <h2 className="text-xs font-semibold uppercase tracking-wide text-muted">Portfolio risk — simulated book only</h2>
          <span className={`tag ${tripped ? "bg-bear/15 text-bear" : "bg-bull/15 text-bull"}`}>
            {tripped ? "EMERGENCY STOP TRIPPED" : "trading allowed"}
          </span>
          <div className="ml-auto flex gap-2">
            {tripped ? (
              <button onClick={() => emergency("clear")} disabled={busy}
                className="rounded bg-bull/20 px-3 py-1 text-xs font-medium text-bull hover:bg-bull/30 disabled:opacity-50">Clear stop</button>
            ) : (
              <>
                <button onClick={() => emergency("trip", false)} disabled={busy}
                  className="rounded bg-bear/20 px-3 py-1 text-xs font-medium text-bear hover:bg-bear/30 disabled:opacity-50">Stop (freeze)</button>
                <button onClick={() => emergency("trip", true)} disabled={busy}
                  className="rounded bg-bear/20 px-3 py-1 text-xs font-medium text-bear hover:bg-bear/30 disabled:opacity-50">Stop + flatten</button>
              </>
            )}
          </div>
        </div>
        {error && <div className="mb-2 rounded border border-bear/50 bg-bear/10 px-3 py-1.5 text-xs text-bear">{error}</div>}
        <div className="grid gap-1 md:grid-cols-3">
          {Object.entries(view.limits).filter(([k]) => !k.startsWith("emergency")).map(([k, v]) => (
            <label key={k} className="flex items-center justify-between gap-2 rounded border border-edge/50 bg-panel2 px-2 py-1 text-[11px]"
              title={`bounds ${view.bounds[k]?.lo}..${view.bounds[k]?.hi}`}>
              <span className="text-muted">{k}</span>
              <input value={edits[k] ?? ""} placeholder={String(v)}
                onChange={(e) => setEdits({ ...edits, [k]: e.target.value })}
                className="w-28 rounded bg-panel px-1 text-right font-mono" />
            </label>
          ))}
        </div>
        <div className="mt-2 flex items-center gap-3">
          <button onClick={save} disabled={busy || Object.values(edits).every((v) => v === "")}
            className="rounded bg-accent/20 px-3 py-1 text-xs font-medium text-accent hover:bg-accent/30 disabled:opacity-50">
            {busy ? "Saving…" : "Save limits"}
          </button>
          <span className="text-[10px] text-muted">{view.note}</span>
        </div>
      </section>
      <section className="card px-4 py-3">
        <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted">Risk events</h2>
        {view.events.length === 0 ? <div className="text-[11px] text-muted">No risk events recorded.</div> : (
          <div className="max-h-60 overflow-auto scroll-thin">
            <table className="w-full text-[11px]">
              <tbody className="font-mono">
                {view.events.map((e, i) => (
                  <tr key={i} className="border-b border-edge/30">
                    <td className="py-0.5 text-muted">{e.day} {istTime(e.ts)}</td>
                    <td>{e.sid}</td>
                    <td className="text-bear">{e.code}</td>
                    <td className="max-w-[20rem] truncate text-muted">{JSON.stringify(e.detail)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </>
  );
}

function DataSettings() {
  const [d, setD] = useState<Awaited<ReturnType<typeof api.labData>> | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const load = useCallback(() => { api.labData().then(setD).catch((e) => setError(String(e.message ?? e))); }, []);
  useEffect(() => { load(); }, [load]);
  const sync = async () => {
    setBusy(true); setError(null); setNote(null);
    try {
      const r = await api.labSyncFutures();
      setNote(`Futures sync: ${JSON.stringify(r)}`);
      load();
    } catch (e) { setError(String((e as Error).message ?? e)); }
    setBusy(false);
  };
  if (error && !d) return <div className="card border-bear/50 bg-bear/10 px-4 py-2 text-xs text-bear">{error}</div>;
  if (!d) return <div className="card px-4 py-6 text-center text-xs text-muted">Loading…</div>;
  return (
    <>
      <section className="card px-4 py-3">
        <div className="mb-2 flex flex-wrap items-center gap-3">
          <h2 className="text-xs font-semibold uppercase tracking-wide text-muted">Datasets (live truth, computed from the files)</h2>
          <button onClick={sync} disabled={busy}
            className="ml-auto rounded bg-accent/20 px-3 py-1 text-xs font-medium text-accent hover:bg-accent/30 disabled:opacity-50">
            {busy ? "Syncing…" : "Sync futures history"}
          </button>
        </div>
        {error && <div className="mb-2 rounded border border-bear/50 bg-bear/10 px-3 py-1.5 text-xs text-bear">{error}</div>}
        {note && <div className="mb-2 rounded border border-edge bg-panel2 px-3 py-1.5 font-mono text-[10px] text-muted">{note}</div>}
        <div className="grid gap-2 md:grid-cols-2">
          {Object.entries(d.readiness).map(([k, v]) => (
            <div key={k} className="rounded border border-edge/60 bg-panel2 px-3 py-2">
              <div className="flex items-baseline gap-2">
                <span className="font-mono text-xs">{k}</span>
                <span className={`tag ${READY_CLS[v.status] ?? "bg-panel2 text-muted"}`}>{v.status}</span>
              </div>
              <p className="font-mono text-[11px] text-muted">{v.from ?? "—"} → {v.to ?? "—"} · {v.rows.toLocaleString("en-IN")} rows</p>
              <p className="text-[10px] text-muted">{v.note}</p>
            </div>
          ))}
        </div>
      </section>
      <section className="card px-4 py-3">
        <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted">
          Data quality — last {d.quality.window_days} days · {d.quality.issue_count} issue(s)
        </h2>
        {d.quality.issues.length === 0 ? <div className="text-[11px] text-muted">No issues detected (missing bars, chain gaps, abnormal prints).</div> : (
          <pre className="max-h-60 overflow-auto whitespace-pre-wrap font-mono text-[10px] text-muted scroll-thin">
            {JSON.stringify(d.quality.issues, null, 1)}
          </pre>
        )}
      </section>
    </>
  );
}

// ---- B09 VWAP 3σ study -----------------------------------------------------

function rateCls(rate: number | null | undefined): string {
  if (rate === null || rate === undefined) return "text-muted";
  if (rate >= 65) return "bg-bull/25 text-bull";
  if (rate >= 55) return "bg-bull/10 text-bull";
  if (rate >= 45) return "text-muted";
  if (rate >= 35) return "bg-bear/10 text-bear";
  return "bg-bear/25 text-bear";
}

function Vwap3Heatmap({ title, buckets, order }: {
  title: string; buckets: Record<string, LabVwap3Bucket>; order?: string[];
}) {
  const keys = order ? order.filter((k) => buckets[k]) : Object.keys(buckets);
  if (!keys.length) return null;
  return (
    <div className="rounded border border-edge/60 bg-panel2 px-3 py-2">
      <div className="mb-1 text-[10px] uppercase tracking-wide text-muted">{title}</div>
      <table className="w-full text-[11px]">
        <tbody className="font-mono">
          {keys.map((k) => {
            const b = buckets[k];
            return (
              <tr key={k} className="border-t border-edge/30">
                <td className="py-0.5 pr-2">{k}{b.low_sample ? <span className="text-yellow-400" title="under 30 touches"> ⚠</span> : null}</td>
                <td className="text-right text-muted">{b.n}</td>
                <td className={`rounded px-1 text-right ${rateCls(b.rate)}`}>
                  {b.rate === null ? "—" : `${num(b.rate, 1)}%`}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function Vwap3ProbCard({ r, compact }: { r: LabVwap3Result; compact?: boolean }) {
  const s = r.summary;
  return (
    <section className="card border-accent/40 px-4 py-3">
      <div className="mb-2 flex flex-wrap items-baseline gap-3">
        <h2 className="text-xs font-semibold uppercase tracking-wide text-accent">
          3σ → VWAP reversion probability
        </h2>
        <span className="text-[10px] text-muted">
          {r.official_mult}σ · {r.timeframe} · stop {r.sl_ext}σ · {r.span.from} → {r.span.to}
        </span>
        {!s.reliable && (
          <span className="tag bg-yellow-500/15 text-yellow-400"
            title={s.reliability_note ?? ""}>low sample</span>
        )}
      </div>
      <div className={`grid gap-2 ${compact ? "md:grid-cols-4" : "md:grid-cols-4"}`}>
        <Tile label="Overall" value={s.return_rate == null ? "—" : `${num(s.return_rate, 1)}%`}
          cls="text-lg" hint={`${s.n} qualifying touch episodes`} />
        <Tile label="Upper band" value={s.return_rate_upper == null ? "—" : `${num(s.return_rate_upper, 1)}%`}
          hint={`${s.upper_n} touches`} />
        <Tile label="Lower band" value={s.return_rate_lower == null ? "—" : `${num(s.return_rate_lower, 1)}%`}
          hint={`${s.lower_n} touches`} />
        <Tile label="Observations" value={String(s.n)}
          cls={s.reliable ? "" : "text-yellow-400"}
          hint={s.reliability_note ?? "sample size adequate"} />
        <Tile label="Median time to VWAP" value={s.median_minutes_to_vwap == null ? "—" : `${s.median_minutes_to_vwap} min`} />
        <Tile label="VWAP before stop" value={s.pct_vwap_before_stop == null ? "—" : `${num(s.pct_vwap_before_stop, 1)}%`} />
        <Tile label="Stop before VWAP" value={s.pct_stop_before_vwap == null ? "—" : `${num(s.pct_stop_before_vwap, 1)}%`} />
        <Tile label="Net option P&L / setup" value={r.option_layer.expectancy == null ? "—" : rs(r.option_layer.expectancy)}
          cls={signCls(r.option_layer.expectancy)}
          hint={`real premiums, chain era only (n=${r.option_layer.n})`} />
      </div>
    </section>
  );
}

function Vwap3CellTable({ title, cells, keys }: {
  title: string; cells: Record<string, LabVwap3Cell>; keys?: string[];
}) {
  const ks = keys ?? Object.keys(cells);
  return (
    <div className="rounded border border-edge/60 bg-panel2 px-3 py-2">
      <div className="mb-1 text-[10px] uppercase tracking-wide text-muted">{title}</div>
      <table className="w-full text-[11px]">
        <thead className="text-[10px] uppercase text-muted">
          <tr><th className="text-left font-normal">cell</th>
            <th className="text-right font-normal">n</th>
            <th className="text-right font-normal">win%</th>
            <th className="text-right font-normal">/trade</th>
            <th className="text-right font-normal">net</th></tr>
        </thead>
        <tbody className="font-mono">
          {ks.map((k) => {
            const c = cells[k];
            if (!c) return null;
            return (
              <tr key={k} className="border-t border-edge/30">
                <td className="py-0.5">{k}{c.low_sample ? <span className="text-yellow-400"> ⚠</span> : null}</td>
                <td className="text-right">{c.n ?? c.status ?? "—"}</td>
                <td className="text-right">{c.win_rate == null ? "—" : num(c.win_rate, 1)}</td>
                <td className={`text-right ${signCls(c.expectancy)}`}>{c.expectancy == null ? "—" : rs(c.expectancy)}</td>
                <td className={`text-right ${signCls(c.net)}`}>{c.net == null ? "—" : rs(c.net)}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function Vwap3Study() {
  const [latest, setLatest] = useState<LabVwap3Result | null>(null);
  const [ranAt, setRanAt] = useState<number | null>(null);
  const [mult, setMult] = useState("3.0");
  const [tf, setTf] = useState("5m");
  const [slExt, setSlExt] = useState("0.5");
  const [sweeps, setSweeps] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [chartDay, setChartDay] = useState<string | null>(null);
  const [chart, setChart] = useState<LabVwap3Day | null>(null);

  useEffect(() => {
    api.labVwap3Latest()
      .then((r) => { setLatest(r.result); setRanAt(r.created_at); })
      .catch(() => { /* no study yet */ });
  }, []);

  const run = async () => {
    setBusy(true); setError(null);
    try {
      const r = await api.labVwap3Study({
        sigma_mult: Number(mult), timeframe: tf, sl_ext: Number(slExt),
        sweeps,
      });
      setLatest(r.result);
      setRanAt(Math.floor(Date.now() / 1000));
    } catch (e) { setError(String((e as Error).message ?? e)); }
    setBusy(false);
  };

  const openChart = async (day: string) => {
    setChartDay(day); setChart(null);
    try {
      setChart(await api.labVwap3Day(day, Number(mult), tf));
    } catch (e) { setError(String((e as Error).message ?? e)); }
  };

  const r = latest;
  return (
    <>
      <section className="card px-4 py-3">
        <div className="flex flex-wrap items-center gap-2 text-xs">
          <h2 className="text-xs font-semibold uppercase tracking-wide text-muted">
            B09 · VWAP σ-band study
          </h2>
          <label className="text-muted">σ <select value={mult} onChange={(e) => setMult(e.target.value)}
            className="rounded border border-edge bg-panel2 px-2 py-1">
            {["2.0", "2.5", "3.0", "3.5", "4.0"].map((m) => <option key={m}>{m}</option>)}
          </select></label>
          <label className="text-muted">tf <select value={tf} onChange={(e) => setTf(e.target.value)}
            className="rounded border border-edge bg-panel2 px-2 py-1">
            <option>5m</option><option>15m</option>
          </select></label>
          <label className="text-muted">stop ext <select value={slExt} onChange={(e) => setSlExt(e.target.value)}
            className="rounded border border-edge bg-panel2 px-2 py-1">
            {["0.25", "0.5", "0.75", "1.0"].map((m) => <option key={m}>{m}</option>)}
          </select>σ</label>
          <label className="flex items-center gap-1 text-muted">
            <input type="checkbox" checked={sweeps} onChange={(e) => setSweeps(e.target.checked)} />
            engine sweeps (~30s)
          </label>
          <button onClick={run} disabled={busy}
            className="rounded bg-accent/20 px-3 py-1 font-medium text-accent hover:bg-accent/30 disabled:opacity-50">
            {busy ? "Running…" : "Run study"}
          </button>
          {ranAt && <span className="text-[10px] text-muted">last run {new Date(ranAt * 1000).toLocaleString("en-IN", { timeZone: "Asia/Kolkata" })}</span>}
        </div>
        {error && <div className="mt-2 rounded border border-bear/50 bg-bear/10 px-3 py-1.5 text-xs text-bear">{error}</div>}
        <p className="mt-1 text-[10px] text-muted">
          Underlying layer answers the reversion question over the FULL index history; the option layer prices the
          same touches with REAL premiums inside the chain era only. 3.0σ stays the official strategy — other
          multipliers are research, promoted only as challengers.
        </p>
      </section>
      {!r && <div className="card px-4 py-6 text-center text-xs text-muted">No study yet — run one above.</div>}
      {r && (
        <>
          <Vwap3ProbCard r={r} />
          <section className="card grid gap-2 px-4 py-3 md:grid-cols-3">
            <Vwap3Heatmap title="Time of day × return rate" buckets={r.heatmaps.hour}
              order={["09:15-10:00", "10:00-11:30", "11:30-13:30", "13:30-14:30", "14:30-15:15"]} />
            <Vwap3Heatmap title="Day of week" buckets={r.heatmaps.dow}
              order={["Mon", "Tue", "Wed", "Thu", "Fri"]} />
            <Vwap3Heatmap title="VWAP slope at touch" buckets={r.heatmaps.slope}
              order={["strong_up", "mild_up", "flat", "mild_down", "strong_down"]} />
            <Vwap3Heatmap title="Volatility regime (VIX pctile)" buckets={r.heatmaps.vol_regime}
              order={["low", "normal", "high", "extreme", "unknown"]} />
            <Vwap3Heatmap title="Band width quintile" buckets={r.heatmaps.width_q}
              order={["Q1", "Q2", "Q3", "Q4", "Q5"]} />
            <Vwap3Heatmap title="Touch of the day" buckets={r.heatmaps.ordinal}
              order={["1", "2", "3+"]} />
            <Vwap3Heatmap title="Entry mode (A touch / B close-inside / C rejection)"
              buckets={r.heatmaps.entry_mode} order={["A", "B", "C"]} />
            <div className="rounded border border-edge/60 bg-panel2 px-3 py-2">
              <div className="mb-1 text-[10px] uppercase tracking-wide text-muted">σ multiplier (research)</div>
              <table className="w-full text-[11px]"><tbody className="font-mono">
                {Object.entries(r.research_mults).map(([m, s2]: [string, LabVwap3Summary]) => (
                  <tr key={m} className="border-t border-edge/30">
                    <td className="py-0.5">{m}σ</td>
                    <td className="text-right text-muted">{s2.n}</td>
                    <td className={`rounded px-1 text-right ${rateCls(s2.return_rate)}`}>
                      {s2.return_rate == null ? "—" : `${num(s2.return_rate, 1)}%`}
                    </td>
                  </tr>
                ))}
                <tr className="border-t border-edge/30 text-accent">
                  <td className="py-0.5">{r.official_mult}σ (official)</td>
                  <td className="text-right">{r.summary.n}</td>
                  <td className={`rounded px-1 text-right ${rateCls(r.summary.return_rate)}`}>
                    {r.summary.return_rate == null ? "—" : `${num(r.summary.return_rate, 1)}%`}
                  </td>
                </tr>
              </tbody></table>
            </div>
            <div className="rounded border border-edge/60 bg-panel2 px-3 py-2">
              <div className="mb-1 text-[10px] uppercase tracking-wide text-muted">Option layer (real premiums, chain era)</div>
              <div className="grid grid-cols-2 gap-x-3 font-mono text-[11px]">
                <span className="text-muted">PE trades</span>
                <span className="text-right">{r.option_layer.pe.n} · wr {num(r.option_layer.pe.win_rate, 1)}% · <span className={signCls(r.option_layer.pe.net)}>{rs(r.option_layer.pe.net)}</span></span>
                <span className="text-muted">CE trades</span>
                <span className="text-right">{r.option_layer.ce.n} · wr {num(r.option_layer.ce.win_rate, 1)}% · <span className={signCls(r.option_layer.ce.net)}>{rs(r.option_layer.ce.net)}</span></span>
                <span className="text-muted">Profit factor</span>
                <span className="text-right">{num(r.option_layer.profit_factor)}</span>
                <span className="text-muted">Max drawdown</span>
                <span className={`text-right ${signCls(r.option_layer.max_drawdown)}`}>{rs(r.option_layer.max_drawdown)}</span>
              </div>
              <p className="mt-1 text-[10px] text-muted">{r.option_layer.note}</p>
            </div>
          </section>
          {r.sweeps && (
            <section className="card grid gap-2 px-4 py-3 md:grid-cols-2">
              <Vwap3CellTable title="Entry mode × stop extension (engine, chain era)"
                cells={r.sweeps.entry_mode_x_sl_ext} />
              <Vwap3CellTable title="Strike selection" cells={r.sweeps.strike}
                keys={["-2", "-1", "0", "1", "2", "delta"]} />
              <Vwap3CellTable title="Expiry" cells={r.sweeps.expiry} keys={["weekly", "monthly"]} />
              <Vwap3CellTable title="Target mode" cells={r.sweeps.target_mode}
                keys={["vwap", "premium_pct", "risk_reward", "trail_after"]} />
              <p className="text-[10px] text-muted md:col-span-2">{r.sweeps.note}</p>
            </section>
          )}
          <section className="card px-4 py-3">
            <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted">
              Touch ledger (latest {r.touches_recent.length}) — click a row to open its chart
            </h2>
            <div className="max-h-80 overflow-auto scroll-thin">
              <table className="w-full text-[11px]">
                <thead className="sticky top-0 bg-panel text-[10px] uppercase text-muted">
                  <tr className="border-b border-edge/60">
                    <th className="py-1 text-left font-normal">day</th>
                    <th className="text-left font-normal">time</th>
                    <th className="text-left font-normal">side</th>
                    <th className="text-right font-normal">#</th>
                    <th className="text-left font-normal">slope</th>
                    <th className="text-left font-normal">outcome</th>
                    <th className="text-right font-normal">mins</th>
                    <th className="text-right font-normal">MAE σ</th>
                    <th className="text-right font-normal">opt net</th>
                  </tr>
                </thead>
                <tbody className="font-mono">
                  {[...r.touches_recent].reverse().map((t, i) => (
                    <tr key={i} onClick={() => openChart(t.day)}
                      className={`cursor-pointer border-b border-edge/30 hover:bg-panel2/50 ${chartDay === t.day ? "bg-accent/10" : ""}`}>
                      <td className="py-0.5">{t.day}</td>
                      <td>{istTime(t.ts)}</td>
                      <td className={t.side === "upper" ? "text-bear" : "text-bull"}>{t.side}</td>
                      <td className="text-right">{t.ordinal}</td>
                      <td className="text-muted">{t.slope}</td>
                      <td className={t.returned ? "text-bull" : "text-muted"}>{t.outcome}</td>
                      <td className="text-right">{t.minutes_to_out ?? "—"}</td>
                      <td className="text-right">{t.mae_sigma ?? "—"}</td>
                      <td className={`text-right ${signCls(t.option?.net)}`}>
                        {t.option ? rs(t.option.net) : "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {chartDay && (
              <div className="mt-3">
                <div className="mb-1 text-[10px] uppercase tracking-wide text-muted">
                  {chartDay} — candles · VWAP (blue) · ±{mult}σ bands (dashed) · touches · trades
                </div>
                {chart ? <Vwap3Chart data={chart} /> :
                  <div className="py-6 text-center text-xs text-muted">Loading chart…</div>}
              </div>
            )}
          </section>
        </>
      )}
    </>
  );
}

// ---- root ------------------------------------------------------------------

const SECTIONS = ["Command", "Library", "Backtest", "Paper", "Analytics", "Learning", "Risk", "Data", "3σ Study"] as const;
type Section = typeof SECTIONS[number];

export function StrategyLab() {
  const [section, setSection] = useState<Section>("Command");
  const [btSid, setBtSid] = useState("F01");
  const overview = usePolling<LabOverview>(() => api.labOverview(), 30000, []);

  return (
    <div className="mx-auto flex min-h-screen max-w-6xl flex-col gap-3 p-3">
      <header className="card flex flex-wrap items-center gap-3 px-4 py-3">
        <a href="/" className="text-xs text-muted hover:text-white">← Dashboard</a>
        <ModuleSwitcher />
        <h1 className="text-sm font-semibold">Algo Strategy Lab</h1>
        <span className="tag bg-accent/15 text-[10px] text-accent">SIMULATION ONLY</span>
        {overview.data?.emergency_stop && <span className="tag bg-bear/15 text-[10px] text-bear">EMERGENCY STOP</span>}
        <span className="text-[11px] text-muted">
          41 NIFTY F&O strategies · backtests, walk-forward and paper are separate evidence · no orders, ever
        </span>
      </header>
      <nav className="inline-flex flex-wrap gap-1">
        {SECTIONS.map((s) => (
          <button key={s} onClick={() => setSection(s)}
            className={`rounded px-3 py-1 text-xs font-medium transition ${
              s === section ? "bg-accent/20 text-accent" : "bg-panel2 text-muted hover:text-white"}`}>
            {s}
          </button>
        ))}
      </nav>
      {section === "Command" && <CommandCenter />}
      {section === "Library" && <Library onBacktest={(sid) => { setBtSid(sid); setSection("Backtest"); }} />}
      {section === "Backtest" && <BacktestLab sid={btSid} setSid={setBtSid} />}
      {section === "Paper" && <PaperTerminal />}
      {section === "Analytics" && <Analytics />}
      {section === "Learning" && <LearningLab />}
      {section === "Risk" && <RiskControl />}
      {section === "Data" && <DataSettings />}
      {section === "3σ Study" && <Vwap3Study />}
      <footer className="pb-2 text-center text-[10px] text-muted">
        Backtested results are hypothetical and assume the stated fills and costs. Paper results are simulations against live data.
        Nothing here places orders or promises profitability.
      </footer>
    </div>
  );
}
