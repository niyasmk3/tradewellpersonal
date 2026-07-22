"use client";

import { useEffect, useState } from "react";
import { ScoreBreakdown, SignalResponse, TradingMode, api } from "@/lib/api";
import { fetchKiteBasket, submitKiteBasket } from "@/lib/kiteBasket";
import { fmt, istToday, parseNum, pctFrom } from "@/lib/format";
import { RiskVisualizer } from "./RiskVisualizer";

function useCountdown(validUntil: number | undefined) {
  const [now, setNow] = useState(() => Math.floor(Date.now() / 1000));
  useEffect(() => {
    const id = setInterval(() => setNow(Math.floor(Date.now() / 1000)), 1000);
    return () => clearInterval(id);
  }, []);
  if (!validUntil) return null;
  const left = validUntil - now;
  if (left <= 0) return "expired";
  const m = Math.floor(left / 60);
  const s = left % 60;
  return `${m}:${s.toString().padStart(2, "0")}`;
}

function Stat({
  label,
  value,
  tone,
  pct,
}: {
  label: string;
  value: string;
  tone?: string;
  /** % move from entry — this is what Kite's GTT stop-loss / target boxes want. */
  pct?: string;
}) {
  return (
    <div className="rounded-md border border-edge bg-panel2 px-3 py-2">
      <div className="text-[10px] uppercase tracking-wide text-muted">{label}</div>
      <div className={`font-mono text-sm ${tone ?? "text-white"}`}>{value}</div>
      {pct && <div className={`font-mono text-[10px] ${tone ?? "text-muted"}`}>{pct} · for GTT</div>}
    </div>
  );
}

function ScoreBars({ score }: { score: ScoreBreakdown }) {
  return (
    <div className="grid grid-cols-2 gap-x-4 gap-y-1.5 sm:grid-cols-3">
      {score.components.map((c) => {
        const pct = c.max > 0 ? (c.points / c.max) * 100 : 0;
        return (
          <div key={c.name} className="min-w-0">
            <div className="flex justify-between text-[10px] text-muted">
              <span className="truncate">{c.name}</span>
              <span className="font-mono">
                {c.points}/{c.max}
              </span>
            </div>
            <div className="mt-0.5 h-1 overflow-hidden rounded-full bg-edge">
              <div className="h-full bg-accent" style={{ width: `${pct}%` }} />
            </div>
          </div>
        );
      })}
    </div>
  );
}

export function SignalPanel({
  data,
  symbol,
  mode,
  error,
  onEntered,
  onIgnore,
  onReprice,
  className = "",
}: {
  data: SignalResponse | null;
  symbol: string;
  mode: TradingMode;
  error?: string | null;
  onEntered: () => void;
  onIgnore: (signalId: string) => void;
  onReprice?: () => void;
  className?: string;
}) {
  const signal = data?.signal ?? null;
  const countdown = useCountdown(signal?.valid_until);
  const [entering, setEntering] = useState(false);
  const [lots, setLots] = useState("1");
  const [entryPx, setEntryPx] = useState("");
  const [busy, setBusy] = useState(false);

  // Bind the form to the card it was opened for: if the active signal changes
  // (bias flip swaps CE→PE), a still-open form must not book the new contract
  // at the old premium. Reset everything whenever the card id changes.
  useEffect(() => {
    setEntering(false);
    setEntryPx("");
    setLots("1");
  }, [signal?.id]);

  const expired = countdown === "expired" || (signal ? signal.state !== "active" : false);

  // How long ago the card was PRICED. The Kite hand-off refuses anything older
  // than 15 min because its entry zone was priced off a stale premium; a
  // positional card stays "active" far longer, so it can look orderable while
  // the hand-off rejects it. Surface that, with a one-click re-price.
  const [nowSec, setNowSec] = useState(() => Math.floor(Date.now() / 1000));
  useEffect(() => {
    const id = setInterval(() => setNowSec(Math.floor(Date.now() / 1000)), 5000);
    return () => clearInterval(id);
  }, []);
  const [repricing, setRepricing] = useState(false);
  const pricedAgoMin = signal ? Math.max(0, Math.floor((nowSec - signal.created_at) / 60)) : 0;
  const STALE_MIN = 15; // must match _MAX_CARD_AGE_S on the backend

  const reprice = async () => {
    if (!signal) return;
    setRepricing(true);
    try {
      const res = await api.repriceSignal(symbol, mode);
      // Refresh re-validates the live score first: if the setup no longer
      // qualifies the signal is CLOSED, not re-priced. Say so — a silently
      // vanishing card would be worse than the stale one.
      if (res.status === "closed") {
        alert(res.reason ?? "Setup no longer valid — signal closed.");
      }
      onReprice?.(); // bump the parent poll so the fresh (or now-absent) card shows
    } catch (e) {
      alert(e instanceof Error ? e.message : "could not refresh the price");
    } finally {
      setRepricing(false);
    }
  };

  /**
   * Hand the order to Kite pre-filled. Tradewell places nothing — this opens
   * Zerodha's basket screen in a new tab where YOU review and confirm.
   * Confirms the quantity first, because a mis-set lots field is the one
   * mistake this button makes faster.
   */
  const tradeInKite = async () => {
    if (!signal || expired) return;
    const lotsN = parseNum(lots);
    if (lotsN === null || lotsN < 1 || !Number.isInteger(lotsN)) {
      alert("Enter a valid whole lot count (1 or more)");
      return;
    }
    // Confirm BEFORE fetching. Confirming after would let the dialog sit open
    // indefinitely while the card expires or the bias flips behind it — the
    // backend guards would have passed minutes earlier, against a card that no
    // longer exists. This way validation runs immediately before the POST.
    const id = signal.id;
    if (
      !window.confirm(
        `Open a Kite order for ${signal.contract}?\n\n` +
          `${lotsN} lot(s) · BUY · LIMIT ₹${fmt(signal.entry_high)}\n\n` +
          `Tradewell places nothing — you review and confirm in Kite.`,
      )
    ) {
      return;
    }
    setBusy(true);
    try {
      const payload = await fetchKiteBasket(symbol, mode, lotsN, id);
      submitKiteBasket(payload);
    } catch (e) {
      alert(e instanceof Error ? e.message : "could not prepare the Kite order");
    } finally {
      setBusy(false);
    }
  };

  const enter = async () => {
    if (!signal || expired) return;
    const lotsN = parseNum(lots);
    if (lotsN === null || lotsN < 1 || !Number.isInteger(lotsN)) {
      alert("Enter a valid whole lot count (1 or more)");
      return;
    }
    let premium: number | undefined;
    if (entryPx.trim()) {
      const p = parseNum(entryPx);
      if (p === null || p <= 0) {
        alert("Enter a valid entry premium");
        return;
      }
      premium = p;
    }
    // No typed premium → send none, so the backend books your LIVE fill
    // (typed > live > signal reference).
    setBusy(true);
    try {
      await api.enterTrade(symbol, mode, lotsN, premium, signal.id);
      setEntering(false);
      setEntryPx("");
      onEntered();
    } catch (e) {
      alert(e instanceof Error ? e.message : "could not enter trade");
    } finally {
      setBusy(false);
    }
  };

  if (symbol !== "NIFTY") {
    return (
      <div className={`card p-4 text-sm text-muted ${className}`}>
        Signals run on <span className="text-white">NIFTY</span> only in Phase 2. Switch to NIFTY to see setups.
      </div>
    );
  }

  // No active signal → no-trade / wait state.
  if (!signal) {
    const action = data?.action ?? "avoid";
    const wait = action === "wait";
    return (
      <div className={`card overflow-y-auto scroll-thin p-4 ${className}`}>
        {error && !data && (
          <div className="mb-2 text-xs text-bear">Signal feed unavailable — {error}</div>
        )}
        <div className="flex items-center gap-2">
          <span className={`tag ${wait ? "bg-yellow-500/15 text-yellow-400" : "bg-panel2 text-muted"}`}>
            {wait ? "WAIT FOR CONFIRMATION" : "NO TRADE"}
          </span>
          {data?.status && <span className="text-sm text-muted">{data.status.regime_label}</span>}
        </div>
        <p className="mt-2 text-sm text-white/80">
          {data?.no_trade_reason ?? "No valid setup right now."}
        </p>
        {data?.score && (
          <div className="mt-3">
            <div className="mb-1 text-[10px] uppercase text-muted">
              Best-direction score ({data.score.direction}) · {data.score.total.toFixed(0)}/100
            </div>
            <ScoreBars score={data.score} />
          </div>
        )}
      </div>
    );
  }

  // The premium the SL/targets were computed from — the base for the GTT %.
  const entryRef = signal.ref_entry_premium ?? signal.entry_high;
  const ce = signal.direction === "CE";
  const dirColor = ce ? "text-bull" : "text-bear";
  const dirBg = ce ? "bg-bull/15 text-bull" : "bg-bear/15 text-bear";
  const conf = signal.confidence;
  const confLabel = conf >= 80 ? "strong" : conf >= 70 ? "valid" : "wait";
  const border = ce ? "border-bull/40" : "border-bear/40";

  return (
    // Pinned shell: header, contract, key numbers and the action row never
    // scroll away — only the rationale (invalidation / reasons / score) does.
    // That guarantees entry zone, SL, targets and the Enter button are on
    // screen whatever the engine emits.
    <div className={`card flex flex-col overflow-hidden border ${border} ${className}`}>
      {/* header */}
      <div className="flex shrink-0 flex-wrap items-center gap-2 px-3 pt-3">
        <span className={`tag ${dirBg} text-xs`}>{ce ? "BUY CE" : "BUY PE"}</span>
        <span className="tag bg-panel2 text-muted">{signal.mode === "positional" ? "Positional" : "Intraday"}</span>
        <span className="text-sm font-semibold">{signal.title}</span>
        <div className="ml-auto flex items-center gap-3">
          <div className="text-right">
            <div className={`font-mono text-lg leading-none ${dirColor}`}>{conf.toFixed(0)}</div>
            <div className="text-[9px] uppercase text-muted">{confLabel}</div>
          </div>
          <div className="text-right">
            <div className="font-mono text-sm leading-none">{countdown ?? "—"}</div>
            <div className="text-[9px] uppercase text-muted">valid</div>
          </div>
        </div>
      </div>

      <div className="mt-2 flex shrink-0 items-center gap-2 px-3 text-sm">
        <span className="font-mono font-medium">{signal.contract}</span>
        {signal.expiry && <span className="text-[11px] text-muted">exp {signal.expiry}</span>}
        {signal.expiry === istToday() && (
          <span
            className="tag bg-yellow-500/15 text-yellow-400"
            title="This contract expires TODAY — gamma and theta are extreme; premiums move much faster than usual"
          >
            0DTE
          </span>
        )}
        {/* Pricing freshness + one-click re-price. Only shown once the pricing
            is old enough to matter; goes amber past the hand-off's 15-min cutoff. */}
        {!expired && pricedAgoMin >= 5 && (
          <span className="ml-auto flex items-center gap-1.5">
            <span
              className={`text-[10px] ${pricedAgoMin >= STALE_MIN ? "text-yellow-400" : "text-muted"}`}
              title={
                pricedAgoMin >= STALE_MIN
                  ? `Priced ${pricedAgoMin}m ago — too old to place in Kite. Refresh to re-price against the current premium.`
                  : `Priced ${pricedAgoMin}m ago`
              }
            >
              priced {pricedAgoMin}m ago
            </span>
            <button
              onClick={reprice}
              disabled={repricing}
              title="Re-price this trade against the current premium — same strike, today's price"
              className={`rounded border px-1.5 py-0.5 text-[10px] transition disabled:opacity-40 ${
                pricedAgoMin >= STALE_MIN
                  ? "border-yellow-500/60 bg-yellow-500/15 text-yellow-400 hover:bg-yellow-500/25"
                  : "border-edge bg-panel text-muted hover:text-white"
              }`}
            >
              {repricing ? "…" : "↻ Refresh"}
            </button>
          </span>
        )}
      </div>

      {/* key numbers — pinned */}
      <div className="mt-2 grid shrink-0 grid-cols-3 gap-1.5 px-3">
        <Stat label="Entry zone" value={`₹${fmt(signal.entry_low)}–₹${fmt(signal.entry_high)}`} />
        <Stat
          // "planned" is load-bearing: no stop order is placed, so this is a
          // level you must act on, not a floor the market guarantees.
          label="Stop-loss (planned)"
          value={`₹${fmt(signal.premium_sl)}`}
          tone="text-bear"
          pct={pctFrom(signal.premium_sl, entryRef)}
        />
        <Stat label="Risk:Reward" value={`1:${signal.risk_reward}`} />
        {signal.quick_target && (
          <Stat
            // The level that turns a spike-and-fade into a scratch instead of a
            // loss: book half, stop to entry, let the rest run to T1/T2.
            label="Book ½ at"
            value={`₹${fmt(signal.quick_target)}`}
            tone="text-accent"
            pct={pctFrom(signal.quick_target, entryRef)}
          />
        )}
        <Stat
          label="Target 1"
          value={`₹${fmt(signal.target1)}`}
          tone="text-bull"
          pct={pctFrom(signal.target1, entryRef)}
        />
        <Stat
          label="Target 2"
          value={`₹${fmt(signal.target2)}`}
          tone="text-bull"
          pct={pctFrom(signal.target2, entryRef)}
        />
        {/* Strike is already in the contract line above ("NIFTY 24800 CE") —
            the tile is spent here on rupee outcomes instead. */}
      </div>

      {/* Rupees at stake, pinned. A risk warning that can scroll away is not a
          warning, so this sits in the shrink-0 shell with the action row. */}
      <div className="mt-2 shrink-0 px-3">
        <RiskVisualizer
          signal={signal}
          lots={Math.max(1, Math.floor(parseNum(lots) ?? 1))}
          onLots={(n) => setLots(String(n))}
          entryOverride={parseNum(entryPx)}
        />
      </div>

      {/* ---- everything below scrolls ---- */}
      <div className="mt-2 min-h-0 flex-1 overflow-y-auto scroll-thin px-3">
      {/* invalidation */}
      <div className="rounded-md border border-edge bg-panel2 px-3 py-2">
        <div className="text-[10px] uppercase tracking-wide text-muted">Underlying confirmation</div>
        <div className="text-sm">{signal.underlying_invalidation}</div>
        <div className="mt-0.5 text-[11px] text-muted">{signal.invalidation_note}</div>
        <div className="mt-1 text-[11px] text-muted">↳ {signal.trailing_sl_rule}</div>
      </div>

      {/* reasons */}
      {signal.reasons.length > 0 && (
        <ul className="mt-3 space-y-0.5">
          {signal.reasons.map((r, i) => (
            <li key={i} className="flex gap-1.5 text-[12px] text-white/80">
              <span className={dirColor}>•</span>
              <span>{r}</span>
            </li>
          ))}
        </ul>
      )}

      {/* score */}
      <div className="mt-3 border-t border-edge pt-3">
        <div className="mb-1.5 text-[10px] uppercase text-muted">Score breakdown · {conf.toFixed(0)}/100</div>
        <ScoreBars score={signal.score} />
      </div>
      </div>
      {/* ---- end scroll region ---- */}

      {/* manual tracking — pinned to the bottom */}
      <div className="shrink-0 border-t border-edge px-3 py-2">
        {expired ? (
          <div className="text-xs text-muted">
            Entry window closed — wait for the next signal.
          </div>
        ) : !entering ? (
          <div className="flex items-center gap-2">
            <button
              onClick={() => setEntering(true)}
              className="rounded-md bg-bull px-3 py-1.5 text-xs font-medium text-black hover:opacity-90"
            >
              Mark as Entered
            </button>
            <button
              onClick={() => onIgnore(signal.id)}
              className="rounded-md bg-panel2 px-3 py-1.5 text-xs text-muted hover:text-white"
            >
              Ignore
            </button>
            <span className="text-[10px] text-muted">tracks your manual fill — no order is placed</span>
          </div>
        ) : (
          <div className="flex flex-wrap items-end gap-2">
            <label className="text-[10px] text-muted">
              Lots
              <input
                value={lots}
                onChange={(e) => setLots(e.target.value)}
                inputMode="numeric"
                className="mt-0.5 block w-16 rounded border border-edge bg-panel2 px-2 py-1 font-mono text-xs outline-none focus:border-accent"
              />
            </label>
            <label className="text-[10px] text-muted">
              Entry ₹
              {/* Deliberately NOT prefilled: an untouched field books your LIVE
                  premium; typing a price overrides it with your actual fill. */}
              <input
                value={entryPx}
                onChange={(e) => setEntryPx(e.target.value)}
                inputMode="decimal"
                placeholder="live fill"
                title={`Leave blank to record the live premium (zone ₹${fmt(signal.entry_low)}–₹${fmt(signal.entry_high)})`}
                className="mt-0.5 block w-24 rounded border border-edge bg-panel2 px-2 py-1 font-mono text-xs outline-none placeholder:text-muted/60 focus:border-accent"
              />
            </label>
            <button
              onClick={enter}
              disabled={busy}
              className="rounded-md bg-bull px-3 py-1.5 text-xs font-medium text-black disabled:opacity-40"
            >
              {busy ? "…" : "Confirm"}
            </button>
            {/* Hands the order to Kite pre-filled — you still confirm there. */}
            <button
              onClick={tradeInKite}
              disabled={busy}
              title="Open this order pre-filled in Kite — you review and confirm there. Tradewell never places orders."
              className="rounded-md border border-accent/60 bg-accent/15 px-3 py-1.5 text-xs font-medium text-accent disabled:opacity-40"
            >
              Trade in Kite ↗
            </button>
            <button onClick={() => setEntering(false)} className="rounded-md bg-panel2 px-3 py-1.5 text-xs text-muted">
              Cancel
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
