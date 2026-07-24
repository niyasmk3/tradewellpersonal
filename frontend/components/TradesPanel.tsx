"use client";

import { useMemo, useState } from "react";
import { Trade, TradeAction, api } from "@/lib/api";
import { fetchKiteProtect, submitKiteBasket } from "@/lib/kiteBasket";
import { fmt, istDate, istDayKey, istDayLabel, istTime, istToday, parseNum, pctFrom, signed } from "@/lib/format";
import { groupClosedByDay, returnPct } from "@/lib/tradeMath";

const REC_LABEL: Record<TradeAction, string> = {
  hold: "Hold",
  book_partial: "Book partial · trail rest",
  move_sl_entry: "Move SL to entry",
  trail_sl: "Trailing stop",
  exit: "Exit",
  target1_reached: "Target 1 reached",
  target2_reached: "Target 2 reached · book remaining",
  stop_loss_hit: "Stop-loss hit · exit",
  invalidated: "Invalidated · exit",
  time_exit: "Time exit · close position",
  stall_exit: "Stalled · no follow-through — consider exit",
};

function recTone(a: TradeAction): string {
  if (["stop_loss_hit", "invalidated", "exit", "time_exit"].includes(a)) return "bg-bear/15 text-bear";
  if (a === "stall_exit") return "bg-yellow-500/15 text-yellow-400";
  if (["target1_reached", "target2_reached", "book_partial"].includes(a)) return "bg-bull/15 text-bull";
  if (a === "trail_sl") return "bg-accent/15 text-accent";
  return "bg-panel2 text-muted";
}

const isOpen = (t: Trade) => t.status === "entered" || t.status === "partial";
const closedTodayEpoch = (ep: number | null) =>
  ep != null && new Date((ep + 19800) * 1000).toISOString().slice(0, 10) === istToday();

/** Realized P&L: booked today, and lifetime across the whole journal. */
export function realizedSummary(trades: Trade[]) {
  const total = trades.reduce((s, t) => s + (t.realized_pnl || 0), 0);
  const today = trades.reduce(
    (s, t) => s + (closedTodayEpoch(t.exited_at) ? t.realized_pnl || 0 : 0),
    0,
  );
  return { today, total };
}

function TradeCard({ t, onChange }: { t: Trade; onChange: () => void }) {
  const [exitPx, setExitPx] = useState<string>("");
  const [busy, setBusy] = useState(false);
  const ce = t.direction === "CE";
  const pnl = t.pnl ?? 0;
  const pnlTone = pnl >= 0 ? "text-bull" : "text-bear";

  /**
   * Hand the protective stop to Kite. Confirms first and quotes the exact
   * quantity, because this is a SELL: submitted without the matching long it
   * would open a short position rather than close one.
   */
  const protectInKite = async () => {
    setBusy(true);
    try {
      const payload = await fetchKiteProtect(t.id);
      if (
        window.confirm(
          `${payload.summary}\n\n${payload.warning ?? ""}\n\n` +
            "Tradewell places nothing — you review and confirm in Kite.",
        )
      ) {
        submitKiteBasket(payload);
      }
    } catch (e) {
      alert(e instanceof Error ? e.message : "could not build the stop order");
    } finally {
      setBusy(false);
    }
  };

  const act = async (fn: (px?: number) => Promise<unknown>) => {
    let px: number | undefined;
    if (exitPx.trim()) {
      const p = parseNum(exitPx);
      if (p === null || p < 0) {
        alert("Enter a valid exit price");
        return;
      }
      px = p;
    }
    setBusy(true);
    try {
      await fn(px);
      // Clear the typed price after every successful action — otherwise a price
      // typed for "Book ½" silently becomes the Exit fill hours later.
      setExitPx("");
      onChange();
    } catch (e) {
      alert(e instanceof Error ? e.message : "action failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="rounded-md border border-edge bg-panel2 p-2">
      <div className="flex items-center gap-2">
        <span className={`tag ${ce ? "bg-bull/15 text-bull" : "bg-bear/15 text-bear"}`}>{t.direction}</span>
        <span className="font-mono text-xs">{t.contract}</span>
        <span className="font-mono text-[10px] text-muted" title={`Entered ${istTime(t.entered_at)} IST`}>
          {istTime(t.entered_at)}
        </span>
        {t.status === "partial" && <span className="tag bg-yellow-500/15 text-yellow-400">partial</span>}
        {/* Confirmed against Zerodha's own position book — the difference
            between "we think you hold this" and "you do". */}
        {t.broker_qty ? (
          <span
            className="tag bg-bull/15 text-bull"
            title={`Zerodha's position book shows ${t.broker_qty} qty${
              t.broker_qty !== t.quantity ? ` — the journal says ${t.quantity}` : ""
            }`}
          >
            {t.broker_qty === t.quantity ? "✓ Kite" : `⚠ Kite ${t.broker_qty}`}
          </span>
        ) : (
          <span className="tag bg-panel2 text-muted" title="Not yet seen in Zerodha's position book">
            unconfirmed
          </span>
        )}
        <div className="ml-auto text-right">
          <div className={`font-mono text-sm ${pnlTone}`}>₹{signed(pnl, 0)}</div>
          <div className={`text-[10px] ${pnlTone}`}>{signed(t.pnl_pct, 1)}%</div>
        </div>
      </div>

      <div className="mt-1.5 grid grid-cols-3 gap-x-2 gap-y-0.5 font-mono text-[11px]">
        <span className="text-muted">In <span className="text-white">₹{fmt(t.entry_premium)}</span></span>
        <span className="text-muted">LTP <span className="text-white">₹{fmt(t.current_premium)}</span></span>
        <span className="text-muted">Qty <span className="text-white">{t.quantity}</span></span>
        <span className="text-muted">SL <span className="text-bear">₹{fmt(t.trailing_sl)}</span></span>
        <span className="text-muted" title={t.t0_hit ? "Booked/risk-free — stop is at entry" : "Book half here; stop moves to entry"}>
          ½ <span className={t.t0_hit ? "text-bull" : "text-accent"}>
            {t.quick_target ? `₹${fmt(t.quick_target)}` : "—"}{t.t0_hit ? " ✓" : ""}
          </span>
        </span>
        <span className="text-muted">T1 <span className="text-bull">₹{fmt(t.target1)}</span></span>
        <span className="text-muted">T2 <span className="text-bull">₹{fmt(t.target2)}</span></span>
      </div>

      {/* Percentages relative to YOUR fill — what Kite's GTT boxes ask for. */}
      <div className="mt-1 flex gap-3 font-mono text-[10px] text-muted" title="Percent move from your entry — paste into Kite's GTT stop-loss / target fields">
        <span>GTT:</span>
        <span className="text-bear">SL {pctFrom(t.trailing_sl, t.entry_premium)}</span>
        <span className="text-bull">T1 {pctFrom(t.target1, t.entry_premium)}</span>
        <span className="text-bull">T2 {pctFrom(t.target2, t.entry_premium)}</span>
      </div>

      <div className={`mt-1.5 rounded px-2 py-1 text-[11px] ${recTone(t.recommendation)}`}>
        {REC_LABEL[t.recommendation]}
        {t.recommendation_note ? ` — ${t.recommendation_note}` : ""}
      </div>

      {/* Sticky invalidation: the thesis broke and nobody has owned the
          decision to keep holding. The banner (and the phone nag) persist
          until an explicit acknowledgment — silence must not read as consent. */}
      {t.invalidation_fired_at != null &&
        (t.invalidation_ack_at == null || t.invalidation_ack_at <= t.invalidation_fired_at) && (
          <div className="mt-1.5 flex items-center gap-2 rounded border border-bear/50 bg-bear/10 px-2 py-1.5 text-[11px] text-bear">
            <span className="min-w-0 flex-1">
              Thesis broke at {istTime(t.invalidation_fired_at)} — exit, or hold by explicit choice.
            </span>
            <button
              disabled={busy}
              onClick={async () => {
                setBusy(true);
                try {
                  await api.ackInvalidation(t.id);
                  onChange();
                } catch (e) {
                  alert(e instanceof Error ? e.message : "could not acknowledge");
                } finally {
                  setBusy(false);
                }
              }}
              className="shrink-0 rounded border border-bear/60 bg-panel2 px-2 py-0.5 text-[10px] text-white hover:border-bear disabled:opacity-50"
              title="Keep holding against the broken thesis — recorded in the journal, stops the re-alerts"
            >
              acknowledge &amp; hold
            </button>
          </div>
        )}

      {/* The stop only caps a loss if it exists as a real order in the market.
          Tradewell places none, so this hands the SL to Kite pre-filled — one
          click, at the moment the position is actually held. */}
      <div className="mt-1.5 flex items-center gap-2">
        <button
          onClick={protectInKite}
          disabled={busy}
          title="Open a stop-loss SELL order for this position in Kite, pre-filled at your current stop. You review and confirm there."
          className="flex-1 rounded border border-bear/60 bg-bear/10 px-2 py-1 text-[11px] font-medium text-bear hover:bg-bear/20 disabled:opacity-40"
        >
          🛡 Place stop in Kite ↗ <span className="font-mono">₹{fmt(t.trailing_sl)}</span>
        </button>
      </div>

      <div className="mt-1.5 flex items-center gap-2">
        <input
          value={exitPx}
          onChange={(e) => setExitPx(e.target.value)}
          inputMode="decimal"
          placeholder={t.current_premium != null ? `₹${fmt(t.current_premium)}` : "exit ₹"}
          className="w-20 rounded border border-edge bg-panel px-2 py-0.5 font-mono text-xs outline-none focus:border-accent"
        />
        <button
          onClick={() => act((px) => api.partialTrade(t.id, px))}
          disabled={busy || t.lots < 2}
          title={t.lots < 2 ? "Need at least 2 lots to book partial" : undefined}
          className="rounded bg-panel px-2 py-0.5 text-xs text-white hover:bg-edge disabled:opacity-40"
        >
          Book ½
        </button>
        <button
          onClick={() => act((px) => api.exitTrade(t.id, px))}
          disabled={busy}
          className="rounded bg-bear/80 px-2.5 py-0.5 text-xs font-medium text-white hover:bg-bear disabled:opacity-40"
        >
          Exit
        </button>
      </div>
    </div>
  );
}

/** OPEN positions only — rail 1, shown whenever money is at risk. */
export function TradesPanel({
  trades,
  error,
  onChange,
  className = "",
}: {
  trades: Trade[];
  error?: string | null;
  onChange: () => void;
  className?: string;
}) {
  const active = trades.filter(isOpen);
  const live = active.reduce((s, t) => s + (t.pnl ?? 0), 0);

  return (
    <div className={`card flex min-h-0 flex-col overflow-hidden ${className}`}>
      <div className="flex shrink-0 items-center justify-between border-b border-edge px-3 py-1.5">
        <h3 className="text-sm font-medium">
          Open Positions <span className="text-muted">({active.length})</span>
        </h3>
        {active.length > 0 && (
          <span className={`font-mono text-sm ${live >= 0 ? "text-bull" : "text-bear"}`}>
            ₹{signed(live, 0)}
          </span>
        )}
      </div>

      {error && (
        <div className="shrink-0 bg-bear/10 px-3 py-1 text-[11px] text-bear">
          Trades feed unreachable — showing last known state
        </div>
      )}

      <div className="min-h-0 flex-1 space-y-1.5 overflow-y-auto scroll-thin p-2">
        {active.map((t) => (
          <TradeCard key={t.id} t={t} onChange={onChange} />
        ))}
        {active.length === 0 && (
          <div className="py-2 text-center text-xs text-muted">No open positions.</div>
        )}
      </div>
    </div>
  );
}

/** Closed-trade journal — a review surface, so it lives in the context rail. */
/**
 * Why a closed trade ended, for the journal.
 *
 * `auto_close_reason` covers rows Tradewell closed itself, but a manually
 * recorded exit had nothing at all — the single most useful fact when reviewing
 * a losing day was missing. So fall back to the last meaningful advisory the
 * monitor emitted before the close, which is what the trader was looking at
 * when they acted.
 */
const EXIT_LABEL: Record<string, string> = {
  stop: "stop-loss hit",
  target1: "target 1 reached",
  target2: "target 2 reached",
  invalidation: "underlying invalidation",
  time_exit: "time exit",
  stall: "stalled — no follow-through",
  "broker flat": "closed at broker",
};

function exitReason(t: Trade): { text: string; tone: string; detail: string } | null {
  if (t.status === "ignored") return null;
  const note = (kind: string) =>
    [...t.events].reverse().find((e) => e.kind === kind)?.note ?? "";
  if (t.auto_close_reason) {
    return {
      text: EXIT_LABEL[t.auto_close_reason] ?? t.auto_close_reason,
      tone: ["target1", "target2"].includes(t.auto_close_reason)
        ? "bg-bull/15 text-bull"
        : "bg-bear/15 text-bear",
      detail: note("auto_closed") || `Auto-closed on ${t.auto_close_reason}`,
    };
  }
  // Manual exit: the last advisory event before it is the honest explanation.
  const advisory = [...t.events]
    .reverse()
    .find((e) => e.kind in EXIT_LABEL || ["stop_loss_hit", "invalidated", "target1", "target2_reached", "time_exit", "stall_exit"].includes(e.kind));
  if (advisory) {
    const map: Record<string, [string, string]> = {
      stop_loss_hit: ["stop-loss hit", "bg-bear/15 text-bear"],
      invalidated: ["underlying invalidation", "bg-bear/15 text-bear"],
      time_exit: ["time exit", "bg-panel2 text-muted"],
      stall_exit: ["stalled — no follow-through", "bg-yellow-500/15 text-yellow-400"],
      target1: ["target 1 reached", "bg-bull/15 text-bull"],
      target2_reached: ["target 2 reached", "bg-bull/15 text-bull"],
    };
    const hit = map[advisory.kind];
    if (hit) return { text: hit[0], tone: hit[1], detail: advisory.note };
  }
  return {
    text: "closed manually",
    tone: "bg-panel2 text-muted",
    detail: "You recorded this exit; the monitor had raised no exit advisory.",
  };
}

function JournalRow({ t, dayKey, onChange }: { t: Trade; dayKey: number; onChange?: () => void }) {
  const pct = returnPct(t.realized_pnl, t.entry_premium, t.initial_quantity, t.quantity);
  // A position opened on an earlier day is filed under the day it was BOOKED,
  // so its entry timestamp needs its own date or the row reads as same-day.
  const enteredElsewhere = istDayKey(t.entered_at) !== dayKey;
  const reason = exitReason(t);

  return (
    <div className="rounded border border-edge/60 bg-panel2 px-2 py-1.5 text-[11px]">
      <div className="flex items-center gap-2">
        <span className={`tag ${t.direction === "CE" ? "bg-bull/15 text-bull" : "bg-bear/15 text-bear"}`}>
          {t.direction}
        </span>
        <span className="truncate font-mono text-muted">{t.contract}</span>
        {/* WHY it ended — the first thing you want when reviewing a bad day. */}
        {reason && (
          <span className={`tag shrink-0 text-[9px] ${reason.tone}`} title={reason.detail}>
            {reason.text}
          </span>
        )}
        {t.status === "ignored" ? (
          <span className="ml-auto text-muted">ignored</span>
        ) : (
          <span className={`ml-auto font-mono ${t.realized_pnl >= 0 ? "text-bull" : "text-bear"}`}>
            ₹{signed(t.realized_pnl, 0)}
            {pct !== null && (
              <span
                className="ml-1 text-[10px] opacity-80"
                title={`Return on ₹${Math.round(
                  t.entry_premium * (t.initial_quantity || t.quantity),
                ).toLocaleString("en-IN")} deployed at entry (gross of charges)`}
              >
                {signed(pct, 1)}%
              </span>
            )}
          </span>
        )}
      </div>

      {/* An auto-close is Tradewell's bookkeeping, not a fill you reported —
          but WHERE the price came from matters: a broker-book close is your
          real fill, and calling it "estimated" taught distrust of true P&L. */}
      {t.auto_closed && (
        <div className="mt-1 flex items-center gap-1.5 rounded bg-yellow-500/10 px-1.5 py-0.5 text-[10px] text-yellow-400">
          <span>
            auto-closed on {t.auto_close_reason} ·{" "}
            {t.exit_price_source === "broker"
              ? "priced from Kite's day-average sell — your real fill"
              : t.exit_price_source === "simulated"
                ? "simulated fill"
                : "price estimated, no order was placed"}
          </span>
          <button
            onClick={async () => {
              if (!window.confirm("Reopen this position? Use it if you are still holding in Kite.")) return;
              try {
                await api.reopenTrade(t.id);
                onChange?.();
              } catch (e) {
                alert(e instanceof Error ? e.message : "could not reopen");
              }
            }}
            className="ml-auto shrink-0 underline decoration-dotted hover:text-white"
          >
            still holding?
          </button>
        </div>
      )}

      {/* A broker-flat close means YOU exited and Tradewell only found out —
          without your reason the expectancy-by-exit report cannot tell a
          disciplined broker-side stop from a fear exit. One tap answers it. */}
      {t.status === "exited" && t.auto_close_reason === "broker flat" && !t.exit_reason && (
        <div className="mt-1 flex flex-wrap items-center gap-1 rounded bg-accent/10 px-1.5 py-1 text-[10px]">
          <span className="text-white/80">why did you exit?</span>
          {["broker stop", "target hit", "fear / cut early", "better setup"].map((r) => (
            <button
              key={r}
              onClick={async () => {
                try {
                  await api.exitReason(t.id, r);
                  onChange?.();
                } catch (e) {
                  alert(e instanceof Error ? e.message : "could not save the reason");
                }
              }}
              className="rounded bg-panel2 px-1.5 py-0.5 text-muted hover:text-white"
            >
              {r}
            </button>
          ))}
          <button
            onClick={async () => {
              const r = window.prompt("Why did you exit?");
              if (!r?.trim()) return;
              try {
                await api.exitReason(t.id, r.trim());
                onChange?.();
              } catch (e) {
                alert(e instanceof Error ? e.message : "could not save the reason");
              }
            }}
            className="rounded bg-panel2 px-1.5 py-0.5 text-muted hover:text-white"
          >
            other…
          </button>
        </div>
      )}
      {t.exit_reason && (
        <div className="mt-0.5 text-[10px] text-muted">
          exit reason: <span className="text-white/80">{t.exit_reason}</span>
        </div>
      )}

      {t.status !== "ignored" && (
        // The audit trail: what you paid, what you got, when, and how long you
        // held it — so a trade can actually be reviewed after the fact.
        <div className="mt-0.5 flex flex-wrap items-center gap-x-2 font-mono text-[10px] text-muted">
          <span className={enteredElsewhere ? "text-yellow-400" : undefined}>
            {enteredElsewhere ? `${istDate(t.entered_at)} ` : ""}
            {istTime(t.entered_at)}
          </span>
          <span className="text-white/80">₹{fmt(t.entry_premium)}</span>
          <span>→</span>
          <span>{istTime(t.exited_at)}</span>
          <span className="text-white/80">₹{fmt(t.exit_premium)}</span>
          <span>· {t.quantity} qty</span>
          {t.exited_at && (
            <span>· held {Math.max(0, Math.round((t.exited_at - t.entered_at) / 60))}m</span>
          )}
        </div>
      )}
    </div>
  );
}

export function TradeJournal({ trades, onChange }: { trades: Trade[]; onChange?: () => void }) {
  const { today, total } = realizedSummary(trades);

  // Grouped by the day the trade was BOOKED (exited), not the day it was
  // opened. That is when the money became real, so each day's subtotal
  // reconciles with the "today" figure above it; an overnight position would
  // otherwise contribute P&L to a day whose total did not include it. Rows
  // entered on an earlier day flag their entry date so it stays unambiguous.
  const days = useMemo(
    () => groupClosedByDay(trades).map((d) => ({ ...d, label: istDayLabel(d.stamp) })),
    [trades],
  );

  if (trades.length === 0) {
    return <div className="p-3 text-xs text-muted">No trades logged yet.</div>;
  }

  return (
    <div className="p-2">
      <div className="mb-2 flex items-center justify-between px-1 text-[11px]">
        <span className="text-muted">Realized</span>
        <span className="font-mono">
          <span className={today >= 0 ? "text-bull" : "text-bear"}>today ₹{signed(today, 0)}</span>
          <span className="text-muted"> · total </span>
          <span className={total >= 0 ? "text-bull" : "text-bear"}>₹{signed(total, 0)}</span>
        </span>
      </div>

      {days.map((d) => (
        <section key={d.key} className="mb-3 last:mb-0">
          {/* Sticky so the day you are reading stays labelled while scrolling. */}
          <header className="sticky top-0 z-10 -mx-2 mb-1 flex items-center gap-2 border-b border-edge bg-panel px-3 py-1">
            <span className="text-[11px] font-medium text-white/90">{d.label}</span>
            <span className="text-[10px] text-muted">
              {d.rows.length} trade{d.rows.length === 1 ? "" : "s"}
              {d.wins + d.losses > 0 && ` · ${d.wins}W/${d.losses}L`}
            </span>
            <span className={`ml-auto font-mono text-[11px] ${d.realized >= 0 ? "text-bull" : "text-bear"}`}>
              ₹{signed(d.realized, 0)}
            </span>
          </header>
          <div className="space-y-1">
            {d.rows.map((t) => (
              <JournalRow key={t.id} t={t} dayKey={d.key} onChange={onChange} />
            ))}
          </div>
        </section>
      ))}

      {days.length === 0 && <div className="text-xs text-muted">Nothing closed yet.</div>}
    </div>
  );
}
