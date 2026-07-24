"use client";

import { OptionChain, OptionRow } from "@/lib/api";
import { fmt, compact, signed } from "@/lib/format";

function OiCell({ value }: { value: number | null }) {
  const v = value ?? 0;
  const tone = v > 0 ? "text-bull" : v < 0 ? "text-bear" : "text-muted";
  // Compact ("+95.2L") not raw ("+95,15,870") — the raw form is ~10 chars and
  // was single-handedly making the table twice as wide as its column.
  return (
    <span className={`font-mono ${tone}`}>
      {value == null ? "—" : `${v >= 0 ? "+" : "−"}${compact(Math.abs(v))}`}
    </span>
  );
}

function Row({ r, atm, hideVol }: { r: OptionRow; atm: number | null; hideVol?: boolean }) {
  const isAtm = atm != null && r.strike === atm;
  const itmCe = atm != null && r.strike < atm; // CE in-the-money below spot
  const itmPe = atm != null && r.strike > atm;

  return (
    <tr className={`border-b border-edge/60 ${isAtm ? "bg-accent/10" : ""}`}>
      {/* CE side */}
      <td className={`px-2 py-1 text-right font-mono ${itmCe ? "text-white" : "text-muted"}`}>
        {compact(r.ce_oi)}
      </td>
      <td className="px-2 py-1 text-right">
        <OiCell value={r.ce_oi_change} />
      </td>
      {!hideVol && <td className="px-2 py-1 text-right font-mono text-muted">{compact(r.ce_volume)}</td>}
      {!hideVol && (
        <td className="px-2 py-1 text-right font-mono text-muted">
          {r.ce_iv != null ? r.ce_iv.toFixed(1) : "—"}
        </td>
      )}
      <td className={`px-2 py-1 text-right font-mono ${itmCe ? "bg-bull/5" : ""}`}>{fmt(r.ce_ltp)}</td>

      {/* Strike */}
      <td className="bg-panel2 px-2 py-1 text-center font-mono text-xs font-semibold">
        {r.strike.toLocaleString("en-IN")}
      </td>

      {/* PE side */}
      <td className={`px-2 py-1 text-left font-mono ${itmPe ? "bg-bear/5" : ""}`}>{fmt(r.pe_ltp)}</td>
      {!hideVol && (
        <td className="px-2 py-1 text-left font-mono text-muted">
          {r.pe_iv != null ? r.pe_iv.toFixed(1) : "—"}
        </td>
      )}
      {!hideVol && <td className="px-2 py-1 text-left font-mono text-muted">{compact(r.pe_volume)}</td>}
      <td className="px-2 py-1 text-left">
        <OiCell value={r.pe_oi_change} />
      </td>
      <td className={`px-2 py-1 text-left font-mono ${itmPe ? "text-white" : "text-muted"}`}>
        {compact(r.pe_oi)}
      </td>
    </tr>
  );
}

export function OptionChainTable({ chain, bare = false }: { chain: OptionChain | null; bare?: boolean }) {
  // REST-freshness cue: the WS staleness badge covers ticks, but this panel
  // would keep rendering its last payload silently if the backend stalls.
  const ageS = chain?.updated_at ? Math.max(0, Math.floor(Date.now() / 1000) - chain.updated_at) : null;
  const stale = ageS != null && ageS > 30;
  return (
    // bare: rendered inside ContextRail, which already supplies card chrome and
    // the title (the tab label) — nesting another card would double the border.
    <div className={bare ? "flex h-full flex-col" : "card flex h-full flex-col"}>
      <div className="flex items-center justify-between border-b border-edge px-3 py-1.5">
        {!bare && <h3 className="text-sm font-medium">Option Chain</h3>}
        <div className={`flex items-center gap-2 text-[11px] text-muted ${bare ? "ml-auto" : ""}`}>
          {stale && (
            <span className="tag bg-yellow-500/15 text-yellow-400" title="Chain data hasn't refreshed recently">
              {ageS! >= 3600 ? `${Math.floor(ageS! / 3600)}h old` : `${Math.floor(ageS! / 60)}m old`}
            </span>
          )}
          {chain?.expiry && <span>exp {chain.expiry}</span>}
          {chain?.pcr != null && (
            <span className="tag bg-panel2">
              PCR <span className={`ml-1 ${chain.pcr >= 1 ? "text-bull" : "text-bear"}`}>{chain.pcr}</span>
            </span>
          )}
        </div>
      </div>

      <div className={`grid ${bare ? "grid-cols-7" : "grid-cols-11"} border-b border-edge px-2 py-1 text-[10px] uppercase text-muted`}>
        <div className={`${bare ? "col-span-3" : "col-span-5"} text-center text-bull`}>Calls (CE)</div>
        <div className="text-center">Strike</div>
        <div className={`${bare ? "col-span-3" : "col-span-5"} text-center text-bear`}>Puts (PE)</div>
      </div>

      <div className={bare ? "min-h-0 flex-1 overflow-y-auto scroll-thin" : "max-h-[520px] overflow-y-auto scroll-thin"}>
        {/* bare: tighter cell padding so CE and PE both fit the rail width —
            a chain you must scroll sideways to compare defeats its purpose. */}
        <table className={`w-full text-xs ${bare ? "[&_td]:px-1 [&_th]:px-1" : ""}`}>
          <thead className="sticky top-0 bg-panel text-[10px] text-muted">
            <tr className="border-b border-edge">
              <th className="px-2 py-1 text-right font-normal">OI</th>
              <th className="px-2 py-1 text-right font-normal">OI Δ</th>
              {!bare && <th className="px-2 py-1 text-right font-normal">Vol</th>}
              {!bare && <th className="px-2 py-1 text-right font-normal">IV</th>}
              <th className="px-2 py-1 text-right font-normal">LTP</th>
              <th className="px-2 py-1 text-center font-normal"> </th>
              <th className="px-2 py-1 text-left font-normal">LTP</th>
              {!bare && <th className="px-2 py-1 text-left font-normal">IV</th>}
              {!bare && <th className="px-2 py-1 text-left font-normal">Vol</th>}
              <th className="px-2 py-1 text-left font-normal">OI Δ</th>
              <th className="px-2 py-1 text-left font-normal">OI</th>
            </tr>
          </thead>
          <tbody>
            {(chain?.rows ?? []).map((r) => (
              <Row key={r.strike} r={r} atm={chain?.atm_strike ?? null} hideVol={bare} />
            ))}
          </tbody>
        </table>
        {(!chain || chain.rows.length === 0) && (
          <div className="py-8 text-center text-sm text-muted">Option chain warming up…</div>
        )}
      </div>
      <div className="border-t border-edge px-3 py-1.5 text-[10px] text-muted">
        OI Δ = build since feed start · IV = Black-Scholes solve, ±5 strikes of ATM
      </div>
    </div>
  );
}
