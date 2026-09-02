/**
 * Chat: the heart of the product. A message stream over your own Drive.
 *
 * Design rules inherited from the brainless terminal aesthetic:
 * messages read as a session log, not a chat-app cosplay. Assistant
 * answers render with an answer-state badge (color = fact), citations
 * as expandable evidence cards under the answer - the quote is always
 * visible because a citation you can't read is a citation you can't
 * trust. The engine's non-answer states (INSUFFICIENT / STALE /
 * CONFLICTING / ERROR) are first-class, each with its own honest copy.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { motion } from "framer-motion";
import { ArrowUp, ChevronDown, Download, Pin, Quote, RotateCcw, Square, Trash2 } from "lucide-react";
import { api, askStream, SetupNeeded, type AskResult, type Citation, type ConversationRow } from "@/lib/api";
import { SourceViewer } from "@/components/source-viewer";
import { Badge, Button } from "@/components/ui";
import { SetupNeededBanner } from "@/components/setup-needed";
import { cn } from "@/lib/utils";

type Turn =
  | { kind: "user"; text: string }
  | { kind: "assistant"; result: AskResult }
  | { kind: "pending" }
  | { kind: "setup"; error: SetupNeeded };

const STATE_TONE: Record<string, "answerable" | "stale" | "conflicting" | "insufficient" | "error"> = {
  ANSWERABLE: "answerable",
  STALE: "stale",
  CONFLICTING: "conflicting",
  INSUFFICIENT: "insufficient",
  ERROR: "error",
};

const STATE_COPY: Record<string, string> = {
  STALE: "The cited sources changed since they were indexed. Sync, then ask again.",
  CONFLICTING: "Your Drive contains conflicting versions of this answer — both are shown below. No silent majority vote.",
  INSUFFICIENT: "Nothing in your indexed Drive supports an answer to this.",
  ERROR: "The engine hit a fault answering this. Nothing was made up — the state above says what happened.",
};

function CitationCard({ c, onJump }: { c: Citation; onJump: (chunkId: number) => void }) {
  const [open, setOpen] = useState(false);
  const jumpable = c.chunk_id != null;
  return (
    <div className="rounded-[--radius-sm] border border-border-subtle bg-bg-overlay">
      <button
        onClick={() => setOpen(!open)}
        className="flex w-full items-center gap-2 px-3 py-2 text-left"
      >
        <ChevronDown
          size={14}
          className={cn(
            "shrink-0 text-text-faint transition-transform",
            open && "rotate-180"
          )}
        />
        <span className="mono truncate text-xs text-accent">
          [{c.evidence_id}]
        </span>
        <span className="truncate text-xs text-text-muted">{c.title}</span>
        {c.page != null && (
          <span className="mono shrink-0 text-xs text-text-faint">p.{c.page}</span>
        )}
        {jumpable && (
          <span
            className="mono ml-auto shrink-0 text-[10px] text-accent opacity-60 transition-opacity group-hover:opacity-100"
          >
            view ↗
          </span>
        )}
      </button>
      {open && (
        <div className="border-t border-border-subtle px-3 py-2">
          <p className="flex gap-2 text-xs leading-relaxed text-text-muted">
            <Quote size={12} className="mt-0.5 shrink-0 text-accent" />
            <span>{c.quote}</span>
          </p>
          <div className="mt-2 flex items-center gap-3">
            {jumpable && (
              <button
                onClick={() => onJump(c.chunk_id!)}
                className="mono text-[10px] text-accent hover:text-accent-hover"
              >
                open in document →
              </button>
            )}
            {c.revision_date && (
              <p className="mono text-[10px] text-text-faint">
                revision {c.revision_date}
              </p>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

function AssistantTurn({
  turn,
  onJump,
}: {
  turn: { result: AskResult };
  onJump: (chunkId: number) => void;
}) {
  const r = turn.result;
  return (
    <div className="space-y-3">
      {r.answer && (
        <div className="whitespace-pre-wrap text-sm leading-relaxed text-text">
          {r.answer}
        </div>
      )}

      <div className="flex flex-wrap items-center gap-2">
        <Badge tone={STATE_TONE[r.state] ?? "insufficient"}>{r.state}</Badge>
        {r.claims_conflict && <Badge tone="conflicting">CONFLICT</Badge>}
        {r.unverifiable_claims.length > 0 && (
          <Badge tone="stale">{r.unverifiable_claims.length} UNVERIFIED</Badge>
        )}
      </div>

      {STATE_COPY[r.state] && !r.answer && (
        <p className="text-sm text-text-muted">{STATE_COPY[r.state]}</p>
      )}

      {r.missing_evidence.length > 0 && (
        <div className="rounded-[--radius-sm] border border-border-subtle p-3">
          <p className="mono mb-1 text-[10px] uppercase tracking-wider text-text-faint">
            missing evidence
          </p>
          <ul className="list-inside list-disc text-xs text-text-muted">
            {r.missing_evidence.map((m, i) => (
              <li key={i}>{m}</li>
            ))}
          </ul>
        </div>
      )}

      {r.citations.length > 0 && (
        <div className="space-y-1.5">
          {r.citations.map((c) => (
            <CitationCard key={c.evidence_id} c={c} onJump={onJump} />
          ))}
        </div>
      )}
    </div>
  );
}

export function ChatPage() {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [liveText, setLiveText] = useState("");
  const [conversations, setConversations] = useState<ConversationRow[]>([]);
  const [activeCid, setActiveCid] = useState<number | null>(null);
  const stopRef = useRef<(() => void) | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const lastQuestion = useRef<string>("");
  const [viewerChunk, setViewerChunk] = useState<number | null>(null);
  const [scope, setScope] = useState<string[]>([]);
  const [scopeEditing, setScopeEditing] = useState(false);
  const [scopeDraft, setScopeDraft] = useState("");
  const [strict, setStrict] = useState(false);

  const refreshConversations = useCallback(async () => {
    try {
      const res = await api.conversations();
      setConversations(res.conversations);
    } catch {
      /* engine down: the stream path will surface the setup banner */
    }
  }, []);

  useEffect(() => {
    void refreshConversations();
  }, [refreshConversations]);

  // onboarding handoff: /chat?q=... pre-fills the composer once per mount
  const [searchParams] = useSearchParams();
  const handoff = searchParams.get("q");
  const handoffUsed = useRef(false);
  useEffect(() => {
    if (handoff && !handoffUsed.current && turns.length === 0) {
      handoffUsed.current = true;
      setInput(handoff);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [handoff]);

  const openConversation = useCallback(async (cid: number) => {
    if (busy) return;
    setActiveCid(cid);
    try {
      const c = await api.getConversation(cid);
      const restored: Turn[] = [];
      for (const t of c.turns) {
        restored.push({ kind: "user", text: t.question });
        restored.push({
          kind: "assistant",
          result: {
            state: t.state as AskResult["state"],
            answer: t.answer,
            citations: t.citations,
            missing_evidence: [],
            unverifiable_claims: [],
            claims_conflict: false,
          },
        });
      }
      setTurns(restored);
      setTimeout(() => bottomRef.current?.scrollIntoView(), 0);
    } catch {
      /* 404 raced a delete; the list refreshes next poll */
    }
  }, [busy]);

  const newConversation = useCallback(() => {
    if (busy) return;
    setActiveCid(null);
    setTurns([]);
  }, [busy]);

  /** Create-or-reuse the session id before the first turn of a chat. */
  const ensureConversation = useCallback(async (): Promise<number | null> => {
    if (activeCid !== null) return activeCid;
    try {
      const c = await api.createConversation();
      setActiveCid(c.id);
      void refreshConversations();
      return c.id;
    } catch {
      return null; // persistence is best-effort; the answer still streams
    }
  }, [activeCid, refreshConversations]);

  const send = useCallback(async () => {
    const question = input.trim();
    if (!question || busy) return;
    setInput("");
    setBusy(true);
    lastQuestion.current = question;
    setTurns((t) => [...t, { kind: "user", text: question }]);
    setLiveText("");
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });

    const cid = await ensureConversation();
    const { events, stop } = askStream(question, true, scope, strict);
    stopRef.current = stop;
    let assembled = "";
    let finalResult: AskResult | null = null;
    try {
      for await (const ev of events) {
        if (ev.type === "delta" && ev.text) {
          assembled += ev.text;
          setLiveText(assembled);
          bottomRef.current?.scrollIntoView({ behavior: "smooth" });
        } else if (ev.type === "done") {
          const result: AskResult = {
            state: (ev.state ?? "ERROR") as AskResult["state"],
            answer: ev.answer ?? "",
            citations: ev.citations ?? [],
            missing_evidence: ev.missing_evidence ?? [],
            unverifiable_claims: ev.unverifiable_claims ?? [],
            claims_conflict: ev.claims_conflict ?? false,
          };
          finalResult = result;
          setTurns((t) => [...t, { kind: "assistant", result }]);
        } else if (ev.type === "error") {
          setTurns((t) => [
            ...t,
            { kind: "assistant", result: { state: "ERROR", answer: ev.detail ?? "", citations: [], missing_evidence: [], unverifiable_claims: [], claims_conflict: false } },
          ]);
        }
      }
    } catch (e) {
      if ((e as Error).name === "AbortError") {
        // user stopped the stream: keep whatever streamed, marked as stopped
        setTurns((t) => [
          ...t,
          { kind: "assistant", result: { state: "INSUFFICIENT", answer: assembled + " — [stopped]", citations: [], missing_evidence: [], unverifiable_claims: [], claims_conflict: false } },
        ]);
      } else {
        const turn: Turn =
          e instanceof SetupNeeded
            ? { kind: "setup", error: e }
            : { kind: "assistant", result: { state: "ERROR", answer: "", citations: [], missing_evidence: [], unverifiable_claims: [], claims_conflict: false } };
        setTurns((t) => [...t, turn]);
      }
    } finally {
      setLiveText("");
      setBusy(false);
      stopRef.current = null;
      bottomRef.current?.scrollIntoView({ behavior: "smooth" });
      // Persist the completed turn (best-effort: persistence never blocks the UX).
      if (cid !== null && finalResult) {
        void api
          .addTurn(cid, {
            question,
            state: finalResult.state,
            answer: finalResult.answer,
            citations_json: JSON.stringify(finalResult.citations),
          })
          .then(() => refreshConversations())
          .catch(() => undefined);
      }
    }
  }, [input, busy, ensureConversation, refreshConversations, scope, strict]);

  const regenerate = useCallback(() => {
    if (!lastQuestion.current || busy) return;
    setInput(lastQuestion.current);
    // send() reads `input` from state, so call it after the state flush
    setTimeout(() => void send(), 0);
  }, [busy, send]);

  const stop = useCallback(() => {
    stopRef.current?.();
  }, []);

  return (
    <div className="mx-auto flex h-full max-w-5xl">
      {/* conversations rail */}
      <aside className="flex w-56 shrink-0 flex-col border-r border-border-subtle">
        <div className="flex items-center justify-between px-3 py-3">
          <span className="mono text-[10px] uppercase tracking-wider text-text-faint">
            chats
          </span>
          <button
            onClick={newConversation}
            className="mono rounded-[--radius-sm] px-2 py-0.5 text-xs text-text-muted transition-colors hover:bg-bg-raised hover:text-accent"
            title="New chat"
          >
            + new
          </button>
        </div>
        <div className="min-h-0 flex-1 space-y-0.5 overflow-y-auto px-2 pb-3">
          {conversations.map((c) => (
            <div
              key={c.id}
              className={
                "group flex cursor-pointer items-center gap-1.5 rounded-[--radius-sm] px-2.5 py-1.5 text-sm transition-colors " +
                (c.id === activeCid
                  ? "bg-accent-dim text-accent"
                  : "text-text-muted hover:bg-bg-raised hover:text-text")
              }
              onClick={() => void openConversation(c.id)}
            >
              {c.pinned && <Pin size={11} className="shrink-0" />}
              <span className="min-w-0 flex-1 truncate">{c.title}</span>
              <span className="hidden shrink-0 gap-0.5 group-hover:flex">
                <PinButton
                  cid={c.id}
                  pinned={c.pinned}
                  onDone={() => void refreshConversations()}
                />
                <ExportButton cid={c.id} title={c.title} />
                <button
                  className="rounded p-0.5 text-text-faint hover:text-state-error"
                  onClick={(e) => {
                    e.stopPropagation();
                    void api.deleteConversation(c.id).then(() => refreshConversations());
                  }}
                >
                  <Trash2 size={11} />
                </button>
              </span>
            </div>
          ))}
          {conversations.length === 0 && (
            <p className="mono px-2.5 py-2 text-[10px] text-text-faint">
              chats persist once you ask something
            </p>
          )}
        </div>
      </aside>

      {/* stream + composer */}
      <div className="mx-auto flex h-full min-w-0 flex-1 flex-col">
      <div className="flex-1 space-y-6 overflow-y-auto px-6 py-6">
        {turns.length === 0 && (
          <div className="flex h-full flex-col items-center justify-center gap-3 text-center">
            <p className="text-base font-medium">Ask your Drive anything</p>
            <p className="max-w-sm text-sm text-text-muted">
              Answers cite exact spans from your files — or say honestly
              what's missing. Nothing leaves this machine but the question.
            </p>
          </div>
        )}

        {turns.map((turn, i) => {
          if (turn.kind === "user") {
            return (
              <div key={i} className="flex justify-end">
                <div className="max-w-[85%] rounded-[--radius] bg-accent-dim px-4 py-2.5 text-sm text-accent">
                  {turn.text}
                </div>
              </div>
            );
          }
          if (turn.kind === "pending") {
            return (
              <div key={i} className="flex gap-2">
                <span className="mono text-xs text-text-faint">thinking…</span>
              </div>
            );
          }
          if (turn.kind === "setup") {
            return (
              <div key={i} className="max-w-lg">
                <SetupNeededBanner error={turn.error} />
              </div>
            );
          }
          return (
            <motion.div
              key={i}
              initial={{ opacity: 0, y: 6 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.18 }}
            >
              <AssistantTurn turn={turn} onJump={setViewerChunk} />
            </motion.div>
          );
        })}
        <div ref={bottomRef} />

        {/* the streaming turn: live text with a cursor while generating */}
        {busy && liveText !== "" && (
          <div className="whitespace-pre-wrap text-sm leading-relaxed text-text">
            {liveText}
            <span className="mono ml-0.5 inline-block animate-pulse text-accent">▍</span>
          </div>
        )}
        {busy && liveText === "" && (
          <div className="flex gap-2">
            <span className="mono text-xs text-text-faint">thinking…</span>
          </div>
        )}
      </div>

      {/* composer */}
      <div className="border-t border-border-subtle p-4">
        {/* corpus controls: scope chips + strictness toggle */}
        <div className="mb-2 flex flex-wrap items-center gap-2">
          <span className="mono text-[10px] uppercase tracking-wider text-text-faint">
            scope
          </span>
          {scope.length === 0 && (
            <button
              onClick={() => setScopeEditing(true)}
              className="mono rounded-full border border-border px-2 py-0.5 text-[10px] text-text-muted transition-colors hover:border-accent/40 hover:text-accent"
            >
              whole drive + set scope
            </button>
          )}
          {scope.map((s) => (
            <button
              key={s}
              onClick={() => setScope((sc) => sc.filter((x) => x !== s))}
              className="mono rounded-full border border-accent/40 bg-accent-dim px-2 py-0.5 text-[10px] text-accent"
              title="click to remove"
            >
              {s} ×
            </button>
          ))}
          {scopeEditing && (
            <input
              autoFocus
              value={scopeDraft}
              onChange={(e) => setScopeDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && scopeDraft.trim()) {
                  setScope((sc) => [...sc, scopeDraft.trim()]);
                  setScopeDraft("");
                }
                if (e.key === "Escape") setScopeEditing(false);
              }}
              onBlur={() => setScopeEditing(false)}
              placeholder="folder or file term, enter…"
              className="mono w-40 rounded-full border border-accent/40 bg-transparent px-2 py-0.5 text-[10px] text-text placeholder:text-text-faint focus:outline-none"
            />
          )}
          <button
            onClick={() => setStrict((v) => !v)}
            className={
              "mono ml-auto rounded-full border px-2 py-0.5 text-[10px] transition-colors " +
              (strict
                ? "border-state-answerable/40 bg-state-answerable/5 text-state-answerable"
                : "border-border text-text-muted hover:text-text")
            }
            title="Strict: every sentence must be cited evidence - no background knowledge"
          >
            strict {strict ? "on" : "off"}
          </button>
        </div>
        <div className="flex items-end gap-2 rounded-[--radius] border border-border bg-bg-raised p-2 focus-within:border-accent/50">
          <textarea
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                void send();
              }
            }}
            placeholder="Ask about anything in your Drive…"
            rows={1}
            className="max-h-32 flex-1 resize-none bg-transparent px-2 py-1.5 text-sm text-text placeholder:text-text-faint focus:outline-none"
          />
          {busy ? (
            <Button variant="default" onClick={stop} className="shrink-0">
              <Square size={13} /> Stop
            </Button>
          ) : (
            <>
              {turns.length >= 2 && (
                <Button variant="ghost" onClick={regenerate} className="shrink-0">
                  <RotateCcw size={14} />
                </Button>
              )}
              <Button
                variant="primary"
                onClick={() => void send()}
                disabled={!input.trim()}
                className="shrink-0"
              >
                <ArrowUp size={15} />
              </Button>
            </>
          )}
        </div>
        <p className="mono mt-2 px-1 text-[10px] text-text-faint">
          enter to send · shift+enter for a new line · every claim cites evidence
        </p>
      </div>
      </div>

      {viewerChunk !== null && (
        <SourceViewer chunkId={viewerChunk} onClose={() => setViewerChunk(null)} />
      )}
    </div>
  );
}

function PinButton({ cid, pinned, onDone }: { cid: number; pinned: boolean; onDone: () => void }) {
  return (
    <button
      className={"rounded p-0.5 " + (pinned ? "text-accent" : "text-text-faint hover:text-accent")}
      onClick={(e) => {
        e.stopPropagation();
        void api.patchConversation(cid, { pinned: !pinned }).then(() => onDone());
      }}
    >
      <Pin size={11} />
    </button>
  );
}

function ExportButton({ cid, title }: { cid: number; title: string }) {
  const download = (e: React.MouseEvent) => {
    e.stopPropagation();
    void api.exportConversation(cid).then(({ content, filename }) => {
      const blob = new Blob([content], { type: "text/markdown" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      a.click();
      URL.revokeObjectURL(url);
      void title;
    });
  };
  return (
    <button
      className="rounded p-0.5 text-text-faint hover:text-accent"
      onClick={download}
    >
      <Download size={11} />
    </button>
  );
}
