"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

/**
 * The module toggle, top bar of both pages: PULSE is the live trading module
 * (signals, journal, paper — the original Tradewell), PATTERNS is the
 * standalone 3-year research lab. One click juggles between them; the active
 * one is lit so you always know which world you're reading.
 */
const MODULES = [
  { key: "pulse", label: "Pulse", href: "/", title: "Live trading module — signals, journal, paper evidence" },
  { key: "patterns", label: "Patterns", href: "/patterns", title: "Research module — 3y NIFTY 5-min patterns lab" },
] as const;

export function ModuleSwitcher() {
  const path = usePathname() ?? "/";
  const activeKey = path.startsWith("/patterns") ? "patterns" : "pulse";
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
