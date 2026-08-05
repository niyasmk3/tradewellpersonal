"use client";

// Patterns Lab — full-page view over the standalone backend Patterns Module
// (3y NIFTY 5-min day-of-week / time-of-day / candle-frequency / S&R levels).
// Read-mostly: results come from backend/.patterns_results.json; the two
// buttons re-fetch Kite data and re-run the analysis.

import { ModuleSwitcher } from "./ModuleSwitcher";
import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from "react";
import dynamic from "next/dynamic";
import {
  CandleFrequency,
  ConditionalCell,
  ConditionalOutcome,
  DayOfWeekStats,
  LevelAlertsResponse,
  LevelRow,
  PatternsLiveRead,
  PatternsResults,
  TimeOfDaySlot,
  api,
} from "@/lib/api";
import { chime } from "@/lib/alerts";
import { patternInfo } from "@/lib/patternInfo";

// lightweight-charts touches the DOM — client-side only, same as PriceChart.
const PatternsTapeChart = dynamic(
  () => import("./PatternsTapeChart").then((m) => m.PatternsTapeChart),
  { ssr: false, loading: () => <div className="h-[36rem] w-full animate-pulse bg-panel2" /> },
);
import { usePolling } from "@/lib/usePolling";

const WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"];

const bullBg = (a: number) => `rgba(22,199,132,${a.toFixed(3)})`;
const bearBg = (a: number) => `rgba(234,57,67,${a.toFixed(3)})`;
const blueBg = (a: number) => `rgba(59,130,246,${a.toFixed(3)})`;

function signCls(v: number, zero = 0) {
  return v > zero ? "text-bull" : v < zero ? "text-bear" : "text-muted";
}

// ------------------------------------------------------------- level watch

function LevelWatchCard() {
  const [data, setData] = useState<LevelAlertsResponse | null>(null);
  const [err, setErr] = useState<string | null>(null);
  // Chime once per new callout while this page is open; first payload only
  // primes the seen-set so a reload doesn't replay the day.
  const seenTs = useRef<number | null>(null);

  useEffect(() => {
    let live = true;
    const load = async () => {
      try {
        const d = await api.levelAlerts();
        if (!live) return;
        setData(d);
        setErr(null);
        const rows = d.alerts ?? [];
        const maxTs = rows.reduce((m, a) => Math.max(m, a.ts), 0);
        if (seenTs.current === null) seenTs.current = maxTs;
        else if (maxTs > seenTs.current) {
          const fresh = rows.filter((a) => a.ts > (seenTs.current as number));
          seenTs.current = maxTs;
          if (fresh.length) chime(fresh.some((a) => a.side === "sell") ? "urgent" : "good");
        }
      } catch (e) {
        if (live) setErr(e instanceof Error ? e.message : String(e));
      }
    };
    load();
    const t = setInterval(load, 10_000);
    return () => {
      live = false;
      clearInterval(t);
    };
  }, []);

  return (
    <section className="card p-3">
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <h2 className="text-xs font-semibold uppercase tracking-wide text-muted">
          Level watch — live callouts on the strongest S/R
        </h2>
        {data?.spot != null && (
          <span className="font-mono text-[10px] text-muted">spot {data.spot.toLocaleString()}</span>
        )}
        {data && !data.enabled && (
          <span className="tag bg-yellow-500/15 text-[9px] text-yellow-400">disabled</span>
        )}
      </div>

      {/* The scoreboard: is following the callouts making money? Win = the
          quoted option moved the called direction within 30 minutes. */}
      {data?.grades && Object.keys(data.grades).length > 0 && (
        <div className="mb-2 flex flex-wrap gap-1.5 text-[10px]">
          {(["buy", "sell"] as const).map((side) => {
            const g = data.grades?.[side];
            if (!g) return null;
            return (
              <span
                key={side}
                className={`tag ${g.win_rate >= 55 ? "bg-bull/15 text-bull" : g.win_rate <= 45 ? "bg-bear/15 text-bear" : "bg-panel2 text-muted"}`}
                title={`${g.n} graded callout(s), 14-day window. Win = ${side === "buy" ? "premium UP" : "premium DOWN (booking avoided a give-back)"} 30 minutes after the callout. Held = spot never travelled 15+ pts through the level within the hour.`}
              >
                {side === "buy" ? "BUY callouts" : "CEILING callouts"}: {g.win_rate}% right ·{" "}
                level held {g.held_rate}%
                {g.avg_prem_move_30m_pct != null &&
                  ` · prem ${g.avg_prem_move_30m_pct > 0 ? "+" : ""}${g.avg_prem_move_30m_pct}% @30m`}{" "}
                (n={g.n})
              </span>
            );
          })}
        </div>
      )}

      {err && (
        <p className="text-[11px] text-muted">
          {err.includes("404")
            ? "The backend hasn't loaded the level watch yet — it arrives with the next backend restart."
            : err}
        </p>
      )}

      {data && !err && (
        <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
          <div>
            <p className="mb-1 text-[10px] uppercase text-muted">
              on watch ({data.watched.length}) — ≥5 touch-days & ≥60% hold only
            </p>
            {data.watched.length === 0 ? (
              <p className="text-[11px] text-muted">
                No strong level within 1.5% of spot right now.
              </p>
            ) : (
              <div className="space-y-0.5">
                {data.watched.map((l) => (
                  <div key={l.level} className="flex items-center gap-2 font-mono text-[11px]">
                    <span className={data.spot && l.level > data.spot ? "text-bear" : "text-bull"}>
                      {l.level.toFixed(0)}
                    </span>
                    <span className="text-muted">
                      held {Math.round(l.hold_rate * 100)}% of {l.days_touched}d ·{" "}
                      {l.total_touches} touches
                    </span>
                    {data.spot != null && (
                      <span className="ml-auto text-[10px] text-muted">
                        {(l.level - data.spot > 0 ? "+" : "") + (l.level - data.spot).toFixed(0)} pts
                      </span>
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>
          <div>
            <p className="mb-1 text-[10px] uppercase text-muted">today&apos;s callouts</p>
            {(data.alerts ?? []).length === 0 ? (
              <p className="text-[11px] text-muted">None yet — a callout fires when a bar
                touches a watched level (phone push + this list).</p>
            ) : (
              <div className="space-y-1">
                {[...data.alerts].reverse().map((a, i) => (
                  <div key={`${a.ts}-${a.level}-${i}`} className="rounded border border-edge/60 bg-panel2 px-2 py-1">
                    <div className="flex items-center gap-2 text-[11px]">
                      <span className={`tag text-[9px] ${
                        a.side === "buy" ? "bg-bull/15 text-bull" : "bg-yellow-500/15 text-yellow-400"
                      }`}>
                        {a.side === "buy" ? "BUY setup" : "CEILING"}
                      </span>
                      {a.code && <span className="tag bg-panel text-[9px] font-mono text-white/80">{a.code}</span>}
                      <span className="font-mono">{a.level.toFixed(0)}</span>
                      <span className="text-muted">
                        {Math.round((a.hold_rate ?? 0) * 100)}% of {a.days_touched}d
                      </span>
                      <span className="ml-auto font-mono text-[10px] text-muted">
                        {new Date(a.ts * 1000).toLocaleTimeString("en-IN", {
                          hour: "2-digit", minute: "2-digit", timeZone: "Asia/Kolkata",
                        })}
                      </span>
                    </div>
                    <div className="mt-0.5 font-mono text-[10px] text-muted">
                      NIFTY {a.strike} CE{a.ce_ltp ? ` @ ₹${a.ce_ltp}` : ""}
                      {a.expiry ? ` · exp ${a.expiry}` : ""} · spot was {a.spot.toLocaleString()}
                    </div>
                    {a.outcomes && (a.outcomes.win != null || a.outcomes.broke) && (
                      <div className="mt-0.5 flex flex-wrap gap-1 text-[9px]">
                        {a.outcomes.win != null && (
                          <span className={`tag ${a.outcomes.win ? "bg-bull/15 text-bull" : "bg-bear/15 text-bear"}`}>
                            {a.outcomes.win ? "✓ right" : "✗ wrong"} @30m
                          </span>
                        )}
                        {a.outcomes["30m"]?.prem != null && a.ce_ltp != null && (
                          <span className="tag bg-panel2 text-muted">
                            prem ₹{a.ce_ltp} → ₹{a.outcomes["30m"]!.prem}
                          </span>
                        )}
                        <span className={`tag ${a.outcomes.broke ? "bg-bear/15 text-bear" : "bg-panel2 text-muted"}`}>
                          {a.outcomes.broke ? "level broke" : "level held"}
                        </span>
                      </div>
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      )}
      <p className="mt-2 text-[10px] leading-relaxed text-muted">
        Context with its own odds attached — a level&apos;s hold rate is a historical
        frequency, not a promise. Callouts are never scored cards; the engine&apos;s
        gate is separate.
      </p>
    </section>
  );
}

// ---------------------------------------------------------------- live read

function CellStat({ c }: { c: ConditionalCell | null | undefined }) {
  if (!c) return <span className="text-muted">—</span>;
  const pct = Math.round(c.hit_rate * 1000) / 10;
  // Color ONLY what the backend's verdict calls a tendency (n≥30 outside the
  // coin band). Recomputing the band here from hit_rate alone painted n=25
  // flukes bright red/green — the exact overconfidence the layer exists to
  // prevent (review catch: the honesty gate must survive rendering).
  const tendency = c.verdict === "tendency";
  return (
    <span className="font-mono">
      <span
        className={!tendency ? "text-muted" : c.hit_rate > 0.55 ? "text-bull" : "text-bear"}
        title={c.verdict === "sample too small" ? `n=${c.n} — too few to call a tendency` : undefined}
      >
        {pct}%
      </span>
      <span className="text-muted"> · {c.avg_bps > 0 ? "+" : ""}{c.avg_bps}bp · n={c.n}</span>
    </span>
  );
}

function LiveReadCard() {
  const [read, setRead] = useState<PatternsLiveRead | null>(null);
  // Level ladder + today's callouts, fetched on the same cadence purely for
  // the tape chart's overlay/markers (the LevelWatchCard keeps its own
  // faster poll for the list + chime).
  const [lw, setLw] = useState<LevelAlertsResponse | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    setBusy(true);
    setErr(null);
    const [r, l] = await Promise.allSettled([api.patternsLiveRead(), api.levelAlerts()]);
    if (r.status === "fulfilled") setRead(r.value);
    else setErr(r.reason instanceof Error ? r.reason.message : String(r.reason));
    if (l.status === "fulfilled") setLw(l.value);
    setBusy(false);
  }, []);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 120_000); // a 5-min tape needs no hot poll
    return () => clearInterval(t);
  }, [refresh]);

  const vol = read?.volume;
  return (
    <section className="card p-3">
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <h2 className="text-xs font-semibold uppercase tracking-wide text-muted">
          Live read — today&apos;s tape vs 3y conditional frequencies
        </h2>
        {read?.last_bar && (
          <span className="text-[10px] text-muted">
            last closed bar {read.last_bar} · {read.bars_today} bars today
          </span>
        )}
        <button
          onClick={refresh}
          disabled={busy}
          className="ml-auto rounded bg-panel2 px-2 py-0.5 text-[10px] text-muted hover:text-white disabled:opacity-50"
        >
          {busy ? "Reading…" : "Refresh"}
        </button>
      </div>

      {err && (
        <p className="text-[11px] text-muted">
          {err.includes("409")
            ? "Stored results predate this layer — click Re-analyze (no re-sync needed)."
            : err.includes("No results")
              ? "No analysis yet — click Sync + Analyze first."
              : err.includes("404")
                ? "The backend hasn't loaded the live-read endpoint yet — it arrives with the next backend restart."
                : err.includes("401")
                  ? "Kite login required — authenticate on the dashboard first."
                  : err}
        </p>
      )}

      {read && !err && (
        <>
          {(read.candles?.length ?? 0) > 0 && (
            <div className="mb-2">
              <PatternsTapeChart
                read={read}
                levels={lw?.watched ?? []}
                alerts={lw?.alerts ?? []}
              />
              <p className="mt-1 text-[10px] text-muted">
                Today&apos;s 5m tape (index). Green/red arrows = candlestick patterns on
                their own bar; cyan/yellow arrows = level callouts (BUY setup / BOOK at
                ceiling) pinned to the bar they fired on; dotted lines = the strong S/R
                ladder (purple = 10+ touch-days).
              </p>
            </div>
          )}
          {vol && (
            <div className="mb-2 flex flex-wrap gap-x-4 gap-y-1 text-[11px]">
              <span>
                <span className="text-muted">volume now </span>
                <span className={`font-mono ${
                  vol.current_regime === "high" ? "text-bull"
                    : vol.current_regime === "low" ? "text-bear" : ""
                }`}>{vol.current_regime ?? "n/a"}</span>
              </span>
              {vol.pace_vs_typical != null && (
                <span>
                  <span className="text-muted">day pace </span>
                  <span className={`font-mono ${signCls(vol.pace_vs_typical - 1)}`}>
                    {vol.pace_vs_typical}×
                  </span>
                  <span className="text-muted"> typical ({vol.pace_days}d)</span>
                </span>
              )}
              {vol.trend && (
                <span>
                  <span className="text-muted">last {vol.trend_window_min}m </span>
                  <span className="font-mono">{vol.trend}</span>
                </span>
              )}
            </div>
          )}
          {read.patterns.length === 0 ? (
            <p className="text-[11px] text-muted">
              No directional candlestick pattern on the last 30 minutes of closed bars.
            </p>
          ) : (
            <table className="w-full text-[11px]">
              <thead>
                <tr className="text-left text-[10px] uppercase text-muted">
                  <th className="py-1 pr-2">bar</th>
                  <th className="py-1 pr-2">pattern</th>
                  <th className="py-1 pr-2">vol</th>
                  <th className="py-1 pr-2">history says (30m, matched vol)</th>
                  <th className="py-1">unconditioned</th>
                </tr>
              </thead>
              <tbody>
                {read.patterns.map((p, i) => {
                  const info = patternInfo(p.pattern);
                  return (
                    <Fragment key={i}>
                      <tr className="border-t border-edge/50">
                        <td className="py-1 pr-2 font-mono text-muted">{p.bar}</td>
                        <td className={`py-1 pr-2 ${p.direction === "bullish" ? "text-bull" : "text-bear"}`}>
                          {p.pattern.replace(/_/g, " ")}
                          <span className="ml-1 text-[9px] text-muted">
                            {p.direction === "bullish" ? "(textbook: up)" : "(textbook: down)"}
                          </span>
                        </td>
                        <td className="py-1 pr-2 font-mono text-muted">{p.volume_regime ?? "—"}</td>
                        <td className="py-1 pr-2">
                          <CellStat c={p.historical_30m} />
                          {!p.conditioned && <span className="text-[9px] text-muted"> (no vol match)</span>}
                        </td>
                        <td className="py-1"><CellStat c={p.historical_30m_all} /></td>
                      </tr>
                      {info && (
                        <tr>
                          <td />
                          <td colSpan={4} className="pb-1.5 pr-2 text-[10px] leading-relaxed text-muted">
                            {info.desc} <span className="text-white/60">Whether that worked HERE is
                            the &ldquo;history says&rdquo; number: above 55% = the textbook move
                            usually followed; 45–55% = coin flip; below 45% = NIFTY usually went
                            the opposite way.</span>
                          </td>
                        </tr>
                      )}
                    </Fragment>
                  );
                })}
              </tbody>
            </table>
          )}
          <p className="mt-2 text-[10px] leading-relaxed text-muted">{read.note} Hit rate
            is in the pattern&apos;s textbook direction — below 45% means the tape
            historically went the OTHER way.</p>
        </>
      )}
    </section>
  );
}

// ------------------------------------------------- volume-conditioned table

function ConditionalTable({ table }: { table: Record<string, ConditionalOutcome> }) {
  const rows = Object.entries(table);
  if (rows.length === 0) return <p className="text-[11px] text-muted">No data.</p>;
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-[11px]">
        <thead>
          <tr className="text-left text-[10px] uppercase text-muted">
            <th className="py-1 pr-2">pattern</th>
            <th className="py-1 pr-2">n</th>
            <th className="py-1 pr-2">30m all</th>
            <th className="py-1 pr-2">high vol</th>
            <th className="py-1 pr-2">low vol</th>
            <th className="py-1">Δ hi−lo</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(([name, p]) => {
            const h = p.horizons["30m"];
            if (!h) return null;
            return (
              <tr key={name} className="border-t border-edge/50">
                <td
                  className={`py-1 pr-2 ${p.direction === "bullish" ? "text-bull" : "text-bear"}`}
                  title={patternInfo(name)?.desc}
                >
                  {name.replace(/_/g, " ")}
                </td>
                <td className="py-1 pr-2 font-mono text-muted">{p.n_total}</td>
                <td className="py-1 pr-2"><CellStat c={h.all} /></td>
                <td className="py-1 pr-2"><CellStat c={h.by_volume?.high} /></td>
                <td className="py-1 pr-2"><CellStat c={h.by_volume?.low} /></td>
                <td className="py-1 font-mono">
                  {h.volume_effect_pp != null ? (
                    <span className={signCls(h.volume_effect_pp)}>
                      {h.volume_effect_pp > 0 ? "+" : ""}{h.volume_effect_pp}pp
                    </span>
                  ) : (
                    <span className="text-muted">—</span>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

// ---------------------------------------------------------------- weekday cards

function WeekdayCard({ name, s }: { name: string; s: DayOfWeekStats }) {
  const up = Math.round(s.up_day_rate * 100);
  return (
    <div className="card flex flex-col gap-2 p-3">
      <div className="flex items-baseline justify-between">
        <span className="text-sm font-semibold">{name}</span>
        <span className="text-[10px] text-muted">n={s.n_days}</span>
      </div>
      <div>
        <div className="mb-1 flex justify-between text-[10px] text-muted">
          <span>up days {up}%</span>
          <span>{s.up_days}/{s.n_days}</span>
        </div>
        <div className="flex h-1.5 overflow-hidden rounded bg-panel2">
          <div className="bg-bull" style={{ width: `${up}%` }} />
          <div className="bg-bear" style={{ width: `${100 - up}%` }} />
        </div>
      </div>
      <div className="grid grid-cols-2 gap-x-3 gap-y-1 text-[11px]">
        <span className="text-muted">open→close</span>
        <span className={`text-right font-mono ${signCls(s.open_to_close_bps.mean)}`}>
          {s.open_to_close_bps.mean > 0 ? "+" : ""}{s.open_to_close_bps.mean} bps
        </span>
        <span className="text-muted">median</span>
        <span className={`text-right font-mono ${signCls(s.open_to_close_bps.median)}`}>
          {s.open_to_close_bps.median > 0 ? "+" : ""}{s.open_to_close_bps.median} bps
        </span>
        <span className="text-muted">avg range</span>
        <span className="text-right font-mono">{Math.round(s.avg_range_bps)} bps</span>
        <span className="text-muted">1st-hr range share</span>
        <span className="text-right font-mono">{Math.round(s.first_hour_range_share * 100)}%</span>
        <span className="text-muted">trend days</span>
        <span className="text-right font-mono">{Math.round(s.trend_day_rate * 100)}%</span>
        {s.gap_up_faded_rate != null && (
          <>
            <span className="text-muted">gap-up faded</span>
            <span className={`text-right font-mono ${s.gap_up_faded_rate > 0.55 ? "text-bear" : ""}`}>
              {Math.round(s.gap_up_faded_rate * 100)}% <span className="text-muted">({s.gap_up_days})</span>
            </span>
          </>
        )}
        {s.gap_down_faded_rate != null && (
          <>
            <span className="text-muted">gap-down faded</span>
            <span className={`text-right font-mono ${s.gap_down_faded_rate > 0.55 ? "text-bull" : ""}`}>
              {Math.round(s.gap_down_faded_rate * 100)}% <span className="text-muted">({s.gap_down_days})</span>
            </span>
          </>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- heatmap

type HeatMetric = "drift" | "consistency" | "activity" | "volume";
const METRICS: { key: HeatMetric; label: string }[] = [
  { key: "drift", label: "Drift (bps)" },
  { key: "consistency", label: "Up-rate" },
  { key: "activity", label: "Activity" },
  { key: "volume", label: "Volume share" },
];

function Heatmap({
  tod, metric, hasVolume,
}: { tod: Record<string, TimeOfDaySlot[]>; metric: HeatMetric; hasVolume: boolean }) {
  const slots = useMemo(() => {
    const all = new Set<string>();
    Object.values(tod).forEach((rows) => rows.forEach((r) => all.add(r.slot)));
    return Array.from(all).sort();
  }, [tod]);

  // Scale each metric to the observed extreme so the palette always spans the
  // data instead of washing out on quiet weeks.
  const maxAbs = useMemo(() => {
    let drift = 0, act = 0, vol = 0, cons = 0;
    Object.values(tod).forEach((rows) =>
      rows.forEach((r) => {
        drift = Math.max(drift, Math.abs(r.mean_ret_bps));
        act = Math.max(act, r.mean_abs_ret_bps);
        vol = Math.max(vol, r.vol_proxy_share ?? 0);
        cons = Math.max(cons, Math.abs(r.up_rate - 0.5));
      }),
    );
    return { drift: drift || 1, act: act || 1, vol: vol || 1, cons: cons || 0.01 };
  }, [tod]);

  const cell = (r: TimeOfDaySlot | undefined) => {
    if (!r) return { bg: "transparent", text: "" };
    switch (metric) {
      case "drift": {
        const a = Math.min(1, Math.abs(r.mean_ret_bps) / maxAbs.drift) * 0.85;
        return {
          bg: r.mean_ret_bps >= 0 ? bullBg(a) : bearBg(a),
          text: `${r.mean_ret_bps > 0 ? "+" : ""}${r.mean_ret_bps.toFixed(0)}`,
        };
      }
      case "consistency": {
        const d = r.up_rate - 0.5;
        const a = Math.min(1, Math.abs(d) / maxAbs.cons) * 0.85;
        return { bg: d >= 0 ? bullBg(a) : bearBg(a), text: `${Math.round(r.up_rate * 100)}` };
      }
      case "activity": {
        const a = (r.mean_abs_ret_bps / maxAbs.act) * 0.9;
        return { bg: blueBg(a), text: `${r.mean_abs_ret_bps.toFixed(0)}` };
      }
      case "volume": {
        const v = r.vol_proxy_share ?? 0;
        const a = (v / maxAbs.vol) * 0.9;
        return { bg: blueBg(a), text: `${(v * 100).toFixed(1)}` };
      }
    }
  };

  if (metric === "volume" && !hasVolume) {
    return <div className="p-4 text-xs text-muted">No volume proxy in the stored data.</div>;
  }

  return (
    <div className="overflow-x-auto scroll-thin">
      <table className="w-full border-separate border-spacing-0.5 text-center text-[10px]">
        <thead>
          <tr>
            <th className="pr-2 text-left font-normal text-muted">IST →</th>
            {slots.map((s) => (
              <th key={s} className="min-w-[46px] font-normal text-muted">{s}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {WEEKDAYS.map((wd) => (
            <tr key={wd}>
              <td className="pr-2 text-left text-muted">{wd.slice(0, 3)}</td>
              {slots.map((slot) => {
                const r = tod[wd]?.find((x) => x.slot === slot);
                const c = cell(r);
                return (
                  <td
                    key={slot}
                    className="rounded px-1 py-1.5 font-mono"
                    style={{ background: c.bg }}
                    title={
                      r
                        ? `${wd} ${slot} · n=${r.n_days} days\ndrift ${r.mean_ret_bps} bps · up ${Math.round(r.up_rate * 100)}%\navg |move| ${r.mean_abs_ret_bps} bps` +
                          (r.vol_proxy_share != null ? `\nvolume share ${(r.vol_proxy_share * 100).toFixed(1)}%` : "")
                        : undefined
                    }
                  >
                    {c.text}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ---------------------------------------------------------------- pattern table

function PatternTable({ freq }: { freq: Record<string, CandleFrequency> }) {
  const rows = Object.entries(freq);
  return (
    <div className="overflow-x-auto scroll-thin">
      <table className="w-full text-left text-[11px]">
        <thead className="text-[10px] uppercase text-muted">
          <tr className="[&>th]:py-1 [&>th]:pr-3 [&>th]:font-medium">
            <th>Pattern</th>
            <th>Bias</th>
            <th className="text-right">Count</th>
            <th className="text-right">Hit 30m</th>
            <th className="text-right">Avg fwd</th>
            <th className="text-right">Busiest day</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(([name, f]) => {
            const o = f.overall;
            const busiest = Object.entries(f.by_weekday).sort((a, b) => b[1].count - a[1].count)[0];
            return (
              <tr key={name} className="border-t border-edge/60 [&>td]:py-1.5 [&>td]:pr-3">
                <td className="font-medium" title={patternInfo(name)?.desc}>
                  {name.replace(/_/g, " ")}
                </td>
                <td>
                  <span
                    className={`tag ${
                      o.direction === "bullish"
                        ? "bg-bull/15 text-bull"
                        : o.direction === "bearish"
                          ? "bg-bear/15 text-bear"
                          : "bg-panel2 text-muted"
                    }`}
                  >
                    {o.direction}
                  </span>
                </td>
                <td className="text-right font-mono">{o.count.toLocaleString()}</td>
                <td className={`text-right font-mono ${o.hit_rate_30m != null ? signCls(o.hit_rate_30m - 0.5) : "text-muted"}`}>
                  {o.hit_rate_30m != null ? `${(o.hit_rate_30m * 100).toFixed(1)}%` : "—"}
                </td>
                <td className={`text-right font-mono ${o.avg_fwd_30m_bps != null ? signCls(o.avg_fwd_30m_bps) : "text-muted"}`}>
                  {o.avg_fwd_30m_bps != null ? `${o.avg_fwd_30m_bps > 0 ? "+" : ""}${o.avg_fwd_30m_bps} bps` : "—"}
                </td>
                <td className="text-right text-muted">
                  {busiest ? `${busiest[0].slice(0, 3)} (${busiest[1].count})` : "—"}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

// ---------------------------------------------------------------- levels ladder

function LevelsLadder({ rows, lastClose, nearOnly }: { rows: LevelRow[]; lastClose: number; nearOnly: boolean }) {
  const shown = useMemo(() => {
    const filtered = nearOnly
      ? rows.filter((l) => Math.abs(l.level - lastClose) / lastClose <= 0.02)
      : rows;
    return [...filtered].sort((a, b) => b.level - a.level);
  }, [rows, lastClose, nearOnly]);
  const maxDays = Math.max(1, ...shown.map((l) => l.days_touched));

  // Where the live index sits inside the ladder — rendered as its own row so
  // support/resistance context is readable at a glance.
  const priceIdx = shown.findIndex((l) => l.level < lastClose);

  if (shown.length === 0) {
    return <div className="p-4 text-xs text-muted">No levels within ±2% of {lastClose.toFixed(0)} — widen the filter.</div>;
  }

  const items: (LevelRow | "price")[] = [...shown];
  items.splice(priceIdx === -1 ? shown.length : priceIdx, 0, "price");

  return (
    <div className="flex flex-col gap-1">
      {items.map((it, i) =>
        it === "price" ? (
          <div key="price" className="flex items-center gap-2 py-0.5">
            <span className="w-16 text-right font-mono text-[11px] text-accent">{lastClose.toFixed(0)}</span>
            <div className="h-px flex-1 bg-accent/60" />
            <span className="text-[10px] text-accent">last close</span>
          </div>
        ) : (
          <div key={it.level} className="group flex items-center gap-2">
            <span className="w-16 text-right font-mono text-[11px]">{it.level.toFixed(0)}</span>
            <div className="relative h-4 flex-1 overflow-hidden rounded-sm bg-panel2">
              <div
                className={`h-full ${it.as_resistance >= it.as_support ? "bg-bear/50" : "bg-bull/50"}`}
                style={{ width: `${(it.days_touched / maxDays) * 100}%` }}
              />
              <span className="absolute inset-y-0 left-1.5 flex items-center text-[10px] text-white/80">
                {it.days_touched}d · {it.total_touches} touches · holds {Math.round(it.hold_rate * 100)}%
              </span>
            </div>
            <span className="hidden w-40 text-[10px] text-muted sm:block">
              R {it.as_resistance} / S {it.as_support} · last {it.last_touch}
            </span>
          </div>
        ),
      )}
    </div>
  );
}

// ---------------------------------------------------------------- page

export function PatternsLab() {
  const status = usePolling(() => api.patternsStatus(), 15000, []);
  const [results, setResults] = useState<PatternsResults | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState<"sync" | "analyze" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [metric, setMetric] = useState<HeatMetric>("drift");
  const [nearOnly, setNearOnly] = useState(true);

  const load = useCallback(async () => {
    try {
      setResults(await api.patternsResults());
      setError(null);
    } catch (e) {
      // 404 = analysis never ran; that's the empty state, not an error.
      const msg = e instanceof Error ? e.message : String(e);
      if (!msg.startsWith("404")) setError(msg);
      setResults(null);
    } finally {
      setLoaded(true);
    }
  }, []);
  useEffect(() => { void load(); }, [load]);

  const syncAndAnalyze = async () => {
    setBusy("sync");
    setError(null);
    try {
      await api.patternsSync();
      setResults(await api.patternsAnalyze());
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      setError(msg.includes("401") || msg.toLowerCase().includes("login")
        ? "Kite login required — authenticate on the dashboard first, then retry."
        : msg);
    } finally {
      setBusy(null);
    }
  };

  const reanalyze = async () => {
    setBusy("analyze");
    setError(null);
    try {
      setResults(await api.patternsAnalyze());
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  };

  const d = results?.data;

  return (
    <div className="mx-auto flex min-h-screen max-w-6xl flex-col gap-3 p-3">
      {/* header */}
      <header className="card flex flex-wrap items-center gap-3 px-4 py-3">
        <a href="/" className="text-xs text-muted hover:text-white">← Dashboard</a>
        <ModuleSwitcher />
        <h1 className="text-sm font-semibold">Patterns Lab · NIFTY 50 · 5m</h1>
        {d && (
          <span className="text-[11px] text-muted">
            {d.days} sessions · {d.bars.toLocaleString()} bars · {d.from} → {d.to}
          </span>
        )}
        <div className="ml-auto flex items-center gap-2">
          {status.data && (
            <span className="text-[10px] text-muted">
              store {status.data.bars.toLocaleString()} bars
            </span>
          )}
          <button
            onClick={syncAndAnalyze}
            disabled={busy !== null}
            className="rounded bg-accent/20 px-3 py-1 text-xs font-medium text-accent hover:bg-accent/30 disabled:opacity-50"
          >
            {busy === "sync" ? "Syncing…" : "Sync + Analyze"}
          </button>
          <button
            onClick={reanalyze}
            disabled={busy !== null || (status.data?.bars ?? 0) === 0}
            className="rounded bg-panel2 px-3 py-1 text-xs text-muted hover:text-white disabled:opacity-50"
          >
            {busy === "analyze" ? "Analyzing…" : "Re-analyze"}
          </button>
        </div>
      </header>

      {error && (
        <div className="card border-bear/50 bg-bear/10 px-4 py-2 text-xs text-bear">{error}</div>
      )}

      {!results && loaded && !error && (
        <div className="card flex flex-col items-center gap-2 p-10 text-center">
          <p className="text-sm">No analysis yet.</p>
          <p className="max-w-md text-xs text-muted">
            Sync pulls ~3 years of NIFTY 50 5-minute candles from Kite (needs today&apos;s
            login) plus NIFTYBEES volume as a proxy, then computes day-of-week tendencies,
            time-of-day heatmaps, candlestick frequencies and support/resistance levels.
          </p>
        </div>
      )}

      {results && (
        <>
          {/* live level-touch callouts — kept HERE, off the trading screens */}
          <LevelWatchCard />

          {/* today's tape vs the conditional table */}
          <LiveReadCard />

          {/* day-of-week */}
          <section>
            <h2 className="mb-2 px-1 text-xs font-semibold uppercase tracking-wide text-muted">
              Day-of-week character
            </h2>
            <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-5">
              {WEEKDAYS.map((wd) =>
                results.day_of_week[wd] ? (
                  <WeekdayCard key={wd} name={wd} s={results.day_of_week[wd]} />
                ) : null,
              )}
            </div>
          </section>

          {/* time-of-day heatmap */}
          <section className="card p-3">
            <div className="mb-2 flex flex-wrap items-center gap-2">
              <h2 className="text-xs font-semibold uppercase tracking-wide text-muted">
                Time-of-day heatmap
              </h2>
              <div className="ml-auto flex gap-1">
                {METRICS.map((m) => (
                  <button
                    key={m.key}
                    onClick={() => setMetric(m.key)}
                    className={`rounded px-2 py-0.5 text-[10px] ${
                      metric === m.key ? "bg-accent/20 text-accent" : "bg-panel2 text-muted hover:text-white"
                    }`}
                  >
                    {m.label}
                  </button>
                ))}
              </div>
            </div>
            <Heatmap tod={results.time_of_day} metric={metric} hasVolume={d?.has_volume_proxy ?? false} />
            {metric === "volume" && (
              <p className="mt-2 text-[10px] text-muted">{d?.volume_note}</p>
            )}
          </section>

          <div className="grid grid-cols-1 gap-3 xl:grid-cols-2">
            {/* candle patterns */}
            <section className="card p-3">
              <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted">
                Candlestick frequency (trend-gated, 3y)
              </h2>
              <PatternTable freq={results.candlestick_frequency} />
            </section>

            {/* levels */}
            <section className="card p-3">
              <div className="mb-2 flex items-center gap-2">
                <h2 className="text-xs font-semibold uppercase tracking-wide text-muted">
                  Support / resistance ladder
                </h2>
                <button
                  onClick={() => setNearOnly((v) => !v)}
                  className={`ml-auto rounded px-2 py-0.5 text-[10px] ${
                    nearOnly ? "bg-accent/20 text-accent" : "bg-panel2 text-muted hover:text-white"
                  }`}
                >
                  {nearOnly ? "±2% of spot" : "all levels"}
                </button>
              </div>
              <LevelsLadder
                rows={results.levels.levels}
                lastClose={d?.last_close ?? 0}
                nearOnly={nearOnly}
              />
              <p className="mt-3 text-[10px] leading-relaxed text-muted">
                {results.levels.method} · {results.levels.pivot_count.toLocaleString()} pivots.
                Strength = distinct days touched; red = mostly rejected from below
                (resistance), green = mostly held from above (support).
              </p>
            </section>
          </div>

          {/* CAS auction-print ledger — official close vs last free tape */}
          {results.auction_print && (
            <section className="card p-3">
              <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted">
                Auction print (CAS) — official close vs the last free tape
              </h2>
              <div className="flex flex-wrap items-center gap-1.5 text-[10px]">
                {results.auction_print.cas_stats ? (
                  <span className="tag bg-panel2 font-mono text-white/80">
                    CAS era: mean {results.auction_print.cas_stats.mean > 0 ? "+" : ""}
                    {results.auction_print.cas_stats.mean} pts · positive{" "}
                    {results.auction_print.cas_stats.positive_rate}% (n=
                    {results.auction_print.cas_stats.n})
                  </span>
                ) : (
                  <span className="text-muted">No CAS sessions in the store yet.</span>
                )}
                {results.auction_print.pre_cas_baseline && (
                  <span className="tag bg-panel2 font-mono text-muted">
                    pre-CAS baseline: {results.auction_print.pre_cas_baseline.mean > 0 ? "+" : ""}
                    {results.auction_print.pre_cas_baseline.mean} pts (n=
                    {results.auction_print.pre_cas_baseline.n})
                  </span>
                )}
              </div>
              {results.auction_print.cas_days.length > 0 && (
                <div className="mt-2 space-y-0.5 font-mono text-[11px]">
                  {[...results.auction_print.cas_days].reverse().map((d) => (
                    <div key={d.date} className="flex items-center gap-3">
                      <span className="text-muted">{d.date}</span>
                      <span>tape {d.tape.toFixed(0)}</span>
                      <span>official {d.official.toFixed(0)}</span>
                      <span className={d.print >= 0 ? "text-bull" : "text-bear"}>
                        {d.print > 0 ? "+" : ""}
                        {d.print.toFixed(1)} pts
                      </span>
                    </div>
                  ))}
                </div>
              )}
              <p className="mt-2 text-[10px] leading-relaxed text-muted">
                {results.auction_print.note}
              </p>
            </section>
          )}

          {/* volume-conditioned pattern outcomes */}
          {results.conditional_outcomes && (
            <section className="card p-3">
              <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted">
                Volume-conditioned outcomes — does participation change the odds?
              </h2>
              <ConditionalTable table={results.conditional_outcomes} />
              <p className="mt-2 text-[10px] leading-relaxed text-muted">
                Hit rate in the pattern&apos;s textbook direction over the next 30 minutes;
                green/red only where n≥30 AND outside the 45–55% coin band — muted cells
                are coins or samples too small to call. A colored cell below 45% = the
                pattern historically resolved AGAINST its textbook read. Volume
                buckets: bar&apos;s NIFTYBEES proxy vs its rolling 20-bar median (≥1.5× high,
                ≤0.7× low). Moves are basis points of the index — a few bp does not pay
                option costs; this is context, not a trigger.
              </p>
            </section>
          )}

          <footer className="card px-4 py-3 text-[10px] leading-relaxed text-muted">
            {results.disclaimer} Generated {results.generated_at?.slice(0, 16).replace("T", " ")} UTC.
          </footer>
        </>
      )}
    </div>
  );
}
