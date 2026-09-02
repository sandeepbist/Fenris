/**
 * Search: the find-the-file verb. Chat answers questions; search finds
 * documents. Live results with match snippets, grouped by file, one
 * click to the source viewer.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { Search as SearchIcon, FileText, Table2 } from "lucide-react";
import { api, SetupNeeded } from "@/lib/api";
import { Card, SkeletonRow } from "@/components/ui";
import { SourceViewer } from "@/components/source-viewer";

interface SearchMatch {
  chunk_id: number;
  file_id: string;
  title: string;
  header: string;
  snippet: string;
  score: number;
  page_nos: number[];
  is_table: boolean;
  modified_time: string | null;
}

interface SearchFile {
  file_id: string;
  title: string;
  modified_time: string | null;
  matches: SearchMatch[];
}

export function SearchPage() {
  const [query, setQuery] = useState("");
  const [files, setFiles] = useState<SearchFile[] | null>(null);
  const [error, setError] = useState<SetupNeeded | null>(null);
  const [viewerChunk, setViewerChunk] = useState<number | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const run = useCallback(async (q: string) => {
    if (!q.trim()) {
      setFiles(null);
      return;
    }
    try {
      const res = await api.search(q);
      setFiles(res.files);
      setError(null);
    } catch (e) {
      if (e instanceof SetupNeeded) setError(e);
    }
  }, []);

  // debounce: type, pause 250ms, results
  useEffect(() => {
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => void run(query), 250);
    return () => {
      if (timer.current) clearTimeout(timer.current);
    };
  }, [query, run]);

  const hasQuery = query.trim().length > 0;

  return (
    <div className="mx-auto max-w-3xl space-y-4 p-6">
      <h1 className="text-lg font-medium">Search</h1>

      <div className="flex items-center gap-2 rounded-[--radius] border border-border bg-bg-raised px-3 py-2.5 focus-within:border-accent/50">
        <SearchIcon size={16} className="shrink-0 text-text-faint" />
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          autoFocus
          placeholder="find the file — names, contents, phrases…"
          className="w-full bg-transparent text-sm text-text placeholder:text-text-faint focus:outline-none"
        />
        {hasQuery && files !== null && (
          <span className="mono shrink-0 text-[10px] text-text-faint">
            {files.length} {files.length === 1 ? "file" : "files"}
          </span>
        )}
      </div>

      {error && (
        <div className="space-y-2">
          <p className="mono text-xs text-state-stale">{error.message}</p>
        </div>
      )}

      {!hasQuery && (
        <Card className="border-dashed p-8 text-center">
          <p className="text-sm text-text-muted">
            Search your indexed Drive by content and title. Type to see
            results as you pause — grouped by file, best matches first.
          </p>
        </Card>
      )}

      {hasQuery && files === null && !error && (
        <div className="space-y-3">
          {[0, 1, 2].map((i) => (
            <SkeletonRow key={i} className="w-full" />
          ))}
        </div>
      )}

      {hasQuery && files !== null && files.length === 0 && (
        <Card className="border-dashed p-8 text-center">
          <p className="text-sm text-text-muted">
            Nothing matches "{query}" in the index. Check the Sources page
            for what's indexed.
          </p>
        </Card>
      )}

      {files?.map((f) => (
        <Card key={f.file_id}>
          <div className="flex items-center justify-between border-b border-border-subtle px-4 py-2.5">
            <p className="flex min-w-0 items-center gap-2 text-sm font-medium">
              <FileText size={14} className="shrink-0 text-accent" />
              <span className="truncate">{f.title}</span>
            </p>
            {f.modified_time && (
              <span className="mono shrink-0 text-[10px] text-text-faint">
                {f.modified_time}
              </span>
            )}
          </div>
          <div className="divide-y divide-border-subtle">
            {f.matches.map((m) => (
              <button
                key={m.chunk_id}
                onClick={() => setViewerChunk(m.chunk_id)}
                className="block w-full px-4 py-3 text-left transition-colors hover:bg-bg-overlay"
              >
                <p className="mono mb-1 flex items-center gap-2 text-[10px] text-text-faint">
                  {m.header || "…"}
                  {m.is_table && (
                    <Table2 size={11} className="shrink-0 text-accent" />
                  )}
                  {m.page_nos.length > 0 && `p.${m.page_nos.join(",p.")}`}
                </p>
                <p className="line-clamp-2 text-sm leading-relaxed text-text-muted">
                  {m.snippet}
                </p>
              </button>
            ))}
          </div>
        </Card>
      ))}

      {viewerChunk !== null && (
        <SourceViewer chunkId={viewerChunk} onClose={() => setViewerChunk(null)} />
      )}
    </div>
  );
}
