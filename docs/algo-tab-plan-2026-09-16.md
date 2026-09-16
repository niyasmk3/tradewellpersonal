# Algo Tab — Plan (16-Sep-2026)

**Goal (user):** an *Algo* module tab that executes strategies automatically,
rather than handing a card to a human who then clicks in Kite.

This crosses the one line the project has held since Phase 1: *"Tradewell never
places, modifies, or exits orders."* That line is load-bearing — it is why the
backend has no auth, why `routes_kite_basket.py` deliberately refuses to
auto-submit, and why every ledger in the repo is a *study*, not a broker. So
this plan treats the boundary crossing as the main engineering problem, not as
a detail on the way to a strategy.

---

## Honesty box — the uncomfortable part, first

**Nothing in this repo has earned live automation yet.** Automation does not
create edge; it removes the human from the loop that currently catches the
engine's mistakes. Current forward evidence, as of today:

| Candidate | Forward evidence | Its own bar | Verdict |
|---|---|---|---|
| Closing Day (15:05 → 09:50) | **16** logged decision cards (`.closing_tonight.jsonl`) | composite was composed after seeing both windows; clean nights ~5–6/month | far short |
| Gold (3 frozen rules) | live since 03-Sep (~2 weeks) | `MIN_FORWARD_SAMPLES=30`, `LIVE_MONEY_BAR` = 100+ episodes, PF >1.2, 2+ regimes | far short |
| Signal engine (CONFIRM class) | 613 paper fills, honest-era | only positive class in the engine, at **+0.08R** | thin, and thin edges die first to slippage |
| Intraday / scalp | `SCALP_LIVE_ENABLED=false` | paper audition, unconverted | no |

So the plan is deliberately **machinery-first**: build and harden the execution
path now — it is the long pole, it is fully testable without risking a rupee,
and it is strategy-agnostic — while the evidence ladders keep running on their
own clocks. Arming live is a *separate, later, per-strategy* decision gated on
the bars those ledgers already declare.

The failure mode to design against is not "the strategy is wrong". It is
"the strategy is wrong **and** nobody is watching **and** it repeats the
mistake 40 times before 15:20".

---

## Part 1 — What "algo trading" now legally means for this account

SEBI's *Safer Participation of Retail Investors in Algorithmic Trading*
framework (Feb-2025 circular, NSE/BSE implementation) became **mandatory for
all brokers on 1-Apr-2026**. For an individual automating their own strategy in
their own account through Kite Connect, the operative facts:

1. **Static IP whitelisting is mandatory for order endpoints.** Registered on
   the Kite Connect developer console; *"all order requests from an
   unregistered IP will be rejected."* Data endpoints, the WebSocket, order
   book and positions are **not** IP-restricted — only order placement is.
2. **≤10 orders/second per client ID** without exchange registration. Above
   that threshold the strategy needs exchange registration and an
   exchange-issued **Algo ID**. Excess requests get HTTP **429**.
3. **The broker is principal.** Zerodha carries the compliance obligation and
   the order tagging; the individual's obligations are the static IP mapping
   and staying under the declared OPS threshold.
4. **Self-use only.** Handing the algo to anyone else — friend, family, a paid
   subscriber — converts you into an *algo provider*, which requires
   empanelment and registration regardless of order rate. This plan assumes
   single-account, single-user, forever.
5. Reported Kite-side specifics to **verify on the console before coding
   against them**: non-zero `market_protection` required on MARKET / SL-M
   orders (`-1` = auto), order slicing capped at 10 slices, MCX does not
   support IOC in the algo segment. Sourced from the Kite Connect forum rather
   than the v3 docs, so treat as "check, then rely".

### The infrastructure consequence nobody enjoys

Tradewell runs on a laptop on a residential connection with a **dynamic** IP.
Order placement will be rejected from it. Three options, in order of preference:

| Option | Shape | Cost / friction |
|---|---|---|
| **A. Split the gateway** | Everything stays on the laptop except a thin order-gateway process on a tiny cloud VM with a static IP; laptop sends *intents* to it over an authenticated local link | ~₹400–800/mo, one more moving part, but keeps research on the laptop where the data lives |
| **B. Move the whole backend** | Run the entire backend on the static-IP VM | simplest topology, but drags the SQLite ledgers, Dukascopy fetches and the macOS-TCC-specific ops loop off the machine they were designed for |
| **C. Static-IP VPN** | Laptop egresses through a fixed-IP VPN endpoint | cheapest, but the whole machine's traffic moves, and a VPN drop becomes a silent order-rejection mode |

**Recommendation: A.** It also falls out of the architecture below for free —
`broker.py` is already the only module allowed to touch order APIs, so making
it a remote call is a one-file change, not a refactor. Nothing in Phases 0–2
below needs the static IP at all, so this decision can wait.

---

## Part 2 — Architecture

House conventions followed: module under `backend/app/algo/`, routes in
`api/routes_algo.py`, three-file frontend tab, append-only JSONL ledgers (no
DB, by standing decision), frozen constants in code rather than `.env`, pure
functions for anything that makes a decision.

```
backend/app/algo/
  contract.py    frozen risk constants + freeze date
  intent.py      OrderIntent / OrderResult models
  broker.py      THE ONLY module in the repo that may import order APIs
  guard.py       pre-trade gate — pure functions over plain data
  killswitch.py  file-backed latch; survives restart; only a human clears it
  runner.py      strategy -> intent loop (app-lifetime task, self-gated)
  reconcile.py   broker truth vs our belief; halts on unexplained drift
  ledger.py      append-only .algo_orders.jsonl audit trail
  adapters/      one thin file per strategy source (closing, gold, signals)
```

### `contract.py` — limits as constants, not config

Same reasoning as `gold/rules.py`: *an `.env`-tunable risk limit is an editable
one, and a limit edited mid-drawdown is not a limit.* Everything here is frozen
in code and changing it is a commit with a date:

```
MAX_ORDERS_PER_SEC     = 2      # regulatory ceiling is 10; we do not need 2
MAX_ORDERS_PER_DAY     = 20
MAX_OPEN_POSITIONS     = 2
MAX_LOTS_PER_ORDER     = 1      # raised only by promotion, never by a knob
MAX_NOTIONAL_RS        = ...    # per position and per day
DAILY_LOSS_CAP_RS      = ...    # trips the kill switch, not just the entry
ALLOWED_SEGMENTS       = {"NFO"}          # widened per promotion
ALLOWED_ORDER_TYPES    = {"LIMIT"}        # MARKET arrives later, if ever
ENTRY_WINDOW_IST       = (9, 20) .. (15, 5)
HARD_FLATTEN_IST       = (15, 15)         # before RMS auto-squareoff at ~15:20
ARM_TTL_S              = 3600             # a live arm expires by itself
```

`MAX_ORDERS_PER_SEC = 2` is deliberate: it keeps us an order of magnitude below
the 10-OPS registration threshold, so no plausible bug can push this account
into "should have been exchange-registered" territory.

### `guard.py` — the part that actually protects the account

Every intent passes a fixed list of checks and gets a stamped verdict. All
pure functions over plain data, unit-tested the way `FeedWatchdog` and `ops.py`
already are:

- kill switch not tripped
- arm state valid and unexpired, and the arm matches *this* strategy
- inside entry window; not inside the CAS freeze (15:15–15:35) for index legs
- token probe says `valid` (not merely `is_authenticated` — the 16-Sep lesson)
- feed healthy, last tick age under threshold, clock skew under 2s
- rate-limit token bucket has a slot
- daily order count, open positions, lots, notional all under cap
- day's realised + unrealised loss under the cap
- instrument is in the allowlist and its expiry is what we think it is
- **idempotency key unseen** — `(strategy, date, leg, side)` hashed; a restart
  or a double-tick must never produce a second entry
- position book reconciled within the last N seconds

A blocked intent is not silently dropped — it is written to the ledger with its
block reason. The blocked-intent stream is itself evidence: in dry-run it tells
you which guard would have fired, and how often.

### `broker.py` — three implementations, one interface

```
DryRunBroker    logs the intent, returns a synthetic ack, touches no network
PaperBroker     routes into the existing paper simulator (slippage + full
                Zerodha charge schedule already modelled there)
LiveBroker      KiteConnect place/modify/cancel, from the whitelisted IP
```

One enum selects it. `LiveBroker` additionally refuses to construct unless the
arm token is present and unexpired. This is the file that later becomes an HTTP
call to the static-IP gateway; nothing else in the repo changes when it does.

### `killswitch.py`

Trips on: the UI button, daily loss cap, N consecutive rejections, feed
unhealthy, token invalid, reconcile drift, clock skew, or an unhandled
exception anywhere in the runner. Once tripped it is **persisted** — a restart
does not clear it, because "restart and it goes away" is how a broken algo
gets a second chance at the same mistake. Only an explicit human action clears
it, and clearing is logged.

### `reconcile.py`

Polls `orders()` / `positions()` and compares against the ledger's belief.
Any of: an order we did not place, a fill quantity we did not expect, a
position that vanished (RMS auto-squareoff, manual intervention), a rejection
we did not record → **trip the kill switch and page** via the existing
`notify.py`. This is also what protects against the genuinely nasty case: the
user manually closes a position in Kite while the runner still thinks it is
open.

---

## Part 3 — Security, because this changes the threat model

Today the backend has **no authentication** and is loopback-bound precisely
because it only exposes readable data. Adding `POST /algo/arm` to an
unauthenticated local API means any process on the machine — or any page the
browser loads that can reach `localhost:8000` — can arm a live trading engine.

Non-negotiables for this module:

1. A shared secret (`ALGO_CONTROL_TOKEN` in `.env`, never committed) required
   on **every** `/algo/*` mutating route, checked by a FastAPI dependency.
2. CORS for `/algo/*` restricted to the exact frontend origin — the existing
   permissive `allow_methods=["*"]` list is fine for research reads, not for
   this.
3. Arming requires a typed confirmation phrase in the UI, not a click, and
   the arm carries a TTL that expires on its own.
4. The `.kite_session.json` file already has `0600`; the algo ledgers get the
   same, and `.algo_*` goes into `.gitignore` before the first line is written.
5. Consider binding `/algo/*` to a separate port bound to `127.0.0.1` only.

---

## Part 4 — The frontend tab

Three files, established pattern: `frontend/app/algo/page.tsx`,
`frontend/components/AlgoLab.tsx`, and one entry in `ModuleSwitcher.tsx`
(`{ key: "algo", label: "Algo", href: "/algo" }`) plus its `path.startsWith`
branch.

The tab's job is *situational awareness under stress*, not analytics:

1. **Arm banner** — the loudest element on the page. `DRY-RUN` (grey) /
   `PAPER` (blue) / `LIVE — armed, expires 14:32` (red, pulsing). A **KILL**
   button pinned and always visible, working even when every other request is
   failing.
2. **Preflight checklist** — static IP matches whitelist · token probe valid ·
   feed healthy · clock sync · margin available · position book reconciled ·
   strategy evidence bar cleared. Any red blocks arming, with the reason.
3. **Intent tape** — today's intents, newest first: time, strategy, instrument,
   side, qty, **guard verdict + block reason**, broker response, fill price,
   live P&L. Blocked intents shown greyed, never hidden.
4. **Rails panel** — the frozen contract rendered read-only beside today's
   counters: `orders 3/20 · open 1/2 · loss ₹-1,240 / ₹-5,000 cap`.
5. **Ledger** — append-only, filterable, exportable (reuse `closing/xlsx.py`).
6. **Honesty box** — which strategy is armed, its forward n, its declared bar,
   and how far short it is. Rendered from the ledgers, not hand-written, so it
   cannot go stale.

---

## Part 5 — Rollout ladder

Each rung has an explicit promotion criterion. No rung is skipped, and the
ladder is per-strategy — clearing it for Closing Day says nothing about Gold.

| Rung | What runs | Money | Promotion criterion |
|---|---|---|---|
| **A0 Dry-run** | runner + guard + `DryRunBroker`; intents logged only | none | 20 sessions with zero unexplained intents, zero guard crashes, and the intent stream matching what the user would have done manually |
| **A1 Paper-routed** | intents route into the existing paper simulator | none | 30 simulated round trips; fill/slippage/charge accounting reconciles against the paper book's own numbers |
| **A2 Live, minimum size** | `LiveBroker`, 1 lot, one strategy, manual arm each morning, human present the whole session | real, minimum | the *strategy's own* evidence bar cleared (Gold's `LIVE_MONEY_BAR`, or the closing rule's equivalent) **and** 20 A1 sessions clean |
| **A3 Live, multi-day arm** | arm persists across sessions under the daily loss cap | real | 40 A2 trades, realised expectancy ≥ the paper book's within CI, zero reconcile trips |
| **A4 Size / breadth** | size up, or add a second strategy | real | one rung at a time; adding a strategy restarts that strategy at A0 |

**Nothing above A1 is authorised by this plan.** A2 is a separate, explicit,
per-strategy decision.

---

## Part 6 — Which strategy goes first, and why

**Closing Day**, for structural reasons rather than because its edge is proven:

- **Two orders per day, total.** One entry at 15:05, one exit at 09:50. That is
  ~0.0001 orders/second — the regulatory ceiling stops being a design concern.
- **Deterministic clock.** `closing/tonight.py` already produces a settled,
  non-provisional card at 15:05 and already logs it append-only. The adapter is
  thin: read the card, emit an intent.
- **No intraday babysitting.** The failure mode "algo repeats a mistake 40
  times before lunch" is structurally impossible.
- **The manual comparison already exists.** 16 logged nights of what the card
  said, so A0's intent stream can be diffed against real history immediately.

Gold is second (3 frozen rules, one `simulate_day` path, already has a live
card loop). The intraday signal engine is **last**, and honestly may never
qualify: fast cadence, thin +0.08R edge, and premium slippage eats exactly that
size of edge.

`routes_kite_basket.py` stays exactly as it is — it remains the manual path,
the fallback when the algo is killed, and the A0 comparison baseline.

---

## Part 7 — Build order

| Step | Deliverable | Depends on |
|---|---|---|
| 1 | `contract.py`, `intent.py`, `killswitch.py` + tests | — |
| 2 | `guard.py` + tests (the largest test file in the module, by design) | 1 |
| 3 | `ledger.py`, `DryRunBroker`, `runner.py` skeleton | 1, 2 |
| 4 | `adapters/closing.py` — card → intent | 3 |
| 5 | `routes_algo.py` + control-token dependency | 3 |
| 6 | `AlgoLab.tsx` + page + switcher entry | 5 |
| 7 | **A0 runs** — 20 sessions, dry | 4, 6 |
| 8 | `PaperBroker` wiring into the paper simulator | 7 |
| 9 | `reconcile.py` + `notify.py` integration | 8 |
| 10 | Static-IP decision + gateway split + `LiveBroker` | 9, and A2 authorisation |

Steps 1–7 risk no money and need no static IP, no new subscription, and no
compliance step. They are also the steps that take the longest to get right.

## Open questions for the user

1. **Static IP route** — cloud VM (A), full move (B), or VPN (C)? Not needed
   until step 10, but it shapes the gateway interface.
2. **Daily loss cap and max notional** — these are account-size decisions; the
   constants above are placeholders.
3. **Is a live arm ever allowed unattended?** A3 assumes yes eventually; if the
   answer is "never", the ladder stops at A2 and the design gets simpler.

---

## Build log

### Steps 1–3 — done, 16-Sep-2026

`backend/app/algo/`: `contract.py`, `intent.py`, `killswitch.py`, `arm.py`,
`guard.py`, `ledger.py`, `broker.py`, `runner.py`.
Tests: `test_algo_guard.py` (17), `test_algo_killswitch.py` (8),
`test_algo_runner.py` (8). All pass; the rest of the backend suite is
unaffected (nothing imports `app.algo` yet).

Four things the build changed or firmed up against the plan above:

1. **Exits inherit their open's mode from the ledger, not from the arm.**
   The plan had the arm carry the mode, which breaks the moment an arm's
   1-hour TTL expires while a Closing Day position is held overnight — the
   exit would have been stranded. `ledger.day_state()` now records which mode
   each leg was opened in, and `GuardInput.exit_mode` reads it back. An exit
   for a leg with no recorded open is refused (`EXIT_UNOWNED`) rather than
   guessed at.

2. **Two check lists, not one.** Opens face all 28 checks; exits face only the
   ten that ask "is this order even placeable" (kill, token, rate, duplicate,
   malformed, day ceiling). A gate that can refuse to let you out is worse
   than no gate. Pinned by
   `test_exits_bypass_the_discretionary_checks`.

3. **Write-ahead ordering.** The ledger row that burns the idempotency key and
   spends the day's order budget is written *before* the broker call. A process
   that dies mid-send restarts into "already sent" rather than into a second
   entry. Pinned by `test_write_ahead_survives_a_broker_that_explodes`.

4. **Disarm is a full stop, same as kill.** There is no "stop opening but keep
   managing" state — one mental model, not two. The UI must say so plainly:
   *disarm stops the runner completely; any open position becomes yours to
   manage in Kite.*

Two bugs caught during the build, both worth remembering:

* `float(now or time.time())` treats an injected `now=0.0` as "not supplied"
  and substitutes the wall clock — the falsy-zero trap, in the one place where
  a silently wrong timestamp would be least visible. Now
  `time.time() if now is None else float(now)` everywhere, pinned by
  `test_zero_timestamp_is_honoured_not_replaced_by_wall_clock`.
* Two guard reasons used implicit string concatenation across a line break
  after `return`, so the `%` argument was dead code and the message shipped a
  raw `%s`. A block reason without its number is useless at 3pm.

`.gitignore` now covers `.algo_orders.jsonl` and `.algo_killswitch.json`.
`LiveBroker` raises on construction and `PaperBroker` raises on use, so the
README's *"Tradewell never places, modifies, or exits orders"* is still
literally true — it stops being true at build step 10, and the README changes
in that same commit.

### Steps 4–6 — done, 16-Sep-2026 (rung A0 is runnable)

`app/algo/adapters/closing.py`, the live half of `runner.py` (`live_world`,
`submit_live`, `algo_loop`), `api/routes_algo.py`, `state.tick_skew_s()`,
config (`ALGO_CONTROL_TOKEN`, `ALGO_POLL_S`), `main.py` wiring, and the tab
(`frontend/app/algo/page.tsx`, `components/AlgoLab.tsx`, switcher entry).
Tests: `test_algo_closing_adapter.py` (9), `test_algo_routes.py` (5). The
algo suite is now 47 tests; the backend suite is otherwise unaffected.

What the build settled:

1. **The adapter reads the LOGGED card, never a live re-evaluation.** It
   polls `.closing_tonight.jsonl` for today's first settled row — the exact
   card the human saw — so the dry-run intent stream is diffable against the
   decision-time log by construction. A missing card is "not yet" and retried
   silently; a policy skip is logged once per day.
2. **`ENTRY_POLICY = "clean"` is a frozen constant.** The 22-Aug filter hunt
   found every filter dead, so this is a hypothesis the A0 ledger helps price,
   not a knob. `"all"` (the unfiltered rule) is the other value.
3. **The contract's open window moved 15:05 → 15:12** because the adapter's
   consistency test caught that the guard would have refused every closing
   entry. The test that caught it now pins that the adapter's clocks fit
   inside the contract's, so a future edit cannot silently starve it.
4. **Clock skew is measured, not assumed:** wall clock at the newest tick's
   receipt minus its exchange timestamp. Kite stamps whole seconds, so the
   tolerance is 3s rather than the plan's 2s. Verified on the running tab at
   18:02: after the close the ticker keeps sending ticks stamped with the
   last trade, so the raw reading was 7006s — the tape's age, not skew. The
   measure is now gated on `is_market_open()` and reads "unmeasurable —
   market closed" outside the session.
5. **Exits name their open by key (`OrderIntent.closes`).** The ledger's
   `open_position_keys()` is the runner's belief about what it holds —
   acked opens minus acked exits that name them — and is what `reconcile.py`
   (step 9) will check against Kite. Until then `day_pnl_rs` is an honest 0
   and `reconciled_at` None, which keeps LIVE blocked by the guard's own rules.
6. **Auth posture:** every mutating route needs `X-Algo-Token` matching
   `.env`; an unset token refuses. `POST /algo/kill` takes no token — the one
   request that must always work is the one that stops the runner. Arming
   needs the typed phrase `ARM CLOSING DRY`; paper and live return 409 by
   construction (`REACHABLE_MODES = ("dry",)`).
7. **An unexpected exception inside an adapter tick trips the kill switch**
   (`RUNNER_EXCEPTION`) and pages. Expected failures — no card, no quote, no
   contract — are handled inside the adapter as logged skips and never reach
   that path.

**To run A0:** set `ALGO_CONTROL_TOKEN` in `backend/.env`, restart, open
`/algo`, paste the token, type `ARM CLOSING DRY` before 15:05 on a trading
day. The arm lasts an hour; the exit leg needs a fresh arm in the 09:50–10:05
window next morning. Promotion criterion (rung A0 → A1): 20 sessions with
zero unexplained intents, zero `RUNNER_EXCEPTION` kills, and the intent
stream matching the decision-time log night for night.

**Next:** step 8 (`PaperBroker` into the paper simulator), step 9
(`reconcile.py` + P&L into `live_world`). The static-IP decision (Part 1)
stays open until step 10.
