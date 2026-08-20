"use client";

// Closing Day Strategy — the overnight continuation study.
//
// One rule under test: at 15:00 compare NIFTY to yesterday's close, buy the
// ATM PE if lower / ATM CE if higher, sell at 09:50 the next morning.
//
// The single design rule of this screen: the MEASURED layer and the MODELLED
// layer never share a card. Everything under "Index" is real 5-minute prints
// and survives any argument about option pricing; everything under "Option" is
// a Black-Scholes reconstruction, because Kite deletes expired contracts and a
// year of real premiums cannot be fetched at any price. Blending them into one
// tidy P&L headline would be the single most misleading thing this tab could
// do, so the model's own error bar is rendered next to the number it bounds.

import { useCallback, useEffect, useState } from "react";
import {
  ClosingAttribution,
  ClosingBucketRow,
  ClosingComparisonRow,
  ClosingMonthRow,
  ClosingPctStats,
  ClosingReasons,
  ClosingResults,
  ClosingSignalSearch,
  ClosingTonight,
  ClosingTonightCheck,
  ClosingTrade,
  ClosingWindow,
  OvernightLadderRung,
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

/** A confidence interval that spans zero has not established a sign. The UI
 *  says so in words rather than leaving a reader to compare two brackets. */
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

function CutTable({ title, rows, note }: {
  title: string;
  rows: Record<string, ClosingPctStats> | undefined;
  note?: string;
}) {
  const entries = Object.entries(rows ?? {});
  if (!entries.length) return null;
  return (
    <div>
      <p className="mb-1 text-[10px] uppercase tracking-wide text-muted">{title}</p>
      <table className="w-full text-[11px]">
        <thead className="text-[10px] uppercase text-muted">
          <tr className="border-b border-edge/60">
            <th className="py-1 text-left font-normal">bucket</th>
            <th className="py-1 text-right font-normal">n</th>
            <th className="py-1 text-right font-normal">win</th>
            <th className="py-1 text-right font-normal">mean</th>
            <th className="py-1 text-right font-normal">median</th>
          </tr>
        </thead>
        <tbody className="font-mono">
          {entries.map(([k, v]) => (
            <tr key={k} className="border-b border-edge/30">
              <td className="py-1 text-left font-sans">{k}</td>
              <td className="py-1 text-right text-muted">{v.n}</td>
              <td className="py-1 text-right">{v.win_rate_pct?.toFixed(0) ?? "—"}%</td>
              <td className={`py-1 text-right ${signCls(v.mean_pct)}`}>{pct(v.mean_pct)}</td>
              <td className={`py-1 text-right ${signCls(v.median_pct)}`}>{pct(v.median_pct)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {note && <p className="mt-1 text-[10px] leading-relaxed text-muted">{note}</p>}
    </div>
  );
}

/** Monthly bars, scaled to the largest absolute month so a single outlier
 *  month cannot make every other month look flat. */
function MonthChart({ rows }: { rows: ClosingMonthRow[] }) {
  const max = Math.max(...rows.map((r) => Math.abs(r.net_rs)), 1);
  return (
    <div className="flex items-end gap-1" style={{ height: "7rem" }}>
      {rows.map((r) => {
        const h = (Math.abs(r.net_rs) / max) * 100;
        const up = r.net_rs >= 0;
        return (
          <div key={r.month} className="flex flex-1 flex-col items-center justify-end gap-0.5"
               title={`${r.month}: ${rs(r.net_rs)} over ${r.trades} trades, win ${r.win_rate_pct}%`}>
            <div className="flex w-full flex-1 flex-col justify-end">
              {up && <div className="w-full rounded-t bg-bull/70" style={{ height: `${h}%` }} />}
            </div>
            <div className="h-px w-full bg-edge" />
            <div className="flex w-full flex-1 flex-col justify-start">
              {!up && <div className="w-full rounded-b bg-bear/70" style={{ height: `${h}%` }} />}
            </div>
            <span className="text-[8px] text-muted">{r.month.slice(2)}</span>
          </div>
        );
      })}
    </div>
  );
}

function WindowCards({ w, label }: { w: ClosingWindow; label: string }) {
  const u = w.underlying;
  const o = w.option;
  if (!u || !o) return null;
  return (
    <div className="grid gap-2 md:grid-cols-2">
      <div className="rounded border border-bull/30 bg-bull/5 p-3">
        <p className="mb-2 text-[10px] uppercase tracking-wide text-bull">
          Index · measured · {label}
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
        <p className="mt-2 text-[10px] leading-relaxed text-muted">{u.note}</p>
      </div>

      <div className="rounded border border-accent/30 bg-accent/5 p-3">
        <p className="mb-2 text-[10px] uppercase tracking-wide text-accent">
          Option · modelled · {label}
        </p>
        <div className="grid grid-cols-3 gap-2">
          <Stat label="win rate" value={`${o.win_rate_pct ?? 0}%`}
                cls={(o.win_rate_pct ?? 0) > 50 ? "text-bull" : "text-bear"}
                sub={`n=${o.n} trades`} />
          <Stat label="mean / trade" value={pct(o.mean_pct)} cls={signCls(o.mean_pct)}
                sub={o.mean_net_pct_ci95
                  ? `95% CI ${o.mean_net_pct_ci95[0]} to ${o.mean_net_pct_ci95[1]} · ${ciVerdict(o.mean_net_pct_ci95)}`
                  : undefined} />
          <Stat label="median / trade" value={pct(o.median_pct)} cls={signCls(o.median_pct)}
                sub="the typical night" />
        </div>
        <div className="mt-2 grid grid-cols-3 gap-2">
          <Stat label="total" value={rs(o.total_net_rs)} cls={signCls(o.total_net_rs)}
                sub={`${w.lots ?? 1} lot · ${w.qty ?? 0} qty`} />
          <Stat label="max drawdown" value={rs(o.max_drawdown_rs)} cls="text-bear"
                sub="peak-to-trough" />
          <Stat label="charges paid" value={rs(o.total_charges_rs)} cls="text-muted"
                sub={`avg premium ${rs(o.mean_premium_rs)}`} />
        </div>
      </div>
    </div>
  );
}


const MODE_LABEL: Record<string, string> = {
  prev_close: "vs yesterday's close",
  day_open: "vs today's open",
};

// The same two references as a bare noun phrase, for prose that already
// supplies the preposition ("...sat from X").
const MODE_NOUN: Record<string, string> = {
  prev_close: "yesterday's close",
  day_open: "today's open",
};

/** The two reference rules side by side. `ce share` is the column that matters
 *  most and is the least obvious: a rule that picks CE on 54% of nights is
 *  partly just long a rising index, and the ALWAYS CE control in the sweep
 *  below shows exactly how much that alone is worth. */
function SignalComparison({ rows, note, activeMode }: {
  rows: ClosingComparisonRow[]; note: string; activeMode?: string;
}) {
  if (!rows?.length) return null;
  return (
    <section className="card px-4 py-3">
      <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted">
        Which reference picks the side?
      </h2>
      <table className="w-full text-[11px]">
        <thead className="text-[10px] uppercase text-muted">
          <tr className="border-b border-edge/60">
            <th className="py-1 text-left font-normal">rule</th>
            <th className="py-1 text-right font-normal">window</th>
            <th className="py-1 text-right font-normal">n</th>
            <th className="py-1 text-right font-normal">ce share</th>
            <th className="py-1 text-right font-normal">continued</th>
            <th className="py-1 text-right font-normal">median pts</th>
            <th className="py-1 text-right font-normal">win</th>
            <th className="py-1 text-right font-normal">mean %</th>
            <th className="py-1 text-right font-normal">total</th>
          </tr>
        </thead>
        <tbody className="font-mono">
          {rows.map((r) => (
            <tr key={`${r.signal_mode}-${r.years}`}
                className={`border-b border-edge/30 ${r.signal_mode === activeMode ? "bg-accent/10" : ""}`}>
              <td className="py-1 text-left font-sans">
                {MODE_LABEL[r.signal_mode] ?? r.signal_mode}
                {r.signal_mode === activeMode && <span className="ml-1 text-accent">←</span>}
              </td>
              <td className="py-1 text-right text-muted">{r.years}y</td>
              <td className="py-1 text-right text-muted">{r.n}</td>
              <td className={`py-1 text-right ${Math.abs(r.ce_share_pct - 50) > 3 ? "text-bear" : "text-muted"}`}>
                {r.ce_share_pct}%
              </td>
              <td className={`py-1 text-right ${r.continued_pct > 50 ? "text-bull" : "text-bear"}`}>
                {r.continued_pct}%
              </td>
              <td className={`py-1 text-right ${signCls(r.median_signed_pts)}`}>
                {signed(r.median_signed_pts, 1)}
              </td>
              <td className="py-1 text-right">{r.win_rate_pct}%</td>
              <td className={`py-1 text-right ${signCls(r.mean_pct)}`}>{pct(r.mean_pct)}</td>
              <td className={`py-1 text-right ${signCls(r.total_net_rs)}`}>{rs(r.total_net_rs)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="mt-1 text-[10px] leading-relaxed text-muted">{note}</p>
    </section>
  );
}

/** Candidate sweep. Ranked on SKILL (drift-adjusted), never on raw mean, and
 *  the two ALWAYS controls are rendered inline rather than hidden — a
 *  candidate that cannot beat "always buy CE" has found nothing. */
function SignalSearch({ search }: { search: ClosingSignalSearch }) {
  if (!search?.available || !search.windows) return null;
  const pooled = search.windows.pooled_3y;
  const insample = search.windows.in_sample_first_2y;
  const holdout = search.windows.holdout_last_1y;
  const byKey = (w: typeof pooled) =>
    Object.fromEntries((w?.results ?? []).map((r) => [r.key, r]));
  const inMap = byKey(insample);
  const outMap = byKey(holdout);
  const survivors = new Set(search.survivors ?? []);
  return (
    <section className="card px-4 py-3">
      <div className="mb-2 flex flex-wrap items-baseline gap-3">
        <h2 className="text-xs font-semibold uppercase tracking-wide text-muted">
          Candidate sweep · index points only
        </h2>
        <span className="text-[11px] text-muted">
          unconditional drift {signed(pooled?.drift_pts, 2)} pts/night — the number
          every control below is really measuring
        </span>
      </div>
      <table className="w-full text-[11px]">
        <thead className="text-[10px] uppercase text-muted">
          <tr className="border-b border-edge/60">
            <th className="py-1 text-left font-normal">rule</th>
            <th className="py-1 text-right font-normal">n</th>
            <th className="py-1 text-right font-normal">ce share</th>
            <th className="py-1 text-right font-normal">hit</th>
            <th className="py-1 text-right font-normal">skill 3y</th>
            <th className="py-1 text-right font-normal">skill 2y in</th>
            <th className="py-1 text-right font-normal">skill 1y out</th>
            <th className="py-1 text-right font-normal">95% CI (3y)</th>
          </tr>
        </thead>
        <tbody className="font-mono">
          {(pooled?.results ?? []).map((r) => {
            const control = r.key.startsWith("always");
            const flipped = (inMap[r.key]?.skill_pts ?? 0) * (outMap[r.key]?.skill_pts ?? 0) < 0;
            return (
              <tr key={r.key}
                  className={`border-b border-edge/30 ${survivors.has(r.key) ? "bg-bull/10" : control ? "bg-panel2" : ""}`}>
                <td className="py-1 text-left font-sans">
                  {r.label}
                  {survivors.has(r.key) && <span className="ml-1 text-bull" title="clears zero on pooled AND holdout">✓</span>}
                  {flipped && <span className="ml-1 text-bear" title="skill flips sign between in-sample and holdout — noise">⚠</span>}
                </td>
                <td className="py-1 text-right text-muted">{r.n}</td>
                <td className={`py-1 text-right ${Math.abs(r.ce_share_pct - 50) > 5 ? "text-bear" : "text-muted"}`}>
                  {r.ce_share_pct}%
                </td>
                <td className="py-1 text-right">{r.hit_pct}%</td>
                <td className={`py-1 text-right ${signCls(r.skill_pts)}`}>{signed(r.skill_pts, 1)}</td>
                <td className={`py-1 text-right ${signCls(inMap[r.key]?.skill_pts)}`}>
                  {inMap[r.key] ? signed(inMap[r.key].skill_pts, 1) : "—"}
                </td>
                <td className={`py-1 text-right ${signCls(outMap[r.key]?.skill_pts)}`}>
                  {outMap[r.key] ? signed(outMap[r.key].skill_pts, 1) : "—"}
                </td>
                <td className="py-1 text-right text-[10px] text-muted">
                  {r.mean_ci95 ? `${r.mean_ci95[0]} … ${r.mean_ci95[1]}` : "—"}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <p className="mt-1 text-[10px] leading-relaxed text-muted">
        ✓ cleared zero on both the pooled window and the holdout · ⚠ skill flips
        sign between in-sample and holdout, which is what noise looks like. {search.note}
      </p>
    </section>
  );
}

// --- tonight at 15:00 --------------------------------------------------------

function CheckChip({ c }: { c: ClosingTonightCheck }) {
  const cls = c.status === "clear" ? "border-bull/40 bg-bull/10 text-bull"
    : c.status === "red" ? "border-bear/40 bg-bear/10 text-bear"
    : "border-edge/60 bg-panel2 text-muted";
  const icon = c.status === "clear" ? "✓" : c.status === "red" ? "✕" : "?";
  return (
    <span title={c.detail} className={`rounded border px-2 py-0.5 text-[10px] ${cls}`}>
      {icon} {c.label}
    </span>
  );
}

const VERDICT_BANNER: Record<string, { cls: string; text: string }> = {
  CLEAN: {
    cls: "border-bull/40 bg-bull/10 text-bull",
    text: "CLEAN — all five checks clear. Historically ~5-6 such nights a month; the clean cell made money in both backtest windows (hypothesis — graded live).",
  },
  FLAGGED: {
    cls: "border-bear/40 bg-bear/10 text-bear",
    text: "FLAGGED — the checks say stand aside tonight.",
  },
  INCOMPLETE: {
    cls: "border-edge/60 bg-panel2 text-muted",
    text: "INCOMPLETE — some inputs are not readable yet; no verdict.",
  },
};

/** The live pre-trade card: today's ledger-style values + the five registered
 *  risk checks, readable at 15:00 before the 15:05 fill. */
function TonightPanel() {
  const [t, setT] = useState<ClosingTonight | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setBusy(true);
    try {
      setT(await api.closingTonight());
      setErr(null);
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      setErr(msg.includes("401") ? "auth" : msg.startsWith("404") ? "restart" : msg);
      setT(null);
    } finally {
      setBusy(false);
    }
  }, []);
  useEffect(() => { void load(); }, [load]);

  const v = t?.values;
  const banner = t?.verdict ? VERDICT_BANNER[t.verdict] : null;
  return (
    <section className="card px-4 py-3">
      <div className="mb-2 flex flex-wrap items-center gap-3">
        <h2 className="text-xs font-semibold uppercase tracking-wide text-muted">
          Tonight at 15:00 · live
        </h2>
        {t?.available && (
          <span className="text-[11px] text-muted">
            {t.weekday} {t.date} · read from {t.signal_time} · as of{" "}
            {t.as_of?.slice(11, 19)} IST
          </span>
        )}
        {t?.provisional && (
          <span className="tag bg-accent/15 text-[10px] text-accent"
                title="not final: before 15:00 the latest bar stands in for the print; between 15:00 and 15:05 the bar is still forming, so the VIX and range checks can still move. The read settles at 15:05 — the fill clock.">
            PROVISIONAL
          </span>
        )}
        <button onClick={() => void load()} disabled={busy}
                className="ml-auto rounded bg-panel2 px-3 py-1 text-xs text-muted hover:text-white disabled:opacity-50">
          {busy ? "Reading…" : "Refresh"}
        </button>
      </div>

      {err === "auth" && (
        <p className="text-[11px] text-muted">
          Kite login required for the live read — authenticate on the dashboard, then Refresh.
        </p>
      )}
      {err === "restart" && (
        <p className="text-[11px] text-muted">
          The backend hasn&apos;t loaded the /closing/tonight endpoint yet — it arrives with the
          next backend restart (⟳ on the dashboard).
        </p>
      )}
      {err && err !== "auth" && err !== "restart" && (
        <p className="text-[11px] text-bear">{err}</p>
      )}
      {t && !t.available && <p className="text-[11px] text-muted">{t.reason}</p>}

      {t?.available && v && (
        <>
          {banner && (
            <p className={`mb-2 rounded border px-2 py-1.5 text-[11px] ${banner.cls}`}>
              {banner.text}
              {t.verdict === "FLAGGED" && t.checks && (
                <span> Red: {t.checks.filter((c) => c.status === "red").map((c) => c.label).join(", ")}.</span>
              )}
            </p>
          )}
          <div className="grid grid-cols-2 gap-2 md:grid-cols-4 lg:grid-cols-8">
            <Stat label="15:00 print" value={fmt(v.p1500, 1) ?? "—"}
                  sub={t.provisional ? "latest bar — provisional" : "signal read"} />
            <Stat label="day open" value={fmt(v.day_open, 1) ?? "—"} sub="09:15" />
            <Stat label="gap vs open" value={signed(v.gap_pts, 1)} cls={signCls(v.gap_pts)}
                  sub={v.direction ? `→ buy ATM ${v.direction}` : "flat — no signal"} />
            <Stat label="14:00" value={v.p1400 != null ? fmt(v.p1400, 1) ?? "—" : "—"}
                  sub="last-hour reference" />
            <Stat label="prev close" value={v.prev_close != null ? fmt(v.prev_close, 1) ?? "—" : "—"}
                  sub="CAS-aware" />
            <Stat label="strike" value={v.strike != null ? fmt(v.strike, 0) ?? "—" : "—"}
                  sub={v.expiry ? `${v.expiry} · ${v.dte}d` : undefined} />
            <Stat label="exit session" value={v.next_trading_day ?? "—"}
                  sub={v.carry_days != null ? `carry ${v.carry_days}d` : undefined} />
            <Stat label="India VIX" value={v.vix_1500 != null ? v.vix_1500.toFixed(2) : "—"}
                  sub={v.vix_prev_close != null && v.vix_0915 != null
                    ? `prev ${v.vix_prev_close.toFixed(2)} · 09:15 ${v.vix_0915.toFixed(2)}`
                    : undefined} />
          </div>
          <div className="mt-2 flex flex-wrap gap-1.5">
            {(t.checks ?? []).map((c) => <CheckChip key={c.key} c={c} />)}
            {v.range_pos != null && (
              <span className="rounded border border-edge/60 bg-panel2 px-2 py-0.5 text-[10px] text-muted"
                    title="position of the 15:00 print in today's range">
                range {v.range_pos.toFixed(2)}
              </span>
            )}
          </div>
          {t.first_eval?.drifted && (
            <p className="mt-2 rounded border border-bear/40 bg-bear/5 px-2 py-1 text-[10px] text-bear">
              The first evaluation today ({t.first_eval.as_of.slice(11, 19)}) said{" "}
              {t.first_eval.verdict} — the data has since drifted; the logged decision-time
              record keeps the first read.
            </p>
          )}
          <p className="mt-2 text-[10px] leading-relaxed text-muted">{t.note}</p>
        </>
      )}
    </section>
  );
}

// --- loss attribution --------------------------------------------------------

/** Ledger-row shorthand for the dominant loss component. */
const REASON_SHORT: Record<string, { label: string; cls: string }> = {
  wrong_direction: { label: "DIR", cls: "bg-bear/20 text-bear" },
  theta_decay: { label: "θ", cls: "bg-accent/20 text-accent" },
  vol_crush: { label: "VOL", cls: "bg-panel2 text-muted" },
  costs: { label: "FEE", cls: "bg-panel2 text-muted" },
};

const VERDICT_CLS: Record<string, string> = {
  "consistent-bad": "bg-bear/15 text-bear",
  "consistent-positive": "bg-bull/15 text-bull",
  "flips — noise": "bg-panel2 text-muted",
  "too few trades": "text-muted/60",
};

/** Anatomy chips for one ledger row: dominant loss reason + calendar flags. */
function WhyChips({ t }: { t: ClosingTrade }) {
  const reason = t.loss_reason ? REASON_SHORT[t.loss_reason] : null;
  const anatomy = t.direction_pct != null
    ? `direction ${signed(t.direction_pct, 1)}pp · theta ${signed(t.theta_pct, 1)}pp · vol ${signed(t.vega_pct, 1)}pp · costs ${signed(t.costs_pct, 1)}pp`
    : "attribution unavailable";
  return (
    <span className="inline-flex gap-0.5">
      {reason && (
        <span title={anatomy} className={`rounded px-1 text-[9px] font-mono ${reason.cls}`}>
          {reason.label}
        </span>
      )}
      {t.f_holiday_bridge === true && (
        <span title={`holiday bridge: exit ${t.carry_days} calendar days out`}
              className="rounded bg-panel2 px-1 text-[9px] font-mono text-muted">B</span>
      )}
      {t.f_month_end === true && (
        <span title="month-end entry: last trading session of the month"
              className="rounded bg-panel2 px-1 text-[9px] font-mono text-muted">M</span>
      )}
    </span>
  );
}

function ReasonTable({ label, reasons }: { label: string; reasons: ClosingReasons }) {
  if (!reasons?.rows?.length) return null;
  return (
    <div>
      <p className="mb-1 text-[10px] uppercase tracking-wide text-muted">
        {label} · {reasons.n_losers} losers of {reasons.n_trades} trades
      </p>
      <table className="w-full text-[11px]">
        <thead className="text-[10px] uppercase text-muted">
          <tr className="border-b border-edge/60">
            <th className="py-1 text-left font-normal">dominant reason</th>
            <th className="py-1 text-right font-normal">n</th>
            <th className="py-1 text-right font-normal">of losers</th>
            <th className="py-1 text-right font-normal">mean</th>
            <th className="py-1 text-right font-normal">total</th>
            <th className="py-1 text-right font-normal" title="losers where the index went the signal's way and decay/spread still ate the premium">
              index right
            </th>
          </tr>
        </thead>
        <tbody className="font-mono">
          {reasons.rows.map((row) => (
            <tr key={row.reason} className="border-b border-edge/30">
              <td className="py-1 text-left font-sans">{row.label}</td>
              <td className="py-1 text-right text-muted">{row.n}</td>
              <td className="py-1 text-right text-muted">{row.share_of_losers_pct}%</td>
              <td className="py-1 text-right text-bear">{pct(row.mean_pct)}</td>
              <td className="py-1 text-right text-bear">{rs(row.total_rs)}</td>
              <td className="py-1 text-right text-muted">{row.index_right_way_n}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function BucketTable({ rows, split }: { rows: ClosingBucketRow[]; split: string }) {
  const cell = (c: ClosingBucketRow["in_sample"]) =>
    c.mean_pct == null
      ? <span className="text-muted/60">n={c.n}</span>
      : <span className={signCls(c.mean_pct)}>{pct(c.mean_pct)} <span className="text-muted">n={c.n}</span></span>;
  return (
    <div>
      <p className="mb-1 text-[10px] uppercase tracking-wide text-muted">
        Calendar buckets · modelled · split at {split}
      </p>
      <table className="w-full text-[11px]">
        <thead className="text-[10px] uppercase text-muted">
          <tr className="border-b border-edge/60">
            <th className="py-1 text-left font-normal">bucket</th>
            <th className="py-1 text-right font-normal">n 3y</th>
            <th className="py-1 text-right font-normal">2y in-sample</th>
            <th className="py-1 text-right font-normal">1y holdout</th>
            <th className="py-1 text-left font-normal pl-3">verdict</th>
          </tr>
        </thead>
        <tbody className="font-mono">
          {rows.map((b) => (
            <tr key={b.tag} className={`border-b border-edge/30 ${b.registered ? "bg-accent/5" : ""}`}>
              <td className="py-1 text-left font-sans">
                {b.tag}
                {b.registered && <span className="ml-1 text-accent" title="pre-registered flag — see below">⚑</span>}
              </td>
              <td className="py-1 text-right text-muted">
                {b.n}{b.below_house_n && <span title="below the 30-per-window house sample" className="text-bear">*</span>}
              </td>
              <td className="py-1 text-right">{cell(b.in_sample)}</td>
              <td className="py-1 text-right">{cell(b.holdout)}</td>
              <td className="py-1 pl-3 text-left">
                <span className={`rounded px-1 text-[9px] ${VERDICT_CLS[b.verdict] ?? "text-muted"}`}>
                  {b.verdict}
                </span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ClosingLadderTable({ rungs, label }: { rungs: OvernightLadderRung[]; label: string }) {
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
            <th className="py-1 text-right font-normal" title="what this rung's added flag removed">removed</th>
          </tr>
        </thead>
        <tbody className="font-mono">
          {rungs.map((rg) => (
            <tr key={rg.label} className="border-b border-edge/30">
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

/** Loss anatomy, the calendar-bucket diagnostic, and the two pre-registered
 *  calendar flags with their live scoreboard. The panel's whole design rule:
 *  the bucket table is a diagnostic that may flip next month; only the frozen
 *  flags carry standing, and only via live nights. */
function AttributionPanel({ a }: { a: ClosingAttribution }) {
  const live = a.flags?.live;
  return (
    <section className="card grid gap-3 px-4 py-3">
      <div className="flex flex-wrap items-baseline gap-3">
        <h2 className="text-xs font-semibold uppercase tracking-wide text-muted">
          Why the losers lost · modelled
        </h2>
        <span className="text-[11px] text-muted">
          every losing night decomposed: spot move → clock → VIX → costs
        </span>
      </div>

      <div className="grid gap-4 md:grid-cols-2">
        <ReasonTable label="Last year" reasons={a.reasons_1y} />
        <ReasonTable label="Three years" reasons={a.reasons_3y} />
      </div>
      <p className="text-[10px] leading-relaxed text-muted">{a.reasons_1y?.note} {a.note}</p>

      <BucketTable rows={a.buckets?.rows ?? []} split={a.buckets?.split ?? ""} />
      <p className="text-[10px] leading-relaxed text-muted">{a.buckets?.note}</p>

      <div className="flex flex-wrap items-baseline gap-3 border-t border-edge/60 pt-3">
        <h3 className="text-xs font-semibold uppercase tracking-wide text-muted">
          Pre-registered calendar flags
        </h3>
        <span className="tag bg-panel2 text-[10px] text-muted">
          registered {a.flags?.registered_on} · flags, not gates
        </span>
      </div>

      <div className="grid gap-2 md:grid-cols-2">
        {(a.flags?.definitions ?? []).map((d) => (
          <div key={d.key} className="rounded border border-edge/60 bg-panel2 px-3 py-2">
            <p className="text-[11px]">
              <span className="font-semibold">{d.label}</span>{" "}
              <span className="tag bg-accent/15 text-[9px] text-accent">{d.status}</span>
            </p>
            <p className="mt-1 text-[11px]">{d.rule}</p>
            <p className="mt-1 text-[10px] leading-relaxed text-muted">{d.evidence}</p>
          </div>
        ))}
      </div>

      {a.flags?.ladder_1y && (
        <ClosingLadderTable rungs={a.flags.ladder_1y} label="Flag ladder · last year · modelled" />
      )}
      {a.flags?.ladder_3y && (
        <ClosingLadderTable rungs={a.flags.ladder_3y} label="Flag ladder · 3 years · modelled" />
      )}

      {live && (
        <div className="rounded border border-accent/30 bg-accent/5 px-3 py-2">
          <p className="text-[10px] uppercase tracking-wide text-accent">
            Live scoreboard — the test that counts
          </p>
          <div className="mt-1 grid grid-cols-2 gap-2 md:grid-cols-4">
            <Stat label="live nights" value={String(live.live_nights)}
                  sub={`after ${live.registered_on}`} />
            <Stat label="flagged nights" value={String(live.flagged_nights)}
                  sub={`${live.verdict_due} more until a verdict (${live.min_sample} rule)`} />
            <Stat label="flagged P&L" value={live.flagged.n ? pct(live.flagged.mean_pct) : "—"}
                  cls={signCls(live.flagged.mean_pct)}
                  sub={live.flagged.n ? `n=${live.flagged.n} · ${rs(live.flagged.total_rs)}` : "no nights yet"} />
            <Stat label="clear P&L" value={live.clear.n ? pct(live.clear.mean_pct) : "—"}
                  cls={signCls(live.clear.mean_pct)}
                  sub={live.clear.n ? `n=${live.clear.n} · ${rs(live.clear.total_rs)}` : "no nights yet"} />
          </div>
          <p className="mt-1 text-[10px] leading-relaxed text-muted">{live.note}</p>
        </div>
      )}

      <p className="text-[10px] leading-relaxed text-muted">{a.flags?.note}</p>
    </section>
  );
}

function errHint(err: string): string {
  if (err.includes("404")) {
    return "The backend hasn't loaded the /closing endpoints yet — they arrive with the next backend restart.";
  }
  return `${err} — is the backend running?`;
}

export function ClosingLab() {
  const status = usePolling(() => api.closingStatus(), 20000, []);
  const [r, setR] = useState<ClosingResults | null>(null);
  const [trades, setTrades] = useState<ClosingTrade[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState<"sync" | "analyze" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showTrades, setShowTrades] = useState(false);

  const load = useCallback(async () => {
    try {
      setR(await api.closingResults());
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
    if (!showTrades || trades.length) return;
    void api.closingTrades(400).then((t) => setTrades(t.rows)).catch(() => undefined);
  }, [showTrades, trades.length]);

  const run = async (mode: "sync" | "analyze") => {
    setBusy(mode);
    setError(null);
    try {
      if (mode === "sync") await api.closingSync();
      setR(await api.closingAnalyze());
      setTrades([]);
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      setError(msg.includes("401") || msg.toLowerCase().includes("login")
        ? "Kite login required — authenticate on the dashboard first, then retry."
        : msg);
    } finally {
      setBusy(null);
    }
  };

  const primary = r?.primary;
  const val = r?.validation;
  const debiased = r?.robustness?.debiased;

  return (
    <div className="mx-auto flex min-h-screen max-w-6xl flex-col gap-3 p-3">
      <header className="card flex flex-wrap items-center gap-3 px-4 py-3">
        <a href="/" className="text-xs text-muted hover:text-white">← Dashboard</a>
        <ModuleSwitcher />
        <h1 className="text-sm font-semibold">Closing Day Strategy · NIFTY 50</h1>
        {r?.rule?.mode && (
          <span className="tag bg-accent/15 text-[10px] text-accent"
                title="the 15:00 reference this analysis was run on">
            {MODE_LABEL[r.rule.mode] ?? r.rule.mode}
          </span>
        )}
        {primary && (
          <span className="text-[11px] text-muted">
            {primary.n} nights · {primary.from} → {primary.to}
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

      {/* the live pre-trade card — rendered regardless of stored results so
          it is usable at 15:00 even before the first Sync + Analyze */}
      <TonightPanel />

      {error && (
        <div className="card border-bear/50 bg-bear/10 px-4 py-2 text-xs text-bear">{error}</div>
      )}

      {loaded && !r && !error && (
        <div className="card px-4 py-6 text-center text-xs text-muted">
          No analysis yet — click <span className="text-accent">Sync + Analyze</span> to pull
          NIFTY and India VIX history and run the study.
        </div>
      )}

      {r && (
        <>
          {/* the rule, stated before any number */}
          <section className="card px-4 py-3">
            <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted">
              The rule under test
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

          {primary && <WindowCards w={primary} label="1 year" />}

          {/* the honest caveat block, deliberately high on the page */}
          {val && (
            <section className="card px-4 py-3">
              <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted">
                How wrong is the option model?
              </h2>
              <div className="grid gap-2 md:grid-cols-4">
                <Stat label="out-of-sample error"
                      value={val.out_of_sample.pooled_median_abs_err_pct != null
                        ? `${val.out_of_sample.pooled_median_abs_err_pct}%`
                        : "—"}
                      sub={`median |error| on premium level, ${val.out_of_sample.sessions ?? 0} held-out sessions`} />
                <Stat label="in-sample error"
                      value={val.level.in_sample?.median_abs_err_pct != null
                        ? `${val.level.in_sample.median_abs_err_pct}%` : "—"}
                      sub={val.level.uncalibrated_baseline?.median_abs_err_pct != null
                        ? `vs ${val.level.uncalibrated_baseline.median_abs_err_pct}% uncalibrated`
                        : undefined} />
                <Stat label="overnight error"
                      value={val.overnight.median_abs_err_pp != null
                        ? `${val.overnight.median_abs_err_pp}pp` : "—"}
                      sub={val.overnight.available
                        ? `on the study's own metric, n=${val.overnight.n_pairs} real pairs`
                        : val.overnight.reason} />
                <Stat label="overnight bias"
                      value={val.overnight.median_bias_pp != null
                        ? `${val.overnight.median_bias_pp}pp` : "—"}
                      cls={signCls(val.overnight.median_bias_pp)}
                      sub={val.overnight.median_bias_pp != null && val.overnight.median_bias_pp < 0
                        ? "model UNDERSTATES the real overnight move"
                        : "model overstates the real overnight move"} />
              </div>

              {primary?.trust?.available && (
                <p className="mt-2 rounded border border-bear/40 bg-bear/5 px-2 py-1.5 text-[10px] leading-relaxed text-bear">
                  Trust boundary: the pricing model was calibrated over VIX{" "}
                  {primary.trust.calibrated_vix_range?.[0]}–{primary.trust.calibrated_vix_range?.[1]}.{" "}
                  {primary.trust.trades_outside_pct}% of these trades, carrying{" "}
                  {primary.trust.abs_pnl_outside_pct}% of the absolute P&amp;L, fell outside that
                  range. Their premiums are extrapolated.
                </p>
              )}

              {debiased && (
                <div className="mt-2 rounded border border-edge/60 bg-panel2 px-2 py-1.5">
                  <p className="text-[10px] uppercase tracking-wide text-muted">
                    Sensitivity · the same year with the measured bias removed
                  </p>
                  <p className="mt-1 text-[11px]">
                    Shifting every exit premium by {signed(debiased.applied_pp, 1)}pp moves the
                    year from{" "}
                    <span className={signCls(primary?.option?.total_net_rs)}>
                      {rs(primary?.option?.total_net_rs)}
                    </span>{" "}
                    to{" "}
                    <span className={signCls(debiased.result.option?.total_net_rs)}>
                      {rs(debiased.result.option?.total_net_rs)}
                    </span>{" "}
                    ({pct(debiased.result.option?.mean_pct)} mean,{" "}
                    {debiased.result.per_month?.positive_months}/
                    {debiased.result.per_month?.months} positive months).
                  </p>
                  <p className="mt-1 text-[10px] leading-relaxed text-muted">{debiased.note}</p>
                </div>
              )}
            </section>
          )}

          {r.signal_comparison && (
            <SignalComparison rows={r.signal_comparison.rows} note={r.signal_comparison.note}
                              activeMode={r.rule?.mode} />
          )}
          {r.signal_search && <SignalSearch search={r.signal_search} />}

          {/* monthly */}
          {primary?.per_month && (
            <section className="card px-4 py-3">
              <div className="mb-2 flex flex-wrap items-baseline gap-3">
                <h2 className="text-xs font-semibold uppercase tracking-wide text-muted">
                  Month by month · modelled
                </h2>
                <span className="text-[11px] text-muted">
                  mean{" "}
                  <span className={signCls(primary.per_month.mean_net_rs)}>
                    {rs(primary.per_month.mean_net_rs)}
                  </span>
                  {" "}· median{" "}
                  <span className={signCls(primary.per_month.median_net_rs)}>
                    {rs(primary.per_month.median_net_rs)}
                  </span>
                  {" "}· {primary.per_month.positive_months}/{primary.per_month.months} months
                  positive · {primary.per_month.mean_trades} trades/month
                </span>
              </div>
              <MonthChart rows={primary.per_month.rows} />
              <table className="mt-3 w-full text-[11px]">
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
                  {primary.per_month.rows.map((m) => (
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
            </section>
          )}

          {/* cuts */}
          <section className="card grid gap-4 px-4 py-3 md:grid-cols-2">
            <CutTable title="By direction · modelled" rows={primary?.by_direction}
                      note="PE and CE are not the same trade: the index drifts up over time, so a CE is fighting less of a headwind but pays for it in the premium it entered at." />
            <CutTable title="By days to expiry · modelled" rows={primary?.by_dte}
                      note="0-1 exits land on expiry morning, where gamma is largest and theta is brutal. 6-7 is the roll that happens when the signal fires on an expiry day." />
            <CutTable title="By distance from the reference · modelled" rows={primary?.by_gap}
                      note={`How far the 15:00 print sat from ${MODE_NOUN[r.rule?.mode] ?? "the reference"}. A bigger gap is a louder signal — and a more expensive option.`} />
            <CutTable title="By entry VIX · modelled" rows={primary?.by_vix}
                      note="Read alongside the trust boundary above: only the lowest bucket sits inside the range the pricing model was calibrated on." />
          </section>

          {/* loss anatomy + registered calendar flags — guarded like the
              calibration block: a stale results file predates it */}
          {r.attribution && <AttributionPanel a={r.attribution} />}

          {/* robustness */}
          <section className="card px-4 py-3">
            <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted">
              Robustness
            </h2>
            {r.robustness?.three_year?.underlying && (
              <div className="mb-3">
                <p className="mb-1 text-[10px] uppercase tracking-wide text-muted">
                  Same rule, three years
                </p>
                <WindowCards w={r.robustness!.three_year} label="3 years" />
              </div>
            )}
            <p className="mb-1 text-[10px] uppercase tracking-wide text-muted">
              Clock sweep · index points only
            </p>
            <table className="w-full text-[11px]">
              <thead className="text-[10px] uppercase text-muted">
                <tr className="border-b border-edge/60">
                  <th className="py-1 text-left font-normal">entry</th>
                  <th className="py-1 text-left font-normal">exit</th>
                  <th className="py-1 text-right font-normal">n</th>
                  <th className="py-1 text-right font-normal">continued</th>
                  <th className="py-1 text-right font-normal">mean pts</th>
                  <th className="py-1 text-right font-normal">median pts</th>
                </tr>
              </thead>
              <tbody className="font-mono">
                {(r.robustness?.variants ?? []).map((v) => {
                  const chosen = v.entry === "15:00" && v.exit === "09:50";
                  return (
                    <tr key={`${v.entry}-${v.exit}`}
                        className={`border-b border-edge/30 ${chosen ? "bg-accent/10" : ""}`}>
                      <td className="py-1 text-left">{v.entry}{chosen && " ←"}</td>
                      <td className="py-1 text-left">{v.exit}</td>
                      <td className="py-1 text-right text-muted">{v.n}</td>
                      <td className={`py-1 text-right ${v.continued_pct > 50 ? "text-bull" : "text-bear"}`}>
                        {v.continued_pct}%
                      </td>
                      <td className={`py-1 text-right ${signCls(v.mean_signed_pts)}`}>
                        {signed(v.mean_signed_pts, 1)}
                      </td>
                      <td className={`py-1 text-right ${signCls(v.median_signed_pts)}`}>
                        {signed(v.median_signed_pts, 1)}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            <p className="mt-1 text-[10px] leading-relaxed text-muted">{r.robustness?.variants_note}</p>
          </section>

          {/* calibration provenance — guarded: a stale results file from an
              earlier schema may predate these blocks, and a crash here takes
              the Re-analyze button down with it (review catch). */}
          {r.model && r.calibration && (
          <section className="card px-4 py-3">
            <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted">
              Where the model's numbers come from
            </h2>
            {r.model.curve_is_fallback && (
              <p className="mb-2 rounded border border-bear/40 bg-bear/5 px-2 py-1.5 text-[10px] text-bear">
                No calibration sample on this machine — the vol curve is a flat
                fallback and every option number below is UNCALIBRATED.
              </p>
            )}
            <div className="grid gap-3 md:grid-cols-2">
              <div>
                <p className="text-[11px]">
                  Carry <span className="font-mono text-accent">{r.model.carry_pct}%/yr</span>{" "}
                  <span className="text-muted">
                    fitted from put-call parity on {r.calibration.carry.n} real ATM quotes across{" "}
                    {r.calibration.carry.sessions} sessions
                  </span>
                </p>
                <p className="mt-1 text-[11px]">
                  ATM IV / VIX ratio{" "}
                  <span className="text-muted">
                    over {r.calibration.iv_curve.n_obs} quotes, VIX{" "}
                    {r.calibration.iv_curve.vix_min}–{r.calibration.iv_curve.vix_max}
                  </span>
                </p>
                <div className="mt-1 flex flex-wrap gap-1">
                  {r.calibration.iv_curve.points.map((p) => (
                    <span key={p.dte} className="tag bg-panel2 font-mono text-[10px] text-muted">
                      {p.dte_mid.toFixed(1)}d → {p.ratio}×
                    </span>
                  ))}
                </div>
              </div>
              <ul className="list-disc space-y-1 pl-4 text-[10px] leading-relaxed text-muted">
                {[...(r.calibration.carry?.caveats ?? []), ...(r.calibration.iv_curve?.caveats ?? [])].map((c, i) => (
                  <li key={i}>{c}</li>
                ))}
              </ul>
            </div>
          </section>
          )}

          {/* ledger */}
          <section className="card px-4 py-3">
            <button onClick={() => setShowTrades((s) => !s)}
                    className="text-xs font-semibold uppercase tracking-wide text-muted hover:text-white">
              {showTrades ? "▾" : "▸"} Trade ledger ({primary?.n ?? 0} nights)
            </button>
            {showTrades && (
              <div className="mt-2 max-h-[32rem] overflow-auto">
                <table className="w-full text-[11px]">
                  <thead className="sticky top-0 bg-panel text-[10px] uppercase text-muted">
                    <tr className="border-b border-edge/60">
                      <th className="py-1 text-left font-normal">date</th>
                      <th className="py-1 text-left font-normal">dir</th>
                      <th className="py-1 text-right font-normal" title="today's 09:15 open">
                        open
                      </th>
                      <th className="py-1 text-right font-normal"
                          title={`the 15:00 print — the signal compares it against ${MODE_NOUN[r.rule?.mode] ?? "the reference"}`}>
                        15:00
                      </th>
                      <th className="py-1 text-right font-normal" title="the next session's 09:50 print — the exit">
                        next 09:50
                      </th>
                      <th className="py-1 text-right font-normal"
                          title={`15:00 minus ${MODE_NOUN[r.rule?.mode] ?? "the reference"} — negative picks PE, positive picks CE`}>
                        gap
                      </th>
                      <th className="py-1 text-right font-normal">strike</th>
                      <th className="py-1 text-right font-normal">dte</th>
                      <th className="py-1 text-right font-normal">move</th>
                      <th className="py-1 text-right font-normal">in</th>
                      <th className="py-1 text-right font-normal">out</th>
                      <th className="py-1 text-right font-normal">net %</th>
                      <th className="py-1 text-right font-normal">net</th>
                      <th className="py-1 text-center font-normal"
                          title="dominant loss component (hover for the full anatomy) + calendar flags">
                        why
                      </th>
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
                        <td className="py-1 text-right text-muted">{fmt(t.signal_price, 0)}</td>
                        {/* The exit print, coloured by whether the night went the
                            signal's way — same verdict as `move`, readable at a
                            glance without subtracting. */}
                        <td className={`py-1 text-right ${signCls(t.signed_move_pts)}`}>
                          {fmt(t.exit_spot, 0)}
                        </td>
                        <td className={`py-1 text-right ${signCls(t.gap_pts)}`}>{signed(t.gap_pts, 0)}</td>
                        <td className="py-1 text-right text-muted">{fmt(t.strike, 0)}</td>
                        <td className="py-1 text-right text-muted">{t.dte_entry.toFixed(0)}</td>
                        <td className={`py-1 text-right ${signCls(t.signed_move_pts)}`}>
                          {signed(t.signed_move_pts, 0)}
                        </td>
                        <td className="py-1 text-right text-muted">{fmt(t.fill_in, 1)}</td>
                        <td className="py-1 text-right text-muted">{fmt(t.fill_out, 1)}</td>
                        <td className={`py-1 text-right ${signCls(t.net_pct)}`}>{pct(t.net_pct, 0)}</td>
                        <td className={`py-1 text-right ${signCls(t.net_rs)}`}>{rs(t.net_rs)}</td>
                        <td className="py-1 text-center"><WhyChips t={t} /></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                <p className="mt-1 text-[10px] text-muted">
                  * entry VIX outside the range the pricing model was calibrated on.
                  Spot columns: today's 09:15 open, the 15:00 print, and the next
                  session's 09:50 print. `move` is signed from the 15:05 fill:
                  positive means the index went the way the signal pointed.
                  `why` (losers only): DIR = wrong direction, θ = theta decay,
                  VOL = vol crush, FEE = spread+charges — hover for the full
                  anatomy. B = holiday-bridge carry, M = month-end entry (the
                  pre-registered flags).
                </p>
              </div>
            )}
          </section>

          {primary?.skipped && Object.keys(primary.skipped).length > 0 && (
            <p className="px-1 text-[10px] text-muted">
              Nights skipped:{" "}
              {Object.entries(primary.skipped).map(([k, v]) => `${k} (${v})`).join(" · ")}
            </p>
          )}
        </>
      )}
    </div>
  );
}
