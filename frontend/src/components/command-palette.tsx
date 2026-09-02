/**
 * The command palette: ⌘K opens, type to filter, arrows navigate,
 * enter executes. Pages + actions + shortcuts, matching the terminal
 * aesthetic - mono, minimal chrome, no decoration.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  Database,
  Keyboard,
  Search,
  LayoutDashboard,
  MessageSquareText,
  RefreshCw,
  Settings as SettingsIcon,
} from "lucide-react";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";

interface Command {
  id: string;
  label: string;
  hint?: string;
  icon: React.ReactNode;
  run: () => void;
}

export function CommandPalette() {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState(0);
  const navigate = useNavigate();
  const inputRef = useRef<HTMLInputElement>(null);

  const commands: Command[] = useMemo(
    () => [
      { id: "dashboard", label: "Dashboard", hint: "index + sync state", icon: <LayoutDashboard size={14} />, run: () => navigate("/") },
      { id: "chat", label: "Chat", hint: "ask your Drive", icon: <MessageSquareText size={14} />, run: () => navigate("/chat") },
      { id: "search", label: "Search", hint: "find the file", icon: <Search size={14} />, run: () => navigate("/search") },
      { id: "sources", label: "Sources", hint: "the index ledger", icon: <Database size={14} />, run: () => navigate("/sources") },
      { id: "settings", label: "Settings", hint: "setup + privacy + legal", icon: <SettingsIcon size={14} />, run: () => navigate("/settings") },
      {
        id: "sync",
        label: "Sync now",
        hint: "drain the Drive change feed",
        icon: <RefreshCw size={14} />,
        run: () => {
          void api.sync().catch(() => undefined);
          navigate("/");
        },
      },
      {
        id: "doctor",
        label: "Run diagnostics",
        hint: "same as fenris doctor",
        icon: <Keyboard size={14} />,
        run: () => {
          void api.doctor().catch(() => undefined);
          navigate("/settings");
        },
      },
    ],
    [navigate],
  );

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return commands;
    return commands.filter(
      (c) =>
        c.label.toLowerCase().includes(q) ||
        (c.hint ?? "").toLowerCase().includes(q),
    );
  }, [commands, query]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setOpen((v) => !v);
        setQuery("");
        setSelected(0);
      }
      if (e.key === "Escape") setOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  useEffect(() => {
    if (open) setTimeout(() => inputRef.current?.focus(), 10);
    else setSelected(0);
  }, [open]);

  useEffect(() => {
    setSelected(Math.min(selected, Math.max(filtered.length - 1, 0)));
  }, [filtered, selected]);

  if (!open) return null;

  const execute = (i: number) => {
    const cmd = filtered[i];
    if (!cmd) return;
    setOpen(false);
    cmd.run();
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center bg-bg/70 pt-32 backdrop-blur-sm"
      onClick={() => setOpen(false)}
    >
      <div
        className="w-full max-w-md overflow-hidden rounded-[--radius] border border-border bg-bg-raised shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <input
          ref={inputRef}
          value={query}
          onChange={(e) => {
            setQuery(e.target.value);
            setSelected(0);
          }}
          onKeyDown={(e) => {
            if (e.key === "ArrowDown") {
              e.preventDefault();
              setSelected((s) => Math.min(s + 1, filtered.length - 1));
            } else if (e.key === "ArrowUp") {
              e.preventDefault();
              setSelected((s) => Math.max(s - 1, 0));
            } else if (e.key === "Enter") {
              e.preventDefault();
              execute(selected);
            }
          }}
          placeholder="type a command…"
          className="mono w-full border-b border-border-subtle bg-transparent px-4 py-3 text-sm text-text placeholder:text-text-faint focus:outline-none"
        />
        <div className="max-h-72 overflow-y-auto p-1.5">
          {filtered.length === 0 && (
            <p className="mono px-3 py-4 text-xs text-text-faint">no match</p>
          )}
          {filtered.map((c, i) => (
            <button
              key={c.id}
              onClick={() => execute(i)}
              className={cn(
                "flex w-full items-center gap-2.5 rounded-[--radius-sm] px-3 py-2 text-left text-sm",
                i === selected
                  ? "bg-accent-dim text-accent"
                  : "text-text-muted hover:bg-bg-overlay",
              )}
              onMouseEnter={() => setSelected(i)}
            >
              <span className="shrink-0 opacity-70">{c.icon}</span>
              <span className="flex-1">{c.label}</span>
              {c.hint && (
                <span className="mono text-[10px] text-text-faint">{c.hint}</span>
              )}
            </button>
          ))}
        </div>
        <div className="mono flex items-center justify-between border-t border-border-subtle px-3 py-1.5 text-[10px] text-text-faint">
          <span>↑↓ navigate · ⏎ run · esc close</span>
          <span>⌘K</span>
        </div>
      </div>
    </div>
  );
}
