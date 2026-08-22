"""Does MMI at ~15:00 predict the 15:00 -> next-09:50 overnight result?

Joins the Closing Day study's per-night trades (3y, day_open headline rule)
with the reconstructed Tickertape MMI daily series.

Two MMI readings per trade, deliberately:
  mmi_same  EOD value of the entry day D. Closest proxy for "MMI at 3pm",
            but the EOD value is finalised ~after the close (and FII flow
            lands in the evening), so it carries a mild look-ahead.
  mmi_prev  EOD value of the previous trading day. Strictly known at 3pm.
If an effect shows only under mmi_same and dies under mmi_prev, it was
look-ahead, not signal.

MMI series: backend/.mmi_daily.csv — reconstructed Tickertape EOD values
(2012->present) from Wayback snapshots of the MMI page + its JSON endpoints
plus a public 2012-2024 dataset; sources agree to 0.00 median difference on
261 overlapping days. Regenerating it means ~1h of rate-limited archive
scraping, so the CSV is kept (gitignored), not the scraper.

Run with backend/.venv python, cwd = backend/.
"""
import csv
import json
import random
import statistics
import sys
from collections import defaultdict
from datetime import date, timedelta

sys.path.insert(0, ".")

from app.closing import store, validate
from app.closing.pricing import build_model
from app.closing.service import _config
from app.closing.study import run_study, _bootstrap_ci
from app.patterns import store as patterns_store

ZONES = [("ExtFear <30", 0, 30), ("Fear 30-50", 30, 50),
         ("Greed 50-70", 50, 70), ("ExtGreed >70", 70, 101)]


def zone(v):
    for name, lo, hi in ZONES:
        if lo <= v < hi:
            return name
    return None


def load_mmi():
    with open(".mmi_daily.csv") as f:
        return {row["date"]: float(row["mmi"]) for row in csv.DictReader(f)}


def prev_trading_mmi(mmi, d_iso):
    d = date.fromisoformat(d_iso)
    for back in range(1, 6):
        v = mmi.get((d - timedelta(days=back)).isoformat())
        if v is not None:
            return v
    return None


def spearman(xs, ys):
    def rank(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
                j += 1
            avg = (i + j) / 2 + 1
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r
    rx, ry = rank(xs), rank(ys)
    mx, my = statistics.mean(rx), statistics.mean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    dx = sum((a - mx) ** 2 for a in rx) ** 0.5
    dy = sum((b - my) ** 2 for b in ry) ** 0.5
    return num / (dx * dy) if dx and dy else 0.0


def perm_p(xs, ys, iters=3000, seed=7):
    obs = abs(spearman(xs, ys))
    rnd = random.Random(seed)
    ys2 = list(ys)
    hits = 0
    for _ in range(iters):
        rnd.shuffle(ys2)
        if abs(spearman(xs, ys2)) >= obs:
            hits += 1
    return (hits + 1) / (iters + 1)


def bucket_table(rows, key, field, label):
    print(f"\n  {label} — by {key}")
    print(f"  {'zone':<14}{'n':>5}{'mean':>9}{'median':>9}{'win%':>7}   ci95(mean)")
    grouped = defaultdict(list)
    for r in rows:
        grouped[zone(r[key])].append(r[field])
    for name, _, _ in ZONES:
        vals = grouped.get(name)
        if not vals:
            continue
        wins = sum(1 for v in vals if v > 0)
        ci = _bootstrap_ci(vals, 2000)
        print(f"  {name:<14}{len(vals):>5}{statistics.mean(vals):>9.2f}"
              f"{statistics.median(vals):>9.2f}{wins / len(vals) * 100:>6.1f}%"
              f"   {ci if ci else '(n<20)'}")


def main():
    mmi = load_mmi()
    print(f"MMI series: {len(mmi)} days, {min(mmi)} -> {max(mmi)}")

    spine = patterns_store.load_frame()
    vix = store.load_vix()
    spot = validate.SpotLookup(spine)
    model, fit = build_model(spot.at)
    cfg = _config(1, fit, "day_open")
    today = date.today()

    run = run_study(spine, vix, model, cfg, start=today - timedelta(days=365 * 3))
    trades = run["trades"]
    print(f"study trades (3y, day_open): {len(trades)}, "
          f"{trades[0]['date']} -> {trades[-1]['date']}")

    joined = []
    for t in trades:
        same = mmi.get(t["date"])
        prev = prev_trading_mmi(mmi, t["date"])
        if same is None and prev is None:
            continue
        r = dict(t)
        r["mmi_same"], r["mmi_prev"] = same, prev
        joined.append(r)

    n_same = sum(1 for r in joined if r["mmi_same"] is not None)
    n_prev = sum(1 for r in joined if r["mmi_prev"] is not None)
    print(f"joined: {len(joined)} trades with any MMI "
          f"({n_same} same-day, {n_prev} prev-day) "
          f"= {n_same / len(trades) * 100:.0f}% same-day coverage")

    for mkey, note in (("mmi_same", "same-day EOD (3pm proxy, mild look-ahead)"),
                       ("mmi_prev", "previous day EOD (strictly known at 3pm)")):
        rows = [r for r in joined if r[mkey] is not None]
        print(f"\n{'=' * 72}\nMMI reading: {note}  (n={len(rows)})")

        bucket_table(rows, mkey, "spot_move_pts",
                     "RAW overnight index move, pts (long-only view — no signal)")
        bucket_table(rows, mkey, "signed_move_pts",
                     "STRATEGY-signed index move, pts (day_open rule)")
        bucket_table(rows, mkey, "net_pct",
                     "STRATEGY option net P&L, % of premium (modelled)")

        for f2, lbl in (("spot_move_pts", "raw move"),
                        ("signed_move_pts", "signed move"),
                        ("net_pct", "option net%")):
            xs = [r[mkey] for r in rows]
            ys = [r[f2] for r in rows]
            rho = spearman(xs, ys)
            p = perm_p(xs, ys)
            print(f"  Spearman MMI level vs {lbl:<12} rho={rho:+.3f}  "
                  f"perm-p={p:.3f}  (n={len(rows)})")

        # direction split: does MMI matter differently for CE vs PE nights?
        for dirn in ("CE", "PE"):
            drows = [r for r in rows if r["direction"] == dirn]
            if len(drows) >= 30:
                xs = [r[mkey] for r in drows]
                ys = [r["net_pct"] for r in drows]
                print(f"  {dirn} nights only: n={len(drows)}, "
                      f"rho(MMI, net%)={spearman(xs, ys):+.3f} "
                      f"perm-p={perm_p(xs, ys):.3f}")

        # stability: does the sign of the relationship hold year by year?
        # A pattern that only lives in one sub-window is sample noise wearing
        # a hypothesis costume.
        print("  year-by-year rho(MMI, net%) / rho(MMI, signed pts):")
        byyear = defaultdict(list)
        for r in rows:
            byyear[r["date"][:4]].append(r)
        for y in sorted(byyear):
            yr = byyear[y]
            if len(yr) < 25:
                print(f"    {y}: n={len(yr)} (too few)")
                continue
            xs = [r[mkey] for r in yr]
            r1 = spearman(xs, [r["net_pct"] for r in yr])
            r2 = spearman(xs, [r["signed_move_pts"] for r in yr])
            g = [r["net_pct"] for r in yr if zone(r[mkey]) == "Greed 50-70"]
            rest = [r["net_pct"] for r in yr if zone(r[mkey]) != "Greed 50-70"]
            gtxt = (f"greed-zone mean {statistics.mean(g):+.1f}% (n={len(g)}) "
                    f"vs rest {statistics.mean(rest):+.1f}% (n={len(rest)})"
                    if len(g) >= 8 and rest else "")
            print(f"    {y}: n={len(yr)}  rho_net={r1:+.3f}  rho_signed={r2:+.3f}  {gtxt}")

    with open(".mmi_joined.json", "w") as f:
        json.dump(joined, f)
    print("\nsaved .mmi_joined.json")


if __name__ == "__main__":
    main()
