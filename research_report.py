#!/usr/bin/env python3
"""Research dashboard — served by the app itself at /research.html.

Writes frontend/public/research.html (Next serves public/ statically, so the
page rides the same origin as the dashboard: http://localhost:3777/research.html)
plus a copy in recorder/data/reports/. Daily text reports are mirrored into
frontend/public/reports/ so the page can LINK full history.

Three tabs: Findings (every card/fill/ledger), Training (what has learned,
what is waiting, harness verdicts), Glossary (every term in plain language).
Regenerated nightly by evening_report.sh. Nothing leaves this machine.
"""
from __future__ import annotations

import html
import json
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REC = ROOT / "recorder"
DATA = REC / "data"
PUB = ROOT / "frontend" / "public"
OUT_PUB = PUB / "research.html"
OUT_COPY = DATA / "reports" / "research.html"
PUB_REPORTS = PUB / "reports"
IST = timezone(timedelta(hours=5, minutes=30))

IGNITION_ROWS, IGNITION_MONTHS = 40, 2


def load_json(p: Path, default):
    try:
        return json.loads(p.read_text())
    except Exception:
        return default


def load_jsonl(p: Path) -> list[dict]:
    out = []
    if p.exists():
        for line in p.read_text().splitlines():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def fmt_ts(ts) -> str:
    try:
        return datetime.fromtimestamp(int(ts), IST).strftime("%d-%b %H:%M")
    except Exception:
        return "—"


def esc(x) -> str:
    return html.escape(str(x))


def section(title: str, body: str, note: str = "") -> str:
    n = f'<p class="note">{note}</p>' if note else ""
    return f"<section><h2>{esc(title)}</h2>{n}{body}</section>"


def table(headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return '<p class="empty">nothing yet</p>'
    h = "".join(f"<th>{esc(c)}</th>" for c in headers)
    b = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f'<div class="scroll"><table><thead><tr>{h}</tr></thead><tbody>{b}</tbody></table></div>'


def money(v: float) -> str:
    cls = "pos" if v > 0 else ("neg" if v < 0 else "")
    return f'<span class="{cls}">₹{v:,.0f}</span>'


GLOSSARY: list[tuple[str, str]] = [
    ("Regime", "The engine's weather forecast for the day: trending up, trending down, or sideways. In sideways/compressed weather it refuses to even look for trades."),
    ("Score (0–100)", "Every 3 minutes the engine grades the market like an exam with six subjects: price action (25), trend (20), options & OI (20), volume (15), volatility (10), news (10)."),
    ("Issue gate (78)", "The pass mark. Below it, no card — no matter how exciting the chart looks."),
    ("Persistence gate", "New rule from the owner: one bar above 78 is a WATCH, not an offer. The score must HOLD above the gate across bars — stops one-bar spikes from triggering whipsaw entries."),
    ("Card", "A complete trade plan issued by the engine: what to buy, the fair entry zone, stop-loss, targets, and an expiry time. Advice, never an order."),
    ("Validity", "Cards die fast by design — ≈8 min intraday, 3 min scalp. An expired card means the moment passed; never act on one."),
    ("Cancelled card", "The engine withdrew its own card early because the trend flipped against it before expiry. Active mind-changing, not an error."),
    ("Entry zone", "The price band the plan considers fair. Above it the chip says 'chasing' — paying more than the plan priced."),
    ("SL / T1 / T2", "Stop-loss ('the idea was wrong, get out'), first target (the honest one), stretch target."),
    ("R (risk multiple)", "Profit/loss measured in units of what you risked. Risk ₹10, make ₹27 → +2.7R. Lets trades of different sizes be compared fairly."),
    ("Expectancy", "Average net result per trade over many trades. THE number. A system can win only 40% of the time and still be profitable if winners are much bigger than losers."),
    ("Win rate vs expectancy", "Beginners chase win rate; professionals chase expectancy. Option buying is structurally a low-win-rate, fat-winner game."),
    ("MFE / MAE", "Best and worst moment of a trade's life (Maximum Favourable/Adverse Excursion). Winners' MFE teaches where targets should be; winners' MAE teaches where stops belong."),
    ("Theta", "The rent you pay to hold a bought option: it loses value every flat minute. Why late or stalled entries are exits — a right thesis that's late still loses."),
    ("OI (Open Interest)", "Count of open option contracts per strike — where the big players stand. The engine reads changes in it as participation."),
    ("PCR", "Put/Call ratio of OI. Roughly: the mood of option writers. ~1 balanced; extremes hint at one-sided positioning."),
    ("VWAP stretch (the rubber band)", "How many ATRs price sits above/below the day's volume-weighted average. Past ~3.5 the engine refuses to chase — rubber bands snap back."),
    ("Hollow card", "A card vetoed by the volume/OI floor but still paper-tracked in a shadow ledger — so the veto itself is graded. So far the floor keeps saving money."),
    ("Paper book", "Every card automatically traded in simulation with slippage and full Zerodha charges. The experiment's only scoreboard — no real money ever."),
    ("Shadow ledger", "A what-if ledger that grades a rule's counterfactual (blocked trades, alternate stops) without touching real behavior. Evidence first, changes later."),
    ("Exit A/B", "Two exit policies graded side by side on every fill (trail vs bank-at-quick-target). The ledger decides at 30+ diverged fills, not opinion."),
    ("Stop calibrator (shadow twin)", "Each fill gets an invisible twin with only the stop repositioned (fitted from winners' MAE). Twin vs actual are paired; the live stop changes only after 30+ diverged pairs prove the twin wins."),
    ("Participant OI (NSE)", "Daily official file of FII / DII / Pro / Client long-short positions — 'what the smart money holds', now collected nightly."),
    ("Bhavcopy (NSE)", "The exchange's end-of-day file of every contract's close, OI, volume. Our nightly download builds option history that outlives expiry — Kite can't provide this."),
    ("Labeled row / training sample", "One graded card joined to (a) its own score breakdown, (b) the market snapshot at its birth, (c) the honest outcome. The unit ML learns from."),
    ("Holdout (locked)", "The newest 28 days of samples, sealed away. Experiments never touch it; only the single final winner is graded on it, once. The defense against fooling ourselves."),
    ("Walk-forward", "Train on months 1..N, test on month N+1, roll forward. Never shuffle time — the future must never leak into training."),
    ("Effect size", "How many standard deviations separate winners from losers on a feature. The nightly ledger's measure of 'does this feature matter so far'."),
    ("Meta-labeling", "Our ML design: the model never predicts the market. It predicts 'given THIS card, what's the probability it wins' — a quality filter over the rule engine."),
    ("Karpathy harness", "The self-running research loop: change one thing → evaluate walk-forward → keep only what consistently wins → log every attempt. Ignites itself when data gates pass."),
    ("Ignition gates", "40+ training rows across 2+ months. Below that, any 'finding' is likely luck; the harness refuses to start early by design."),
    ("Devil's advocate", "Borrowed from the TradingAgents research idea of bull-vs-bear debate: when a card fires, one Claude call argues the OPPOSITE case and scores how strong that counter-case is (0–100). It never blocks a trade — the score is just one more dataset ingredient, graded at 30+ fills like everything else."),
    ("Option selling (the mirror)", "The seller collects the premium the buyer pays and profits when nothing dramatic happens — theta (the rent) works FOR them. Wins small and often, loses rarely but hugely. Naked selling needs ~₹1.5–2L margin per lot; the capped-risk form is a spread."),
    ("Straddle", "One CE plus one PE at the same at-the-money strike. SELLING it is the classic 'collect rent from both sides' bet that the market stays quiet — and the classic way to get hurt when it doesn't."),
    ("Seller shadows", "Our zero-risk studies of the sell side: the exact other side of our fills (fade), straddle-selling in specific windows, and overnight decay — all computed from our own recorded tape, graded nightly, decision at 30+ samples."),
    ("Theta (time decay)", "An option is a melting ice cube: part of its price is pure time, and that part evaporates every day even if NIFTY stands still. Mild while expiry is weeks away, brutal in the final days — which is why cheap 1–2 day options feel like bargains and behave like nearly-melted ice. Buyers PAY theta daily; sellers collect it."),
    ("Delta", "How much the option's premium moves when NIFTY moves 1 point. A near-ATM option has delta around 0.5: NIFTY drops 100 → a PE gains roughly 40–50. Deep out-of-the-money options have tiny delta — the market must travel far before they even notice."),
    ("Gamma", "How fast delta itself changes. Near expiry gamma explodes: tiny NIFTY wiggles flip an option's value violently in minutes. It's why expiry-day trading is a casino and why the condor module refuses to open positions at DTE 0 — 'gamma risk EXTREME' is this."),
    ("Vega / IV crush", "Sensitivity to implied volatility — the market's fear meter (VIX). When fear deflates, ALL premiums shrink, even if your direction was right. Being correct about NIFTY and still losing money on the option is usually vega's work; it's why our thesis-level label grades the market call separately from the premium outcome."),
]


def build() -> str:
    now = datetime.now(IST)
    paper = [t for t in load_json(ROOT / "backend" / ".paper_trades.json", [])
             if isinstance(t, dict)]
    archive = load_jsonl(ROOT / "backend" / ".signals_archive.jsonl")
    ledger = load_jsonl(DATA / "learning_ledger.jsonl")

    def is_shadow(t: dict) -> bool:
        return str(t.get("notes") or "").startswith("hollow")

    real = [t for t in paper if not is_shadow(t)]
    shadow = [t for t in paper if is_shadow(t)]

    # Data-quality quarantine (21-Aug, external review confirmed): overridden
    # rows plus any exit stamped outside Mon-Fri 09:15-15:45 IST — the paper
    # monitor can 'exit' pre-open on frozen quotes; those P&Ls are fiction.
    quarantined_ids = {e.get("id") for e in
                       load_json(DATA / "journal_overrides.json", {}).get("exclude", [])}

    def off_session(t: dict) -> bool:
        ts = t.get("exited_at")
        if not isinstance(ts, (int, float)):
            return False
        dt = datetime.fromtimestamp(ts, IST)
        return dt.weekday() >= 5 or not (555 <= dt.hour * 60 + dt.minute <= 945)

    n_quarantined = sum(1 for t in real if t.get("exited_at")
                        and (t.get("id") in quarantined_ids or off_session(t)))
    closed = [t for t in real if t.get("exited_at")
              and isinstance(t.get("realized_pnl"), (int, float))
              and t.get("id") not in quarantined_ids and not off_session(t)]
    wins = [t for t in closed if t["realized_pnl"] > 0]
    gross = sum(t["realized_pnl"] for t in closed)
    sh_closed = [t for t in shadow if t.get("exited_at")
                 and isinstance(t.get("realized_pnl"), (int, float))]
    sh_gross = sum(t["realized_pnl"] for t in sh_closed)

    # Entry quality, the book's north-star (19-Aug R&D-ledger finding): a fill
    # that never touches +5% before exit has never won; the whole loss pile
    # lives there. Exit policy can only redistribute the touched group.
    def touched5(t: dict) -> bool:
        e, m = t.get("entry_premium") or 0, t.get("mfe_premium") or 0
        return bool(e) and (m - e) / e >= 0.05

    touched = [t for t in closed if touched5(t)]
    duds = [t for t in closed if not touched5(t)]
    touch_html = ""
    if closed:
        t_pnl = sum(t["realized_pnl"] for t in touched)
        d_pnl = sum(t["realized_pnl"] for t in duds)
        pct = 100 * len(touched) / len(closed)
        touch_html = (
            f'<p class="note"><b>Entry quality (north-star): '
            f'{len(touched)}/{len(closed)} real fills touched +5% before exit '
            f'({pct:.0f}%).</b> Touched pile: {money(t_pnl)} gross · '
            f'never-touched pile: {money(d_pnl)} gross. A signal that never '
            f'moves +5% our way has never won a rupee here — raising this '
            f'percentage is what "better signals" means; exits only decide '
            f'how much of the touched pile we keep.</p>')
    if n_quarantined:
        touch_html += (
            f'<p class="note">Data-quality quarantine: {n_quarantined} fill(s) '
            f'excluded from every number on this page — off-session exits '
            f'priced on frozen quotes and human-ruled-out rows '
            f'(recorder/data/journal_overrides.json has each reason).</p>')
    score_rows = []
    if closed:
        score_rows.append(["REAL book", str(len(closed)),
                           f"{len(wins)}W / {len(closed) - len(wins)}L",
                           money(gross), money(gross / len(closed))])
    if sh_closed:
        sh_w = sum(1 for t in sh_closed if t["realized_pnl"] > 0)
        score_rows.append(['<span class="note">shadow research fills (vetoed/refire — never counted)</span>',
                           str(len(sh_closed)),
                           f"{sh_w}W / {len(sh_closed) - sh_w}L",
                           money(sh_gross), money(sh_gross / len(sh_closed))])
    scoreboard = table(["Book", "Closed", "Win / Loss", "Total P&L (gross)", "Avg / trade"],
                       score_rows)

    gold = load_json(DATA / "gold_climatology.json", {})
    gold_html = ('<p class="note">No gold data yet — the first nightly pass '
                 'writes this section automatically.</p>')
    if gold:
        def top_hours(d: dict | None, k: int = 4) -> str:
            items = sorted(((v, h) for h, v in (d or {}).items()),
                           reverse=True)[:k]
            return ", ".join(f"{h}:00 ({v}%)" for v, h in items) or "—"
        md = gold.get("mcx_day") or {}
        gold_html = (
            f"<p><b>MCX GOLD</b> — busiest hours (IST): "
            f"{top_hours(gold.get('mcx_hour_shares_ist'))}. "
            f"Avg daily range {md.get('avg_range_pct', '—')}% · avg overnight "
            f"gap {md.get('avg_gap_pct', '—')}% "
            f"({md.get('gap_over_half_pct_share', '—')}% of days gap &gt;0.5%).</p>"
            f"<p><b>XAUUSD (international spot)</b> — busiest hours (IST): "
            f"{top_hours(gold.get('xau_hour_shares_ist'))}. "
            f"{gold.get('xau_sessions_stored', 0)} sessions stored.</p>"
            f"<p class=\"note\">Updated {esc(str(gold.get('updated', '')))} · "
            "climatology only — no signals, no trades, no opinions. "
            "Venue-portable by design: MCX is the legal venue today; XAUUSD "
            "is the NRI-era venue. Charter and rules: recorder/mcx/README.md</p>")

    # LIVE book — broker truth. The app's journal is immutable once closed
    # (by design); the reconciled Kite tradebook is the system of record for
    # what the human actually did. Rendered so the true history is visible.
    lb = load_json(DATA / "live_book.json", {})
    live_html = ('<p class="note">No tradebook imported yet — run '
                 'recorder/tradebook_import.py on a Kite Console CSV.</p>')
    if lb.get("round_trips"):
        s = lb.get("summary", {})
        rows_lb = [[t["sell_at"][:10], esc(t["symbol"]), str(t["qty"]),
                    f'{t["buy"]:.2f} → {t["sell"]:.2f}',
                    money(t["gross"])]
                   for t in reversed(lb["round_trips"])]
        live_html = (
            f'<p><b>{s.get("trips", 0)} round trips · {s.get("wins", 0)} wins · '
            f'gross {money(s.get("gross_rupees", 0))}</b> '
            f'<span class="note">(before ~₹90/trip charges; source: Kite '
            f'Console tradebook — import a fresh CSV to update)</span></p>'
            + table(["Exit date", "Contract", "Qty", "Buy → Sell", "Gross"],
                    rows_lb))
        if lb.get("open_positions"):
            live_html += "".join(
                f'<p class="note">OPEN: {esc(o["symbol"])} x{o["qty"]} @ '
                f'{o["buy"]} since {esc(o["buy_at"][:16])}</p>'
                for o in lb["open_positions"])

    nc = load_json(DATA / "nifty_climatology.json", {})
    nifty_clim_html = '<p class="note">First nightly pass pending.</p>'
    if nc.get("sessions"):
        g5, g3 = nc.get("gap_05", {}), nc.get("gap_03", {})
        orb = nc.get("orb_continuation", {})
        dow = nc.get("avg_range_by_dow", {})
        wk = " · ".join(f"{k} {v}%" for k, v in dow.items()
                        if k in ("Mon", "Tue", "Wed", "Thu", "Fri"))
        nifty_clim_html = (
            f"<ul style='margin-left:18px'>"
            f"<li><b>Big gaps do NOT fill:</b> gaps ≥0.5% closed back to the "
            f"previous day's price only <b>{g5.get('fill_pct')}%</b> of the "
            f"time ({g5.get('n')} cases); even ≥0.3% gaps filled just "
            f"{g3.get('fill_pct')}% ({g3.get('n')}). NIFTY gaps RUN — the "
            f"same lesson gold's H1 backtest paid ₹79k to learn.</li>"
            f"<li><b>Opening-range 'breakout' is a coin flip:</b> the first "
            f"30 minutes' direction continued through the day only "
            f"{orb.get('pct')}% of the time ({orb.get('n')} qualifying days) "
            f"— the classic retail ORB pattern has no edge here.</li>"
            f"<li><b>The 10:00–12:00 band carries "
            f"{nc.get('toxic_window_share_pct')}% of daily movement</b> — "
            f"proportional to its length: the toxic window is not quiet, it "
            f"moves without going anywhere. Chop, not sleep.</li>"
            f"<li><b>No magic weekday:</b> average ranges nearly identical "
            f"({wk}).</li></ul>")

    def book_tag(t: dict) -> str:
        if not is_shadow(t):
            return "real"
        return "shadow-refire" if "refire" in str(t.get("notes")) else "shadow-hollow"

    fill_rows = [[
        fmt_ts(t.get("entered_at")), esc(t.get("contract", "")), esc(t.get("mode", "")),
        esc(book_tag(t)),
        f"{t.get('entry_premium', 0):.2f} → "
        + (f"{t.get('exit_premium'):.2f}" if t.get("exit_premium") else "open"),
        money(t["realized_pnl"]) if t.get("exited_at") and isinstance(t.get("realized_pnl"), (int, float)) else "—",
        esc(t.get("auto_close_reason") or t.get("exit_reason") or t.get("status", "")),
        f"score {t.get('entry_score', '—')}",
    ] for t in sorted(paper, key=lambda t: t.get("entered_at") or 0, reverse=True)]
    fills = table(["Entered", "Contract", "Mode", "Book", "Premium", "P&L (gross)",
                   "Exit / status", "Entry score"], fill_rows)

    cards: dict[str, dict] = {}
    for r in archive:
        c = r.get("card") if isinstance(r.get("card"), dict) else r
        if c.get("id"):
            cards[c["id"]] = c
    card_rows = [[fmt_ts(c.get("created_at")), esc(c.get("contract", "")),
                  esc(c.get("mode", "")), esc(c.get("state", "")),
                  str(c.get("confidence", ""))]
                 for c in sorted(cards.values(),
                                 key=lambda c: c.get("created_at") or 0, reverse=True)]
    cards_html = table(["Issued", "Contract", "Mode", "Final state", "Score"], card_rows)

    led_rows = []
    for e in reversed(ledger[-21:]):
        sep = e.get("top_separating_features") or {}
        cal = e.get("score_calibration") or {}
        led_rows.append([
            esc(e.get("date", "")), str(e.get("graded_total", "")),
            esc(", ".join(f"{k} ({v:+})" for k, v in list(sep.items())[:3]) or "—"),
            esc(f"wins {cal.get('avg_score_wins')} vs losses {cal.get('avg_score_losses')}"
                if cal.get("avg_score_wins") is not None else "—"),
            "🔥 ready" if e.get("ignition_ready") else "waiting",
        ])
    ledger_html = table(["Night", "Graded", "Top separating features (effect size)",
                         "Score calibration", "Ignition"], led_rows)

    ss = load_json(DATA / "seller_shadows.json", {})
    if ss:
        f = ss.get("fade_the_cards", {})
        t1s, t2s = ss.get("theta_toxic_window", {}), ss.get("theta_late_window", {})
        od = ss.get("overnight_decay", {})
        seller_html = table(
            ["Study", "Samples", "Seller wins", "Seller net (est)"],
            [["Fade the cards (other side of our fills)", str(f.get("fills", 0)),
              str(f.get("seller_wins", 0)), money(f.get("net_est", 0))],
             ["Sell ATM straddle 10:30→12:00", str(t1s.get("days", 0)),
              str(t1s.get("wins", 0)), money(t1s.get("net_est", 0))],
             ["Sell ATM straddle 14:15→15:20", str(t2s.get("days", 0)),
              str(t2s.get("wins", 0)), money(t2s.get("net_est", 0))],
             ["Overnight ATM straddle (close→close)", str(od.get("nights", 0)),
              str(od.get("wins", 0)), money(od.get("net_est", 0))]])
    else:
        seller_html = '<p class="empty">appears after the next nightly run</p>'

    last = ledger[-1] if ledger else {}
    cf = last.get("exit_cf") or {}
    if cf:
        exit_cf_html = table(
            ["Real fills", "Touched +5%", "Written rules", "Bank @ +5%", "Bank +5% / cut −5%"],
            [[str(cf.get("real_fills")), str(cf.get("touched_5pct")),
              money(cf.get("gross_actual", 0)), money(cf.get("gross_bank5", 0)),
              money(cf.get("gross_bank5_cut5", 0))]])
    else:
        exit_cf_html = '<p class="empty">appears after the next nightly learning pass</p>'
    n_train = last.get("train_rows", 0)
    pct = min(100, int(100 * n_train / IGNITION_ROWS))
    countdown = (
        f'<div class="bar"><div class="fill" style="width:{pct}%"></div></div>'
        f"<p><b>{n_train} / {IGNITION_ROWS}</b> training rows · "
        f"holdout {last.get('holdout_rows', 0)} (locked) · needs {IGNITION_MONTHS}+ months "
        f"spread. The harness launches itself the night both gates pass — no one has to "
        f"remember.</p>")

    res_p = REC / "experiment" / "results.tsv"
    if res_p.exists() and len(res_p.read_text().splitlines()) > 1:
        lines = [l.split("\t") for l in res_p.read_text().splitlines()]
        harness = table(lines[0], lines[1:])
        training_state = "🔥 RESEARCH RUNNING — see verdicts below"
    else:
        harness = ('<p class="empty">No research runs yet — this table fills automatically '
                   'once the countdown completes. Rules: recorder/experiment/PROGRAM.md</p>')
        training_state = "⏳ COLLECTING EVIDENCE — model training has NOT started (by design)"

    trained_now = table(["Learner", "Status"], [
        ["T1 targets (upstream)", "Self-tunes from winners' MFE — needs 30+ clean fills; static ladder until then"],
        ["Stop calibrator (upstream)", "Shadow twins active since 05-Aug — live stop unchanged until 30+ diverged pairs"],
        ["Exit A/B (upstream)", "Grading trail vs bank-at-QT on every fill — decision at 30+ diverged"],
        ["Persistence gate (upstream)", "Live since 05-Aug — one bar above 78 is a watch, not an offer"],
        ["Nightly learning ledger (ours)", "Runs every close — feature separation + score calibration, logged forever"],
        ["Karpathy harness (ours)", "Armed, auto-ignites at 40 rows / 2 months — see countdown"],
        ["Devil's advocate (ours, 11-Aug)", "One Claude call argues AGAINST each new card; its counter-score is stamped into the birth snapshot as a dataset feature — decides nothing, on probation like everything else"],
    ])

    nse = cap = '<p class="empty">db missing</p>'
    db_p = DATA / "tradewell_history.db"
    if db_p.exists():
        db = sqlite3.connect(db_p)
        def q(sql):
            try:
                return db.execute(sql).fetchall()
            except sqlite3.Error:
                return []
        days = (q("SELECT COUNT(DISTINCT day) FROM nse_fo_eod") or [[0]])[0][0]
        rows_eod = (q("SELECT COUNT(*) FROM nse_fo_eod") or [[0]])[0][0]
        poi = (q("SELECT COUNT(DISTINCT day) FROM nse_participant_oi") or [[0]])[0][0]
        fii = q("SELECT day, fut_idx_long - fut_idx_short FROM nse_participant_oi "
                "WHERE participant='FII' ORDER BY day DESC LIMIT 5")
        nse = table(["Metric", "Value"],
                    [["Option EOD history", f"{days} days · {rows_eod:,} contracts"],
                     ["Participant OI days", str(poi)]]) \
            + "<h3>FII net index-future position (latest days)</h3>" \
            + table(["Day", "Net contracts (long − short)"],
                    [[esc(d), f"{v:+,.0f}"] for d, v in fii])
        snaps = q("SELECT endpoint, COUNT(*) FROM snapshots GROUP BY endpoint ORDER BY 2 DESC")
        cap = table(["Stream", "Rows"], [[esc(e), f"{n:,}"] for e, n in snaps])

    # Mirror daily text reports into public/ so they are linkable.
    PUB_REPORTS.mkdir(parents=True, exist_ok=True)
    txts = sorted((DATA / "reports").glob("??-??-????.txt"), reverse=True)
    for t in txts:
        shutil.copy2(t, PUB_REPORTS / t.name)
    links = " · ".join(f'<a href="/reports/{t.name}">{t.stem}</a>' for t in txts[:30])
    history = f"<p>{links or 'first report lands at the next 16:00'}</p>"

    gloss = "".join(f"<dt>{esc(t)}</dt><dd>{esc(d)}</dd>" for t, d in GLOSSARY)

    style = """
    body{background:#0d1117;color:#c9d1d9;font:14px/1.55 -apple-system,system-ui,sans-serif;
         max-width:980px;margin:0 auto;padding:24px}
    h1{color:#e6edf3;font-size:22px;margin-bottom:4px}
    h2{color:#e6edf3;font-size:16px;border-bottom:1px solid #21262d;padding-bottom:6px;margin-top:26px}
    h3{font-size:13px;color:#8b949e}
    table{border-collapse:collapse;width:100%;font-size:13px}
    th{color:#8b949e;text-align:left;padding:6px 10px;border-bottom:1px solid #30363d}
    td{padding:6px 10px;border-bottom:1px solid #21262d}
    .scroll{overflow-x:auto} .pos{color:#3fb950} .neg{color:#f85149}
    .empty,.note,.meta{color:#8b949e;font-size:12px}
    .bar{background:#21262d;border-radius:6px;height:14px;overflow:hidden;margin:8px 0}
    .fill{background:linear-gradient(90deg,#1f6feb,#3fb950);height:100%}
    .state{font-size:15px;padding:10px 14px;border:1px solid #30363d;border-radius:8px;
           background:#161b22;margin:14px 0}
    nav{display:flex;gap:6px;margin:18px 0;border-bottom:1px solid #21262d;padding-bottom:10px}
    nav button{background:#161b22;color:#c9d1d9;border:1px solid #30363d;border-radius:6px;
               padding:7px 16px;font-size:13px;cursor:pointer}
    nav button.on{background:#1f6feb;border-color:#1f6feb;color:#fff}
    .tab{display:none}.tab.on{display:block}
    dt{color:#e6edf3;font-weight:600;margin-top:12px} dd{margin:2px 0 0 0;color:#9da7b1}
    a{color:#58a6ff;text-decoration:none}
    """
    js = """
    function show(id){
      document.querySelectorAll('.tab').forEach(t=>t.classList.remove('on'));
      document.querySelectorAll('nav button').forEach(b=>b.classList.remove('on'));
      document.getElementById(id).classList.add('on');
      document.getElementById('b-'+id).classList.add('on');
      location.hash=id;
    }
    window.onload=()=>show(location.hash?location.hash.slice(1):'findings');
    """
    return f"""<!doctype html><meta charset="utf-8">
<title>Tradewell — Research</title><style>{style}</style><script>{js}</script>
<h1>Tradewell Research</h1>
<p class="meta">Regenerated {now.strftime('%d-%b-%Y %H:%M IST')} · auto-refreshes nightly at
16:00 · served by your own stack · data never leaves this machine ·
<a href="/">← back to trading dashboard</a></p>
<div class="state">{training_state}</div>
<nav>
<button id="b-findings" onclick="show('findings')">Findings</button>
<button id="b-training" onclick="show('training')">Training status</button>
<button id="b-glossary" onclick="show('glossary')">Glossary</button>
</nav>

<div class="tab" id="findings">
{section("Paper book scoreboard", scoreboard + touch_html,
         "Gross figures; net-of-charges lives on the dashboard Paper tab.")}
{section("Your LIVE book — broker truth", live_html,
         "Reconciled from Kite Console's tradebook (FIFO-paired). The "
         "dashboard journal is immutable once rows close — this table is "
         "what actually happened at the broker, and it wins every "
         "disagreement.")}
{section("Every fill, graded", fills)}
{section("Every card the engine issued", cards_html)}
{section("Gold lab — experiment #2 (climatology)", gold_html,
         "A second evidence stream, same rules: measure first, believe later. "
         "Gold has no signal engine — this section is the market's own habits, "
         "refreshed nightly from MCX and Dukascopy data.")}
{section("NIFTY climatology — 3-year habits", nifty_clim_html,
         "Knowledge, not signals: base rates from 747 sessions of 3-minute "
         "candles. A map of how this market usually behaves — useful priors "
         "for the human today and for the research run later.")}
{section("NSE data collection (free archives, nightly)", nse)}
{section("Live capture health", cap)}
{section("Daily report archive (full history)", history)}
</div>

<div class="tab" id="training">
{section("What is learning right now", trained_now,
         "Ledgers accumulate evidence automatically; policy flips stay human — by design.")}
{section("Exit-style counterfactual (owner-style, real book only)", exit_cf_html,
         "Same fills, three exit policies. Bank@+5% = sell at the first +5% touch. "
         "Bank+cut = also exit the moment a trade goes −5% against you. Tiny sample — "
         "treat as a hypothesis being graded, not a verdict.")}
{section("Seller shadows — would the umbrella shop have won?", seller_html,
         "Zero-risk studies of option SELLING on our own tape, refreshed nightly. "
         "No margin costs modelled; charges approximated. A decision needs 30+ samples "
         "— and real selling needs capital and defined-risk spreads regardless.")}
{section("Research ignition countdown", countdown)}
{section("Nightly learning ledger", ledger_html,
         "Effect size = standard deviations separating winners from losers. Tiny samples wobble; watch trends across weeks.")}
{section("Karpathy research runs (every experiment, PASS/FAIL)", harness)}
</div>

<div class="tab" id="glossary">
{section("Every term, in plain language", f"<dl>{gloss}</dl>")}
</div>
"""


if __name__ == "__main__":
    page = build()
    OUT_PUB.parent.mkdir(parents=True, exist_ok=True)
    OUT_PUB.write_text(page)
    OUT_COPY.parent.mkdir(parents=True, exist_ok=True)
    OUT_COPY.write_text(page)
    print(f"research page → http://localhost:3777/research.html (file: {OUT_PUB})")
