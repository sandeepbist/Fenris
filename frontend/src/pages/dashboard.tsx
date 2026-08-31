/**
 * Dashboard: the first screen. Answers three questions at a glance —
 * is the index alive, how big is it, when did it last sync — and puts
 * the sync-now control one click away.
 *
 * Every API state has a deliberate visual: loading = skeletons (no
 * layout shift), setup missing = the banner with the fix, first run =
 * an empty state whose primary action IS the fix, errors = inline.
 */

import { useCallback, useEffect, useState } from "react";
import { motion } from "framer-motion";
import { RefreshCw, AlertTriangle } from "lucide-react";
import {
  api,
  SetupNeeded,
  type Health,
  type Status,
  type SyncReport,
} from "@/lib/api";
import {
  Card,
  CardHeader,
  Stat,
  Badge,
  Button,
  SkeletonRow,
} from "@/components/ui";
import { SetupNeededBanner, LoadError } from "@/components/setup-needed";

type Phase =
  | { kind: "loading" }
  | { kind: "setup"; error: SetupNeeded }
  | { kind: "error"; error: Error }
  | { kind: "ready"; health: Health; status: Status };

function relTime(iso: string | null): string {
  if (!iso) return "never";
  const secs = Math.floor((Date.now() - new Date(iso + "Z").getTime()) / 1000);
  if (secs < 60) return "just now";
  if (secs < 3600) return `${Math.floor(secs / 60)} min ago`;
  if (secs < 86400) return `${Math.floor(secs / 3600)} h ago`;
  return `${Math.floor(secs / 86400)} d ago`;
}

export function DashboardPage() {
  const [phase, setPhase] = useState<Phase>({ kind: "loading" });
  const [syncing, setSyncing] = useState(false);
  const [syncResult, setSyncResult] = useState<SyncReport | null>(null);
  const [syncSetup, setSyncSetup] = useState<SetupNeeded | null>(null);

  const load = useCallback(async () => {
    setPhase({ kind: "loading" });
    try {
      const [health, status] = await Promise.all([api.health(), api.status()]);
      setPhase({ kind: "ready", health, status });
    } catch (e) {
      if (e instanceof SetupNeeded) setPhase({ kind: "setup", error: e });
      else if (e instanceof Error) setPhase({ kind: "error", error: e });
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const runSync = useCallback(
    async (full: boolean) => {
      setSyncing(true);
      setSyncResult(null);
      setSyncSetup(null);
      try {
        const report = await api.sync(full);
        setSyncResult(report);
        void load(); // refresh counts after the pass
      } catch (e) {
        if (e instanceof SetupNeeded) setSyncSetup(e);
      } finally {
        setSyncing(false);
      }
    },
    [load]
  );

  if (phase.kind === "loading") {
    return (
      <div className="mx-auto max-w-3xl space-y-4 p-6">
        <h1 className="text-lg font-medium">Dashboard</h1>
        <Card>
          <CardHeader title="Index" />
          <div className="grid grid-cols-3 divide-x divide-border-subtle">
            {[0, 1, 2].map((i) => (
              <Stat key={i} label="…" value={<SkeletonRow className="w-16" />} />
            ))}
          </div>
        </Card>
        <Card>
          <CardHeader title="Sync" />
          <div className="grid grid-cols-3 divide-x divide-border-subtle">
            {[0, 1, 2].map((i) => (
              <Stat key={i} label="…" value={<SkeletonRow className="w-16" />} />
            ))}
          </div>
        </Card>
      </div>
    );
  }

  if (phase.kind === "setup") {
    return (
      <div className="mx-auto max-w-3xl space-y-4 p-6">
        <h1 className="text-lg font-medium">Dashboard</h1>
        <SetupNeededBanner error={phase.error} />
      </div>
    );
  }

  if (phase.kind === "error") {
    return (
      <div className="mx-auto max-w-3xl space-y-4 p-6">
        <h1 className="text-lg font-medium">Dashboard</h1>
        <LoadError error={phase.error} />
      </div>
    );
  }

  const { health, status } = phase;
  const firstRun =
    !health.has_db || health.index.drive_files === 0;

  if (firstRun) {
    return (
      <div className="mx-auto max-w-3xl space-y-4 p-6">
        <h1 className="text-lg font-medium">Dashboard</h1>
        <Card className="border-dashed p-10 text-center">
          <p className="text-base font-medium">Nothing indexed yet</p>
          <p className="mx-auto mt-2 max-w-sm text-sm text-text-muted">
            Connect your Drive once (<span className="mono text-xs">fenris drive auth</span>),
            then run the first sync. Your files stay on your Drive —
            Fenris only reads.
          </p>
          <div className="mt-6 flex justify-center gap-2">
            <Button variant="primary" onClick={() => runSync(false)} disabled={syncing}>
              {syncing ? "syncing…" : "Sync now"}
            </Button>
            <Button variant="ghost" onClick={() => void load()}>
              <RefreshCw size={14} /> Refresh
            </Button>
          </div>
          {syncSetup && (
            <div className="mt-4 text-left">
              <SetupNeededBanner error={syncSetup} />
            </div>
          )}
        </Card>
      </div>
    );
  }

  const cursorTone =
    status.cursor?.status === "ok"
      ? "answerable"
      : status.cursor?.status === "resync"
        ? "stale"
        : "insufficient";

  return (
    <div className="mx-auto max-w-3xl space-y-4 p-6">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-medium">Dashboard</h1>
        <Button variant="ghost" onClick={() => void load()}>
          <RefreshCw size={14} /> Refresh
        </Button>
      </div>

      <Card>
        <CardHeader title="Index" />
        <div className="grid grid-cols-3 divide-x divide-border-subtle">
          <Stat label="active sources" value={health.index.sources_active} tone="accent" />
          <Stat label="chunks" value={health.index.chunks} />
          <Stat label="tables" value={health.index.tables} />
          <Stat label="drive files" value={health.index.drive_files} />
          <Stat
            label="failed"
            value={health.index.sources_failed}
            tone={health.index.sources_failed > 0 ? "danger" : "default"}
          />
          <Stat
            label="skipped"
            value={health.index.sources_skipped}
            tone={health.index.sources_skipped > 0 ? "warn" : "default"}
          />
        </div>
        {health.index.sources_failed + health.index.sources_skipped > 0 && (
          <p className="mono border-t border-border-subtle px-4 py-2 text-xs text-text-faint">
            skipped = format not parsed in the slim build (PDF/image) or
            on the skip list — see Sources for per-file reasons
          </p>
        )}
      </Card>

      <Card>
        <CardHeader
          title="Sync"
          action={
            <div className="flex items-center gap-2">
              {status.stale_sync && (
                <Badge tone="stale">STALE</Badge>
              )}
              <Button variant="primary" onClick={() => runSync(false)} disabled={syncing}>
                {syncing ? "syncing…" : "Sync now"}
              </Button>
            </div>
          }
        />
        <div className="grid grid-cols-3 divide-x divide-border-subtle">
          <Stat
            label="cursor"
            value={<Badge tone={cursorTone as "answerable"}>{status.cursor?.status ?? "none"}</Badge>}
          />
          <Stat label="last drain" value={relTime(status.last_drain_at)} />
          <Stat label="retired sources" value={status.retired_sources} />
        </div>
        {status.stale_sync && (
          <p className="mono border-t border-border-subtle px-4 py-2 text-xs text-state-stale">
            last sync is older than the freshness window — answers may not
            reflect recent Drive changes
          </p>
        )}
      </Card>

      {syncResult && (
        <motion.div
          initial={{ opacity: 0, y: 8 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.2 }}
        >
          <Card>
            <CardHeader
              title="Last sync"
              action={
                syncResult.resync_triggered ? (
                  <Badge tone="stale">{syncResult.resync_triggered}</Badge>
                ) : undefined
              }
            />
            <div className="grid grid-cols-4 divide-x divide-border-subtle">
              <Stat label="changes" value={syncResult.changes_processed} />
              <Stat label="re-ingested" value={syncResult.content_reingests} tone="accent" />
              <Stat label="tombstoned" value={syncResult.tombstoned} />
              <Stat
                label="failed"
                value={syncResult.failed}
                tone={syncResult.failed > 0 ? "danger" : "default"}
              />
            </div>
            {syncResult.errors.length > 0 && (
              <div className="border-t border-border-subtle px-4 py-2">
                {syncResult.errors.slice(0, 3).map((err, i) => (
                  <p key={i} className="mono flex items-center gap-2 text-xs text-state-error">
                    <AlertTriangle size={12} className="shrink-0" />
                    <span className="truncate">{err}</span>
                  </p>
                ))}
              </div>
            )}
          </Card>
        </motion.div>
      )}

      {syncSetup && <SetupNeededBanner error={syncSetup} />}
    </div>
  );
}
