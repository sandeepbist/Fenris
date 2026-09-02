/**
 * Revision history: every indexed version of the open document, newest
 * first; pick two to see what changed. The differentiator no shipped
 * product has - built on Fenris's revision-aware projection.
 */

import { useEffect, useState } from "react";
import { ChevronDown, History } from "lucide-react";
import { api, type RevisionDiffPayload, type RevisionRow } from "@/lib/api";
import { Badge } from "@/components/ui";
import { cn } from "@/lib/utils";

export function RevisionHistory({
  driveId,
  fileId,
}: {
  driveId: string;
  fileId: string;
}) {
  const [open, setOpen] = useState(false);
  const [revs, setRevs] = useState<RevisionRow[] | null>(null);
  const [picked, setPicked] = useState<number[]>([]);
  const [diff, setDiff] = useState<RevisionDiffPayload | null>(null);

  useEffect(() => {
    if (!open || revs !== null || !driveId) return;
    api
      .fileRevisions(driveId, fileId)
      .then((r) => setRevs(r.revisions))
      .catch(() => setRevs([]));
  }, [open, revs, driveId, fileId]);

  useEffect(() => {
    if (picked.length !== 2 || !driveId) {
      setDiff(null);
      return;
    }
    const [a, b] = picked;
    api
      .revisionDiff(driveId, fileId, a, b)
      .then(setDiff)
      .catch(() => setDiff(null));
  }, [picked, driveId, fileId]);

  if (!driveId) return null;
  const hasHistory = (revs?.length ?? 0) > 1;

  return (
    <div className="border-t border-border-subtle">
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 px-5 py-2.5 text-left"
      >
        <History size={13} className="shrink-0 text-text-faint" />
        <span className="text-sm text-text-muted">Revision history</span>
        {revs && hasHistory && (
          <span className="mono text-[10px] text-text-faint">
            {revs.length} versions
          </span>
        )}
        <ChevronDown
          size={13}
          className={cn(
            "ml-auto shrink-0 text-text-faint transition-transform",
            open && "rotate-180",
          )}
        />
      </button>

      {open && (
        <div className="space-y-2 px-5 pb-4">
          {revs === null && (
            <p className="mono text-xs text-text-faint">loading…</p>
          )}
          {revs?.length === 0 && (
            <p className="mono text-xs text-text-faint">no revisions indexed</p>
          )}
          {revs?.length === 1 && (
            <p className="mono text-xs text-text-faint">
              only one revision indexed - edits create history after a sync
            </p>
          )}
          {hasHistory && (
            <>
              <p className="mono text-[10px] text-text-faint">
                pick two to diff
              </p>
              <div className="space-y-1">
                {revs!.map((r) => (
                  <button
                    key={r.source_id}
                    onClick={() =>
                      setPicked((p) =>
                        p.includes(r.source_id)
                          ? p.filter((x) => x !== r.source_id)
                          : [...p, r.source_id].slice(-2),
                      )
                    }
                    className={cn(
                      "flex w-full items-center gap-2 rounded-[--radius-sm] px-3 py-1.5 text-left text-sm transition-colors",
                      picked.includes(r.source_id)
                        ? "bg-accent-dim text-accent"
                        : "text-text-muted hover:bg-bg-overlay",
                    )}
                  >
                    <span className="mono truncate text-xs">
                      rev {r.revision}
                    </span>
                    {r.active && <Badge tone="answerable">current</Badge>}
                    <span className="mono ml-auto shrink-0 text-[10px] text-text-faint">
                      {r.chunk_count} chunks
                    </span>
                  </button>
                ))}
              </div>
            </>
          )}

          {diff && (
            <div className="rounded-[--radius-sm] border border-border-subtle p-3">
              <p className="mb-2 text-sm text-text">{diff.summary}</p>
              {diff.removed.map((c, i) => (
                <p
                  key={`r${i}`}
                  className="mb-1 border-l-2 border-state-error/60 pl-2 text-xs leading-relaxed text-text-muted"
                >
                  − {c.body.slice(0, 200)}
                </p>
              ))}
              {diff.added.map((c, i) => (
                <p
                  key={`a${i}`}
                  className="mb-1 border-l-2 border-state-answerable/60 pl-2 text-xs leading-relaxed text-text-muted"
                >
                  + {c.body.slice(0, 200)}
                </p>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
