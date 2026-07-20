import { API_BASE, TradingMode } from "./api";

export interface BasketPayload {
  url: string;
  api_key: string;
  data: string;
  summary: string;
  tradingsymbol: string;
  quantity: number;
  /** Present when the hand-off needs an explicit caution before submitting. */
  warning?: string | null;
}

/**
 * Kite Publisher hand-off. Kite requires a real browser form POST (it is an
 * offsite-order flow, not a JSON API), so we build a hidden form and submit it.
 *
 * This does NOT place an order. It opens Zerodha's basket screen with the order
 * pre-filled; the user reviews and confirms there. `target="_blank"` keeps the
 * dashboard alive so live signals and open positions stay on screen.
 */
export function submitKiteBasket(p: BasketPayload) {
  const form = document.createElement("form");
  form.method = "POST";
  form.action = p.url;
  form.target = "_blank";
  form.rel = "noopener";
  form.style.display = "none";

  const add = (name: string, value: string) => {
    const input = document.createElement("input");
    input.type = "hidden";
    input.name = name;
    input.value = value;
    form.appendChild(input);
  };
  add("api_key", p.api_key);
  add("data", p.data);

  document.body.appendChild(form);
  form.submit();
  document.body.removeChild(form);
}

export async function fetchKiteBasket(
  symbol: string,
  mode: TradingMode,
  lots: number,
  signalId: string,
): Promise<BasketPayload> {
  const qs = new URLSearchParams({
    symbol,
    mode,
    lots: String(lots),
    signal_id: signalId,
  });
  const res = await fetch(`${API_BASE}/kite/basket?${qs}`, { cache: "no-store" });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(typeof body?.detail === "string" ? body.detail : `Kite basket failed (${res.status})`);
  }
  return res.json();
}

/**
 * The protective stop for a position you already hold.
 *
 * Deliberately a separate call from `fetchKiteBasket`, and keyed on a TRADE
 * rather than a signal: the order it returns is a SELL, and a SELL with no long
 * behind it opens a naked short. The backend refuses closed positions for the
 * same reason — this is not a variant of the entry basket, it is a different
 * and more dangerous instrument.
 */
export async function fetchKiteProtect(tradeId: string): Promise<BasketPayload> {
  const qs = new URLSearchParams({ trade_id: tradeId });
  const res = await fetch(`${API_BASE}/kite/protect?${qs}`, { cache: "no-store" });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(typeof body?.detail === "string" ? body.detail : `Could not build the stop order (${res.status})`);
  }
  return res.json();
}
