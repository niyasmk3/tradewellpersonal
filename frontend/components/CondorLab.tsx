"use client";

// The Condor tab: range-regime credit structures, advisory-only and TRIAL.
// Same honesty rules as the rest of the house: a quality NO TRADE beats a
// weak signal (the no-card state is first-class, not an error), every rupee
// figure that is an estimate says so, and nothing here places, modifies or
// exits an order — positions are journals of fills YOU executed in Kite.
// Polls every 60s to match the backend's evaluation cadence.

import { useCallback, useEffect, useState } from "react";
import {
  api,
  ApiError,
  CondorCard,
  CondorHistoryRow,
  CondorLeg,
  CondorPosition,
  CondorResponse,
  CondorWhatIf,
  CondorWhatIfRow,
  PositionView,
  ScorePart,
} from "@/lib/api";
import { compact, fmt, fmtInt, istDateTime, istTime, parseNum, signed } from "@/lib/format";
import { ModuleSwitcher } from "./ModuleSwitcher";

const SYMBOL = "NIFTY";

// ---------------------------------------------------------------- tone maps

function regimeTone(regime: string): string {
  if (regime === "RANGE_BOUND") return "bg-bull/15 text-bull";
  if (regime.startsWith("MILD_")) return "bg-yellow-500/15 text-yellow-400";
  if (regime === "TRENDING" || regime === "VOLATILE_UNSAFE") return "bg-bear/15 text-bear";
  return "bg-panel2 text-muted"; // NOT_QUALIFIED / WARMING_UP / unknown
}

function breakoutTone(band: string): string {
  if (band === "LOW") return "bg-bull/15 text-bull";
  if (band === "MEDIUM") return "bg-yellow-500/15 text-yellow-400";
  if (band === "HIGH" || band === "EXTREME") return "bg-bear/15 text-bear";
  return "bg-panel2 text-muted";
}

function volTone(vol: string): string {
  if (vol === "EXTREME") return "bg-bear/15 text-bear";
  if (vol === "ELEVATED") return "bg-yellow-500/15 text-yellow-400";
  if (vol === "NORMAL") return "bg-bull/15 text-bull";
  return "bg-panel2 text-muted"; // LOW / UNKNOWN
}

function qualityTone(quality: string): string {
  if (quality === "HIGH QUALITY") return "bg-bull/15 text-bull";
  if (quality === "WATCHLIST") return "bg-accent/15 text-accent";
  return "bg-panel2 text-muted";
}

function adviceTone(advice: string): string {
  if (advice.startsWith("EXIT")) return "bg-bear/15 text-bear";
  if (advice.includes("PROFIT")) return "bg-bull/15 text-bull";
  if (advice.includes("ADJUST")) return "bg-yellow-500/15 text-yellow-400";
  return "bg-panel2 text-muted"; // HOLD
}

function cardStateTone(state: string): string {
  if (state === "active") return "bg-bull/15 text-bull";
  if (state === "withdrawn") return "bg-yellow-500/15 text-yellow-400";
  return "bg-panel2 text-muted"; // expired
}

// ------------------------------------------------------------- small pieces

/** Signed rupees: "+₹12,340" / "−₹8,120". */
const rupees = (n: number | null | undefined): string =>
  n == null || Number.isNaN(n) ? "—" : `${n < 0 ? "−" : "+"}₹${fmtInt(Math.abs(n))}`;

function Stat({ label, value, tone, sub }: {
  label: string;
  value: string;
  tone?: string;
  sub?: string;
}) {
  return (
    <div className="rounded-md border border-edge bg-panel2 px-3 py-2">
      <div className="text-[10px] uppercase tracking-wide text-muted">{label}</div>
      <div className={`font-mono text-sm ${tone ?? "text-white"}`}>{value}</div>
      {sub && <div className="font-mono text-[10px] text-muted">{sub}</div>}
    </div>
  );
}

/** Stat tile whose value is a state chip (regime / breakout / vol). */
function ChipStat({ label, chip, chipTone, sub }: {
  label: string;
  chip: string;
  chipTone: string;
  sub?: string;
}) {
  return (
    <div className="rounded-md border border-edge bg-panel2 px-3 py-2">
      <div className="text-[10px] uppercase tracking-wide text-muted">{label}</div>
      <div className="mt-0.5">
        <span className={`tag text-[10px] ${chipTone}`}>{chip || "—"}</span>
      </div>
      {sub && <div className="mt-0.5 font-mono text-[10px] text-muted">{sub}</div>}
    </div>
  );
}

function findLeg(legs: CondorLeg[], side: "SELL" | "BUY", right: string): CondorLeg | undefined {
  return legs.find((l) => l.side === side && l.right === right);
}

/** "24400/24600 PE · 25300/25500 CE" — wing/short each side. */
function strikesSummary(legs: CondorLeg[]): string {
  const wpe = findLeg(legs, "BUY", "PE")?.strike;
  const spe = findLeg(legs, "SELL", "PE")?.strike;
  const sce = findLeg(legs, "SELL", "CE")?.strike;
  const wce = findLeg(legs, "BUY", "CE")?.strike;
  if (wpe == null || spe == null || sce == null || wce == null) {
    return legs.map((l) => `${l.side[0]}${fmtInt(l.strike)}${l.right}`).join(" ");
  }
  return `${fmtInt(wpe)}/${fmtInt(spe)} PE · ${fmtInt(sce)}/${fmtInt(wce)} CE`;
}

function LegRow({ leg }: { leg: CondorLeg }) {
  const sell = leg.side === "SELL";
  const px = leg.mid ?? leg.ltp;
  return (
    <div
      className={`flex flex-wrap items-baseline gap-x-2 rounded px-2 py-1 font-mono text-[12px] ${
        sell ? "border border-edge bg-panel2 text-white" : "text-muted"
      }`}
    >
      <span className={sell ? "font-semibold" : ""}>{leg.side}</span>
      <span>{fmtInt(leg.strike)}</span>
      <span className={leg.right === "CE" ? "text-bull" : "text-bear"}>{leg.right}</span>
      <span>@ ₹{fmt(px)}</span>
      <span className="text-[10px] text-muted">
        (Δ {leg.delta != null ? leg.delta.toFixed(2) : "—"}, IV{" "}
        {leg.iv != null ? `${(leg.iv * 100).toFixed(0)}%` : "—"})
      </span>
      {leg.spread_pct != null && (
        <span className="ml-auto text-[10px] text-muted">spread {fmt(leg.spread_pct, 1)}%</span>
      )}
    </div>
  );
}

/** Score-part bars — the SignalPanel ScoreBars idiom, detail on hover. */
function ScoreBars({ parts }: { parts: ScorePart[] }) {
  if (parts.length === 0) return null;
  return (
    <div className="grid grid-cols-2 gap-x-4 gap-y-1.5 sm:grid-cols-3">
      {parts.map((c) => {
        const pct = c.max > 0 ? Math.max(0, Math.min(100, (c.points / c.max) * 100)) : 0;
        return (
          <div key={c.name} className="min-w-0" title={c.detail || c.name}>
            <div className="flex justify-between text-[10px] text-muted">
              <span className="truncate">{c.name}</span>
              <span className="font-mono">
                {c.points}/{c.max}
              </span>
            </div>
            <div className="mt-0.5 h-1 overflow-hidden rounded-full bg-edge">
              <div className="h-full bg-accent" style={{ width: `${pct}%` }} />
            </div>
            {c.detail && <div className="truncate text-[9px] text-muted/80">{c.detail}</div>}
          </div>
        );
      })}
    </div>
  );
}

function HealthMeter({ health, band }: { health: number | null; band: string | null }) {
  if (health == null) return null;
  const bar = band === "Critical" ? "bg-bear" : band === "Warning" ? "bg-yellow-500" : "bg-bull";
  const text = band === "Critical" ? "text-bear" : band === "Warning" ? "text-yellow-400" : "text-bull";
  return (
    <div className="flex items-center gap-1.5" title="Composite 0-100: distance to shorts, capture, breakout risk">
      <span className="text-[10px] uppercase tracking-wide text-muted">health</span>
      <div className="h-1.5 w-24 overflow-hidden rounded-full bg-edge">
        <div className={`h-full ${bar}`} style={{ width: `${Math.max(0, Math.min(100, health))}%` }} />
      </div>
      <span className={`font-mono text-[10px] ${text}`}>{health}</span>
      {band && <span className={`text-[10px] ${text}`}>{band}</span>}
    </div>
  );
}

function NumField({ label, value, onChange, width = "w-24" }: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  width?: string;
}) {
  return (
    <label className="text-[10px] text-muted">
      {label}
      <input
        value={value}
        onChange={(e) => onChange(e.target.value)}
        inputMode="decimal"
        className={`mt-0.5 block ${width} rounded border border-edge bg-panel2 px-2 py-1 font-mono text-xs outline-none focus:border-accent`}
      />
    </label>
  );
}

// ---------------------------------------------------------------- top strip

function TopStrip({ r, stale, busy, onRefresh }: {
  r: CondorResponse;
  stale: boolean;
  busy: boolean;
  onRefresh: () => void;
}) {
  return (
    <section className="card p-3">
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <h2 className="text-xs font-semibold uppercase tracking-wide text-muted">Market state</h2>
        {r.data_problems.map((p) => (
          <span key={p} className="tag bg-yellow-500/15 text-[9px] text-yellow-400" title="Data problem this evaluation">
            ⚠ {p}
          </span>
        ))}
        {stale && (
          <span
            className="tag bg-yellow-500/15 text-[9px] text-yellow-400"
            title="The last refresh failed — this strip shows the previous successful read"
          >
            stale
          </span>
        )}
        <span className="ml-auto text-[10px] text-muted">evaluated {istTime(r.evaluated_at)}</span>
        <button
          onClick={onRefresh}
          disabled={busy}
          className="rounded bg-panel2 px-2 py-0.5 text-[10px] text-muted hover:text-white disabled:opacity-50"
        >
          {busy ? "Reading…" : "Refresh"}
        </button>
      </div>
      <div className="grid grid-cols-2 gap-1.5 sm:grid-cols-4 lg:grid-cols-7">
        <Stat label="Instrument" value={r.symbol} />
        <Stat label="Expiry" value={r.expiry ?? "—"} sub={r.dte != null ? `${r.dte} DTE` : undefined} />
        <Stat label="Spot" value={fmt(r.spot)} />
        <Stat
          label="India VIX"
          value={fmt(r.vix)}
          sub={r.vix_percentile != null ? `${Math.round(r.vix_percentile)}th pctile` : undefined}
        />
        <ChipStat
          label="Market regime"
          chip={r.regime}
          chipTone={regimeTone(r.regime)}
          sub={`confidence ${r.regime_confidence}%`}
        />
        <ChipStat
          label="Breakout risk"
          chip={r.breakout_band}
          chipTone={breakoutTone(r.breakout_band)}
          sub={`score ${r.breakout_score}`}
        />
        <ChipStat label="Volatility" chip={r.vol_regime} chipTone={volTone(r.vol_regime)} />
      </div>
    </section>
  );
}

// --------------------------------------------------------------- signal card

function SignalCard({ r }: { r: CondorResponse }) {
  const card = r.card;

  if (!card) {
    // First-class state, styled deliberately: the engine said no, and the
    // gates that said it are the content.
    return (
      <section className="card p-6">
        <div className="text-center text-lg font-semibold tracking-widest text-muted">
          NO IRON CONDOR TRADE
        </div>
        <p className="mt-1 text-center text-[11px] text-muted">
          A quality NO TRADE beats a weak signal — these gates said no:
        </p>
        {r.no_trade_reasons.length > 0 ? (
          <ul className="mx-auto mt-3 max-w-xl space-y-1">
            {r.no_trade_reasons.map((reason, i) => (
              <li key={i} className="flex gap-2 text-[12px] text-white/70">
                <span className="text-muted">•</span>
                <span>{reason}</span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="mt-3 text-center text-[12px] text-white/70">
            No qualifying structure this evaluation.
          </p>
        )}
      </section>
    );
  }

  const orderedLegs = [
    findLeg(card.legs, "SELL", "CE"),
    findLeg(card.legs, "SELL", "PE"),
    findLeg(card.legs, "BUY", "CE"),
    findLeg(card.legs, "BUY", "PE"),
  ].filter((l): l is CondorLeg => l != null);

  return (
    <section className="card p-3">
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <span className={`tag text-xs ${qualityTone(card.quality)}`}>{card.quality}</span>
        {card.trial && (
          <span
            className="tag bg-amber-400/15 text-[10px] text-amber-300"
            title="Shadow-first: rendered here, never pushed as an alert — the shadow ledger renders the verdict at n≥30"
          >
            TRIAL
          </span>
        )}
        <span className="font-mono text-sm">
          score {card.score.toFixed(0)}<span className="text-muted">/100</span>
        </span>
        <span className="ml-auto text-[10px] text-muted">
          {card.lots} lot(s) × {card.lot_size} · valid until {istTime(card.valid_until)}
        </span>
      </div>

      {/* the four legs — shorts loud, wings muted */}
      <div className="space-y-1">
        {orderedLegs.map((leg) => (
          <LegRow key={`${leg.side}-${leg.right}-${leg.strike}`} leg={leg} />
        ))}
      </div>

      {/* economics */}
      <div className="mt-2 grid grid-cols-2 gap-1.5 sm:grid-cols-3 lg:grid-cols-4">
        <Stat
          label="Net credit"
          value={`${fmt(card.credit_mid, 1)} pts`}
          sub={`ideal ${fmt(card.credit_ideal_low, 0)}–${fmt(card.credit_ideal_high, 0)} · min ${fmt(card.credit_min_acceptable, 0)} · avoid <${fmt(card.credit_avoid_below, 0)}`}
        />
        <Stat label="Width" value={`${fmtInt(card.width)} pts`} sub="per side" />
        <Stat label="Max profit" value={`₹${fmtInt(card.max_profit)}`} tone="text-bull" sub="net of est. charges" />
        <Stat label="Max loss" value={`₹${fmtInt(card.max_loss)}`} tone="text-bear" sub="incl. est. charges" />
        <Stat label="Breakevens" value={`${fmtInt(card.be_low)} – ${fmtInt(card.be_high)}`} />
        <Stat label="POP" value={card.pop != null ? `${(card.pop * 100).toFixed(0)}%` : "—"} />
        <Stat label="Risk : reward" value={fmt(card.risk_reward)} sub="max profit ÷ max loss" />
        <Stat label="Margin (est.)" value={card.margin_estimate != null ? `₹${compact(card.margin_estimate)}` : "—"} />
        <Stat label="Net Δ" value={signed(card.net_delta)} sub="per structure, 1 lot" />
        <Stat label="Net Θ" value={signed(card.net_theta)} tone={card.net_theta != null && card.net_theta > 0 ? "text-bull" : undefined} sub="pts/day" />
        {/* "vega" spelled out: the label row is CSS-uppercased and capital nu
            (Ν) is indistinguishable from a Latin N. */}
        <Stat label="Net ν (vega)" value={signed(card.net_vega)} />
        <Stat label="Net Γ" value={signed(card.net_gamma, 4)} />
      </div>

      {/* rationale */}
      {card.reasons.length > 0 && (
        <div className="mt-3">
          <div className="mb-1 text-[10px] uppercase tracking-wide text-muted">Why this trade</div>
          <ul className="space-y-0.5">
            {card.reasons.map((reason, i) => (
              <li key={i} className="flex gap-1.5 text-[12px] text-white/80">
                <span className="text-bull">✓</span>
                <span>{reason}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
      {card.risks.length > 0 && (
        <div className="mt-2">
          <div className="mb-1 text-[10px] uppercase tracking-wide text-muted">Risks</div>
          <ul className="space-y-0.5">
            {card.risks.map((risk, i) => (
              <li key={i} className="flex gap-1.5 text-[12px] text-yellow-400/90">
                <span>⚠</span>
                <span>{risk}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="mt-3 border-t border-edge pt-3">
        <div className="mb-1.5 text-[10px] uppercase tracking-wide text-muted">
          Score breakdown · {card.score.toFixed(0)}/100
        </div>
        <ScoreBars parts={card.score_parts} />
      </div>
    </section>
  );
}

// ----------------------------------------------------------------- analysis

function AnalysisCard({ r }: { r: CondorResponse }) {
  const ems: [string, number | null][] = [
    ["straddle", r.em_straddle],
    ["IV", r.em_iv],
    ["ATR", r.em_atr],
    ["RV", r.em_rv],
  ];
  const votes = Object.entries(r.regime_votes);
  return (
    <section className="card p-3">
      <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted">
        Analysis — expected move & regime
      </h2>

      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px]">
        <span className="text-[10px] uppercase tracking-wide text-muted">Expected move</span>
        {ems.map(([name, v]) => {
          const primary = v != null && r.em_primary != null && Math.abs(v - r.em_primary) < 1e-6;
          return (
            <span key={name} className={`font-mono ${primary ? "text-white" : "text-muted"}`}>
              ±{fmt(v, 0)} <span className="text-[10px]">{name}</span>
              {primary && <span className="tag ml-1 bg-accent/15 text-[9px] text-accent">primary</span>}
            </span>
          );
        })}
        {r.expected_low != null && r.expected_high != null && (
          <span className="font-mono">
            <span className="text-[10px] uppercase text-muted">range </span>
            {fmt(r.expected_low, 0)} – {fmt(r.expected_high, 0)}
          </span>
        )}
        <span className="font-mono">
          <span className="text-[10px] uppercase text-muted">IV/RV </span>
          {fmt(r.iv_over_rv)}
        </span>
      </div>

      {votes.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-1">
          {votes.map(([name, v]) => (
            <span
              key={name}
              className={`tag text-[9px] ${v ? "bg-bull/15 text-bull" : "bg-bear/15 text-bear"}`}
            >
              {v ? "✓" : "✗"} {name.replace(/_/g, " ")}
            </span>
          ))}
        </div>
      )}

      {r.regime_vetoes.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-1">
          {r.regime_vetoes.map((veto) => (
            <span key={veto} className="tag bg-bear/15 text-[9px] text-bear">
              veto: {veto}
            </span>
          ))}
        </div>
      )}

      {r.breakout_parts.length > 0 && (
        <ul className="mt-2 space-y-0.5">
          {r.breakout_parts.map((part, i) => (
            <li key={i} className="text-[10px] text-muted">
              · {part}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

// ------------------------------------------------------------ payoff diagram

/** P&L at expiry from the 4 legs + credit_mid × lot_size × lots. Pure inline
 *  SVG (the Spark/EquityCurve idiom) — client-side, charges not included. */
function PayoffCard({ card }: { card: CondorCard }) {
  const sCE = findLeg(card.legs, "SELL", "CE");
  const sPE = findLeg(card.legs, "SELL", "PE");
  const wCE = findLeg(card.legs, "BUY", "CE");
  const wPE = findLeg(card.legs, "BUY", "PE");
  if (!sCE || !sPE || !wCE || !wPE || card.width <= 0) return null;

  const qty = (card.lot_size || 1) * (card.lots || 1);
  const pnlAt = (s: number) =>
    (card.credit_mid -
      (Math.max(s - sCE.strike, 0) - Math.max(s - wCE.strike, 0) +
        Math.max(sPE.strike - s, 0) - Math.max(wPE.strike - s, 0))) * qty;

  const x0 = wPE.strike - card.width;
  const x1 = wCE.strike + card.width;
  const W = 640;
  const H = 220;
  const mL = 10;
  const mR = 10;
  const mT = 18;
  const mB = 26;
  const maxP = pnlAt((sPE.strike + sCE.strike) / 2); // plateau between shorts
  const minP = pnlAt(x0); // max loss beyond wings
  const span = maxP - minP || 1;
  const pad = 0.08 * span;
  const yTop = maxP + pad;
  const yBot = minP - pad;
  const X = (s: number) => mL + ((s - x0) / (x1 - x0)) * (W - mL - mR);
  const Y = (p: number) => mT + ((yTop - p) / (yTop - yBot)) * (H - mT - mB);
  const y0 = Y(0);
  // Where THIS curve crosses zero (credit at mid; the card's breakevens may
  // sit a shade inside once charges are counted — those get the dashed lines).
  const zl = sPE.strike - card.credit_mid;
  const zr = sCE.strike + card.credit_mid;
  const beL = card.be_low || zl;
  const beH = card.be_high || zr;

  const pt = (s: number, p: number) => `${X(s).toFixed(1)},${Y(p).toFixed(1)}`;
  const curve = [x0, wPE.strike, sPE.strike, sCE.strike, wCE.strike, x1]
    .map((s) => pt(s, pnlAt(s)))
    .join(" ");
  const profitPoly = `${X(zl).toFixed(1)},${y0.toFixed(1)} ${pt(sPE.strike, maxP)} ${pt(sCE.strike, maxP)} ${X(zr).toFixed(1)},${y0.toFixed(1)}`;
  const lossLeft = `${X(x0).toFixed(1)},${y0.toFixed(1)} ${pt(x0, minP)} ${pt(wPE.strike, minP)} ${X(zl).toFixed(1)},${y0.toFixed(1)}`;
  const lossRight = `${X(zr).toFixed(1)},${y0.toFixed(1)} ${pt(wCE.strike, minP)} ${pt(x1, minP)} ${X(x1).toFixed(1)},${y0.toFixed(1)}`;

  const strikeTicks: { s: number; muted: boolean }[] = [
    { s: wPE.strike, muted: true },
    { s: sPE.strike, muted: false },
    { s: sCE.strike, muted: false },
    { s: wCE.strike, muted: true },
  ];

  return (
    <section className="card p-3">
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <h2 className="text-xs font-semibold uppercase tracking-wide text-muted">Payoff at expiry</h2>
        <span className="text-[10px] text-muted">
          from the 4 legs at mid credit × {card.lots} lot(s) × {card.lot_size} — charges not included
        </span>
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full" role="img" aria-label="Iron condor payoff at expiry">
        {/* profit / loss regions */}
        <polygon points={profitPoly} fill="#16c784" opacity="0.15" />
        <polygon points={lossLeft} fill="#ea3943" opacity="0.15" />
        <polygon points={lossRight} fill="#ea3943" opacity="0.15" />
        {/* zero line */}
        <line x1={mL} y1={y0} x2={W - mR} y2={y0} stroke="#1f2937" strokeWidth="1" />
        {/* breakevens */}
        {[beL, beH].map((be) => (
          <g key={be}>
            <line
              x1={X(be)} y1={mT} x2={X(be)} y2={H - mB}
              stroke="#8b98a9" strokeWidth="1" strokeDasharray="3 3" opacity="0.7"
            />
            <text x={X(be)} y={mT - 5} textAnchor="middle" fontSize="9" fill="#8b98a9" className="font-mono">
              {fmtInt(be)}
            </text>
          </g>
        ))}
        {/* current spot */}
        {card.spot != null && card.spot >= x0 && card.spot <= x1 && (
          <g>
            <line x1={X(card.spot)} y1={mT} x2={X(card.spot)} y2={H - mB} stroke="#3b82f6" strokeWidth="1.5" />
            <text x={X(card.spot)} y={H - mB + 18} textAnchor="middle" fontSize="9" fill="#3b82f6" className="font-mono">
              spot {fmtInt(card.spot)}
            </text>
          </g>
        )}
        {/* payoff curve */}
        <polyline points={curve} fill="none" stroke="#e6edf3" strokeWidth="1.5" />
        {/* max profit / max loss labels */}
        <text
          x={X((sPE.strike + sCE.strike) / 2)} y={Y(maxP) - 5}
          textAnchor="middle" fontSize="10" fill="#16c784" className="font-mono"
        >
          +₹{compact(maxP)}
        </text>
        <text x={X(x1) - 4} y={Y(minP) - 5} textAnchor="end" fontSize="10" fill="#ea3943" className="font-mono">
          −₹{compact(Math.abs(minP))}
        </text>
        {/* strike labels */}
        {strikeTicks.map(({ s, muted }) => (
          <text
            key={s} x={X(s)} y={H - 8} textAnchor="middle" fontSize="9"
            fill={muted ? "#8b98a9" : "#e6edf3"} className="font-mono"
          >
            {fmtInt(s)}
          </text>
        ))}
      </svg>
    </section>
  );
}

// ------------------------------------------------------------- record a fill

interface FillForm {
  lots: string;
  sce: string; scef: string;
  spe: string; spef: string;
  wce: string; wcef: string;
  wpe: string; wpef: string;
  notes: string;
}

const BLANK_FILL: FillForm = {
  lots: "1", sce: "", scef: "", spe: "", spef: "", wce: "", wcef: "", wpe: "", wpef: "", notes: "",
};

function RecordFill({ card, onDone }: { card: CondorCard | null; onDone: () => void }) {
  const [open, setOpen] = useState(false);
  const [f, setF] = useState<FillForm>(BLANK_FILL);
  const [busy, setBusy] = useState(false);

  const set = (k: keyof FillForm) => (v: string) => setF((prev) => ({ ...prev, [k]: v }));
  const str = (n: number | null | undefined) => (n == null ? "" : String(n));

  const openForm = () => {
    if (card) {
      const g = (side: "SELL" | "BUY", right: string) => findLeg(card.legs, side, right);
      setF({
        lots: String(card.lots || 1),
        sce: str(g("SELL", "CE")?.strike), scef: str(g("SELL", "CE")?.mid ?? g("SELL", "CE")?.ltp),
        spe: str(g("SELL", "PE")?.strike), spef: str(g("SELL", "PE")?.mid ?? g("SELL", "PE")?.ltp),
        wce: str(g("BUY", "CE")?.strike), wcef: str(g("BUY", "CE")?.mid ?? g("BUY", "CE")?.ltp),
        wpe: str(g("BUY", "PE")?.strike), wpef: str(g("BUY", "PE")?.mid ?? g("BUY", "PE")?.ltp),
        notes: "",
      });
    } else {
      setF(BLANK_FILL);
    }
    setOpen(true);
  };

  const submit = async () => {
    const lots = parseNum(f.lots);
    const sce = parseNum(f.sce); const scef = parseNum(f.scef);
    const spe = parseNum(f.spe); const spef = parseNum(f.spef);
    const wce = parseNum(f.wce); const wcef = parseNum(f.wcef);
    const wpe = parseNum(f.wpe); const wpef = parseNum(f.wpef);
    if (lots === null || lots < 1 || !Number.isInteger(lots)) {
      alert("Enter a valid whole lot count (1 or more)");
      return;
    }
    if ([sce, scef, spe, spef, wce, wcef, wpe, wpef].some((v) => v === null || v < 0)) {
      alert("Fill in all 8 numbers — 4 strikes and 4 fill premiums (points)");
      return;
    }
    const credit = scef! + spef! - wcef! - wpef!;
    if (
      !window.confirm(
        `Record this condor fill (${lots} lot(s))?\n\n` +
          `SELL ${fmtInt(sce)} CE @ ${fmt(scef)} · SELL ${fmtInt(spe)} PE @ ${fmt(spef)}\n` +
          `BUY ${fmtInt(wce)} CE @ ${fmt(wcef)} · BUY ${fmtInt(wpe)} PE @ ${fmt(wpef)}\n` +
          `Net credit ${fmt(credit)} pts\n\n` +
          `This journals YOUR manual Kite fills — Tradewell places nothing.`,
      )
    ) {
      return;
    }
    setBusy(true);
    try {
      await api.condorEnter({
        card_id: card?.id ?? null,
        symbol: SYMBOL,
        expiry: card?.expiry ?? null,
        lots,
        short_ce_strike: sce!, short_ce_fill: scef!,
        short_pe_strike: spe!, short_pe_fill: spef!,
        wing_ce_strike: wce!, wing_ce_fill: wcef!,
        wing_pe_strike: wpe!, wing_pe_fill: wpef!,
        notes: f.notes.trim() || null,
      });
      setOpen(false);
      setF(BLANK_FILL);
      onDone();
    } catch (e) {
      alert(e instanceof Error ? e.message : "could not record the fill");
    } finally {
      setBusy(false);
    }
  };

  if (!open) {
    return (
      <button
        onClick={openForm}
        className="rounded bg-panel2 px-2 py-0.5 text-[10px] text-muted hover:text-white"
        title="Journal a condor you filled manually in Kite — prefills from the live card when one exists"
      >
        Record fill
      </button>
    );
  }
  return (
    <div className="mt-2 w-full rounded-md border border-edge bg-panel2/60 p-2">
      <div className="mb-1 text-[10px] uppercase tracking-wide text-muted">
        Record fill — strikes + entry premiums (points per unit)
      </div>
      <div className="flex flex-wrap items-end gap-2">
        <NumField label="Lots" value={f.lots} onChange={set("lots")} width="w-14" />
        <NumField label="Short CE strike" value={f.sce} onChange={set("sce")} />
        <NumField label="@ fill" value={f.scef} onChange={set("scef")} width="w-20" />
        <NumField label="Short PE strike" value={f.spe} onChange={set("spe")} />
        <NumField label="@ fill" value={f.spef} onChange={set("spef")} width="w-20" />
        <NumField label="Wing CE strike" value={f.wce} onChange={set("wce")} />
        <NumField label="@ fill" value={f.wcef} onChange={set("wcef")} width="w-20" />
        <NumField label="Wing PE strike" value={f.wpe} onChange={set("wpe")} />
        <NumField label="@ fill" value={f.wpef} onChange={set("wpef")} width="w-20" />
        <label className="text-[10px] text-muted">
          Notes
          <input
            value={f.notes}
            onChange={(e) => set("notes")(e.target.value)}
            className="mt-0.5 block w-40 rounded border border-edge bg-panel2 px-2 py-1 text-xs outline-none focus:border-accent"
          />
        </label>
        <button
          onClick={submit}
          disabled={busy}
          className="rounded-md bg-bull px-3 py-1.5 text-xs font-medium text-black disabled:opacity-40"
        >
          {busy ? "…" : "Record"}
        </button>
        <button
          onClick={() => setOpen(false)}
          className="rounded-md bg-panel2 px-3 py-1.5 text-xs text-muted hover:text-white"
        >
          Cancel
        </button>
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ monitor

function OpenPositionRow({ view, onDone }: { view: PositionView; onDone: () => void }) {
  const p = view.position;
  const [exiting, setExiting] = useState(false);
  const [debit, setDebit] = useState("");
  const [busy, setBusy] = useState(false);

  const doExit = async () => {
    const d = parseNum(debit);
    if (d === null || d < 0) {
      alert("Enter the debit paid to close the structure (points, 0 or more)");
      return;
    }
    if (
      !window.confirm(
        `Close condor ${strikesSummary(p.legs)}?\n\n` +
          `Exit debit ${fmt(d)} pts vs credit ${fmt(p.credit_fill)} pts collected.\n\n` +
          `This records YOUR manual Kite exit — Tradewell places nothing.`,
      )
    ) {
      return;
    }
    setBusy(true);
    try {
      await api.condorExit(p.id, { exit_debit: d, reason: "manual" });
      setExiting(false);
      setDebit("");
      onDone();
    } catch (e) {
      alert(e instanceof Error ? e.message : "could not record the exit");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="rounded-md border border-edge bg-panel2/40 p-2">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-[12px] text-white">{strikesSummary(p.legs)}</span>
        <span className="text-[10px] text-muted">
          exp {p.expiry ?? "—"} · {p.lots} lot(s) × {p.lot_size} · entered {istDateTime(p.entered_at)}
          {p.adjustments > 0 && ` · ${p.adjustments} adj`}
        </span>
        <span className={`tag ml-auto text-[10px] font-semibold ${adviceTone(view.status_advice)}`}>
          {view.status_advice}
        </span>
      </div>

      <div className="mt-1.5 flex flex-wrap gap-x-4 gap-y-1 font-mono text-[11px]">
        <span>
          <span className="text-[10px] uppercase text-muted">now </span>
          {fmt(view.combined_mid, 1)} <span className="text-muted">vs {fmt(p.credit_fill, 1)} pts credit</span>
        </span>
        <span className={view.pnl != null && view.pnl < 0 ? "text-bear" : "text-bull"}>
          <span className="text-[10px] uppercase text-muted">P&L </span>
          {rupees(view.pnl)}
        </span>
        <span>
          <span className="text-[10px] uppercase text-muted">captured </span>
          {view.captured_pct != null ? `${fmt(view.captured_pct, 0)}%` : "—"}
        </span>
        <span>
          <span className="text-[10px] uppercase text-muted">to short CE </span>
          {fmtInt(view.dist_short_ce)} pts
          {view.dist_short_ce_em != null && <span className="text-muted"> ({fmt(view.dist_short_ce_em, 1)}×EM)</span>}
        </span>
        <span>
          <span className="text-[10px] uppercase text-muted">to short PE </span>
          {fmtInt(view.dist_short_pe)} pts
          {view.dist_short_pe_em != null && <span className="text-muted"> ({fmt(view.dist_short_pe_em, 1)}×EM)</span>}
        </span>
        <span>
          <span className="text-[10px] uppercase text-muted">Δ </span>
          {signed(view.net_delta)}
        </span>
        <span>
          <span className="text-[10px] uppercase text-muted">Θ </span>
          {signed(view.net_theta)}
        </span>
      </div>

      <div className="mt-1.5 flex flex-wrap items-center gap-3">
        <HealthMeter health={view.health} band={view.health_band} />
        {view.status_detail && <span className="text-[11px] text-muted">{view.status_detail}</span>}
        {!exiting ? (
          <button
            onClick={() => setExiting(true)}
            className="ml-auto rounded border border-bear/60 bg-bear/15 px-2 py-0.5 text-[10px] font-medium text-bear hover:bg-bear/25"
          >
            Exit…
          </button>
        ) : (
          <span className="ml-auto flex items-end gap-2">
            <NumField label="Exit debit (pts)" value={debit} onChange={setDebit} width="w-24" />
            <button
              onClick={doExit}
              disabled={busy}
              className="rounded-md bg-bear px-3 py-1.5 text-xs font-medium text-white disabled:opacity-40"
            >
              {busy ? "…" : "Record exit"}
            </button>
            <button
              onClick={() => setExiting(false)}
              className="rounded-md bg-panel2 px-3 py-1.5 text-xs text-muted hover:text-white"
            >
              Cancel
            </button>
          </span>
        )}
      </div>

      {view.adjustment && (
        <div className="mt-2 rounded-md border border-yellow-500/40 bg-yellow-500/10 p-2 text-[11px]">
          <div className="font-semibold text-yellow-400">↻ {view.adjustment.action}</div>
          <div className="mt-0.5 font-mono text-white/80">close {view.adjustment.close}</div>
          <div className="font-mono text-white/80">open {view.adjustment.open}</div>
          <div className="mt-0.5 flex flex-wrap gap-x-4 font-mono text-muted">
            <span>added credit {fmt(view.adjustment.added_credit, 1)} pts</span>
            <span>new total credit {fmt(view.adjustment.new_total_credit, 1)} pts</span>
            <span>new max loss ₹{fmtInt(view.adjustment.new_max_loss)}</span>
            <span>new BE {view.adjustment.new_breakevens}</span>
          </div>
        </div>
      )}
    </div>
  );
}

function PositionsCard({ positions, err, card, onDone }: {
  positions: { open: PositionView[]; closed: CondorPosition[] } | null;
  err: string | null;
  card: CondorCard | null;
  onDone: () => void;
}) {
  return (
    <section className="card p-3">
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <h2 className="text-xs font-semibold uppercase tracking-wide text-muted">Position monitor</h2>
        <span className="text-[10px] text-muted">
          journals of your manual Kite fills — Tradewell places and exits nothing
        </span>
        <div className="ml-auto">
          <RecordFill card={card} onDone={onDone} />
        </div>
      </div>

      {err && <p className="text-[11px] text-muted">{err}</p>}
      {!err && positions && positions.open.length === 0 && (
        <p className="text-[11px] text-muted">No open condors on the journal.</p>
      )}

      {positions && positions.open.length > 0 && (
        <div className="space-y-2">
          {positions.open.map((v) => (
            <OpenPositionRow key={v.position.id} view={v} onDone={onDone} />
          ))}
        </div>
      )}

      {positions && positions.closed.length > 0 && (
        <div className="mt-3">
          <div className="mb-1 text-[10px] uppercase tracking-wide text-muted">Closed</div>
          <div className="overflow-x-auto">
            <table className="w-full text-left text-[11px]">
              <thead>
                <tr className="text-[10px] uppercase text-muted">
                  <th className="py-1 pr-2">exited</th>
                  <th className="py-1 pr-2">structure</th>
                  <th className="py-1 pr-2">credit → debit</th>
                  <th className="py-1 pr-2">reason</th>
                  <th className="py-1 text-right">realized</th>
                </tr>
              </thead>
              <tbody>
                {positions.closed.map((p) => (
                  <tr key={p.id} className="border-t border-edge/40">
                    <td className="py-1 pr-2 font-mono text-muted">{istDateTime(p.exited_at)}</td>
                    <td className="py-1 pr-2 font-mono">{strikesSummary(p.legs)}</td>
                    <td className="py-1 pr-2 font-mono">
                      {fmt(p.credit_fill, 1)} → {fmt(p.exit_debit, 1)}
                    </td>
                    <td className="py-1 pr-2 text-muted">{p.exit_reason ?? "—"}</td>
                    <td
                      className={`py-1 text-right font-mono ${
                        (p.realized_pnl ?? 0) < 0 ? "text-bear" : "text-bull"
                      }`}
                    >
                      {rupees(p.realized_pnl)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </section>
  );
}

// ------------------------------------------------------------------ what-if

function WhatIfCard({ cardId, positionId }: { cardId: string | null; positionId: string | null }) {
  const [grid, setGrid] = useState<CondorWhatIf | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const run = async () => {
    setBusy(true);
    try {
      // Always name the structure being priced — an empty request lets the
      // backend fall back to a card even when the user is looking at a
      // position, silently pricing the wrong structure.
      const req = cardId ? { card_id: cardId } : positionId ? { position_id: positionId } : {};
      setGrid(await api.condorWhatif(req));
      setErr(null);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const dayLabel = (d: number | "expiry") =>
    d === "expiry" ? "at expiry" : d === 0 ? "today" : `+${d} day${d === 1 ? "" : "s"}`;
  const ivLabel = (s: number) =>
    s === 0 ? "IV unchanged" : `IV ${s > 0 ? "+" : "−"}${Math.abs(Math.round(s * 100))}%`;

  // One table per days value, in server order.
  const groups: { days: number | "expiry"; rows: CondorWhatIfRow[] }[] = [];
  for (const row of grid?.rows ?? []) {
    const g = groups.find((x) => x.days === row.days);
    if (g) g.rows.push(row);
    else groups.push({ days: row.days, rows: [row] });
  }

  return (
    <section className="card p-3">
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <h2 className="text-xs font-semibold uppercase tracking-wide text-muted">What-if scenarios</h2>
        <span className="text-[10px] text-muted">
          Black-Scholes repricing at shifted spot / time / IV — estimates, not fills
        </span>
        <button
          onClick={run}
          disabled={busy}
          className="ml-auto rounded border border-accent/60 bg-accent/15 px-2 py-0.5 text-[10px] font-medium text-accent hover:bg-accent/25 disabled:opacity-50"
        >
          {busy ? "Running…" : grid ? "Re-run scenarios" : "Run scenarios"}
        </button>
      </div>

      {err && <p className="text-[11px] text-muted">{err}</p>}
      {!grid && !err && (
        <p className="text-[11px] text-muted">
          Run to see structure P&L across spots, IV shifts and days forward.
        </p>
      )}

      {grid && (
        <div className="space-y-3">
          {groups.map((g) => (
            <div key={String(g.days)}>
              <div className="mb-1 text-[10px] uppercase tracking-wide text-muted">{dayLabel(g.days)}</div>
              <div className="overflow-x-auto">
                <table className="w-full text-left text-[11px]">
                  <thead>
                    <tr className="text-[10px] uppercase text-muted">
                      <th className="py-1 pr-2">scenario</th>
                      {grid.spots.map((s) => (
                        <th key={s} className="py-1 pr-2 text-right font-mono">
                          {fmtInt(s)}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {g.rows.map((row) => (
                      <tr key={`${String(row.days)}-${row.iv_shift}`} className="border-t border-edge/40">
                        <td className="py-1 pr-2 text-muted">{ivLabel(row.iv_shift)}</td>
                        {row.cells.map((c) => (
                          <td key={c.spot} className="py-0.5 pr-2 text-right">
                            <span
                              className={`inline-block rounded px-1 font-mono ${
                                c.pnl < 0 ? "bg-bear/10 text-bear" : "bg-bull/10 text-bull"
                              }`}
                            >
                              {c.pnl < 0 ? "−" : "+"}₹{compact(Math.abs(c.pnl))}
                            </span>
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

// ------------------------------------------------------------------ history

function HistoryCard({ rows, err }: { rows: CondorHistoryRow[] | null; err: string | null }) {
  return (
    <section className="card p-3">
      <div className="mb-2 flex items-center gap-2">
        <h2 className="text-xs font-semibold uppercase tracking-wide text-muted">
          Card history — last 7 days
        </h2>
        {rows && <span className="text-[10px] text-muted">{rows.length} card(s)</span>}
      </div>
      {err && <p className="text-[11px] text-muted">{err}</p>}
      {rows && rows.length === 0 && !err && (
        <p className="text-[11px] text-muted">No cards issued in the window.</p>
      )}
      {rows && rows.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full text-left text-[11px]">
            <thead>
              <tr className="text-[10px] uppercase text-muted">
                <th className="py-1 pr-2">id</th>
                <th className="py-1 pr-2">created</th>
                <th className="py-1 pr-2">structure</th>
                <th className="py-1 pr-2 text-right">credit</th>
                <th className="py-1 pr-2 text-right">score</th>
                <th className="py-1 pr-2">quality</th>
                <th className="py-1">state</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.id} className="border-t border-edge/40">
                  <td className="py-1 pr-2 font-mono text-muted" title={r.id}>
                    {r.id.slice(0, 8)}
                  </td>
                  <td className="py-1 pr-2 font-mono">{istDateTime(r.created_at)}</td>
                  <td className="py-1 pr-2 font-mono">{strikesSummary(r.legs)}</td>
                  <td className="py-1 pr-2 text-right font-mono">{fmt(r.credit_mid, 1)}</td>
                  <td className="py-1 pr-2 text-right font-mono">{Math.round(r.score)}</td>
                  <td className="py-1 pr-2">
                    <span className={`tag text-[9px] ${qualityTone(r.quality)}`}>{r.quality}</span>
                  </td>
                  <td className="py-1">
                    <span className={`tag text-[9px] ${cardStateTone(r.state)}`}>{r.state}</span>
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

// -------------------------------------------------------- module-off states

function ModuleStateCard({ status, message, busy, onRefresh }: {
  status: number | null;
  message: string;
  busy: boolean;
  onRefresh: () => void;
}) {
  const friendly = status === 409 || status === 503;
  return (
    <section className="card p-6 text-center">
      <div className="text-sm font-semibold uppercase tracking-widest text-muted">
        {status === 409 ? "Module not running" : status === 503 ? "Warming up" : "Condor feed unavailable"}
      </div>
      <p className="mx-auto mt-2 max-w-lg text-[11px] text-muted">
        {status === 409
          ? "The Condor module is disabled (CONDOR_ENABLED=false) or the feed is stopped. Nothing is broken on this page — enable the module and restart the feed, and this tab lights up on its own."
          : status === 503
            ? "The feed is up but the engine hasn't produced its first evaluation yet. It evaluates every 60 seconds — this page re-polls on the same cadence, so the first read appears by itself."
            : message}
      </p>
      {!friendly && <p className="mx-auto mt-1 max-w-lg text-[10px] text-muted/70">{message}</p>}
      <button
        onClick={onRefresh}
        disabled={busy}
        className="mt-3 rounded bg-panel2 px-3 py-1 text-[11px] text-muted hover:text-white disabled:opacity-50"
      >
        {busy ? "Checking…" : "Check now"}
      </button>
    </section>
  );
}

// --------------------------------------------------------------------- main

export function CondorLab() {
  const [state, setState] = useState<CondorResponse | null>(null);
  const [stateErr, setStateErr] = useState<{ status: number | null; message: string } | null>(null);
  const [positions, setPositions] = useState<{ open: PositionView[]; closed: CondorPosition[] } | null>(null);
  const [posErr, setPosErr] = useState<string | null>(null);
  const [history, setHistory] = useState<CondorHistoryRow[] | null>(null);
  const [histErr, setHistErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    setBusy(true);
    const [st, pos, hist] = await Promise.allSettled([
      api.condorState(SYMBOL),
      api.condorPositions(),
      api.condorHistory(SYMBOL, 7),
    ]);
    if (st.status === "fulfilled") {
      setState(st.value);
      setStateErr(null);
    } else {
      const e = st.reason;
      setStateErr({
        status: e instanceof ApiError ? e.status : null,
        message: e instanceof Error ? e.message : String(e),
      });
    }
    if (pos.status === "fulfilled") {
      setPositions(pos.value);
      setPosErr(null);
    } else {
      const e = pos.reason;
      // Positions ride the same service as the card feed — a 409 here just
      // repeats the module-off story, so keep the copy short.
      setPosErr(
        e instanceof ApiError && e.status === 409
          ? "Positions are unavailable while the module is stopped."
          : e instanceof Error ? e.message : String(e),
      );
    }
    if (hist.status === "fulfilled") {
      setHistory(hist.value.rows);
      setHistErr(null);
    } else {
      setHistErr(hist.reason instanceof Error ? hist.reason.message : String(hist.reason));
    }
    setBusy(false);
  }, []);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 60_000); // backend evaluates every 60s
    return () => clearInterval(t);
  }, [refresh]);

  const card = state?.card ?? null;
  const whatifAvailable = card != null || (positions?.open.length ?? 0) > 0;

  return (
    <div className="mx-auto flex min-h-screen max-w-6xl flex-col gap-3 p-3">
      <header className="flex flex-wrap items-center gap-3">
        <ModuleSwitcher />
        <h1 className="text-sm font-semibold">IRON CONDOR · {SYMBOL}</h1>
        <span
          className="tag bg-yellow-500/15 text-[10px] text-yellow-400"
          title="Shadow-first module: cards render here but are never pushed as alerts, and every position is a journal of a trade you executed manually in Kite"
        >
          TRIAL — advisory only, signals not pushed
        </span>
      </header>

      {state ? (
        <>
          <TopStrip r={state} stale={stateErr != null} busy={busy} onRefresh={refresh} />
          <SignalCard r={state} />
          <AnalysisCard r={state} />
          {card && <PayoffCard card={card} />}
        </>
      ) : stateErr ? (
        <ModuleStateCard
          status={stateErr.status}
          message={stateErr.message}
          busy={busy}
          onRefresh={refresh}
        />
      ) : (
        <div className="card px-4 py-6 text-center text-xs text-muted">Reading the condor feed…</div>
      )}

      <PositionsCard positions={positions} err={posErr} card={card} onDone={refresh} />

      {whatifAvailable && (
        <WhatIfCard
          cardId={card?.id ?? null}
          positionId={positions?.open[0]?.position.id ?? null}
        />
      )}

      <HistoryCard rows={history} err={histErr} />
    </div>
  );
}
