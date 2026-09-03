"use client";

// The R&D tab: a read-only research surface over the paper book, answering one
// question — does a signal deliver >=5% premium profit inside its mode's
// window? (scalp 30m, intraday 120m, positional 240m capped 14:50 IST).
// Everything rendered here is the backend's precomputed analytics; no
// client-side recomputation beyond display shaping. Two evidence classes ride
// every number: EXACT (R1 touch ladder) and APPROX (pre-ladder lower bound) —
// they are labelled, never blended silently. Suggestions never come from this
// layer; they only ever come from a policy ledger that cleared its bar (R3).

import { useCallback, useEffect, useState } from "react";
import {
  api,
  RndCandidates,
  RndCutCell,
  RndLedgerResponse,
  RndModeKpi,
  RndPolicies,
  RndSummary,
} from "@/lib/api";
import { fmt, istDateTime, signed } from "@/lib/format";
import { ModuleSwitcher } from "./ModuleSwitcher";

const MODES = ["scalp", "intraday", "positional"] as const;
type ModeKey = (typeof MODES)[number];

const BUCKETS = ["<3", "3-5", "5-10", "10-20", ">20"] as const;

// Stacked-bar segment shades: the further right of +5% the touch got, the
// greener; "<3" (never even reached +3) stays muted.
const BUCKET_BAR_CLASS: Record<string, string> = {
  "<3": "bg-muted/30",
  "3-5": "bg-accent/40",
  "5-10": "bg-bull/40",
  "10-20": "bg-bull/70",
  ">20": "bg-bull",
};

const BUCKET_CHIP_CLASS: Record<string, string> = {
  "<3": "bg-panel2 text-muted",
  "3-5": "bg-accent/15 text-accent",
  "5-10": "bg-bull/10 text-bull",
  "10-20": "bg-bull/20 text-bull",
  ">20": "bg-bull/30 text-bull",
};

const HEAT_LABELS = ["none", "-3", "-5", "-10", "-20"] as const;

const CUT_DIMS = [
  { key: "score_band", label: "Score band" },
  { key: "tape_state", label: "Tape" },
  { key: "golden", label: "Golden" },
  { key: "entry_hour", label: "Entry hour" },
  { key: "day_of_week", label: "Day of week" },
  { key: "direction", label: "Direction" },
] as const;
type CutDim = (typeof CUT_DIMS)[number]["key"];

// Reach-curve series: p5 wears the accent — it IS the headline question.
const CURVE_SERIES = [
  { key: "p3", label: "+3%", color: "#8b98a9" },
  { key: "p5", label: "+5%", color: "#3b82f6" },
  { key: "p10", label: "+10%", color: "#16c784" },
  { key: "p20", label: "+20%", color: "#eab308" },
] as const;

const LEDGER_DISPLAY_CAP = 100;

/** A 404 here means the backend process predates the /rnd routes, not that
 *  it is down. */
function errHint(err: string): string {
  if (err.includes("404")) {
    return "The backend hasn't loaded the R&D endpoints yet — they arrive with the next backend restart.";
  }
  return `${err} — is the backend running?`;
}

function pctLabel(x: number | null | undefined, dp = 1): string {
  return x == null ? "—" : `${x.toFixed(dp)}%`;
}

/** Headline tone: bull >=50, caution 30-50, bear <30; grey while accumulating. */
function headlineClass(p5: number | null, sufficient: boolean): string {
  if (!sufficient || p5 == null) return "text-muted";
  if (p5 >= 50) return "text-bull";
  if (p5 >= 30) return "text-yellow-400";
  return "text-bear";
}

function windowLabel(s: RndSummary, mode: ModeKey): string {
  const min = s.windows_min[mode];
  return mode === "positional"
    ? `5% in ${min}m (cap ${s.positional_cutoff_ist})`
    : `5% in ${min}m`;
}

/** ⚠ chip on any aggregate carrying approx (lower-bound) rows. */
function ApproxChip({ note }: { note?: string }) {
  return (
    <span
      className="tag bg-yellow-500/15 text-[9px] text-yellow-400"
      title={note ?? "Approx rows are a lower bound."}
    >
      ⚠ approx = lower bound
    </span>
  );
}

// ---------------------------------------------------------------------------
// b. KPI strip
// ---------------------------------------------------------------------------

function KpiRow({ s, mode, note }: { s: RndSummary; mode: ModeKey; note?: string }) {
  const k: RndModeKpi = s.modes[mode];
  const sufficient = k.n >= s.min_sample;
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1 border-t border-edge/40 py-2 first:border-t-0">
      <span className="w-20 shrink-0 text-[11px] font-semibold uppercase tracking-wide">
        {mode}
      </span>
      <span className="w-40 shrink-0 text-[10px] uppercase text-muted">{windowLabel(s, mode)}</span>
      <span className={`font-mono text-2xl font-semibold ${headlineClass(k.p5_in_window, sufficient)}`}>
        {pctLabel(k.p5_in_window)}
      </span>
      {!sufficient && (
        <span className="tag bg-panel2 text-[9px] text-muted">
          accumulating (n={k.n}/{s.min_sample})
        </span>
      )}
      <span className="text-[11px] text-muted">
        {/* Never blend the evidence classes: exact = first crossing; approx =
            time-of-PEAK, an upper bound (review C3). */}
        {k.median_min_to_5_exact != null ? (
          <>
            median to 5%{" "}
            <span className="font-mono text-white/80">{fmt(k.median_min_to_5_exact, 1)}m</span>
            <span className="text-[9px]"> (exact, n={k.n_exact_times})</span>
          </>
        ) : (
          <>
            median to peak{" "}
            <span className="font-mono text-white/80">
              {k.median_min_to_peak_approx == null ? "—" : `≤${fmt(k.median_min_to_peak_approx, 1)}m`}
            </span>
            <span className="text-[9px]"> (approx upper bound)</span>
          </>
        )}
      </span>
      <span className="text-[11px] text-muted">
        n <span className="font-mono text-white/80">{k.n}</span>{" "}
        <span className="text-[10px]">
          ({k.n_exact}/{k.n_approx} exact/approx)
        </span>
      </span>
      {(k.n_pending > 0 || k.n_no_window > 0) && (
        <span className="tag bg-panel2 text-[9px] text-muted">
          excluded: {k.n_pending > 0 ? `${k.n_pending} pending` : ""}
          {k.n_pending > 0 && k.n_no_window > 0 ? " · " : ""}
          {k.n_no_window > 0 ? `${k.n_no_window} no-window` : ""}
        </span>
      )}
      {k.n_approx > 0 && <ApproxChip note={note} />}
    </div>
  );
}

// ---------------------------------------------------------------------------
// c. Bucket distribution
// ---------------------------------------------------------------------------

function BucketBar({ kpi, mode, sufficient = true }:
                   { kpi: RndModeKpi; mode: ModeKey; sufficient?: boolean }) {
  if (kpi.n === 0) {
    return (
      <div className="py-1">
        <span className="w-20 text-[11px] font-semibold uppercase tracking-wide">{mode}</span>
        <span className="ml-4 text-[11px] text-muted">no clean fills yet</span>
      </div>
    );
  }
  return (
    // Under min_sample the bar renders desaturated with an accumulating tag —
    // full-color evidence on a thin sample is the exact impression the plan's
    // grey-when-accumulating rule forbids (review C10).
    <div className={`py-1 ${sufficient ? "" : "opacity-40 grayscale"}`}>
      <div className="mb-1 flex items-center gap-2">
        <span className="w-20 shrink-0 text-[11px] font-semibold uppercase tracking-wide">{mode}</span>
        {!sufficient && (
          <span className="tag bg-panel2 text-[9px] text-muted">accumulating</span>
        )}
        <div className="flex h-4 flex-1 overflow-hidden rounded bg-panel2">
          {BUCKETS.map((b) => {
            const cell = kpi.buckets[b];
            if (!cell || cell.n === 0) return null;
            const pct = cell.pct ?? 0;
            return (
              <div
                key={b}
                className={BUCKET_BAR_CLASS[b]}
                style={{ width: `${pct}%` }}
                title={`${b}: ${cell.n} (${pctLabel(pct)})`}
              />
            );
          })}
        </div>
      </div>
      <div className="ml-[5.5rem] flex flex-wrap gap-x-4 gap-y-0.5 text-[10px] text-muted">
        {BUCKETS.map((b) => {
          const cell = kpi.buckets[b];
          if (!cell) return null;
          return (
            <span key={b}>
              <span className={`mr-1 inline-block h-2 w-2 rounded-sm align-middle ${BUCKET_BAR_CLASS[b]}`} />
              {b}: <span className="font-mono text-white/70">{cell.n}</span>{" "}
              ({pctLabel(cell.pct, 0)})
            </span>
          );
        })}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// d. Reach curve (hand-rolled SVG, Spark idiom — no chart libraries)
// ---------------------------------------------------------------------------

/** Piecewise-linear position on the index scale, so the non-linear minute
 *  axis (15/30/60/120/240) stays readable. */
function minutesToIdx(min: number, ticks: number[]): number {
  const last = ticks.length - 1;
  if (last < 1) return 0;
  const first = ticks[0] as number;
  if (min <= first) return 0;
  if (min >= (ticks[last] as number)) return last;
  for (let i = 1; i <= last; i += 1) {
    const hi = ticks[i] as number;
    if (min <= hi) {
      const lo = ticks[i - 1] as number;
      return i - 1 + (min - lo) / (hi - lo);
    }
  }
  return last;
}

function ReachCurve({ kpi, mode, windowMin, sufficient = true }:
                    { kpi: RndModeKpi; mode: ModeKey; windowMin: number; sufficient?: boolean }) {
  const W = 300;
  const H = 110;
  const PAD_L = 26;
  const PAD_R = 8;
  const PAD_T = 6;
  const PAD_B = 16;
  const ticks = kpi.reach_curve.map((p) => p.minutes);
  const lastIdx = Math.max(ticks.length - 1, 1);
  const x = (min: number) =>
    PAD_L + (minutesToIdx(min, ticks) / lastIdx) * (W - PAD_L - PAD_R);
  const y = (v: number) =>
    H - PAD_B - (Math.max(0, Math.min(v, 100)) / 100) * (H - PAD_T - PAD_B);

  if (kpi.n === 0 || kpi.reach_curve.length < 2) {
    return (
      <div className="min-w-[300px] flex-1">
        <p className="text-[11px] font-semibold uppercase tracking-wide">{mode}</p>
        <p className="py-4 text-center text-[10px] text-muted">no clean fills yet</p>
      </div>
    );
  }

  const wx = x(windowMin);
  return (
    // Grey-when-accumulating (review C10) — same rule as the KPI headline.
    <div className={`min-w-[300px] flex-1 ${sufficient ? "" : "opacity-40 grayscale"}`}>
      <p className="mb-1 text-[11px] font-semibold uppercase tracking-wide">
        {mode} <span className="ml-1 font-normal normal-case text-muted">n={kpi.n}</span>
        {!sufficient && (
          <span className="tag ml-2 bg-panel2 text-[9px] normal-case text-muted">accumulating</span>
        )}
      </p>
      <svg width={W} height={H} className="block">
        {/* gridlines at 0/50/100% */}
        {[0, 50, 100].map((g) => (
          <g key={g}>
            <line x1={PAD_L} y1={y(g)} x2={W - PAD_R} y2={y(g)} stroke="#252b38" />
            <text x={PAD_L - 4} y={y(g) + 3} textAnchor="end" fontSize="8" fill="#8b98a9">
              {g}
            </text>
          </g>
        ))}
        {/* the mode's own window — the verdict line */}
        <line x1={wx} y1={PAD_T} x2={wx} y2={H - PAD_B} stroke="#3a4356" strokeDasharray="3 3" />
        <text x={wx} y={PAD_T + 6} textAnchor="middle" fontSize="8" fill="#8b98a9">
          {windowMin}m
        </text>
        {CURVE_SERIES.map((srs) => {
          const pts = kpi.reach_curve
            .filter((p) => p[srs.key] != null)
            .map((p) => `${x(p.minutes).toFixed(1)},${y(p[srs.key] as number).toFixed(1)}`);
          if (pts.length < 2) return null;
          return (
            <polyline
              key={srs.key}
              points={pts.join(" ")}
              fill="none"
              stroke={srs.color}
              strokeWidth={srs.key === "p5" ? 2 : 1.2}
            />
          );
        })}
        {ticks.map((m) => (
          <text key={m} x={x(m)} y={H - 4} textAnchor="middle" fontSize="8" fill="#8b98a9">
            {m}
          </text>
        ))}
      </svg>
    </div>
  );
}

// ---------------------------------------------------------------------------
// e. Conditioning cuts
// ---------------------------------------------------------------------------

function CutRow({ c, minSample }: { c: RndCutCell; minSample: number }) {
  const barCls = c.sufficient
    ? c.p5_in_window >= 50
      ? "bg-bull/70"
      : c.p5_in_window >= 30
        ? "bg-yellow-500/60"
        : "bg-bear/60"
    : "bg-muted/30";
  return (
    <div className="flex items-center gap-2 text-[11px]">
      <span className={`w-24 shrink-0 truncate ${c.sufficient ? "" : "text-muted"}`} title={c.key}>
        {c.key}
      </span>
      <span className="w-10 shrink-0 font-mono text-[10px] text-muted">n={c.n}</span>
      <div className="h-3 flex-1 overflow-hidden rounded bg-panel2">
        <div className={`h-full ${barCls}`} style={{ width: `${Math.min(c.p5_in_window, 100)}%` }} />
      </div>
      <span className={`w-12 shrink-0 text-right font-mono ${c.sufficient ? "" : "text-muted"}`}>
        {pctLabel(c.p5_in_window)}
      </span>
      {!c.sufficient && (
        <span className="tag bg-panel2 text-[9px] text-muted">
          n={c.n}/{minSample}
        </span>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// g. Ledger bits
// ---------------------------------------------------------------------------

function TapeChip({ tape }: { tape: string | null }) {
  if (!tape) return <span className="text-muted">—</span>;
  const cls =
    tape === "developing"
      ? "bg-bull/10 text-bull"
      : tape === "stretched"
        ? "bg-yellow-500/15 text-yellow-400"
        : "bg-panel2 text-muted";
  return <span className={`tag text-[9px] ${cls}`}>{tape}</span>;
}

function BucketChip({ bucket }: { bucket: string | null }) {
  if (!bucket) return <span className="text-muted">—</span>;
  return (
    <span className={`tag text-[9px] ${BUCKET_CHIP_CLASS[bucket] ?? "bg-panel2 text-muted"}`}>
      {bucket}
    </span>
  );
}

// ---------------------------------------------------------------------------

export function RndLab() {
  const [summary, setSummary] = useState<RndSummary | null>(null);
  const [ledger, setLedger] = useState<RndLedgerResponse | null>(null);
  const [policies, setPolicies] = useState<RndPolicies | null>(null);
  const [candidates, setCandidates] = useState<RndCandidates | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [ledgerErr, setLedgerErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [cutDim, setCutDim] = useState<CutDim>("score_band");

  const refresh = useCallback(async () => {
    setBusy(true);
    const [s, l, p, c] = await Promise.allSettled([
      api.rndSummary(), api.rndLedger(200), api.rndPolicies(), api.rndCandidates(),
    ]);
    if (s.status === "fulfilled") { setSummary(s.value); setErr(null); }
    else setErr(s.reason instanceof Error ? s.reason.message : String(s.reason));
    if (l.status === "fulfilled") { setLedger(l.value); setLedgerErr(null); }
    else setLedgerErr(l.reason instanceof Error ? l.reason.message : String(l.reason));
    if (p.status === "fulfilled") setPolicies(p.value);
    if (c.status === "fulfilled") setCandidates(c.value);
    setBusy(false);
  }, []);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 60_000);
    return () => clearInterval(t);
  }, [refresh]);

  const approxNote = summary?.notes[0];
  const totalN = summary
    ? MODES.reduce((acc, m) => acc + summary.modes[m].n, 0)
    : 0;
  const heat = summary?.heat_before_capture;
  const heatRows: [string, Record<string, number>][] = [];
  if (heat?.exact) heatRows.push(["exact", heat.exact]);
  if (heat?.approx) heatRows.push(["approx", heat.approx]);
  const rows = ledger?.rows ?? [];
  const shown = rows.slice(0, LEDGER_DISPLAY_CAP);

  return (
    <div className="mx-auto flex min-h-screen max-w-6xl flex-col gap-3 p-3">
      <header className="flex flex-wrap items-center gap-3">
        <ModuleSwitcher />
        <h1 className="text-sm font-semibold uppercase tracking-wide">
          R&amp;D — Signal Window Analytics
        </h1>
        <span className="tag bg-panel2 text-[9px] text-muted">
          read-only research — suggestions come only from cleared policy ledgers
        </span>
        <button
          onClick={refresh}
          disabled={busy}
          className="ml-auto rounded bg-panel2 px-2 py-0.5 text-[10px] text-muted hover:text-white disabled:opacity-50"
        >
          {busy ? "Reading…" : "Refresh"}
        </button>
      </header>

      {err && !summary && (
        <div className="card px-4 py-3 text-xs text-muted">
          R&amp;D analytics unavailable — {errHint(err)}
        </div>
      )}

      {summary && (
        <>
          {err && (
            <div className="card px-4 py-2 text-[11px] text-muted">
              Last refresh failed ({err}) — showing the previous successful read.
            </div>
          )}

          {/* b. KPI strip */}
          <section className="card p-3">
            <h2 className="mb-1 text-xs font-semibold uppercase tracking-wide text-muted">
              Does the signal deliver 5% inside its window?
            </h2>
            {totalN === 0 ? (
              <p className="text-[11px] text-muted">no clean fills yet</p>
            ) : (
              MODES.map((m) => <KpiRow key={m} s={summary} mode={m} note={approxNote} />)
            )}
          </section>

          {/* c. Bucket distribution */}
          <section className="card p-3">
            <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted">
              Bucket distribution — highest level touched inside the window
            </h2>
            {MODES.map((m) => (
              <BucketBar key={m} kpi={summary.modes[m]} mode={m}
                         sufficient={summary.modes[m].n >= summary.min_sample} />
            ))}
          </section>

          {/* d. Reach curve */}
          <section className="card p-3">
            <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted">
              Reach curve — % of fills touching each level by minute (uncapped)
            </h2>
            <div className="flex flex-wrap gap-4">
              {MODES.map((m) => (
                <ReachCurve
                  key={m}
                  kpi={summary.modes[m]}
                  mode={m}
                  windowMin={summary.windows_min[m]}
                  sufficient={summary.modes[m].n >= summary.min_sample}
                />
              ))}
            </div>
            <div className="mt-1 flex flex-wrap gap-3 text-[10px] text-muted">
              {CURVE_SERIES.map((srs) => (
                <span key={srs.key}>
                  <span
                    className="mr-1 inline-block h-0.5 w-4 align-middle"
                    style={{ background: srs.color }}
                  />
                  {srs.label}
                </span>
              ))}
              <span>┊ dashed = the mode&apos;s own window</span>
            </div>
          </section>

          {/* e. Conditioning cuts */}
          <section className="card p-3">
            <div className="mb-2 flex flex-wrap items-center gap-2">
              <h2 className="text-xs font-semibold uppercase tracking-wide text-muted">
                Conditioning cuts — 5%-in-window by
              </h2>
              <div className="inline-flex flex-wrap gap-1">
                {CUT_DIMS.map((d) => (
                  <button
                    key={d.key}
                    onClick={() => setCutDim(d.key)}
                    className={`rounded px-2 py-0.5 text-[10px] font-medium transition ${
                      d.key === cutDim ? "bg-accent text-white" : "bg-panel2 text-muted hover:text-white"
                    }`}
                  >
                    {d.label}
                  </button>
                ))}
              </div>
            </div>
            {summary.cuts[cutDim].length === 0 ? (
              <p className="text-[11px] text-muted">no data for this cut yet</p>
            ) : (
              <div className="space-y-1">
                {summary.cuts[cutDim].map((c) => (
                  <CutRow key={c.key} c={c} minSample={summary.min_sample} />
                ))}
              </div>
            )}
            <p className="mt-2 text-[10px] text-muted">
              Cuts are one dimension at a time by design — the sample cannot support
              interactions yet. All modes pooled; each trade judged against its own
              mode&apos;s window.
            </p>
          </section>

          {/* f. Heat before capture */}
          <section className="card p-3">
            <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted">
              Heat before capture — deepest adverse touch before the +5%
            </h2>
            {heatRows.length === 0 ? (
              <p className="text-[11px] text-muted">no +5%-in-window reachers yet</p>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full max-w-md text-left text-[11px]">
                  <thead>
                    <tr className="text-[10px] uppercase text-muted">
                      <th className="py-1 pr-2">evidence</th>
                      {HEAT_LABELS.map((h) => (
                        <th key={h} className="py-1 pr-2 font-mono normal-case">{h}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {heatRows.map(([cls, counts]) => (
                      <tr key={cls} className="border-t border-edge/40">
                        <td className="py-1 pr-2 text-muted">{cls}</td>
                        {HEAT_LABELS.map((h) => (
                          <td key={h} className="py-1 pr-2 font-mono">
                            {counts[h] ?? 0}
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            <p className="mt-2 text-[10px] text-muted">
              For the fills that DID reach +5% inside their window: the deepest
              negative level first-touched on the way there — the drawdown
              harvesting the 5% requires you to sit through.
            </p>
          </section>

        </>
      )}

      {/* f2. Policy ledgers (R3) */}
      <section className="card p-3">
        <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted">
          Policy ledgers — pre-registered exits, graded live against baseline
        </h2>
        {!policies ? (
          <p className="text-[11px] text-muted">loading…</p>
        ) : !policies.enabled ? (
          <p className="text-[11px] text-muted">
            RND_POLICY_LEDGERS is off (or the paper book is stopped) — twins are
            not being booked.
          </p>
        ) : Object.keys(policies.policies).length === 0 ? (
          <p className="text-[11px] text-muted">no policy pairs yet — twins book with the next clean paper fill.</p>
        ) : (
          Object.entries(policies.policies).map(([tag, p]) => (
            <div key={tag} className="border-t border-edge/40 py-2 first:border-t-0">
              <p className="text-[11px]">
                <span className="font-mono font-semibold uppercase">{tag}</span>{" "}
                <span className="text-muted">{p.desc}</span>
              </p>
              <div className="mt-1 flex flex-wrap gap-x-6 gap-y-1">
                {Object.keys(p.modes).length === 0 && (
                  <span className="text-[10px] text-muted">no closed pairs yet</span>
                )}
                {Object.entries(p.modes).map(([mode, m]) => (
                  <span key={mode} className="text-[11px]">
                    <span className="uppercase text-muted">{mode}</span>{" "}
                    {m.verdict ? (
                      <span className={`tag ${
                        m.verdict.startsWith("BEATS")
                          ? "bg-bull/15 text-bull" : "bg-bear/15 text-bear"
                      }`}>
                        {m.verdict}
                      </span>
                    ) : (
                      <span className="tag bg-panel2 text-muted">
                        accumulating ({m.diverged}/{policies.min_diverged} diverged
                        {m.avg_delta != null ? ` · Δ₹${fmt(m.avg_delta, 0)}/pair so far` : ""})
                      </span>
                    )}
                  </span>
                ))}
              </div>
            </div>
          ))
        )}
        <p className="mt-2 text-[10px] text-muted">
          Each policy runs as a full paper twin of every clean fill (stops still
          apply); a verdict exists only at {policies?.min_diverged ?? 30}+ diverged
          pairs, and acting on one is a human decision, never the code's.
        </p>
      </section>

      {/* f3. Scanner candidates (R3) */}
      <section className="card p-3">
        <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted">
          Insight scanner — candidates, not suggestions
        </h2>
        {!candidates ? (
          <p className="text-[11px] text-muted">loading…</p>
        ) : candidates.candidates.length === 0 ? (
          <p className="text-[11px] text-muted">
            No cell clears the frozen bars (n≥{candidates.frozen.min_n},
            gap≥{candidates.frozen.gap_pp}pp, DEV/TEST sign agreement across{" "}
            {candidates.frozen.dev_test_split_ist} IST) — an empty list is a
            valid, and common, result.
          </p>
        ) : (
          <div className="space-y-1">
            {candidates.candidates.map((c) => (
              <div key={`${c.dim}:${c.key}`} className="flex flex-wrap items-center gap-2 text-[11px]">
                <span className={`tag ${
                  c.direction === "favorable" ? "bg-bull/15 text-bull" : "bg-bear/15 text-bear"
                }`}>
                  {c.direction}
                </span>
                <span className="font-mono">{c.dim} = {c.key}</span>
                <span className="text-muted">
                  n={c.n} · gap {signed(c.gap_pp, 2)}pp (DEV {signed(c.dev_gap_pp, 2)} /
                  TEST {signed(c.test_gap_pp, 2)})
                </span>
                <span className="tag bg-yellow-500/15 text-[9px] text-yellow-400">
                  candidate — needs its own pre-registered ledger
                </span>
              </div>
            ))}
          </div>
        )}
        {candidates && (
          // Sweep breadth rendered ALWAYS, not only when empty: a candidate
          // shown without its comparison count reads stronger than it is
          // (review C10 — multiple comparisons, uncorrected by design).
          <p className="mt-2 text-[10px] text-muted">
            {candidates.note} ({candidates.cells_checked} cells checked, uncorrected)
          </p>
        )}
      </section>

      {/* g. Ledger */}
      <section className="card p-3">
        <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted">
          Ledger — every clean fill, judged against its own window
        </h2>
        {ledgerErr && !ledger && (
          <p className="text-[11px] text-muted">Ledger unavailable — {errHint(ledgerErr)}</p>
        )}
        {ledger && rows.length === 0 && (
          <p className="text-[11px] text-muted">no clean fills yet</p>
        )}
        {rows.length > 0 && (
          <>
            {ledgerErr && (
              <p className="mb-1 text-[10px] text-yellow-400">
                Last refresh failed — showing the previous successful read.
              </p>
            )}
            <div className="overflow-x-auto scroll-thin">
              <table className="w-full text-left text-[11px]">
                <thead>
                  <tr className="text-[10px] uppercase text-muted">
                    <th className="py-1 pr-2">time</th>
                    <th className="py-1 pr-2">mode</th>
                    <th className="py-1 pr-2">contract</th>
                    <th className="py-1 pr-2">score</th>
                    <th className="py-1 pr-2">tape</th>
                    <th className="py-1 pr-2">bucket</th>
                    <th className="py-1 pr-2">5% in win</th>
                    <th className="py-1 pr-2">min to 5</th>
                    <th className="py-1 pr-2">mfe/mae</th>
                    <th className="py-1 pr-2">exit</th>
                    <th className="py-1 pr-2">net P&amp;L</th>
                    <th className="py-1" />
                  </tr>
                </thead>
                <tbody>
                  {shown.map((r, i) => (
                    <tr key={r.id ?? `${r.entered_at}-${i}`} className="border-t border-edge/40">
                      <td className="whitespace-nowrap py-1 pr-2 font-mono text-[10px]">
                        {istDateTime(r.entered_at)}
                      </td>
                      <td className="py-1 pr-2 text-muted">{r.mode ?? "—"}</td>
                      <td className="whitespace-nowrap py-1 pr-2 font-mono text-[10px]">
                        {r.contract ?? "—"}
                        {r.golden && <span title="GOLDEN at fill"> 🌟</span>}
                      </td>
                      <td className="py-1 pr-2 font-mono">{r.score ?? "—"}</td>
                      <td className="py-1 pr-2"><TapeChip tape={r.tape} /></td>
                      <td className="py-1 pr-2"><BucketChip bucket={r.bucket} /></td>
                      <td className="py-1 pr-2">
                        {r.p5_in_window == null
                          ? <span className="text-muted" title={r.not_gradeable_reason ?? undefined}>—</span>
                          : r.p5_in_window
                            ? <span className="text-bull">✓</span>
                            : <span className="text-bear">✗</span>}
                      </td>
                      <td className="py-1 pr-2 font-mono">
                        {r.min_to_5 == null ? "—" : `${fmt(r.min_to_5, 1)}m`}
                      </td>
                      <td className="whitespace-nowrap py-1 pr-2 font-mono text-[10px]">
                        {/* signed() + sign-aware tone: a negative MFE must not
                            render "+-2.0%" in bull green (review C8). */}
                        <span className={r.mfe_pct != null && r.mfe_pct < 0 ? "text-bear" : "text-bull"}>
                          {r.mfe_pct == null ? "—" : `${signed(r.mfe_pct, 1)}%`}
                        </span>
                        {" / "}
                        <span className={r.mae_pct != null && r.mae_pct > 0 ? "text-bull" : "text-bear"}>
                          {r.mae_pct == null ? "—" : `${signed(r.mae_pct, 1)}%`}
                        </span>
                      </td>
                      <td className="max-w-[8rem] truncate py-1 pr-2 text-[10px] text-muted" title={r.exit_reason ?? undefined}>
                        {r.exit_reason ?? "—"}
                      </td>
                      <td className={`py-1 pr-2 font-mono ${
                        r.realized_pnl == null ? "text-muted" : r.realized_pnl >= 0 ? "text-bull" : "text-bear"
                      }`}>
                        {r.realized_pnl == null ? "—" : fmt(r.realized_pnl, 0)}
                      </td>
                      <td className="py-1">
                        <span className={`tag text-[8px] ${
                          r.exact ? "bg-panel2 text-muted" : "bg-yellow-500/15 text-yellow-400"
                        }`}>
                          {r.exact ? "exact" : "approx"}
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {ledger && ledger.total > shown.length && (
              <p className="mt-1 text-[10px] text-muted">
                showing {shown.length} of {ledger.total} clean fills
              </p>
            )}
          </>
        )}
      </section>

      {/* h. Notes */}
      {summary && summary.notes.length > 0 && (
        <section className="card p-3">
          <ul className="list-disc space-y-1 pl-4 text-[10px] text-muted">
            {summary.notes.map((n) => (
              <li key={n}>{n}</li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}
