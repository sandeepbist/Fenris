/**
 * Sources: what Fenris has read from your Drive, and what it did with
 * each file. The table is the honest ledger - status colors are facts
 * (ok/skipped/failed/unreadable), trashed and retired states visible,
 * not hidden. A personal corpus is small enough to browse whole.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { RefreshCw } from "lucide-react";
import { api, SetupNeeded, type SourceRow } from "@/lib/api";
import { Badge, Button, Card, CardHeader, SkeletonRow } from "@/components/ui";
import { SetupNeededBanner, LoadError } from "@/components/setup-needed";
import { cn } from "@/lib/utils";

const STATUS_TONE: Record<string, "answerable" | "stale" | "error" | "insufficient" | "accent"> = {
  ok: "answerable",
  skipped: "stale",
  failed: "error",
  unreadable: "error",
  pending_export: "stale",
};

export function SourcesPage() {
  const [phase, setPhase] = useState<
    | { kind: "loading" }
    | { kind: "setup"; error: SetupNeeded }
    | { kind: "error"; error: Error }
    | { kind: "ready"; rows: SourceRow[] }
  >({ kind: "loading" });
  const [filter, setFilter] = useState<string>("all");

  const load = useCallback(async () => {
    setPhase({ kind: "loading" });
    try {
      const res = await api.sources(500);
      setPhase({ kind: "ready", rows: res.sources });
    } catch (e) {
      if (e instanceof SetupNeeded) setPhase({ kind: "setup", error: e });
      else if (e instanceof Error) setPhase({ kind: "error", error: e });
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const counts = useMemo(() => {
    const c: Record<string, number> = { all: 0, ok: 0, skipped: 0, failed: 0, retired: 0 };
    const rows = phase.kind === "ready" ? phase.rows : [];
    for (const r of rows) {
      c.all++;
      if (r.active) c[r.status] = (c[r.status] ?? 0) + 1;
      else c.retired++;
    }
    return c;
  }, [phase]);

  if (phase.kind === "loading") {
    return (
      <div className="mx-auto max-w-4xl space-y-4 p-6">
        <h1 className="text-lg font-medium">Sources</h1>
        <Card className="p-4">
          {[0, 1, 2, 3, 4].map((i) => (
            <SkeletonRow key={i} className="mb-3 w-full" />
          ))}
        </Card>
      </div>
    );
  }
  if (phase.kind === "setup")
    return (
      <div className="mx-auto max-w-4xl space-y-4 p-6">
        <h1 className="text-lg font-medium">Sources</h1>
        <SetupNeededBanner error={phase.error} />
      </div>
    );
  if (phase.kind === "error")
    return (
      <div className="mx-auto max-w-4xl space-y-4 p-6">
        <h1 className="text-lg font-medium">Sources</h1>
        <LoadError error={phase.error} />
      </div>
    );

  const rows = phase.rows.filter((r) => {
    if (filter === "all") return true;
    if (filter === "retired") return !r.active;
    return r.active && r.status === filter;
  });

  return (
    <div className="mx-auto max-w-4xl space-y-4 p-6">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-medium">Sources</h1>
        <Button variant="ghost" onClick={() => void load()}>
          <RefreshCw size={14} /> Refresh
        </Button>
      </div>

      <div className="flex flex-wrap gap-2">
        {["all", "ok", "skipped", "failed", "retired"].map((f) => (
          <button
            key={f}
            onClick={() => setFilter(f)}
            className={cn(
              "mono rounded-full border px-3 py-1 text-[11px] tracking-wide",
              filter === f
                ? "border-accent/40 bg-accent-dim text-accent"
                : "border-border text-text-muted hover:text-text"
            )}
          >
            {f} {counts[f] ?? 0}
          </button>
        ))}
      </div>

      <Card>
        <CardHeader title={`${rows.length} ${rows.length === 1 ? "file" : "files"}`} />
        {rows.length === 0 ? (
          <div className="p-8 text-center">
            <p className="text-sm text-text-muted">
              {phase.rows.length === 0
                ? "Nothing indexed yet — run a sync from the Dashboard."
                : "No files match this filter."}
            </p>
          </div>
        ) : (
          <div className="divide-y divide-border-subtle">
            {rows.map((r) => (
              <div key={r.file_id} className="flex items-center gap-3 px-4 py-2.5">
                <span
                  className={cn(
                    "h-1.5 w-1.5 shrink-0 rounded-full",
                    r.active ? "bg-state-answerable" : "bg-text-faint"
                  )}
                />
                <div className="min-w-0 flex-1">
                  <p className={cn("truncate text-sm", !r.active && "text-text-faint line-through")}>
                    {r.title}
                  </p>
                  <p className="mono truncate text-[10px] text-text-faint">
                    {r.revision} · {r.indexed_at ?? "—"}
                  </p>
                </div>
                {r.trashed && <Badge tone="insufficient">TRASHED</Badge>}
                {!r.active && !r.trashed && <Badge tone="insufficient">RETIRED</Badge>}
                <Badge tone={STATUS_TONE[r.status] ?? "insufficient"}>{r.status}</Badge>
              </div>
            ))}
          </div>
        )}
      </Card>
    </div>
  );
}
