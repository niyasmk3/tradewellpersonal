// Beginner-readable descriptions for the candlestick patterns the backend
// detects (app/patterns/detect.py — keep the keys in sync). Each entry says
// what the shape IS and which way the TEXTBOOK expects price to move next.
//
// The honesty layer lives next to it in the UI: the "history says" column is
// what NIFTY 5-min actually did over 3 years, and it frequently disagrees
// with the textbook — a hit rate below ~45% means the pattern usually
// resolved the OTHER way here. Teach the classic meaning, trust the column.

export type PatternInfo = {
  /** Textbook direction of the expected move. */
  dir: "up" | "down" | "neutral";
  /** One or two short sentences a beginner can read at a glance. */
  desc: string;
};

export const PATTERN_INFO: Record<string, PatternInfo> = {
  doji: {
    dir: "neutral",
    desc: "Open and close are almost equal — a tug-of-war with no winner. Signals indecision, not direction.",
  },
  spinning_top: {
    dir: "neutral",
    desc: "Small body with wicks both sides — both buyers and sellers tried, neither won. Indecision.",
  },
  hammer: {
    dir: "up",
    desc: "After a dip: a long lower wick shows sellers pushed price down but buyers dragged it all the way back. Textbook: bounce UP.",
  },
  inverted_hammer: {
    dir: "up",
    desc: "After a dip: a long upper wick — an early attempt by buyers to lift price. Textbook: turn UP.",
  },
  hanging_man: {
    dir: "down",
    desc: "Hammer shape but after a rise — buyers had to rescue the bar once already. Textbook: warning of a drop DOWN.",
  },
  shooting_star: {
    dir: "down",
    desc: "After a rise: a long upper wick — the rally poked higher and was rejected. Textbook: turn DOWN.",
  },
  bull_marubozu: {
    dir: "up",
    desc: "A full-body green candle with almost no wicks — one-sided buying start to finish. Textbook: momentum carries UP.",
  },
  bear_marubozu: {
    dir: "down",
    desc: "A full-body red candle with almost no wicks — one-sided selling. Textbook: momentum carries DOWN.",
  },
  bull_engulfing: {
    dir: "up",
    desc: "A green candle completely swallows the previous red one — buyers overwhelmed the sellers. Textbook: move UP.",
  },
  bear_engulfing: {
    dir: "down",
    desc: "A red candle completely swallows the previous green one — sellers overwhelmed the buyers. Textbook: move DOWN.",
  },
  bull_harami: {
    dir: "up",
    desc: "A tiny candle held inside the previous big red one — the selling stalled. Textbook: turn UP.",
  },
  bear_harami: {
    dir: "down",
    desc: "A tiny candle held inside the previous big green one — the buying stalled. Textbook: turn DOWN.",
  },
  piercing_line: {
    dir: "up",
    desc: "After a red candle, a green one opens lower but closes past its midpoint — buyers fought back hard. Textbook: UP.",
  },
  dark_cloud_cover: {
    dir: "down",
    desc: "After a green candle, a red one opens higher but closes below its midpoint — sellers took over. Textbook: DOWN.",
  },
  tweezer_bottom: {
    dir: "up",
    desc: "Two candles with matching lows — the same floor held twice in a row. Textbook: bounce UP off that floor.",
  },
  tweezer_top: {
    dir: "down",
    desc: "Two candles with matching highs — the same ceiling rejected price twice. Textbook: turn DOWN from that ceiling.",
  },
  morning_star: {
    dir: "up",
    desc: "Three steps: big red candle, tiny pause candle, then a strong green one — a turn built in stages. Textbook: UP.",
  },
  evening_star: {
    dir: "down",
    desc: "Three steps: big green candle, tiny pause, then a strong red one. Textbook: turn DOWN.",
  },
  three_white_soldiers: {
    dir: "up",
    desc: "Three strong green candles in a row, each closing higher — steady buying. Textbook: uptrend continues UP.",
  },
  three_black_crows: {
    dir: "down",
    desc: "Three strong red candles in a row, each closing lower — steady selling. Textbook: downtrend continues DOWN.",
  },
  inside_bar: {
    dir: "neutral",
    desc: "The whole bar fits inside the previous bar's range — the market is coiling. A breakout can go either way.",
  },
};

/** Lookup that tolerates unknown/renamed patterns. */
export function patternInfo(name: string): PatternInfo | null {
  return PATTERN_INFO[name] ?? null;
}
