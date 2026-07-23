export const fmt = (n: number | null | undefined, dp = 2): string =>
  n === null || n === undefined || Number.isNaN(n)
    ? "—"
    : n.toLocaleString("en-IN", { minimumFractionDigits: dp, maximumFractionDigits: dp });

export const fmtInt = (n: number | null | undefined): string =>
  n === null || n === undefined || Number.isNaN(n)
    ? "—"
    : Math.round(n).toLocaleString("en-IN");

// Compact large numbers (OI / volume): 12.3L, 4.5K
export const compact = (n: number | null | undefined): string => {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  const a = Math.abs(n);
  if (a >= 1e7) return (n / 1e7).toFixed(2) + "Cr";
  if (a >= 1e5) return (n / 1e5).toFixed(2) + "L";
  if (a >= 1e3) return (n / 1e3).toFixed(1) + "K";
  return String(Math.round(n));
};

export const signed = (n: number | null | undefined, dp = 2): string => {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return (n >= 0 ? "+" : "") + fmt(n, dp);
};

// Parse a user-typed number, tolerating Indian grouping ("1,200"). Returns null
// for blank/invalid input so callers can reject rather than silently coerce to NaN.
export const parseNum = (s: string): number | null => {
  const cleaned = s.replace(/,/g, "").trim();
  if (!cleaned) return null;
  const n = Number(cleaned);
  return Number.isFinite(n) ? n : null;
};

// Today's date in IST as "YYYY-MM-DD" (matches backend expiry strings).
/** Move from `base` to `value` as a signed %, e.g. "−18%" / "+27%".
 *  Kite's GTT stop-loss / target boxes are entered in percent, so the card
 *  shows both the ₹ level and the % you'd type there. */
export const pctFrom = (value: number | null | undefined, base: number | null | undefined): string => {
  if (value == null || base == null || !base) return "—";
  const p = ((value - base) / base) * 100;
  return `${p >= 0 ? "+" : "−"}${Math.abs(p).toFixed(0)}%`;
};

export const istToday = (): string =>
  new Date(Date.now() + 5.5 * 3600 * 1000).toISOString().slice(0, 10);

/** IST calendar-day bucket for an epoch — the grouping key for the journal. */
export const istDayKey = (epoch: number | null | undefined): number =>
  epoch ? Math.floor((epoch + 19800) / 86400) : 0;

/** "Mon 20 Jul 2026" — a journal day heading. */
export const istDayLabel = (epoch: number | null | undefined): string => {
  if (!epoch) return "Undated";
  return new Date(epoch * 1000).toLocaleDateString("en-IN", {
    weekday: "short",
    day: "2-digit",
    month: "short",
    year: "numeric",
    timeZone: "Asia/Kolkata",
  });
};

export const istDate = (epoch: number | null | undefined): string => {
  if (!epoch) return "—";
  return new Date(epoch * 1000).toLocaleDateString("en-IN", {
    day: "2-digit",
    month: "short",
    timeZone: "Asia/Kolkata",
  });
};

/** "22 Jul 12:03:04" — when the row can span days, the time alone is ambiguous. */
export const istDateTime = (epoch: number | null | undefined): string => {
  if (!epoch) return "—";
  return `${istDate(epoch)} ${istTime(epoch)}`;
};

export const istTime = (epoch: number | null | undefined): string => {
  if (!epoch) return "—";
  return new Date(epoch * 1000).toLocaleTimeString("en-IN", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    timeZone: "Asia/Kolkata",
  });
};
