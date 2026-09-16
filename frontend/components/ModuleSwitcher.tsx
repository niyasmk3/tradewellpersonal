"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

/**
 * The module toggle, top bar of every module page: PULSE is the live trading
 * module (signals, journal, paper — the original Tradewell), PATTERNS is the
 * standalone 3-year research lab, CLOSING DAY is the overnight-continuation
 * study. The active one is lit so you always know which world you're reading.
 */
const MODULES = [
  { key: "pulse", label: "Pulse", href: "/", title: "Live trading module — signals, journal, paper evidence" },
  { key: "patterns", label: "Patterns", href: "/patterns", title: "Research module — 3y NIFTY 5-min patterns lab" },
  { key: "rnd", label: "R&D", href: "/rnd", title: "Research & Development — signal window analytics" },
  { key: "closing", label: "Closing Day", href: "/closing", title: "Closing Day Strategy — 15:00 direction, held overnight to 09:50 (unfiltered study)" },
  { key: "gold", label: "Gold", href: "/gold", title: "Gold lab — MCX GOLDM + XAUUSD shadow research: three frozen rules, forward samples only (paper)" },
  { key: "algo", label: "Algo", href: "/algo", title: "Algo execution — arm a strategy's adapter behind the guard; dry-run only today (no order can be placed)" },
] as const;

export function ModuleSwitcher() {
  const path = usePathname() ?? "/";
  const activeKey = path.startsWith("/patterns")
    ? "patterns"
    : path.startsWith("/rnd")
      ? "rnd"
      : path.startsWith("/closing")
        ? "closing"
        : path.startsWith("/gold")
          ? "gold"
          : path.startsWith("/algo")
            ? "algo"
            : "pulse";
  return (
    <nav className="inline-flex rounded-md border border-edge bg-panel p-0.5" aria-label="Module">
      {MODULES.map((m) => (
        <Link
          key={m.key}
          href={m.href}
          title={m.title}
          className={`rounded px-2.5 py-0.5 text-xs font-medium transition ${
            m.key === activeKey ? "bg-accent text-white" : "text-muted hover:text-white"
          }`}
        >
          {m.label}
        </Link>
      ))}
    </nav>
  );
}
