"use client";

// The Opening tab: the first 45 minutes (09:15-10:00 IST) as its own screen.
// Three layers, same honesty rules as the Patterns Lab they come from:
//   LIVE   — today's forming state (gap, OR levels, direction, pace) joined
//            to the stored study's matching cells. Features freeze at 10:00.
//   STUDY  — the 3y tables: loudness, gap behaviour, the ORB coin verdict,
//            continuation and trend-day cells. Frequencies, not forecasts.
//   BOARD  — the last 10 mornings graded offline against those same tables.
// Every cell shows its n; "coin" and "= base rate" render as the verdicts
// they are, never dressed up as signals.

import { useCallback, useEffect, useState } from "react";
import {
  api,
  OpeningCell,
  OpeningGapBucketCell,
  OpeningLive,
  OpeningRateCell,
  OpeningResponse,
} from "@/lib/api";
import { ModuleSwitcher } from "./ModuleSwitcher";

const BUCKETS = ["big_down", "down", "flat", "up", "big_up"] as const;
const BUCKET_LABEL: Record<string, string> = {
  big_down: "big gap ↓", down: "gap ↓", flat: "flat", up: "gap ↑", big_up: "big gap ↑",
};
const BUCKET_CLASS: Record<string, string> = {
  big_down: "bg-bear/20 text-bear", down: "bg-bear/10 text-bear",
  flat: "bg-panel2 text-muted", up: "bg-bull/10 text-bull", big_up: "bg-bull/20 text-bull",
};

function pct(x: number | null | undefined, digits = 1): string {
  return x == null ? "—" : `${(100 * x).toFixed(digits)}%`;
}

function BucketChip({ bucket, bps }: { bucket: string | null; bps?: number | null }) {
  if (!bucket) return <span className="tag bg-panel2 text-[10px] text-muted">no gap read</span>;
  // Math.round: a -0.4bps gap must print "0bps", not "-0bps"
  const r = bps != null ? Math.round(bps) : null;
  return (
    <span className={`tag text-[10px] ${BUCKET_CLASS[bucket] ?? "bg-panel2 text-muted"}`}>
      {BUCKET_LABEL[bucket] ?? bucket}
      {r != null && <span className="ml-1 opacity-80">{r > 0 ? "+" : ""}{r}bps</span>}
    </span>
  );
}

// Only "tendency" lights up — coin, = base rate AND "sample too small" all
// render muted; a thin sample must not out-shout a coin.
function Verdict({ v }: { v?: string }) {
  if (!v) return null;
  const cls = v === "tendency" ? "bg-emerald-500/15 text-emerald-300" : "bg-panel2 text-muted";
  return <span className={`tag text-[9px] ${cls}`}>{v}</span>;
}

/** Signed-outcome cell: hit rate on the coin band. */
function Signed({ label, cell }: { label: string; cell?: OpeningCell | null }) {
  if (!cell) return null;
  return (
    <div className="flex items-center gap-2 text-[11px]">
      <span className="w-36 shrink-0 text-muted">{label}</span>
      <span className="font-mono">{pct(cell.hit_rate)}</span>
      <span className="font-mono text-muted">{cell.avg_bps > 0 ? "+" : ""}{cell.avg_bps}bps avg</span>
      <span className="text-[10px] text-muted">n={cell.n}</span>
      <Verdict v={cell.verdict} />
    </div>
  );
}

/** Rate cell: judged against its own base rate. */
function Rated({ label, cell }: { label: string; cell?: OpeningRateCell | null }) {
  if (!cell) return null;
  return (
    <div className="flex items-center gap-2 text-[11px]">
      <span className="w-36 shrink-0 text-muted">{label}</span>
      <span className="font-mono">{pct(cell.rate)}</span>
      <span className="font-mono text-muted">
        base {pct(cell.base_rate)} ({cell.edge_pp > 0 ? "+" : ""}{cell.edge_pp}pp)
      </span>
      <span className="text-[10px] text-muted">n={cell.n}</span>
      <Verdict v={cell.verdict} />
    </div>
  );
}

function errHint(err: string): string {
  if (err.includes("401")) return "Kite login required — authenticate on the Pulse dashboard first.";
  if (err.includes("predate")) return "Stored results predate the opening study — open Patterns and click Re-analyze (no re-sync needed).";
  if (err.includes("No results")) return "No analysis yet — open Patterns and click Sync + Analyze first.";
  if (err.includes("404")) return "The backend hasn't loaded the opening endpoints yet — they arrive with the next backend restart.";
  return err;
}

function LiveCard({ live, busy, onRefresh, err }: {
  live: OpeningLive | null; busy: boolean; onRefresh: () => void; err: string | null;
}) {
  const s = live?.state;
  return (
    <section className="card p-3">
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <h2 className="text-xs font-semibold uppercase tracking-wide text-muted">
          Live — today&apos;s opening vs the study
        </h2>
        {live && (
          <span className="text-[10px] text-muted">
            {live.window_complete
              ? "window frozen at 10:00"
              : live.bars_in_window >= 9
                ? "window incomplete — feed hole"
                : `bar ${live.bars_in_window}/9 closed`}
            {live.bars_today > live.bars_in_window && ` · ${live.bars_today} bars today`}
          </span>
        )}
        {err && live && (
          <span className="tag bg-yellow-500/15 text-[9px] text-yellow-400"
                title="The last refresh failed — this card shows the previous successful read">
            stale
          </span>
        )}
        <button
          onClick={onRefresh}
          disabled={busy}
          className="ml-auto rounded bg-panel2 px-2 py-0.5 text-[10px] text-muted hover:text-white disabled:opacity-50"
        >
          {busy ? "Reading…" : "Refresh"}
        </button>
      </div>

      {err && <p className="text-[11px] text-muted">{errHint(err)}</p>}
      {live && !err && !s && (
        <p className="text-[11px] text-muted">{live.status ?? "No closed bars yet today."}</p>
      )}

      {s && (
        <>
          <div className="flex flex-wrap items-center gap-2">
            <BucketChip bucket={s.gap_bucket} bps={s.gap_bps} />
            {!s.aligned_0915 && (
              <span className="tag bg-yellow-500/15 text-[10px] text-yellow-400"
                    title="First closed bar is not 09:15 — gap and window features are unavailable for this session">
                misaligned open
              </span>
            )}
            {s.f45_dir_so_far != null && s.f45_dir_so_far !== 0 && (
              <span className={`tag text-[10px] ${s.f45_dir_so_far > 0 ? "bg-bull/15 text-bull" : "bg-bear/15 text-bear"}`}>
                first-45 {s.f45_dir_so_far > 0 ? "up" : "down"}{live!.window_complete ? "" : " so far"}
              </span>
            )}
            {s.or45_tercile && (
              <span className="tag bg-indigo-500/15 text-[10px] text-indigo-300">
                OR45 {s.or45_tercile} ({s.or45_bps_so_far}bps)
              </span>
            )}
            {s.above_proxy_vwap != null && (
              <span className="tag bg-panel2 text-[10px] text-muted">
                {s.above_proxy_vwap ? "above" : "below"} proxy-VWAP
              </span>
            )}
            {s.pace_vs_typical != null && (
              <span className="tag bg-panel2 text-[10px] text-muted">
                {s.pace_vs_typical}x typical volume
              </span>
            )}
          </div>

          <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 font-mono text-[10px] text-muted">
            {s.prev_session && (
              <span title={`Prior stored session (${s.prev_session.date})`}>
                prev close <span className="text-white/80">{s.prev_session.close.toFixed(1)}</span>
                {" "}({s.prev_session.date})
              </span>
            )}
            {s.or15 && <span>OR15 <span className="text-white/80">{s.or15.low.toFixed(1)}–{s.or15.high.toFixed(1)}</span></span>}
            {s.or30 && <span>OR30 <span className="text-white/80">{s.or30.low.toFixed(1)}–{s.or30.high.toFixed(1)}</span></span>}
            <span>
              OR45{s.or45.complete ? "" : " (forming)"}{" "}
              <span className="text-white/80">{s.or45.low.toFixed(1)}–{s.or45.high.toFixed(1)}</span>
            </span>
            <span>last <span className="text-white/80">{s.last_close.toFixed(1)}</span></span>
          </div>

          {live!.matched && (live!.matched.continuation || live!.matched.trend_day
            || live!.matched.gap || live!.matched.continuation_by_or45
            || live!.matched.trend_day_by_or45) && (
            <div className="mt-2 space-y-1 rounded bg-panel2/60 p-2">
              <p className="text-[10px] uppercase tracking-wide text-muted">
                What sessions like this one did (each cell carries its own n)
              </p>
              <Signed label="morning carries to close" cell={live!.matched.continuation} />
              <Rated label="trend day" cell={live!.matched.trend_day} />
              <Signed label={`carry, OR45 ${live!.state?.or45_tercile ?? ""}`}
                      cell={live!.matched.continuation_by_or45} />
              <Rated label={`trend day, OR45 ${live!.state?.or45_tercile ?? ""}`}
                     cell={live!.matched.trend_day_by_or45} />
              {live!.matched.gap && (
                <div className="flex items-center gap-2 text-[11px]">
                  <span className="w-36 shrink-0 text-muted">gap fills by close</span>
                  <span className="font-mono">{pct(live!.matched.gap.fill_by_close.rate)}</span>
                  <span className="font-mono text-muted">base {pct(live!.matched.gap.fill_by_close.base_rate)}</span>
                  <span className="text-[10px] text-muted">n={live!.matched.gap.n}</span>
                  <Verdict v={live!.matched.gap.fill_by_close.verdict} />
                </div>
              )}
            </div>
          )}
          <p className="mt-2 text-[10px] text-muted">{live!.note}</p>
        </>
      )}
    </section>
  );
}

export function OpeningLab() {
  const [data, setData] = useState<OpeningResponse | null>(null);
  const [live, setLive] = useState<OpeningLive | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [liveErr, setLiveErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    setBusy(true);
    const [study, lv] = await Promise.allSettled([api.patternsOpening(), api.patternsOpeningLive()]);
    if (study.status === "fulfilled") { setData(study.value); setErr(null); }
    else setErr(study.reason instanceof Error ? study.reason.message : String(study.reason));
    if (lv.status === "fulfilled") { setLive(lv.value); setLiveErr(null); }
    else setLiveErr(lv.reason instanceof Error ? lv.reason.message : String(lv.reason));
    setBusy(false);
  }, []);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 120_000); // 5-min bars need no hot poll
    return () => clearInterval(t);
  }, [refresh]);

  const study = data?.study;
  const vol = study?.volatility;
  const board = data?.recent_mornings;

  return (
    <div className="mx-auto flex min-h-screen max-w-6xl flex-col gap-3 p-3">
      <header className="flex flex-wrap items-center gap-3">
        <ModuleSwitcher />
        <h1 className="text-sm font-semibold">Opening · NIFTY 50 · 09:15–10:00</h1>
        {study?.sessions && (
          <span className="text-[11px] text-muted">
            {study.sessions.n} sessions · {study.sessions.from} → {study.sessions.to}
          </span>
        )}
      </header>

      <LiveCard live={live} busy={busy} onRefresh={refresh} err={liveErr} />

      {err && <div className="card px-4 py-2 text-xs text-muted">{errHint(err)}</div>}

      {study && (
        <>
          {vol && (
            <section className="card p-3">
              <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted">
                How loud is the open
              </h2>
              <div className="flex flex-wrap gap-x-6 gap-y-1 text-[11px]">
                {vol.first45_vs_rest_bar_range.median_ratio != null ? (
                  <span>
                    <span className="font-mono text-white/90">{vol.first45_vs_rest_bar_range.median_ratio}x</span>{" "}
                    <span className="text-muted">bar range vs rest of day (louder on {pct(vol.first45_vs_rest_bar_range.sessions_louder_than_rest)} of sessions)</span>
                  </span>
                ) : (
                  <span className="text-muted">{vol.first45_vs_rest_bar_range.note ?? "range ratio unavailable"}</span>
                )}
                <span>
                  <span className="font-mono text-white/90">{pct(vol.or45_share_of_day_range.median, 0)}</span>{" "}
                  <span className="text-muted">of the day&apos;s range already printed by 10:00</span>
                </span>
                <span>
                  <span className="font-mono text-white/90">{pct(vol.first45_vol_share_median, 0)}</span>{" "}
                  <span className="text-muted">of the day&apos;s volume (proxy)</span>
                </span>
              </div>
            </section>
          )}

          {study.gap && (
            <section className="card p-3">
              <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted">
                Gap behaviour (flat = ±{study.gap.buckets_bps.flat}bps · big = ±{study.gap.buckets_bps.big}bps)
              </h2>
              <div className="overflow-x-auto">
                <table className="w-full text-left text-[11px]">
                  <thead>
                    <tr className="text-[10px] uppercase text-muted">
                      <th className="py-1 pr-2">bucket</th>
                      <th className="py-1 pr-2">n</th>
                      <th className="py-1 pr-2">fades by close</th>
                      <th className="py-1 pr-2">fills by close</th>
                      <th className="py-1 pr-2">by 10:00</th>
                      <th className="py-1 pr-2">by 11:00</th>
                      <th className="py-1" />
                    </tr>
                  </thead>
                  <tbody>
                    {BUCKETS.map((b) => {
                      const cell: OpeningGapBucketCell | undefined = study.gap!.by_bucket[b];
                      if (!cell) return null;
                      return (
                        <tr key={b} className="border-t border-edge/40">
                          <td className="py-1 pr-2"><BucketChip bucket={b} /></td>
                          <td className="py-1 pr-2 font-mono">{cell.n}</td>
                          <td className="py-1 pr-2 font-mono">{pct(cell.fade_rate)}</td>
                          <td className="py-1 pr-2 font-mono">
                            {pct(cell.fill_by_close.rate)}
                            <span className="ml-1 text-muted">vs {pct(cell.fill_by_close.base_rate)} base</span>
                          </td>
                          <td className="py-1 pr-2 font-mono">{pct(cell.fill_by_10)}</td>
                          <td className="py-1 pr-2 font-mono">{pct(cell.fill_by_11)}</td>
                          <td className="py-1"><Verdict v={cell.fill_by_close.verdict} /></td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </section>
          )}

          {study.or30_breakout && (
            <section className="card p-3">
              <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted">
                Opening-range breakout
              </h2>
              <p className="mb-2 text-[11px] text-muted">
                {pct(study.or30_breakout.break_rate)} of sessions close outside OR30 (median{" "}
                {study.or30_breakout.median_break_minutes_after_open ?? "—"} min after open,{" "}
                {pct(study.or30_breakout.break_up_share)} upward).{" "}
                {/* The verdict is the backend's to make — static copy must not
                    contradict a future re-analysis (review catch). */}
                {[study.or30_breakout.fwd_30m, study.or30_breakout.fwd_60m]
                  .every((c) => !c || c.verdict === "coin")
                  ? "The forward returns are a coin — the textbook ORB does not work on NIFTY 5m."
                  : "Forward returns below — the verdict chips speak."}
              </p>
              <div className="space-y-1">
                <Signed label="30m after first break" cell={study.or30_breakout.fwd_30m} />
                <Signed label="60m after first break" cell={study.or30_breakout.fwd_60m} />
              </div>
            </section>
          )}

          {study.continuation_10_to_close && (
            <section className="card p-3">
              <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted">
                Does the morning carry to the close?
              </h2>
              {study.continuation_10_to_close.note && (
                <p className="mb-1 text-[10px] text-muted">{study.continuation_10_to_close.note}</p>
              )}
              <div className="space-y-1">
                <Signed label="all sessions" cell={study.continuation_10_to_close.all} />
                {BUCKETS.map((b) => (
                  <Signed key={b} label={BUCKET_LABEL[b]} cell={study.continuation_10_to_close!.by_gap?.[b]} />
                ))}
                <Signed label="proxy-VWAP confirms" cell={study.continuation_10_to_close.vwap_confirms} />
                <Signed label="proxy-VWAP diverges" cell={study.continuation_10_to_close.vwap_diverges} />
              </div>
            </section>
          )}

          {study.trend_day && (
            <section className="card p-3">
              <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted">
                Trend-day odds (base {pct(study.trend_day.base.rate)}, n={study.trend_day.base.n})
              </h2>
              <div className="space-y-1">
                {BUCKETS.map((b) => (
                  <Rated key={b} label={BUCKET_LABEL[b]} cell={study.trend_day!.by_gap?.[b]} />
                ))}
              </div>
            </section>
          )}
        </>
      )}

      {board && board.mornings.length > 0 && (
        <section className="card p-3">
          <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted">
            Scoreboard — the last {board.mornings.length} mornings, graded
          </h2>
          {board.headline.length > 0 && (
            <div className="mb-2 flex flex-wrap gap-2">
              {board.headline.map((h) => (
                <span key={h.tendency} className="tag bg-panel2 text-[10px] text-muted">
                  {h.tendency}: <span className="ml-1 font-mono text-white/90">{h.hits}/{h.n}</span>
                </span>
              ))}
            </div>
          )}
          <div className="overflow-x-auto">
            <table className="w-full text-left text-[11px]">
              <thead>
                <tr className="text-[10px] uppercase text-muted">
                  <th className="py-1 pr-2">date</th>
                  <th className="py-1 pr-2">gap</th>
                  <th className="py-1 pr-2">carried</th>
                  <th className="py-1 pr-2">filled</th>
                  <th className="py-1 pr-2">faded</th>
                  <th className="py-1 pr-2">trend day</th>
                </tr>
              </thead>
              <tbody>
                {board.mornings.slice().reverse().map((m) => (
                  <tr key={m.date} className="border-t border-edge/40">
                    <td className="py-1 pr-2 font-mono">{m.date}</td>
                    <td className="py-1 pr-2"><BucketChip bucket={m.gap_bucket} bps={m.gap_bps} /></td>
                    <td className="py-1 pr-2">{yn(m.outcomes.continued_to_close)}</td>
                    <td className="py-1 pr-2">{yn(m.outcomes.filled_by_close)}</td>
                    <td className="py-1 pr-2">{yn(m.outcomes.gap_faded)}</td>
                    <td className="py-1 pr-2">{yn(m.outcomes.trend_day)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="mt-2 text-[10px] text-muted">{board.note}</p>
        </section>
      )}

      {study?.note && <p className="pb-2 text-[10px] text-muted">{study.note}</p>}
    </div>
  );
}

function yn(v: boolean | null | undefined) {
  if (v == null) return <span className="text-muted">—</span>;
  return v
    ? <span className="text-bull">yes</span>
    : <span className="text-bear">no</span>;
}
