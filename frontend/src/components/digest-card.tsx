/**
 * DigestCard: the P2.4 surface. Shows the latest digest (counts + markdown
 * preview), generates one on demand, and manages schedules (add daily /
 * weekly at HH:MM, toggle, delete). A quiet Drive renders as a valid
 * "nothing changed" digest — never an error state.
 */

import { useCallback, useEffect, useState } from "react";
import { CalendarClock, Mail, Trash2, Play } from "lucide-react";
import {
  api,
  type DigestPayload,
  type DigestScheduleRow,
} from "@/lib/api";
import { Badge, Button, Card, CardHeader, SkeletonRow } from "@/components/ui";

const DOW_NAMES = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"];

export function DigestCard({ onGenerated }: { onGenerated?: () => void }) {
  const [digest, setDigest] = useState<DigestPayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [generating, setGenerating] = useState(false);
  const [expanded, setExpanded] = useState(false);

  const [schedules, setSchedules] = useState<DigestScheduleRow[] | null>(null);
  const [showAdd, setShowAdd] = useState(false);
  const [name, setName] = useState("Weekly Drive digest");
  const [at, setAt] = useState("08:00");
  const [daily, setDaily] = useState(false);
  const [dow, setDow] = useState("mon");
  const [addError, setAddError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [latest, scheds] = await Promise.all([
        api.latestDigest(),
        api.digestSchedules(),
      ]);
      setDigest(latest.digest);
      setSchedules(scheds.schedules);
    } catch {
      /* digest surfaces degrade silently: an unreachable engine shows
         skeletons, matching the dashboard's other cards */
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const generate = useCallback(async () => {
    setGenerating(true);
    try {
      const r = await api.generateDigest(7);
      setDigest({
        generated_at: r.generated_at,
        window_start: r.window_start,
        window_days: r.window_days,
        counts: r.counts,
        markdown: r.markdown,
      });
      onGenerated?.();
    } catch {
      /* the button returns to idle; the last digest stays visible */
    } finally {
      setGenerating(false);
    }
  }, [onGenerated]);

  const addSchedule = useCallback(async () => {
    setAddError(null);
    if (!name.trim()) {
      setAddError("name required");
      return;
    }
    try {
      await api.addDigestSchedule({
        name: name.trim(),
        at,
        daily,
        dow,
        window_days: 7,
      });
      setShowAdd(false);
      const scheds = await api.digestSchedules();
      setSchedules(scheds.schedules);
    } catch (e) {
      setAddError(e instanceof Error ? e.message : "failed to add");
    }
  }, [name, at, daily, dow]);

  const toggle = useCallback(async (sid: number, enabled: boolean) => {
    await api.toggleDigestSchedule(sid, enabled).catch(() => undefined);
    const scheds = await api.digestSchedules();
    setSchedules(scheds.schedules);
  }, []);

  const remove = useCallback(async (sid: number) => {
    await api.deleteDigestSchedule(sid).catch(() => undefined);
    const scheds = await api.digestSchedules();
    setSchedules(scheds.schedules);
  }, []);

  const c = digest?.counts;
  const quiet = c && c.new + c.updated + c.removed + c.problems + c.skipped === 0;

  return (
    <Card>
      <CardHeader
        title="Digest"
        action={
          <div className="flex items-center gap-2">
            <Button variant="ghost" onClick={() => setExpanded((v) => !v)}>
              <CalendarClock size={14} /> {expanded ? "hide" : "schedules"}
            </Button>
            <Button variant="primary" onClick={() => void generate()} disabled={generating}>
              <Play size={14} /> {generating ? "digesting…" : "Generate now"}
            </Button>
          </div>
        }
      />
      {loading ? (
        <div className="space-y-2 px-4 py-4">
          <SkeletonRow className="w-3/4" />
          <SkeletonRow className="w-1/2" />
        </div>
      ) : digest ? (
        <>
          <div className="px-4 py-3">
            <p className="mono text-xs text-text-muted">
              {digest.window_start} → {digest.generated_at} (~{digest.window_days}d window)
            </p>
            <div className="mt-2 flex flex-wrap items-center gap-1.5">
              <Badge tone={c && c.new > 0 ? "answerable" : "insufficient"}>
                {c?.new ?? 0} new
              </Badge>
              <Badge tone={c && c.updated > 0 ? "accent" : "insufficient"}>
                {c?.updated ?? 0} updated
              </Badge>
              <Badge tone={c && c.removed > 0 ? "stale" : "insufficient"}>
                {c?.removed ?? 0} removed
              </Badge>
              <Badge tone={c && c.problems > 0 ? "error" : "insufficient"}>
                {c?.problems ?? 0} problems
              </Badge>
              <Badge tone={c && c.skipped > 0 ? "conflicting" : "insufficient"}>
                {c?.skipped ?? 0} skipped
              </Badge>
            </div>
            {quiet && (
              <p className="mono mt-2 text-xs text-text-faint">
                nothing changed in this window — a quiet Drive is a valid digest
              </p>
            )}
          </div>
          <details className="border-t border-border-subtle">
            <summary className="mono cursor-pointer select-none px-4 py-2 text-xs text-text-muted hover:bg-bg-overlay">
              full digest
            </summary>
            <pre className="mono max-h-72 overflow-auto px-4 pb-3 text-xs leading-relaxed text-text">
              {digest.markdown}
            </pre>
          </details>
        </>
      ) : (
        <div className="px-4 py-4">
          <p className="text-sm text-text-muted">No digest yet.</p>
          <p className="mono mt-1 text-xs text-text-faint">
            generate one now, or schedule "every Monday 8:00: what changed"
          </p>
        </div>
      )}

      {expanded && (
        <div className="border-t border-border-subtle">
          {schedules === null ? (
            <div className="px-4 py-3">
              <SkeletonRow />
            </div>
          ) : schedules.length === 0 && !showAdd ? (
            <div className="flex items-center justify-between px-4 py-3">
              <p className="mono text-xs text-text-faint">no schedules</p>
              <Button variant="default" onClick={() => setShowAdd(true)}>
                <Mail size={14} /> Add schedule
              </Button>
            </div>
          ) : (
            <div className="divide-y divide-border-subtle">
              {schedules.map((s) => (
                <div key={s.id} className="flex items-center gap-3 px-4 py-2.5">
                  <Badge tone={s.enabled ? "answerable" : "insufficient"}>
                    {s.enabled ? "ON" : "OFF"}
                  </Badge>
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-sm">{s.name}</p>
                    <p className="mono text-[10px] text-text-faint">
                      {s.daily ? "daily" : `weekly ${DOW_NAMES[Number(s.dow) % 7]}`} at {s.at} · window {s.window_days}d
                      {s.last_run_at ? ` · last ${s.last_run_at}` : " · never run"}
                    </p>
                  </div>
                  <Button variant="ghost" onClick={() => void toggle(s.id, !s.enabled)}>
                    {s.enabled ? "turn off" : "turn on"}
                  </Button>
                  <Button variant="ghost" onClick={() => void remove(s.id)}>
                    <Trash2 size={14} />
                  </Button>
                </div>
              ))}
            </div>
          )}
          {showAdd ? (
            <div className="space-y-2 border-t border-border-subtle px-4 py-3">
              <div className="flex gap-2">
                <input
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  placeholder="schedule name"
                  className="mono min-w-0 flex-1 rounded-[--radius-sm] border border-border bg-transparent px-2 py-1 text-xs text-text placeholder:text-text-faint focus:outline-none focus:border-accent"
                />
                <input
                  value={at}
                  onChange={(e) => setAt(e.target.value)}
                  placeholder="08:00"
                  className="mono w-20 rounded-[--radius-sm] border border-border bg-transparent px-2 py-1 text-xs text-text focus:outline-none focus:border-accent"
                />
              </div>
              <div className="flex items-center gap-2 text-xs text-text-muted">
                <button
                  onClick={() => setDaily(true)}
                  className={`mono rounded-[--radius-sm] border px-2 py-0.5 ${daily ? "border-accent text-accent" : "border-border"}`}
                >
                  daily
                </button>
                <button
                  onClick={() => setDaily(false)}
                  className={`mono rounded-[--radius-sm] border px-2 py-0.5 ${!daily ? "border-accent text-accent" : "border-border"}`}
                >
                  weekly
                </button>
                {!daily &&
                  DOW_NAMES.map((d) => (
                    <button
                      key={d}
                      onClick={() => setDow(d)}
                      className={`mono rounded-[--radius-sm] border px-1.5 py-0.5 ${dow === d ? "border-accent text-accent" : "border-border"}`}
                    >
                      {d}
                    </button>
                  ))}
              </div>
              {addError && <p className="mono text-xs text-state-error">{addError}</p>}
              <div className="flex gap-2">
                <Button variant="primary" onClick={() => void addSchedule()}>
                  Save
                </Button>
                <Button variant="ghost" onClick={() => setShowAdd(false)}>
                  Cancel
                </Button>
              </div>
            </div>
          ) : (
            schedules !== null &&
            schedules.length > 0 && (
              <div className="border-t border-border-subtle px-4 py-2.5">
                <Button variant="default" onClick={() => setShowAdd(true)}>
                  <Mail size={14} /> Add schedule
                </Button>
              </div>
            )
          )}
        </div>
      )}
    </Card>
  );
}
