#!/usr/bin/env python3
"""Research dashboard — everything the experiment knows, on one page.

Regenerated nightly by evening_report.sh into:
    recorder/data/reports/research.html
Open it anytime (bookmark it); it is self-contained, works offline, and
never leaves this machine. Sections: scoreboard, every graded fill, the
learning ledger, research-ignition countdown, harness results, NSE data
collection, capture health.
"""
from __future__ import annotations

import html
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REC = ROOT / "recorder"
DATA = REC / "data"
OUT = DATA / "reports" / "research.html"
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


def build() -> str:
    now = datetime.now(IST)
    paper = [t for t in load_json(ROOT / "backend" / ".paper_trades.json", [])
             if isinstance(t, dict)]
    hollow_raw = load_json(ROOT / "backend" / ".hollow_signals.json", None)
    archive = load_jsonl(ROOT / "backend" / ".signals_archive.jsonl")
    ledger = load_jsonl(DATA / "learning_ledger.jsonl")

    # ---- scoreboard ----
    closed = [t for t in paper if t.get("status") not in ("entered", None)
              and isinstance(t.get("realized_pnl"), (int, float))
              and t.get("exited_at")]
    wins = [t for t in closed if t["realized_pnl"] > 0]
    gross = sum(t["realized_pnl"] for t in closed)
    score_rows = [[
        str(len(closed)),
        f"{len(wins)}W / {len(closed) - len(wins)}L",
        money(gross) + ' <span class="note">(gross — charges shown on dashboard Paper tab)</span>',
        money(gross / len(closed)) if closed else "—",
    ]]
    scoreboard = table(["Closed fills", "Win / Loss", "Total P&L", "Avg / trade"],
                       score_rows if closed else [])

    # ---- fills ----
    fill_rows = []
    for t in sorted(paper, key=lambda t: t.get("entered_at") or 0, reverse=True):
        pnl = t.get("realized_pnl")
        fill_rows.append([
            fmt_ts(t.get("entered_at")),
            esc(t.get("contract", "")), esc(t.get("mode", "")),
            f"{t.get('entry_premium', 0):.2f} → "
            + (f"{t.get('exit_premium'):.2f}" if t.get("exit_premium") else "open"),
            money(pnl) if isinstance(pnl, (int, float)) and t.get("exited_at") else "—",
            esc(t.get("auto_close_reason") or t.get("exit_reason") or t.get("status", "")),
            f"score {t.get('entry_score', '—')}",
        ])
    fills = table(["Entered", "Contract", "Mode", "Premium", "P&L (gross)", "Exit / status", "Entry score"],
                  fill_rows)

    # ---- cards ----
    cards: dict[str, dict] = {}
    for r in archive:
        c = r.get("card") if isinstance(r.get("card"), dict) else r
        if c.get("id"):
            cards[c["id"]] = c
    card_rows = [[
        fmt_ts(c.get("created_at")), esc(c.get("contract", "")),
        esc(c.get("mode", "")), esc(c.get("state", "")),
        str(c.get("confidence", "")),
    ] for c in sorted(cards.values(), key=lambda c: c.get("created_at") or 0, reverse=True)]
    cards_html = table(["Issued", "Contract", "Mode", "Final state", "Score"], card_rows)

    # ---- learning ledger ----
    led_rows = []
    for e in reversed(ledger[-14:]):
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
                         "Score calibration", "Research ignition"], led_rows)

    # ---- ignition countdown ----
    last = ledger[-1] if ledger else {}
    n_train = last.get("train_rows", 0)
    pct = min(100, int(100 * n_train / IGNITION_ROWS))
    countdown = (
        f'<div class="bar"><div class="fill" style="width:{pct}%"></div></div>'
        f'<p>{n_train} / {IGNITION_ROWS} training rows · holdout {last.get("holdout_rows", 0)} '
        f'(locked) · needs {IGNITION_MONTHS}+ months spread. The harness launches itself '
        f"the night both gates pass.</p>"
    )

    # ---- harness results ----
    res_p = REC / "experiment" / "results.tsv"
    if res_p.exists():
        lines = [l.split("\t") for l in res_p.read_text().splitlines()]
        harness = table(lines[0], lines[1:]) if len(lines) > 1 else '<p class="empty">header only</p>'
    else:
        harness = ('<p class="empty">No research runs yet — starts automatically at the '
                   'countdown above. The rules it will follow: recorder/experiment/PROGRAM.md</p>')

    # ---- NSE + capture health ----
    nse = cap = '<p class="empty">db missing</p>'
    db_p = DATA / "tradewell_history.db"
    if db_p.exists():
        db = sqlite3.connect(db_p)
        def q(sql):
            try:
                return db.execute(sql).fetchall()
            except sqlite3.Error:
                return []
        days = q("SELECT COUNT(DISTINCT day) FROM nse_fo_eod")
        rows_eod = q("SELECT COUNT(*) FROM nse_fo_eod")
        poi = q("SELECT COUNT(DISTINCT day) FROM nse_participant_oi")
        fii = q("SELECT day, fut_idx_long - fut_idx_short FROM nse_participant_oi "
                "WHERE participant='FII' ORDER BY day DESC LIMIT 5")
        nse = table(
            ["Metric", "Value"],
            [["Option EOD history", f"{(days or [[0]])[0][0]} days · {(rows_eod or [[0]])[0][0]:,} contracts"],
             ["Participant OI days", str((poi or [[0]])[0][0])]]
        ) + "<h3>FII net index-future position (latest days)</h3>" + table(
            ["Day", "Net contracts (long − short)"],
            [[esc(d), f"{v:+,.0f}"] for d, v in fii])
        snaps = q("SELECT endpoint, COUNT(*) FROM snapshots GROUP BY endpoint ORDER BY 2 DESC")
        births = q("SELECT COUNT(*) FROM card_births")
        cap = table(["Stream", "Rows"], [[esc(e), f"{n:,}"] for e, n in snaps]
                    + [["card-birth snapshots", str((births or [[0]])[0][0])]])

    style = """
    body{background:#0d1117;color:#c9d1d9;font:14px/1.5 -apple-system,system-ui,sans-serif;
         max-width:960px;margin:0 auto;padding:24px}
    h1{color:#e6edf3;font-size:22px} h2{color:#e6edf3;font-size:16px;border-bottom:1px solid #21262d;
         padding-bottom:6px;margin-top:28px} h3{font-size:13px;color:#8b949e}
    table{border-collapse:collapse;width:100%;font-size:13px}
    th{color:#8b949e;text-align:left;padding:6px 10px;border-bottom:1px solid #30363d}
    td{padding:6px 10px;border-bottom:1px solid #21262d}
    .scroll{overflow-x:auto} .pos{color:#3fb950} .neg{color:#f85149}
    .empty,.note{color:#8b949e;font-size:12px}
    .bar{background:#21262d;border-radius:6px;height:14px;overflow:hidden;margin:8px 0}
    .fill{background:linear-gradient(90deg,#1f6feb,#3fb950);height:100%}
    .meta{color:#8b949e;font-size:12px}
    """
    return f"""<!doctype html><meta charset="utf-8">
<title>Tradewell — Research Dashboard</title><style>{style}</style>
<h1>Tradewell Research Dashboard</h1>
<p class="meta">Regenerated {now.strftime('%d-%b-%Y %H:%M IST')} · refreshed nightly at 16:00 ·
data never leaves this machine</p>
{section("Paper book scoreboard", scoreboard,
         "Net-of-charges figures live on the dashboard Paper tab; this page shows the raw ledger.")}
{section("Every fill, graded", fills)}
{section("Every card the engine issued", cards_html)}
{section("Research ignition countdown", countdown)}
{section("Nightly learning ledger", ledger_html,
         "Effect size = how many standard deviations separate winners from losers on that feature. "
         "Tiny samples wobble — trends across weeks are what matter.")}
{section("Karpathy research runs (results.tsv)", harness)}
{section("NSE data collection (free archives, nightly)", nse)}
{section("Live capture health", cap)}
<p class="meta">Deeper digging: recorder/data/reports/*.txt (daily) ·
recorder/data/learning_ledger.jsonl · recorder/HOW-TO-READ-THE-DASHBOARD.md ·
recorder/TRAINING.md · recorder/GOAL.md</p>
"""


if __name__ == "__main__":
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(build())
    print(f"research dashboard → {OUT}")
