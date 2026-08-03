"use client";

import { ReactNode, useEffect } from "react";

export type ContextTab = {
  key: string;
  label: string;
  node: ReactNode;
  badge?: boolean;
  count?: number;
};

/**
 * Tabbed rail for the reference panels (chain / news / journal / backtest).
 * These are mutually exclusive in practice — stacking them cost ~1,000px of
 * permanent height and forced constant scrolling.
 *
 * Every pane stays MOUNTED (inactive ones are `hidden`), so a finished backtest,
 * the chain's scroll position and in-flight polling all survive a tab switch.
 * Tabs never auto-switch: the layout must not move under the trader's cursor.
 */
export function ContextRail({
  tabs,
  tab,
  onTab,
  className = "",
}: {
  tabs: ContextTab[];
  tab: string;
  onTab: (k: string) => void;
  className?: string;
}) {
  // If the active tab disappears (e.g. Backtest hidden on non-NIFTY), fall back.
  useEffect(() => {
    if (tabs.length && !tabs.some((t) => t.key === tab)) onTab(tabs[0].key);
  }, [tabs, tab, onTab]);

  return (
    <div className={`card flex min-h-0 flex-col overflow-hidden ${className}`}>
      {/* flex-wrap: eight tabs overflow the rail's fixed width, and an
          overflow-hidden card silently amputates whatever renders last. */}
      <div className="flex shrink-0 flex-wrap items-center gap-1 border-b border-edge px-1.5 py-1">
        {tabs.map((t) => {
          const active = t.key === tab;
          return (
            <button
              key={t.key}
              onClick={() => onTab(t.key)}
              className={`relative rounded px-2.5 py-1 text-xs font-medium transition ${
                active ? "bg-panel2 text-white" : "text-muted hover:text-white"
              }`}
            >
              {t.label}
              {t.count != null && t.count > 0 && (
                <span className="ml-1 rounded bg-accent/20 px-1 font-mono text-[10px] text-accent">
                  {t.count}
                </span>
              )}
              {t.badge && (
                <span className="absolute right-0.5 top-0.5 h-1.5 w-1.5 rounded-full bg-yellow-400" />
              )}
            </button>
          );
        })}
      </div>

      {tabs.map((t) => (
        <div
          key={t.key}
          className={
            t.key === tab
              ? "min-h-0 flex-1 overflow-y-auto scroll-thin"
              : "hidden"
          }
        >
          {t.node}
        </div>
      ))}
    </div>
  );
}
