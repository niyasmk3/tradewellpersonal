# How to Read Tradewell Like It Reads the Market

*A plain-language guide. Keep it open beside the dashboard for the first week.*

---

## The 10-second read (do this every time you glance)

Look at **three things, top-left**, in order:

1. **REGIME** — *what kind of day the engine thinks it is.*
   "Strong Bullish", "Moderate Bullish", "Sideways"… This is its weather
   forecast. Sideways/compression = it won't even look for trades.

2. **The action chip** — *what it wants to do right now:*

   | Chip | Meaning in plain words |
   |---|---|
   | `NO TRADE` | "Nothing worth doing. I'm sitting on my hands." |
   | `WAIT FOR CONFIRMATION` | "I see something forming. Not convinced yet." |
   | A signal card appears | "NOW. Here's exactly what, where, and how much risk." |

3. **The score line** — *how close it is to acting.*
   "Setup forming — score 72 (needs 78)" reads as: **out of 100 marks,
   the setup scored 72, and the pass mark is 78.** Six marks short = no trade.
   The engine re-grades every 3 minutes when a candle closes.

That's the whole habit. Regime → chip → score. Ten seconds.

---

## The score is a recipe with 6 ingredients

A trade idea must score marks in six subjects — you can see each one
under the score (e.g. "Volume confirm... 15/15"):

| Ingredient | Max | Layman question it answers |
|---|---|---|
| Price action & structure | 25 | "Is the chart actually breaking out, or just wiggling?" |
| Trend & momentum | 20 | "Is the road sloping in our direction?" |
| Options & OI | 20 | "Are the big players positioned to agree with us?" |
| Volume confirmation | 15 | "Is there a real crowd behind this move, or is it empty noise?" |
| Volatility conditions | 10 | "Is the sea calm enough to sail?" |
| News sentiment | 10 | "Are today's headlines helping or fighting us?" |

A perfect chart with **no volume** is like a shop with bright lights and no
customers — the engine learned (from real losses) to refuse it. That's why
one weak ingredient can hold everything back even when the total looks close.

---

## The bouncers (why a good score can still get refused)

Even at 78+, a card must get past door security. Each bouncer exists because
a real losing day taught the owner a lesson:

| Bouncer | Plain meaning |
|---|---|
| **Rubber band (extension)** | If price is stretched more than ~3.5 ATRs above its average, buying now is chasing a rubber band at full stretch — it snaps back. *(This is what blocked today: stretch was +4.9.)* |
| **Volume floor** | No crowd, no trade — even if the chart looks perfect. |
| **OI floor** | If option positioning doesn't participate, the move is hollow. |
| **14:15 cutoff** | No NEW intraday bets after 14:15 — late-day entries lost 7 out of 7 in the audited week. |
| **Re-fire guard** | If a bet on a strike just lost, don't touch the same stove for 2 hours. |
| **Fresh price only** | If the option's quote is older than ~2 minutes, the entry zone may no longer exist — refuse. |
| **Post-gap quiet** | After a data blackout, wait 10 minutes before trusting the tape. |

**Key mindset:** "no signal" is not the engine sleeping. It is the engine
*working* — every 3 minutes it grades the market and says "not good enough."
The owner's profit came from the discipline of these refusals, not from
trading a lot.

---

## Market Pulse = the weather report (bottom-left)

This panel explains the "why" in numbers, and the sentence at the bottom
is the engine explaining itself in plain English — **always read that
sentence**; it's the layman translation you asked for. The numbers above it:

- **Range pos 99%** — price is at the very top of today's range
  (100% = the day's high). Buying the top of the range = buying the
  most expensive price of the day.
- **VWAP stretch +4.9 ATR** — the rubber band number. How far price has
  run above its day-average. Past ~3.5, the engine refuses to chase.
- **Range used 83%** — 83% of a normal day's travel is already spent.
  The fuel tank is mostly empty.
- **Volume 1.2×** — crowd size vs normal. Below ~1× = thin, suspicious.
- **PCR** — put/call ratio, the mood of option writers. ~1 = balanced.

---

## The score-trend mini chart

Green line = the bullish (CE) score over time. Red = bearish (PE).
Dotted line = the pass mark. You can *watch a trade forming*: when the green
line climbs toward the dotted line, the engine is warming up. That's your cue
to pay attention — before any card exists.

---

## When a card finally appears

It answers every question in one box, top to bottom:

- **Contract** — what to buy (e.g. "NIFTY 24700 CE").
- **Entry zone** — the price band it considers fair. Outside it, don't chase.
- **SL (stop-loss)** — "if it falls here, the idea is wrong; out, no debate."
- **T1 / T2** — first and second profit targets. T1 is the honest one.
- **Validity** — cards expire (≈8 min intraday). An expired card is dead;
  never act on it.
- **Confidence** — the score that issued it.
- **Reasons** — the ingredients that earned the marks, in words.
- **Invalidation** — the index level that kills the whole thesis
  (e.g. "NIFTY must stay above 24,610").

The **Paper tab** then takes the trade automatically in simulation — real
premiums, slippage, full Zerodha charges — and grades it to the end. You
never need to do anything for the experiment to learn.

---

## Your alert without a phone: the bell 🔔

You said no mobile alerts — the dashboard has a built-in alternative:
**click the bell icon (top-right, next to P&L) once.** From then on, a new
card **chimes and pops a desktop notification** even when you're in another
tab. That's the engine tapping your shoulder only when something real
happens — the "alerts unarmed" chip refers to phone push, which stays off
as you wanted.

---

## Daily rhythm (the whole routine)

| Time (IST) | What | Who |
|---|---|---|
| 08:45–09:10 | One-minute Kite login at localhost:3777 | **You** (the only manual step) |
| 09:15 | Market opens; engine + recorder work alone | Machine |
| Any glance | Regime → chip → score, 10 seconds | You |
| 14:15 | No new intraday cards (by rule) | Machine |
| 15:20 | Intraday time-exit of anything open | Machine |
| 15:30 | Close | — |
| 16:00 | Evening report writes itself → `recorder/data/reports/` | Machine |
| Evening | Read the report with tea; `analyze.py` anytime | You (optional) |

One login, a few glances, one report. Everything else is the machine
building your evidence.
