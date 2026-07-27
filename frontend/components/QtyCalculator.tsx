"use client";

import { useEffect, useMemo, useState } from "react";
import { API_BASE } from "@/lib/api";
import { parseNum } from "@/lib/format";

/**
 * Lot/quantity calculator — the "tradewell Qty.xlsx" sheet as a live tab.
 *
 * Mirrors the sheet's math exactly:
 *   raw shares = fund ÷ premium          (B4 = B3/B2)
 *   lots       = TRUNC(raw ÷ lot size)   (B5) — floor, a partial lot can't be bought
 *   quantity   = lots × lot size         (B6)
 * plus the two numbers those imply: rupees deployed and fund left unused.
 *
 * The sheet labelled the ₹63 input "PE/CE strike value"; it is the option
 * PREMIUM (price per share), so it is labelled that way here — the strike
 * itself never enters this arithmetic. Same formula the signal cards use for
 * their fund prefill; this tab is the manual what-if version.
 */
const inr = (n: number, dp = 0) =>
  n.toLocaleString("en-IN", { minimumFractionDigits: dp, maximumFractionDigits: dp });

function Row({ label, value, strong, title }: {
  label: string; value: string; strong?: boolean; title?: string;
}) {
  return (
    <div className="flex items-baseline justify-between gap-2" title={title}>
      <span className="text-[11px] text-muted">{label}</span>
      <span className={`font-mono ${strong ? "text-base text-white" : "text-xs text-white/80"}`}>
        {value}
      </span>
    </div>
  );
}

export function QtyCalculator() {
  const [premium, setPremium] = useState("");
  const [fund, setFund] = useState("");
  const [lot, setLot] = useState("65");
  const [fundPrefilled, setFundPrefilled] = useState(false);

  // Prefill the fund from Trading settings (the day's deployable budget) the
  // first time, if one is set — still fully editable. Never overwrite typing.
  useEffect(() => {
    fetch(`${API_BASE}/settings/risk-limits`, { cache: "no-store" })
      .then((r) => r.json())
      .then((v) => {
        const tf = (v?.fields ?? []).find((f: { key: string }) => f.key === "trading_fund");
        if (tf?.value > 0) {
          setFund((cur) => (cur === "" ? String(tf.value) : cur));
          setFundPrefilled(true);
        }
      })
      .catch(() => {});
  }, []);

  const out = useMemo(() => {
    const p = parseNum(premium);
    const f = parseNum(fund);
    const l = parseNum(lot);
    if (p === null || f === null || l === null || p <= 0 || f <= 0 || l <= 0 || !Number.isInteger(l))
      return null;
    const rawShares = f / p;                     // B4
    const lots = Math.floor(rawShares / l);      // B5 (TRUNC)
    const qty = lots * l;                        // B6
    const deployed = qty * p;
    return { rawShares, lots, qty, deployed, unused: f - deployed, perLot: p * l };
  }, [premium, fund, lot]);

  const input =
    "w-full rounded border border-edge bg-panel px-2 py-1 text-right font-mono text-xs outline-none focus:border-accent";

  return (
    <div className="flex h-full flex-col gap-2 overflow-y-auto scroll-thin p-2 text-sm">
      <div className="grid grid-cols-3 gap-2">
        <label className="block">
          <span className="text-[10px] uppercase text-muted">Premium ₹/share</span>
          <input value={premium} onChange={(e) => setPremium(e.target.value)}
                 inputMode="decimal" placeholder="63" className={input} />
        </label>
        <label className="block">
          <span className="text-[10px] uppercase text-muted">Fund ₹</span>
          <input value={fund} onChange={(e) => setFund(e.target.value)}
                 inputMode="decimal" placeholder="19,00,000" className={input} />
        </label>
        <label className="block">
          <span className="text-[10px] uppercase text-muted">Lot size</span>
          <input value={lot} onChange={(e) => setLot(e.target.value)}
                 inputMode="numeric" className={input} />
        </label>
      </div>
      {fundPrefilled && (
        <div className="text-[10px] text-muted">fund prefilled from Trading settings — edit freely</div>
      )}

      {out ? (
        <div className="space-y-1.5 rounded-md border border-edge bg-panel2 p-3">
          <Row label="Lots (whole)" value={inr(out.lots)} strong
               title="floor(fund ÷ premium ÷ lot size) — a partial lot cannot be bought" />
          <Row label="Quantity (Kite qty box)" value={inr(out.qty)} strong
               title="lots × lot size — the number to type into Kite" />
          <div className="my-1 border-t border-edge/60" />
          <Row label="Raw shares (fund ÷ premium)" value={inr(out.rawShares, 2)} />
          <Row label="Cost of one lot" value={`₹${inr(out.perLot, 2)}`} />
          <Row label="Deployed (qty × premium)" value={`₹${inr(out.deployed, 2)}`} />
          <Row label="Fund left unused" value={`₹${inr(out.unused, 2)}`} />
        </div>
      ) : (
        <div className="rounded-md border border-edge bg-panel2 p-3 text-xs text-muted">
          Enter the option premium and your fund — lot size must be a whole number
          (NIFTY 65; check the card for BANKNIFTY/FINNIFTY).
        </div>
      )}

      <div className="text-[10px] leading-relaxed text-muted">
        Affordability only — how many lots the fund can BUY at this premium. It is not a
        risk suggestion (the card&apos;s suggested lots handles that) and ignores charges.
        Same formula as the card&apos;s fund prefill: lots = fund ÷ (premium × lot size),
        floored to whole lots.
      </div>
    </div>
  );
}
