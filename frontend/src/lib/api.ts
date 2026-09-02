/**
 * Typed API client for the Fenris local engine (src/fenris/api.py).
 * The contracts mirror the Python dicts exactly, including the 503
 * {detail} failure shape, which surfaces as SetupNeeded.
 */

const BASE = import.meta.env.DEV ? "" : ""; // dev: vite proxy /api -> :8000; prod: same origin

export type AnswerState =
  | "ANSWERABLE"
  | "INSUFFICIENT"
  | "STALE"
  | "CONFLICTING"
  | "ERROR";

export class SetupNeeded extends Error {
  reason: string;
  constructor(reason: string) {
    super(reason);
    this.name = "SetupNeeded";
    this.reason = reason;
  }
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${BASE}${path}`, {
      headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
      ...init,
    });
  } catch {
    throw new SetupNeeded("Engine unreachable — is `fenris api` running?");
  }
  if (res.status === 503) {
    const body = (await res.json().catch(() => ({ detail: "unknown" }))) as {
      detail: string;
    };
    throw new SetupNeeded(body.detail);
  }
  if (!res.ok) throw new Error(`API ${res.status}: ${await res.text()}`);
  return res.json() as Promise<T>;
}

// ---- contracts ---------------------------------------------------------------

export interface Health {
  ok: boolean;
  db_path: string;
  has_db: boolean;
  index: {
    sources_active: number;
    sources_failed: number;
    sources_skipped: number;
    chunks: number;
    tables: number;
    drive_files: number;
  };
}

export interface Status {
  cursor: { page_token: string; status: string } | null;
  last_drain_at: string | null;
  stale_sync: boolean;
  drive_files: number;
  active_sources: number;
  retired_sources: number;
  chunks: number;
}

export interface DoctorCheck {
  name: string;
  ok: boolean;
  detail: string;
}

export interface DoctorReport {
  checks: DoctorCheck[];
  all_ok: boolean;
}

export interface SourceRow {
  file_id: string;
  title: string;
  path: string | null;
  revision: string;
  status: string; // ok | failed | skipped | unreadable | pending_export
  active: boolean;
  indexed_at: string | null;
  trashed: boolean;
}

export interface SyncReport {
  changes_processed: number;
  content_reingests: number;
  metadata_only: number;
  tombstoned: number;
  failed: number;
  resync_triggered: string;
  errors: string[];
}

export interface Citation {
  evidence_id: string;
  title: string;
  page: number | null;
  quote: string;
  revision_date: string | null;
  chunk_id?: number | null;
  char_start?: number | null;
  char_end?: number | null;
}

export interface AskResult {
  state: AnswerState;
  answer: string;
  citations: Citation[];
  missing_evidence: string[];
  unverifiable_claims: string[];
  claims_conflict: boolean;
}

export interface EvalRun {
  id: number;
  started_at: string;
  verdict: string | null;
}

// ---- conversations (P0.2) ----------------------------------------------------

export interface ConversationRow {
  id: number;
  title: string;
  pinned: boolean;
  created_at: string | null;
  updated_at: string | null;
  turn_count?: number;
}

export interface StoredTurn {
  question: string;
  state: AnswerState;
  answer: string;
  citations: Citation[];
  created_at: string | null;
}

// ---- streaming ---------------------------------------------------------------

export interface StreamEvent {
  type: "delta" | "done" | "error";
  text?: string;
  detail?: string;
  // done events carry the full AskResult fields
  state?: AnswerState;
  answer?: string;
  citations?: Citation[];
  missing_evidence?: string[];
  unverifiable_claims?: string[];
  claims_conflict?: boolean;
}

/**
 * Consume /api/ask/stream (SSE). Returns an async iterator of events and an
 * abort handle: calling stop() mid-stream ends the fetch without consuming
 * further deltas.
 */
export function askStream(
  question: string,
  rerank = true,
): { events: AsyncIterable<StreamEvent>; stop: () => void } {
  const controller = new AbortController();
  const decoder = new TextDecoder();

  async function* iterate(): AsyncIterable<StreamEvent> {
    let buffer = "";
    const res = await fetch("/api/ask/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question, rerank }),
      signal: controller.signal,
    });
    if (!res.ok || !res.body) {
      throw new SetupNeeded(`engine error ${res.status}`);
    }
    const reader = res.body.getReader();
    try {
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        // SSE events are separated by a blank line
        let sep: number;
        while ((sep = buffer.indexOf("\n\n")) !== -1) {
          const block = buffer.slice(0, sep);
          buffer = buffer.slice(sep + 2);
          for (const line of block.split("\n")) {
            if (line.startsWith("data: ")) {
              try {
                yield JSON.parse(line.slice(6)) as StreamEvent;
              } catch {
                // partial JSON in a torn frame: skip, the retry logic of the
                // SSE framing self-corrects on the next blank-line boundary
              }
            }
          }
        }
      }
    } finally {
      controller.abort(); // no-op if already finished; releases the reader
    }
  }
  return { events: iterate(), stop: () => controller.abort() };
}

export interface SourceViewChunk {
  chunk_id: number;
  ordinal: number;
  header: string;
  body: string;
  char_start: number | null;
  char_end: number | null;
}

export interface SourceView {
  title: string;
  file_id: string;
  revision: string;
  modified_time: string | null;
  cited: {
    chunk_id: number;
    ordinal: number;
    header: string;
    char_start: number | null;
    char_end: number | null;
  };
  chunks: SourceViewChunk[];
}

export interface SearchMatch {
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

export interface SearchResult {
  files: {
    file_id: string;
    title: string;
    modified_time: string | null;
    matches: SearchMatch[];
  }[];
  total_chunks: number;
}

// ---- client ------------------------------------------------------------------

export const api = {
  health: () => req<Health>("/api/health"),

  search: (query: string, tableOnly = false) =>
    req<SearchResult>("/api/search", {
      method: "POST",
      body: JSON.stringify({ query, table_only: tableOnly }),
    }),

  doctor: () => req<DoctorReport>("/api/doctor"),

  sourceView: (chunkId: number) =>
    req<SourceView>(`/api/source-view?chunk_id=${chunkId}`),

  // conversations
  conversations: () => req<{ conversations: ConversationRow[] }>("/api/conversations"),
  createConversation: (title?: string) =>
    req<{ id: number; title: string; pinned: boolean }>("/api/conversations", {
      method: "POST",
      body: JSON.stringify({ title: title ?? null }),
    }),
  getConversation: (cid: number) =>
    req<{
      id: number; title: string; pinned: boolean;
      created_at: string | null; updated_at: string | null;
      turns: StoredTurn[];
    }>(`/api/conversations/${cid}`),
  patchConversation: (cid: number, patch: { title?: string; pinned?: boolean }) =>
    req<{ ok: boolean }>(`/api/conversations/${cid}`, {
      method: "PATCH",
      body: JSON.stringify(patch),
    }),
  deleteConversation: (cid: number) =>
    req<{ ok: boolean }>(`/api/conversations/${cid}`, { method: "DELETE" }),
  exportConversation: (cid: number, fmt: "md" | "html" = "md") =>
    req<{ format: string; content: string; filename: string }>(
      `/api/conversations/${cid}/export?fmt=${fmt}`),
  addTurn: (cid: number, turn: {
    question: string; state: string; answer: string; citations_json: string;
  }) =>
    req<{ turn_id: number }>(`/api/conversations/${cid}/turns`, {
      method: "POST",
      body: JSON.stringify(turn),
    }),

  status: () => req<Status>("/api/status"),

  sources: (limit = 200) =>
    req<{ sources: SourceRow[]; count: number }>(`/api/sources?limit=${limit}`),

  sync: (full = false) =>
    req<SyncReport>("/api/sync", { method: "POST", body: JSON.stringify({ full }) }),

  ask: (question: string, rerank = true) =>
    req<AskResult>("/api/ask", {
      method: "POST",
      body: JSON.stringify({ question, rerank }),
    }),

  evalOverview: () => req<{ runs: EvalRun[] }>("/api/eval/overview"),
};
