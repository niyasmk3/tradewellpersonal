# Cadence Replay — P1-3 (03-Aug-2026)

The 30-Jul audit's P1-3: "extend the backtest with live gap/cooldown/flip-guard
and sweep *cadence* at fixed gate 78 — the capture experiment the evidence says
matters most." Done. The result **overturns the audit's own hypothesis**.

## Method

- 60 days of 3-minute NIFTY futures (04-Jun → 03-Aug, 42 sessions, 5,126 bars),
  same technical-only score proxy and conservative fills as `audit_replay`.
- Step 1 removed the confound: the audit engine holds ONE position and is blind
  while in it. This replay enumerates **every** bar clearing regime + gate 78 +
  risk floor as an independent candidate with its own play-out:
  **641 slot-free candidates (15.3/session)** — the flicker clustering the
  audit's WATCH→CONFIRM note predicted, in full view for the first time.
- Step 2 swept a live-shaped throttle over that one candidate list:
  max_open {1,2,3} × min_gap {0,300,600,1200s} × cooldown {0,300,900s} ×
  per-day cap {5,10,∞}, flip-guard fixed at the live 1800s. Same candidates,
  same outcomes — only the cadence policy varies. 108 policies + the live one
  + a no-throttle ceiling.
- Capture measured against the same mechanical move universe (≥40pt move,
  ≤12pt adverse first): 205 events in this window.

## Results

| Policy | Trades | Exp (R) | Total R | Win% | Capture |
|---|---|---|---|---|---|
| **Live** (open2, gap600, cd300, mpd10) | 253 | −0.187 | −47.2 | 38.3 | 20.0% |
| **No throttle at all** (ceiling) | 641 | −0.152 | −97.4 | 39.0 | **21.5%** |
| Best total-R policy (open1, gap600, cd0) | 186 | −0.149 | −27.8 | 38.7 | 14.6% |
| Best capture policy (open3, gap600) | 302 | −0.199 | −57.9 | 37.4 | 21.0% |

Axis isolation at the live anchor:

| max_open (gap600, cd300, mpd10) | Trades | Exp (R) | Total R | Capture |
|---|---|---|---|---|
| 1 | 168 | −0.172 | −28.8 | 16.1% |
| 2 (live) | 253 | −0.187 | −47.2 | 20.0% |
| 3 | 277 | −0.209 | −57.9 | 21.0% |

min_gap 0→1200s at open2: capture moves 19.0→20.5% (noise), total R −47 → −37.

## Findings

1. **The ~18–21% capture ceiling is a DETECTION ceiling, not a slot artifact.**
   Removing every throttle — infinite slots, zero gaps, no daily cap — lifts
   capture from 20.0% to just 21.5%, while more than doubling the total loss.
   The audit's §8 hypothesis ("slot occupancy caps capture, P1-3 targets the
   ~18% ceiling") is measured and **rejected**: ~78% of the mechanical moves
   have no same-direction gate-78 candidate anywhere near them under ANY
   cadence. The recall prize lives where P1-2's instrumentation and P1-4's
   setup detectors are pointed — candidates the engine cannot currently see —
   not in taking more of the candidates it already has.
2. **No cadence policy is profitable on this proxy** (expectancy −0.15 to
   −0.22R across all 108). Cadence scales exposure; it cannot change the sign
   of per-trade economics. Every extra slot/looser gap bought capture at
   roughly linear extra loss. "Fix economics before capture" stands,
   strengthened.
3. **The current live policy is not the problem.** open2/gap600/cd300 sits
   mid-grid on every metric; tightening to open1 would have cut the replayed
   loss ~40% for 4pp of capture — but tuning cadence inside a negative-EV
   regime is rearranging deck chairs, and this proxy omits OI/news gating.
   **No production cadence change is recommended from this replay.**
4. **641 candidates vs ~168–253 taken** quantifies threshold flicker: the same
   thesis re-clears the gate again and again (the WATCH→CONFIRM persistence
   idea, P2-3, now has its dataset).

## Caveats

Technical-only score (OI/news absent in history); conservative fills
(slippage both legs, stop-first on ambiguous bars); the event universe is
mechanical, not judgmental. Numbers grade the UNDERLYING future — the paper
book, which pays theta and spreads, remains the deciding evidence for any
production change.
