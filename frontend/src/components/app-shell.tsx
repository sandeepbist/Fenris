/**
 * The app shell: top bar with the wolf mark, sidebar nav, routed content.
 * Dark, quiet, terminal-adjacent — chrome recedes, content speaks.
 */

import { NavLink, Outlet } from "react-router-dom";
import {
  LayoutDashboard,
  MessageSquareText,
  Database,
  Settings,
} from "lucide-react";
import { WolfMark } from "@/components/wolf-mark";
import { cn } from "@/lib/utils";

const NAV = [
  { to: "/", label: "Dashboard", icon: LayoutDashboard },
  { to: "/chat", label: "Chat", icon: MessageSquareText },
  { to: "/sources", label: "Sources", icon: Database },
  { to: "/settings", label: "Settings", icon: Settings },
];

export function AppShell() {
  return (
    <div className="flex h-screen flex-col bg-bg text-text">
      {/* top bar */}
      <header className="flex h-12 shrink-0 items-center gap-3 border-b border-border-subtle px-4">
        <WolfMark size={26} glow />
        <span className="mono text-sm font-semibold tracking-[0.25em] text-text">
          FENRIS
        </span>
        <span className="mono text-xs text-text-faint">
          rag over your own drive
        </span>
      </header>

      <div className="flex min-h-0 flex-1">
        {/* sidebar */}
        <nav className="flex w-48 shrink-0 flex-col gap-1 border-r border-border-subtle p-3">
          {NAV.map(({ to, label, icon: Icon }) => (
            <NavLink
              key={to}
              to={to}
              end={to === "/"}
              className={({ isActive }) =>
                cn(
                  "flex items-center gap-2.5 rounded-[--radius-sm] px-3 py-2 text-sm",
                  isActive
                    ? "bg-accent-dim text-accent"
                    : "text-text-muted hover:bg-bg-raised hover:text-text"
                )
              }
            >
              <Icon size={16} strokeWidth={1.75} />
              {label}
            </NavLink>
          ))}

          <div className="mt-auto px-3 py-2">
            <span className="mono text-[10px] text-text-faint">
              local · private
            </span>
          </div>
        </nav>

        {/* content */}
        <main className="min-w-0 flex-1 overflow-auto bg-bg">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
