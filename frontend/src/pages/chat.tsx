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

import { useCallback, useRef, useState } from "react";
import { motion } from "framer-motion";
import { ArrowUp, ChevronDown, Quote } from "lucide-react";
import { api, SetupNeeded, type AskResult, type Citation } from "@/lib/api";
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

function CitationCard({ c }: { c: Citation }) {
  const [open, setOpen] = useState(false);
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
      </button>
      {open && (
        <div className="border-t border-border-subtle px-3 py-2">
          <p className="flex gap-2 text-xs leading-relaxed text-text-muted">
            <Quote size={12} className="mt-0.5 shrink-0 text-accent" />
            <span>{c.quote}</span>
          </p>
          {c.revision_date && (
            <p className="mono mt-2 text-[10px] text-text-faint">
              revision {c.revision_date}
            </p>
          )}
        </div>
      )}
    </div>
  );
}

function AssistantTurn({ turn }: { turn: { result: AskResult } }) {
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
            <CitationCard key={c.evidence_id} c={c} />
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
  const bottomRef = useRef<HTMLDivElement>(null);

  const send = useCallback(async () => {
    const question = input.trim();
    if (!question || busy) return;
    setInput("");
    setBusy(true);
    setTurns((t) => [...t, { kind: "user", text: question }, { kind: "pending" }]);
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });

    try {
      const result = await api.ask(question);
      setTurns((t) => [...t.slice(0, -1), { kind: "assistant", result }]);
    } catch (e) {
      const turn: Turn =
        e instanceof SetupNeeded
          ? { kind: "setup", error: e }
          : { kind: "assistant", result: { state: "ERROR", answer: "", citations: [], missing_evidence: [], unverifiable_claims: [], claims_conflict: false } };
      setTurns((t) => [...t.slice(0, -1), turn]);
    } finally {
      setBusy(false);
      bottomRef.current?.scrollIntoView({ behavior: "smooth" });
    }
  }, [input, busy]);

  return (
    <div className="mx-auto flex h-full max-w-3xl flex-col">
      {/* stream */}
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
              <AssistantTurn turn={turn} />
            </motion.div>
          );
        })}
        <div ref={bottomRef} />
      </div>

      {/* composer */}
      <div className="border-t border-border-subtle p-4">
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
          <Button
            variant="primary"
            onClick={() => void send()}
            disabled={busy || !input.trim()}
            className="shrink-0"
          >
            <ArrowUp size={15} />
          </Button>
        </div>
        <p className="mono mt-2 px-1 text-[10px] text-text-faint">
          enter to send · shift+enter for a new line · every claim cites evidence
        </p>
      </div>
    </div>
  );
}
