/**
 * Related files: what else in the corpus relates to the open document.
 * Co-cited (retrieved together in past answers) and vector-similar
 * neighbors, rank-fused. Click one to jump straight to its chunks.
 */

import { useEffect, useState } from "react";
import { Link2 } from "lucide-react";
import { api } from "@/lib/api";
import { Badge } from "@/components/ui";

interface RelatedEntry {
  file_id: string;
  title: string;
  reason: string;
  score: number;
}

const REASON_LABEL: Record<string, string> = {
  "co-cited": "cited together",
  similar: "similar",
  "co-cited+similar": "cited + similar",
};

export function RelatedFiles({
  driveId,
  fileId,
  onOpenChunk,
}: {
  driveId: string;
  fileId: string;
  onOpenChunk: (chunkId: number) => void;
}) {
  const [related, setRelated] = useState<RelatedEntry[] | null>(null);
  const [chunkByFile, setChunkByFile] = useState<Record<string, number>>({});

  useEffect(() => {
    if (!driveId || !fileId) return;
    let live = true;
    setRelated(null);
    api
      .relatedFiles(driveId, fileId)
      .then((r) => {
        if (!live) return;
        setRelated(r.related);
        // resolve each neighbor's first chunk for the jump
        const jumps: Record<string, number> = {};
        for (const entry of r.related) {
          api
            .fileRevisions(driveId, entry.file_id)
            .then((rev) => {
              const active = rev.revisions.find((x) => x.active);
              if (active && active.source_id) {
                // fetch one chunk id via search on the file's title
                api
                  .search(entry.title.split(" ").slice(0, 3).join(" "))
                  .then((res) => {
                    const hit = res.files.find((f) => f.file_id === entry.file_id);
                    if (hit && hit.matches.length) {
                      jumps[entry.file_id] = hit.matches[0].chunk_id;
                      setChunkByFile({ ...jumps });
                    }
                  })
                  .catch(() => undefined);
              }
            })
            .catch(() => undefined);
        }
      })
      .catch(() => live && setRelated([]));
    return () => {
      live = false;
    };
  }, [driveId, fileId]);

  if (related === null || related.length === 0) return null;

  return (
    <div className="border-t border-border-subtle px-5 py-3">
      <p className="mono mb-2 flex items-center gap-1.5 text-[10px] uppercase tracking-wider text-text-faint">
        <Link2 size={11} /> related
      </p>
      <div className="space-y-1">
        {related.map((r) => {
          const chunkId = chunkByFile[r.file_id];
          return (
            <button
              key={r.file_id}
              onClick={() => chunkId && onOpenChunk(chunkId)}
              disabled={!chunkId}
              className="flex w-full items-center gap-2 rounded-[--radius-sm] px-2.5 py-1.5 text-left text-sm text-text-muted transition-colors hover:bg-bg-overlay hover:text-text disabled:cursor-default disabled:opacity-60"
            >
              <span className="min-w-0 flex-1 truncate">{r.title}</span>
              <Badge tone="insufficient">{REASON_LABEL[r.reason] ?? r.reason}</Badge>
            </button>
          );
        })}
      </div>
    </div>
  );
}
