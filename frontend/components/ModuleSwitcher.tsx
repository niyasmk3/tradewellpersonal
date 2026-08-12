"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

/**
 * The module toggle, top bar of every module page: PULSE is the live trading
 * module (signals, journal, paper — the original Tradewell), PATTERNS is the
 * standalone 3-year research lab, OPENING is the first-45 screen built on the
 * lab's spine. The active one is lit so you always know which world you're
 * reading.
 */
const MODULES = [
  { key: "pulse", label: "Pulse", href: "/", title: "Live trading module — signals, journal, paper evidence" },
  { key: "patterns", label: "Patterns", href: "/patterns", title: "Research module — 3y NIFTY 5-min patterns lab" },
  { key: "opening", label: "Opening", href: "/opening", title: "Opening window — first-45 live read, study tables, scoreboard" },
  { key: "condor", label: "Condor", href: "/condor", title: "Iron Condor — range-regime credit structures (advisory, trial)" },
  { key: "rnd", label: "R&D", href: "/rnd", title: "Research & Development — signal window analytics" },
] as const;

export function ModuleSwitcher() {
  const path = usePathname() ?? "/";
  const activeKey = path.startsWith("/patterns")
    ? "patterns"
    : path.startsWith("/opening")
      ? "opening"
      : path.startsWith("/condor")
        ? "condor"
        : path.startsWith("/rnd")
          ? "rnd"
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
