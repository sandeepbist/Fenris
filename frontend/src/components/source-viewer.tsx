/**
 * The source viewer: click a citation, read the document around it.
 * The cited chunk is highlighted and scrolled to; neighbors give context.
 * Everything comes from the local projection - no Drive fetch, works
 * offline.
 */

import { useEffect, useRef, useState } from "react";
import { X } from "lucide-react";
import { api, type SourceView } from "@/lib/api";
import { Badge, SkeletonRow } from "@/components/ui";
import { cn } from "@/lib/utils";

export function SourceViewer({
  chunkId,
  onClose,
}: {
  chunkId: number;
  onClose: () => void;
}) {
  const [view, setView] = useState<SourceView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const citedRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let live = true;
    api
      .sourceView(chunkId)
      .then((v) => live && setView(v))
      .catch((e: Error) => live && setError(e.message));
    return () => {
      live = false;
    };
  }, [chunkId]);

  useEffect(() => {
    if (view) setTimeout(() => citedRef.current?.scrollIntoView({ block: "center" }), 30);
  }, [view]);

  // esc closes
  useEffect(() => {
    const h = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, [onClose]);

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-bg/80 p-6 backdrop-blur-sm"
      onClick={onClose}
    >
      <div
        className="flex max-h-full w-full max-w-2xl flex-col rounded-[--radius] border border-border bg-bg-raised"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-start justify-between gap-3 border-b border-border-subtle px-5 py-3">
          <div className="min-w-0">
            <p className="truncate text-sm font-medium">{view?.title ?? "Loading…"}</p>
            {view && (
              <p className="mono text-[10px] text-text-faint">
                rev {view.revision}
                {view.modified_time ? ` · ${view.modified_time}` : ""} · local projection
              </p>
            )}
          </div>
          <button onClick={onClose} className="rounded p-1 text-text-faint hover:text-text">
            <X size={16} />
          </button>
        </div>

        <div className="min-h-0 flex-1 space-y-3 overflow-y-auto px-5 py-4">
          {error && (
            <p className="mono text-xs text-state-error">{error}</p>
          )}
          {!view && !error && (
            <div className="space-y-3">
              {[0, 1, 2, 3].map((i) => (
                <SkeletonRow key={i} className="w-full" />
              ))}
            </div>
          )}
          {view?.chunks.map((c) => {
            const isCited = c.chunk_id === view.cited.chunk_id;
            return (
              <div
                key={c.chunk_id}
                ref={isCited ? citedRef : undefined}
                className={cn(
                  "rounded-[--radius-sm] border px-4 py-3",
                  isCited
                    ? "border-accent/60 bg-accent-dim"
                    : "border-border-subtle bg-bg-overlay"
                )}
              >
                <p className="mono mb-1 text-[10px] uppercase tracking-wider text-text-faint">
                  {c.header || `chunk ${c.ordinal}`}
                </p>
                <p className="whitespace-pre-wrap text-sm leading-relaxed text-text">
                  {c.body}
                </p>
                {isCited && (
                  <div className="mt-2">
                    <Badge tone="accent">cited</Badge>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
